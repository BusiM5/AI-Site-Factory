import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


client = TestClient(main.app)


@pytest.fixture(autouse=True)
def isolated_pipeline_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_DB_PATH", str(tmp_path / "pipeline.db"))
    main.init_pipeline_db()
    main.LEADS_DB.clear()
    main.CONTENT_DB.clear()
    main.PREVIEW_DB.clear()
    main.DISCOVERY_DB.clear()
    main.PIPELINE_DB.clear()
    main.LOG_BUFFER.clear()
    yield


def apify_item(index, category="Restaurant", province="Gauteng"):
    return {
        "title": f"Lead Business {index}",
        "website": f"https://lead-{index}.example.com",
        "phone": f"+27 11 000 {index:04d}",
        "categoryName": category,
        "address": f"{index} Market Street, {province}, South Africa",
        "countryCode": "ZA",
        "rating": 4.5,
        "reviewsCount": 20 + index,
        "googleMapsUrl": f"https://maps.example.com/{index}",
    }


def stub_generation(monkeypatch, model_calls=None):
    calls = model_calls if model_calls is not None else []
    monkeypatch.setattr(main, "scrape_contact_details", lambda lead: {"email": lead.email, "phone": lead.phone, "website": lead.website})
    monkeypatch.setattr(
        main,
        "generate_page_prompt_with_gemini",
        lambda context, template: calls.append("gemini_prompt") or {"pagePrompt": "Build the page."},
    )
    monkeypatch.setattr(
        main,
        "generate_draft_html_with_groq",
        lambda context, template, page_prompt: calls.append("groq_html") or {
            "html": "<!doctype html><html><head><title>Draft</title></head><body>Draft</body></html>",
            "notes": "Draft",
        },
    )
    monkeypatch.setattr(
        main,
        "finalize_html_with_gemini",
        lambda context, template, page_prompt, draft_html: calls.append("gemini_final") or {
            "html": "<!doctype html><html><head><title>Final</title></head><body>Final</body></html>",
            "qaNotes": "Final",
        },
    )
    return calls


def fake_deploy_with_history(
    canonical_key,
    business_name,
    site_html,
    pipeline_id,
    approval_id,
    approved_by,
    regenerate_existing_site=True,
    publish_mode="direct-netlify",
    github_export=None,
):
    history_id = f"history-{canonical_key}"
    result = {
        "deployAction": "CREATED",
        "siteCreated": True,
        "siteReused": False,
        "siteId": f"site-{canonical_key}",
        "siteName": "ai-site-alpha",
        "deployId": f"deploy-{canonical_key}",
        "state": "ready",
        "url": "https://alpha.netlify.app",
        "deploymentHistoryId": history_id,
        "publishMode": publish_mode,
        "githubExport": github_export,
    }
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, pipeline_id, approval_id, site_id, site_name,
                deploy_id, url, deploy_action, state, html_checksum, deployed_at,
                approved_by, publish_mode, github_export_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                canonical_key,
                pipeline_id,
                approval_id,
                result["siteId"],
                result["siteName"],
                result["deployId"],
                result["url"],
                result["deployAction"],
                result["state"],
                main.html_checksum(site_html),
                main.now_iso(),
                approved_by,
                publish_mode,
                main.json.dumps(github_export, default=str) if github_export else None,
                main.json.dumps(result, default=str),
            ),
        )
    return result


def lead_payload():
    return main.DiscoveredLead(
        leadKey="lead-1",
        businessName="Alpha Plumbing",
        email="alpha@example.com",
        category="Plumbing",
        location="Durban",
    ).model_dump()


