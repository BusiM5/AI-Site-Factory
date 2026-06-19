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


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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


def lead_payload():
    return main.DiscoveredLead(
        leadKey="lead-1",
        businessName="Alpha Plumbing",
        email="alpha@example.com",
        category="Plumbing",
        location="Durban",
    ).model_dump()


def fake_github_export(canonical_key, business_name, site_html, pipeline_id=None, approval_id=None):
    return {
        "exportAction": "CREATED",
        "repository": f"owner/ai-site-{canonical_key[:8]}",
        "repoName": f"ai-site-{canonical_key[:8]}",
        "repoUrl": f"https://github.com/owner/ai-site-{canonical_key[:8]}",
        "branch": "main",
        "path": "index.html",
        "htmlChecksum": main.html_checksum(site_html),
        "indexContentSha": "index-sha",
        "readmeContentSha": "readme-sha",
        "commitSha": f"commit-{canonical_key[:8]}",
        "htmlUrl": f"https://github.com/owner/ai-site-{canonical_key[:8]}/blob/main/index.html",
        "pipelineId": pipeline_id,
        "approvalId": approval_id,
        "exportedAt": main.now_iso(),
    }


def stub_generation(monkeypatch, model_calls=None, export_calls=None):
    calls = model_calls if model_calls is not None else []
    exports = export_calls if export_calls is not None else []
    monkeypatch.setattr(main, "scrape_contact_details", lambda lead: {"email": lead.email, "phone": lead.phone, "website": lead.website})
    monkeypatch.setattr(
        main,
        "generate_page_prompt_with_gemini",
        lambda context, template: calls.append("gemini_prompt") or {"pagePrompt": "Build the page with Bootstrap 5 and GSAP."},
    )
    monkeypatch.setattr(
        main,
        "generate_draft_html_with_groq",
        lambda context, template, page_prompt: calls.append("groq_html") or {
            "html": "<!doctype html><html><head><title>Draft</title></head><body><main>Draft</main></body></html>",
            "notes": "Draft",
        },
    )
    monkeypatch.setattr(
        main,
        "finalize_html_with_gemini",
        lambda context, template, page_prompt, draft_html: calls.append("gemini_final") or {
            "html": "<!doctype html><html><head><title>Final</title></head><body><main>Final</main></body></html>",
            "qaNotes": "Final",
        },
    )

    def export_stub(canonical_key, business_name, site_html, pipeline_id=None, approval_id=None):
        exports.append((canonical_key, approval_id))
        return fake_github_export(canonical_key, business_name, site_html, pipeline_id, approval_id)

    monkeypatch.setattr(main, "export_site_to_github", export_stub)
    return calls, exports


def fake_git_deploy_with_history(
    canonical_key,
    business_name,
    pipeline_id,
    approval_id,
    approved_by,
    github_export,
    regenerate_existing_site=False,
):
    history_id = f"history-{canonical_key}"
    result = {
        "deployAction": "CREATED",
        "siteCreated": True,
        "siteReused": False,
        "siteId": f"site-{canonical_key}",
        "siteName": "ai-site-alpha",
        "buildId": f"build-{canonical_key}",
        "deployId": f"deploy-{canonical_key}",
        "state": "ready",
        "url": "https://alpha.netlify.app",
        "deploymentHistoryId": history_id,
        "publishMode": "github-netlify",
        "githubExport": github_export,
        "githubRepoUrl": github_export["repoUrl"],
        "githubRepoFullName": github_export["repository"],
        "commitSha": github_export["commitSha"],
    }
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, pipeline_id, approval_id, site_id, site_name,
                deploy_id, build_id, url, deploy_action, state, html_checksum, deployed_at,
                approved_by, approval_status, github_repo_full_name, github_repo_url,
                commit_sha, publish_mode, github_export_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                canonical_key,
                pipeline_id,
                approval_id,
                result["siteId"],
                result["siteName"],
                result["deployId"],
                result["buildId"],
                result["url"],
                result["deployAction"],
                result["state"],
                github_export["htmlChecksum"],
                main.now_iso(),
                approved_by,
                "APPROVED",
                github_export["repository"],
                github_export["repoUrl"],
                github_export["commitSha"],
                "github-netlify",
                main.json.dumps(github_export, default=str),
                main.json.dumps(result, default=str),
            ),
        )
    return result


