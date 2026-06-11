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


def test_discover_leads_searches_all_provinces_and_returns_ten(monkeypatch):
    calls = []

    def fake_apify(query, limit, location="South Africa"):
        province = location.split(",")[0]
        province_index = len(calls) + 1
        calls.append((query, location))
        return [
            apify_item(province_index * 10, province=province),
            apify_item(province_index * 10 + 1, province=province),
        ]

    monkeypatch.setattr(main, "run_apify_google_maps", fake_apify)

    response = client.post(
        "/api/leads/discover",
        json={
            "presetId": "restaurants",
            "location": "South Africa",
            "query": "family restaurants",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sourceStatus"] == "READY"
    assert len(payload["leads"]) == 10
    assert len(calls) == len(main.SOUTH_AFRICA_PROVINCES)
    assert payload["leads"][0]["province"] == "Eastern Cape"
    assert payload["provinceStats"]["Gauteng"]["rawItems"] == 2
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
    assert second.json()["duplicatesSkipped"] == len(main.SOUTH_AFRICA_PROVINCES)


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
        lambda canonical_key, business_name, site_html, pipeline_id, approval_id, approved_by, regenerate_existing_site=True: {
            "deployAction": "CREATED",
            "siteCreated": True,
            "siteReused": False,
            "siteId": "site-1",
            "siteName": "ai-site-alpha",
            "deployId": "deploy-1",
            "state": "ready",
            "url": "https://alpha.netlify.app",
            "deploymentHistoryId": "history-1",
        },
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

    lead = main.DiscoveredLead(
        leadKey="lead-1",
        businessName="Alpha Plumbing",
        email="alpha@example.com",
        category="Plumbing",
        location="Durban",
    ).model_dump()

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