def test_discover_leads_searches_requested_location(monkeypatch):
    calls = []

    def fake_apify(query, limit, location="South Africa"):
        calls.append((query, location))
        return [
            apify_item(10, province="Gauteng"),
            apify_item(11, province="Gauteng"),
        ]

    monkeypatch.setattr(main, "run_apify_google_maps", fake_apify)

    response = client.post(
        "/api/leads/discover",
        json={
            "presetId": "restaurants",
            "location": "Gauteng, South Africa",
            "query": "family restaurants",
            "limit": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sourceStatus"] == "READY"
    assert len(payload["leads"]) == 2
    assert calls == [("family restaurants in Gauteng, South Africa", "Gauteng, South Africa")]
    assert payload["leads"][0]["location"] == "Gauteng, South Africa"
    assert payload["provinceStats"]["Gauteng, South Africa"]["rawItems"] == 2
    assert payload["duplicatesSkipped"] == 0


def test_discover_leads_skips_previously_seen_leads(monkeypatch):
    def fake_apify(query, limit, location="South Africa"):
        province = location.split(",")[0]
        return [apify_item(1, province=province)]

    monkeypatch.setattr(main, "run_apify_google_maps", fake_apify)

    first = client.post(
        "/api/leads/discover",
        json={"presetId": "restaurants", "location": "South Africa", "limit": 10},
    )
    second = client.post(
        "/api/leads/discover",
        json={"presetId": "restaurants", "location": "South Africa", "limit": 10},
    )

    assert first.status_code == 200
    assert len(first.json()["leads"]) == 1
    assert second.status_code == 200
    assert second.json()["leads"] == []
    assert second.json()["duplicatesSkipped"] == 1


def test_normalize_apify_items_handles_missing_fields_and_duplicates():
    items = [
        {"title": "Same Business", "website": "same.example.com", "countryCode": "ZA"},
        {"title": "Same Business", "website": "https://same.example.com", "countryCode": "ZA"},
        {"title": "No Contact Business", "categoryName": "Plumbing", "countryCode": "ZA"},
        {"website": "https://missing-name.example.com", "countryCode": "ZA"},
    ]

    leads = main.normalize_apify_items(
        items,
        fallback_category="General Services",
        location="South Africa",
        limit=10,
    )

    assert len(leads) == 2
    assert leads[0].domain == "same.example.com"
    assert leads[1].email is None
    assert leads[1].category == "Plumbing"


def test_pipeline_run_processes_multiple_leads(monkeypatch):
    model_calls = []

    monkeypatch.setattr(main, "scrape_contact_details", lambda lead: {"email": lead.email, "phone": lead.phone, "website": lead.website})
    monkeypatch.setattr(
        main,
        "generate_page_prompt_with_gemini",
        lambda context, template: model_calls.append("gemini_prompt") or {"pagePrompt": "Build the page."},
    )
    monkeypatch.setattr(
        main,
        "generate_draft_html_with_groq",
        lambda context, template, page_prompt: model_calls.append("groq_html") or {
            "html": "<!doctype html><html><head><title>Draft</title></head><body><main><section>Draft</section></main></body></html>",
            "notes": "Draft",
        },
    )
    monkeypatch.setattr(
        main,
        "finalize_html_with_gemini",
        lambda context, template, page_prompt, draft_html: model_calls.append("gemini_final") or {
            "html": "<!doctype html><html><head><title>Final</title></head><body><main><section id='hero'>Final</section></main></body></html>",
            "qaNotes": "Final",
        },
    )
    monkeypatch.setattr(
        main,
        "deploy_site_to_netlify_for_lead",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deployment must wait for approval")),
    )
    monkeypatch.setattr(
        main,
        "create_zendesk_outreach_ticket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("zendesk must wait for approval")),
    )

    leads = [
        main.DiscoveredLead(
            leadKey="lead-1",
            businessName="Alpha Plumbing",
            email="alpha@example.com",
            category="Plumbing",
            location="Durban",
        ).model_dump(),
        main.DiscoveredLead(
            leadKey="lead-2",
            businessName="Beta Dental",
            email="beta@example.com",
            category="Dental",
            location="Cape Town",
        ).model_dump(),
    ]

    response = client.post(
        "/api/pipeline/run",
        json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": leads},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PENDING_APPROVAL"
    assert len(payload["results"]) == 2
    assert payload["results"][0]["status"] == "PENDING_APPROVAL"
    assert payload["results"][0]["pendingApprovalId"]
    assert payload["results"][0]["deployment"] is None
    assert payload["results"][0]["zendesk"] is None
    assert model_calls == [
        "gemini_prompt",
        "groq_html",
        "gemini_final",
        "gemini_prompt",
        "groq_html",
        "gemini_final",
    ]

    approvals = client.get("/api/approvals?status=PENDING").json()["approvals"]
    assert len(approvals) == 2


def test_approval_deploys_existing_pipeline_output_and_syncs_zendesk(monkeypatch):
    monkeypatch.setattr(main, "scrape_contact_details", lambda lead: {"email": lead.email, "phone": lead.phone, "website": lead.website})
    monkeypatch.setattr(main, "generate_page_prompt_with_gemini", lambda context, template: {"pagePrompt": "Build the page."})
    monkeypatch.setattr(
        main,
        "generate_draft_html_with_groq",
        lambda context, template, page_prompt: {
            "html": "<!doctype html><html><head><title>Draft</title></head><body>Draft</body></html>",
            "notes": "Draft",
        },
    )
    monkeypatch.setattr(
        main,
        "finalize_html_with_gemini",
        lambda context, template, page_prompt, draft_html: {
            "html": "<!doctype html><html><head><title>Final</title></head><body>Final</body></html>",
            "qaNotes": "Final",
        },
    )
    monkeypatch.setattr(
        main,
        "deploy_site_to_netlify_for_lead",
        fake_deploy_with_history,
    )
    monkeypatch.setattr(
        main,
        "generate_outreach_with_groq",
        lambda context, site_url: {
            "subject": f"Website preview for {context['businessName']}",
            "body": f"Please see {site_url}",
            "siteUrl": site_url,
            "recipientEmail": context["email"],
        },
    )
    monkeypatch.setattr(
        main,
        "create_zendesk_outreach_ticket",
        lambda context, deployment, outreach, pipeline_id: {
            "syncStatus": "TICKET_CREATED",
            "ticketId": 123,
            "ticketUrl": "https://example.zendesk.com/agent/tickets/123",
        },
    )

    lead = lead_payload()

    pipeline_response = client.post(
        "/api/pipeline/run",
        json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead]},
    )
    approval_id = pipeline_response.json()["results"][0]["pendingApprovalId"]

    approval_response = client.post(
        f"/api/approvals/{approval_id}/approve",
        json={"approvedBy": "Ops", "notes": "Approved"},
    )

    assert approval_response.status_code == 200
    payload = approval_response.json()
    assert payload["status"] == "APPROVED"
    assert payload["deployment"]["url"] == "https://alpha.netlify.app"
    assert payload["zendesk"]["ticketUrl"].endswith("/123")

    detail = client.get(f"/api/approvals/{approval_id}?includeHtml=true")
    assert detail.status_code == 200
    assert "<html>" in detail.json()["pendingPreviewHtml"]

    run_detail = client.get(f"/api/pipeline/runs/{pipeline_response.json()['pipelineId']}")
    assert run_detail.status_code == 200
    steps = [step["step"] for step in run_detail.json()["steps"]]
    assert "netlify_deploy" in steps
    assert "groq_outreach" in steps
    assert "zendesk_ticket" in steps

    runs = client.get("/api/pipeline/runs").json()["runs"]
    assert runs[0]["status"] == "COMPLETED"


def test_pipeline_run_reuses_existing_pending_approval_without_model_calls(monkeypatch):
    model_calls = stub_generation(monkeypatch, [])
    lead = lead_payload()

    first = client.post(
        "/api/pipeline/run",
        json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead]},
    )
    first_approval_id = first.json()["results"][0]["pendingApprovalId"]

    model_calls.clear()
    second = client.post(
        "/api/pipeline/run",
        json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead]},
    )

    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "PENDING_APPROVAL"
    assert payload["results"][0]["status"] == "PENDING_APPROVAL"
    assert payload["results"][0]["currentStep"] == "reused_pending_approval"
    assert payload["results"][0]["pendingApprovalId"] == first_approval_id
    assert payload["results"][0]["stepHistory"][0]["status"] == "SKIPPED"
    assert model_calls == []