def test_discover_leads_searches_requested_location_and_caches(monkeypatch):
    calls = []

    def fake_apify(query, limit, location="South Africa"):
        calls.append((query, location, limit))
        return [apify_item(10, province="Gauteng"), apify_item(11, province="Gauteng")]

    monkeypatch.setattr(main, "run_apify_google_maps", fake_apify)

    first = client.post(
        "/api/leads/discover",
        json={"presetId": "restaurants", "location": "Gauteng, South Africa", "query": "family restaurants", "limit": 2},
    )
    second = client.post(
        "/api/leads/discover",
        json={"presetId": "restaurants", "location": "Gauteng, South Africa", "query": "family restaurants", "limit": 2},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert len(second.json()["leads"]) == 2
    assert calls == [("family restaurants in Gauteng, South Africa", "Gauteng, South Africa", 2)]


def test_discover_force_refresh_skips_previously_seen_duplicate(monkeypatch):
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location="South Africa": [apify_item(1, province="Gauteng")])

    first = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1})
    second = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1, "forceRefresh": True})

    assert first.status_code == 200
    assert len(first.json()["leads"]) == 1
    assert second.status_code == 200
    assert second.json()["cached"] is False
    assert second.json()["leads"] == []
    assert second.json()["duplicatesSkipped"] == 1


def test_pipeline_generates_bootstrap_gsap_html_and_exports_to_github_before_approval(monkeypatch):
    model_calls, export_calls = stub_generation(monkeypatch)
    monkeypatch.setattr(
        main,
        "deploy_github_repo_to_netlify_for_lead",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deployment must wait for approval")),
    )

    response = client.post("/api/pipeline/run", json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead_payload()]})

    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]
    assert payload["status"] == "PENDING_APPROVAL"
    assert result["status"] == "PENDING_APPROVAL"
    assert result["pendingApprovalId"]
    assert result["githubExport"]["repoUrl"].startswith("https://github.com/")
    assert "bootstrap@5" in result["pendingPreviewHtml"]
    assert "gsap" in result["pendingPreviewHtml"].lower()
    assert model_calls == ["gemini_prompt", "groq_html", "gemini_final"]
    assert len(export_calls) == 1

    detail = client.get(f"/api/approvals/{result['pendingApprovalId']}").json()
    assert detail["status"] == "PENDING"
    assert detail["githubExport"]["commitSha"]


def test_fallback_landing_page_renderer_is_bootstrap_gsap_and_polished():
    html = main.render_site_html(
        {
            "businessName": "Alpha Plumbing",
            "industry": "Plumbing",
            "location": "Durban",
            "email": "alpha@example.com",
            "phone": "+27 31 000 0000",
            "website": "https://alpha.example.com",
            "summary": "Local plumbing support for Durban customers.",
        },
        {
            "headline": "Reliable Plumbing Help in Durban",
            "subheadline": "Fast, practical plumbing support for local homes and businesses.",
            "about": "Alpha Plumbing helps Durban customers with practical service and clear communication.",
            "ctaLabel": "Request a Quote",
            "contactIntro": "Contact the team to discuss your plumbing needs.",
            "footerText": "Alpha Plumbing | Durban",
            "services": [
                {"title": "Emergency Repairs", "description": "Responsive help for urgent plumbing problems."},
                {"title": "Installations", "description": "Support for fixtures, pipes, and upgrades."},
                {"title": "Maintenance", "description": "Routine checks that help prevent future issues."},
                {"title": "Customer Support", "description": "Clear communication from first contact."},
            ],
        },
        {"accent": "#0f9f96", "background": "#f8fbff"},
        ["data:image/svg+xml;base64,hero", "data:image/svg+xml;base64,one", "data:image/svg+xml;base64,two", "data:image/svg+xml;base64,three", "data:image/svg+xml;base64,four"],
    )

    lower = html.lower()
    assert "bootstrap@5.3.3" in lower
    assert "gsap@3.12.5" in lower
    assert "class=\"hero hero-section\"" in lower
    assert "btn-brand" in lower
    assert lower.count("service-card") >= 4
    assert "about-band" in lower
    assert "contact-card" in lower
    assert "<footer>" in lower


def test_pipeline_records_github_export_without_netlify_before_approval(monkeypatch):
    stub_generation(monkeypatch)

    response = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]})
    pipeline_id = response.json()["pipelineId"]
    steps = client.get(f"/api/pipeline/runs/{pipeline_id}").json()["steps"]
    step_names = [step["step"] for step in steps]

    assert response.status_code == 200
    assert "github_export" in step_names
    assert "netlify_deploy" not in step_names


def test_approval_deploys_from_github_and_clears_successful_html(monkeypatch):
    stub_generation(monkeypatch)
    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", fake_git_deploy_with_history)
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"})
    monkeypatch.setattr(
        main,
        "create_zendesk_outreach_ticket",
        lambda context, deployment, outreach, pipeline_id: {"syncStatus": "TICKET_CREATED", "ticketId": 123, "ticketUrl": "https://zendesk.test/123"},
    )

    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]
    approved = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"}).json()

    assert approved["status"] == "APPROVED"
    assert approved["deployment"]["url"] == "https://alpha.netlify.app"
    assert approved["deployment"]["githubRepoUrl"].startswith("https://github.com/")
    assert approved["deployment"]["buildId"]
    assert approved["zendesk"]["ticketId"] == 123

    detail = client.get(f"/api/approvals/{approval_id}?includeHtml=true").json()
    assert detail["pendingPreviewHtml"] is None
    assert detail["previewAvailable"] is False

    steps = client.get(f"/api/pipeline/runs/{pipeline['pipelineId']}").json()["steps"]
    step_names = [step["step"] for step in steps]
    assert step_names.index("github_export") < step_names.index("netlify_deploy")


def test_failed_github_netlify_deployment_keeps_generated_html(monkeypatch):
    stub_generation(monkeypatch)

    def failed_deploy(*args, **kwargs):
        raise RuntimeError("netlify build failed")

    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", failed_deploy)
    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]

    response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})
    detail = client.get(f"/api/approvals/{approval_id}?includeHtml=true").json()

    assert response.status_code == 502
    assert detail["status"] == "DEPLOY_FAILED"
    assert detail["pendingPreviewHtml"]
    assert detail["previewAvailable"] is True
    assert detail["errors"][0]["retryable"] is True


def test_pipeline_reuses_pending_github_export_without_model_calls(monkeypatch):
    model_calls, export_calls = stub_generation(monkeypatch)
    lead = lead_payload()

    first = client.post("/api/pipeline/run", json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead]})
    first_approval_id = first.json()["results"][0]["pendingApprovalId"]

    model_calls.clear()
    export_calls.clear()
    second = client.post("/api/pipeline/run", json={"templateId": "default-service", "sourceBatchId": "batch-1", "leads": [lead]})

    assert second.status_code == 200
    result = second.json()["results"][0]
    assert result["currentStep"] == "reused_pending_approval"
    assert result["pendingApprovalId"] == first_approval_id
    assert result["githubExport"]["commitSha"]
    assert model_calls == []
    assert export_calls == []


def test_github_export_failure_is_retryable_and_keeps_html(monkeypatch):
    calls, _exports = stub_generation(monkeypatch)
    attempts = {"count": 0}

    def flaky_export(canonical_key, business_name, site_html, pipeline_id=None, approval_id=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary github failure")
        return fake_github_export(canonical_key, business_name, site_html, pipeline_id, approval_id)

    monkeypatch.setattr(main, "export_site_to_github", flaky_export)

    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]})
    result = pipeline.json()["results"][0]

    assert pipeline.status_code == 200
    assert pipeline.json()["status"] == "FAILED"
    assert result["status"] == "EXPORT_FAILED"
    assert result["pendingPreviewHtml"]
    assert result["structuredErrors"][0]["retryable"] is True

    retried = client.post(f"/api/approvals/{result['pendingApprovalId']}/retry-export", json={"requestedBy": "Ops"})
    assert retried.status_code == 200
    assert retried.json()["status"] == "PENDING"
    assert attempts["count"] == 2
    assert calls == ["gemini_prompt", "groq_html", "gemini_final"]