def test_pipeline_run_reuses_approved_deployment_without_redeploying(monkeypatch):
    model_calls = stub_generation(monkeypatch, [])
    monkeypatch.setattr(main, "deploy_site_to_netlify_for_lead", fake_deploy_with_history)
    monkeypatch.setattr(
        main,
        "generate_outreach_with_groq",
        lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"},
    )
    monkeypatch.setattr(
        main,
        "create_zendesk_outreach_ticket",
        lambda context, deployment, outreach, pipeline_id: {"syncStatus": "TICKET_CREATED", "ticketId": 123, "ticketUrl": "https://zendesk.test/123"},
    )
    lead = lead_payload()

    pipeline_response = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})
    approval_id = pipeline_response.json()["results"][0]["pendingApprovalId"]
    approval_response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})
    assert approval_response.status_code == 200

    model_calls.clear()
    monkeypatch.setattr(
        main,
        "deploy_site_to_netlify_for_lead",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deployment should be reused")),
    )
    second = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})

    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "COMPLETED"
    assert payload["results"][0]["status"] == "COMPLETED_REUSED"
    assert payload["results"][0]["currentStep"] == "reused_deployment"
    assert payload["results"][0]["deployment"]["url"] == "https://alpha.netlify.app"
    assert model_calls == []


def test_pipeline_run_resumes_zendesk_after_post_deploy_failure_without_redeploying(monkeypatch):
    stub_generation(monkeypatch, [])
    monkeypatch.setattr(main, "deploy_site_to_netlify_for_lead", fake_deploy_with_history)
    outreach_calls = []
    zendesk_calls = []
    monkeypatch.setattr(
        main,
        "generate_outreach_with_groq",
        lambda context, site_url: outreach_calls.append("outreach") or {"subject": "Preview", "body": f"See {site_url}"},
    )

    def flaky_zendesk(context, deployment, outreach, pipeline_id):
        zendesk_calls.append("zendesk")
        if len(zendesk_calls) == 1:
            raise RuntimeError("Zendesk is temporarily unavailable.")
        return {"syncStatus": "TICKET_CREATED", "ticketId": 456, "ticketUrl": "https://zendesk.test/456"}

    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", flaky_zendesk)
    lead = lead_payload()

    pipeline_response = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})
    approval_id = pipeline_response.json()["results"][0]["pendingApprovalId"]
    approval_response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})
    assert approval_response.status_code == 200
    assert approval_response.json()["status"] == "DEPLOYED_ZENDESK_FAILED"

    monkeypatch.setattr(
        main,
        "deploy_site_to_netlify_for_lead",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deployment should not run during outreach resume")),
    )
    resumed = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})

    assert resumed.status_code == 200
    payload = resumed.json()
    assert payload["status"] == "COMPLETED"
    assert payload["results"][0]["status"] == "COMPLETED_RESUMED"
    assert payload["results"][0]["zendesk"]["ticketId"] == 456
    assert outreach_calls == ["outreach"]
    assert zendesk_calls == ["zendesk", "zendesk"]
    assert [step["step"] for step in payload["results"][0]["stepHistory"]] == [
        "netlify_deploy",
        "groq_outreach",
        "zendesk_ticket",
    ]


def test_force_regenerate_creates_new_approval_and_supersedes_old_pending(monkeypatch):
    model_calls = stub_generation(monkeypatch, [])
    lead = lead_payload()

    first = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})
    first_approval_id = first.json()["results"][0]["pendingApprovalId"]

    model_calls.clear()
    second = client.post(
        "/api/pipeline/run",
        json={"templateId": "default-service", "leads": [lead], "forceRegenerate": True},
    )
    second_approval_id = second.json()["results"][0]["pendingApprovalId"]

    assert second.status_code == 200
    assert second_approval_id != first_approval_id
    assert model_calls == ["gemini_prompt", "groq_html", "gemini_final"]
    approvals = client.get("/api/approvals?status=ALL").json()["approvals"]
    statuses = {approval["approvalId"]: approval["status"] for approval in approvals}
    assert statuses[first_approval_id] == "SUPERSEDED"
    assert statuses[second_approval_id] == "PENDING"


def test_github_publish_mode_exports_before_netlify(monkeypatch):
    stub_generation(monkeypatch, [])
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    monkeypatch.setenv("GITHUB_REPO", "sites")
    monkeypatch.setenv("GITHUB_BRANCH", "main")
    monkeypatch.setattr(main, "deploy_site_to_netlify_for_lead", fake_deploy_with_history)
    monkeypatch.setattr(
        main,
        "generate_outreach_with_groq",
        lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"},
    )
    monkeypatch.setattr(
        main,
        "create_zendesk_outreach_ticket",
        lambda context, deployment, outreach, pipeline_id: {"syncStatus": "TICKET_CREATED", "ticketId": 789, "ticketUrl": "https://zendesk.test/789"},
    )

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    github_calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        github_calls.append(("get", url, params))
        return FakeResponse(404, {})

    def fake_put(url, headers=None, json=None, timeout=None):
        github_calls.append(("put", url, json))
        assert json["branch"] == "main"
        assert json["content"]
        return FakeResponse(
            201,
            {
                "content": {"sha": "content-sha", "html_url": "https://github.test/owner/sites/index.html"},
                "commit": {"sha": "commit-sha"},
            },
        )

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)

    lead = lead_payload()
    pipeline_response = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead]})
    approval_id = pipeline_response.json()["results"][0]["pendingApprovalId"]
    approval_response = client.post(
        f"/api/approvals/{approval_id}/approve",
        json={"approvedBy": "Ops", "publishMode": "github-netlify"},
    )

    assert approval_response.status_code == 200
    payload = approval_response.json()
    assert payload["status"] == "APPROVED"
    assert payload["publishMode"] == "github-netlify"
    assert payload["githubExport"]["commitSha"] == "commit-sha"
    assert payload["deployment"]["publishMode"] == "github-netlify"
    assert github_calls[0][0] == "get"
    assert github_calls[1][0] == "put"

    detail = client.get(f"/api/approvals/{approval_id}").json()
    assert detail["githubExport"]["commitSha"] == "commit-sha"
    steps = client.get(f"/api/pipeline/runs/{pipeline_response.json()['pipelineId']}").json()["steps"]
    step_names = [step["step"] for step in steps]
    assert step_names.index("github_export") < step_names.index("netlify_deploy")