def test_github_export_creates_unique_repo_and_commits_readme_then_index(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    created_repos = []
    put_paths = []

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url == "https://api.github.com/user/repos"
        created_repos.append(json["name"])
        assert json["private"] is False
        return FakeResponse(
            201,
            {"id": len(created_repos), "name": json["name"], "full_name": f"owner/{json['name']}", "html_url": f"https://github.com/owner/{json['name']}", "default_branch": "main", "private": False},
        )

    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(404, {})

    def fake_put(url, headers=None, json=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        put_paths.append(path)
        return FakeResponse(201, {"content": {"sha": f"{path}-sha", "html_url": f"https://github.test/{path}"}, "commit": {"sha": f"{path}-commit"}})

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)

    first = main.export_site_to_github("canonical-one", "Alpha Plumbing", "<html>one</html>", "pipeline-1", "approval-1")
    second = main.export_site_to_github("canonical-two", "Alpha Plumbing", "<html>two</html>", "pipeline-2", "approval-2")

    assert first["repository"].startswith("owner/ai-site-alpha-plumbing-")
    assert first["repoUrl"].startswith("https://github.com/owner/")
    assert first["commitSha"] == "index.html-commit"
    assert put_paths == ["README.md", "index.html", "README.md", "index.html"]
    assert len(set(created_repos)) == 2
    with main.get_pipeline_db() as db:
        rows = db.execute("SELECT * FROM github_site_repos WHERE export_status = 'EXPORTED'").fetchall()
    assert len(rows) == 2


def test_netlify_git_deploy_uses_build_api_not_zip_upload(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    calls = []
    github_export = fake_github_export("canonical-one", "Alpha Plumbing", "<html>one</html>", "pipeline-1", "approval-1")

    def fake_post(url, headers=None, json=None, params=None, data=None, timeout=None):
        calls.append(("post", url, json, data))
        assert data is None
        if url.endswith("/api/v1/sites"):
            assert json["repo"]["repo_path"] == github_export["repository"]
            return FakeResponse(201, {"id": "site-1", "name": "ai-site-alpha", "ssl_url": "https://alpha.netlify.app", "admin_url": "https://app.netlify.com/sites/alpha"})
        if url.endswith("/builds"):
            return FakeResponse(201, {"id": "build-1", "deploy_id": "deploy-1", "done": True})
        raise AssertionError(url)

    def fake_get(url, headers=None, timeout=None):
        calls.append(("get", url, None, None))
        if "/deploys/" in url:
            return FakeResponse(200, {"id": "deploy-1", "state": "ready", "ssl_url": "https://alpha.netlify.app"})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)

    deployment = main.deploy_github_repo_to_netlify_for_lead(
        "canonical-one",
        "Alpha Plumbing",
        "pipeline-1",
        "approval-1",
        "Ops",
        github_export,
    )

    assert deployment["state"] == "ready"
    assert deployment["buildId"] == "build-1"
    assert not any("/deploys" in call[1] and call[0] == "post" for call in calls)
    history = client.get("/api/deployments/history").json()["deployments"]
    assert history[0]["github_repo_url"] == github_export["repoUrl"]
    assert history[0]["build_id"] == "build-1"


def test_no_owner_fields_in_responses(monkeypatch):
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location="South Africa": [apify_item(1, province="Gauteng")])
    discovery = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1}).json()
    assert "ownerName" not in discovery["leads"][0]
    assert "ownerEmail" not in discovery["leads"][0]
    assert "ownerStatus" not in discovery["leads"][0]

    approval_id = main.create_approval_record(
        pipeline_id="pipeline-owner",
        canonical_key="canonical-owner",
        lead_key="lead-owner",
        business_name="Owner Test",
        site_html="<!doctype html><html><body>Owner Test</body></html>",
        context={"businessName": "Owner Test", "ownerName": "Ops", "ownerEmail": "ops@example.com", "ownerStatus": "assigned"},
        site_content={"finalHtmlChecksum": "checksum"},
        template={"id": "default-service"},
    )
    approval = client.get(f"/api/approvals/{approval_id}").json()
    assert "ownerName" not in approval["context"]
    assert "ownerEmail" not in approval["context"]
    summary = client.get("/api/reporting/summary").json()
    assert "ownerPerformance" not in summary


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
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret-value")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "cxsupporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "owner@company.test")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-secret-value")

    response = client.get("/api/debug/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "READY"
    masked = payload["providers"]["github"]["checks"][1]["maskedValue"]
    assert masked != "github-secret-value"
    assert "chars" in masked


def test_debug_probe_reports_missing_environment(monkeypatch):
    for name in [
        "APIFY_API_TOKEN",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "NETLIFY_AUTH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
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
        authorization="Bearer raw-token-value",
    )

    response = client.get("/api/debug/logs?limit=5")

    assert response.status_code == 200
    redaction_log = next(log for log in response.json()["logs"] if log["event"] == "test.redaction")
    details = redaction_log["details"]
    assert details["email"] != "owner@example.com"
    assert details["apiToken"] != "super-secret-token"
    assert details["authorization"] != "Bearer raw-token-value"