def test_update_lead_owner_updates_registry_and_pending_approval_context():
    lead = main.DiscoveredLead(
        leadKey="lead-owner",
        canonicalLeadKey="canonical-owner",
        businessName="Owner Test",
        email="owner-test@example.com",
        category="Restaurant",
        location="Gauteng, South Africa",
        province="Gauteng",
    )
    main.upsert_lead_registry(lead)
    approval_id = main.create_approval_record(
        pipeline_id="pipeline-owner",
        canonical_key="canonical-owner",
        lead_key="lead-owner",
        business_name="Owner Test",
        site_html="<!doctype html><html><body>Owner Test</body></html>",
        context={"businessName": "Owner Test"},
        site_content={"finalHtmlChecksum": "checksum"},
        template={"id": "default-service"},
    )

    response = client.post(
        "/api/leads/canonical-owner/owner",
        json={"ownerName": "Ops Lead", "ownerEmail": "ops@example.com", "ownerStatus": "working"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ownerName"] == "Ops Lead"
    assert payload["ownerEmail"] == "ops@example.com"
    assert payload["ownerStatus"] == "working"

    approval = client.get(f"/api/approvals/{approval_id}").json()
    assert approval["context"]["ownerName"] == "Ops Lead"
    assert approval["context"]["ownerEmail"] == "ops@example.com"


def test_model_safe_value_chunks_large_text():
    payload = {"notes": "a" * 45}

    chunked = main.model_safe_value(payload, chunk_size=10, max_chunks=3)

    assert chunked["notes"]["_chunked"] is True
    assert chunked["notes"]["totalChunks"] == 5
    assert chunked["notes"]["includedChunks"] == 3
    assert chunked["notes"]["omittedChunks"] == 2
    assert len(chunked["notes"]["chunks"]) == 3


def test_debug_status_redacts_provider_values(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "apify-secret-value")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-value")
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret-value")
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret-value")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "cxsupporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "owner@company.test")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-secret-value")

    response = client.get("/api/debug/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "READY"
    masked = payload["providers"]["apify"]["checks"][0]["maskedValue"]
    assert masked != "apify-secret-value"
    assert "chars" in masked


def test_debug_probe_reports_missing_environment(monkeypatch):
    for name in [
        "APIFY_API_TOKEN",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "NETLIFY_AUTH_TOKEN",
        "ZENDESK_SUBDOMAIN",
        "ZENDESK_EMAIL",
        "ZENDESK_API_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)

    response = client.post("/api/debug/probe", json={"includeExternal": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "INVALID"
    assert payload["checks"][0]["name"] == "environment"
    assert payload["checks"][0]["status"] == "INVALID"
    assert payload["checks"][1]["name"] == "backend"
    assert payload["checks"][1]["status"] == "VALID"


def test_debug_logs_redact_sensitive_details():
    main.LOG_BUFFER.clear()

    main.log_event(
        "info",
        "test.redaction",
        "Sensitive details should be masked.",
        email="owner@example.com",
        apiToken="super-secret-token",
    )

    response = client.get("/api/debug/logs?limit=5")

    assert response.status_code == 200
    redaction_log = next(log for log in response.json()["logs"] if log["event"] == "test.redaction")
    details = redaction_log["details"]
    assert details["email"] != "owner@example.com"
    assert details["apiToken"] != "super-secret-token"
