import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


client = TestClient(main.app)


def apify_item(index, category="Restaurant"):
    return {
        "title": f"Lead Business {index}",
        "website": f"https://lead-{index}.example.com",
        "phone": f"+27 11 000 {index:04d}",
        "categoryName": category,
        "address": f"{index} Market Street",
        "rating": 4.5,
        "reviewsCount": 20 + index,
        "googleMapsUrl": f"https://maps.example.com/{index}",
    }


def test_discover_leads_retries_and_returns_ten(monkeypatch):
    calls = []

    def fake_apify(query, limit):
        calls.append(query)
        if len(calls) == 1:
            return [apify_item(index) for index in range(5)]
        return [apify_item(index) for index in range(12)]

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
    assert len(calls) == 2
    assert payload["leads"][0]["businessName"] == "Lead Business 0"


def test_normalize_apify_items_handles_missing_fields_and_duplicates():
    items = [
        {"title": "Same Business", "website": "same.example.com"},
        {"title": "Same Business", "website": "https://same.example.com"},
        {"title": "No Contact Business", "categoryName": "Plumbing"},
        {"website": "https://missing-name.example.com"},
    ]

    leads = main.normalize_apify_items(
        items,
        fallback_category="General Services",
        location="Durban",
        limit=10,
    )

    assert len(leads) == 2
    assert leads[0].domain == "same.example.com"
    assert leads[1].email is None
    assert leads[1].category == "Plumbing"


def test_pipeline_run_processes_multiple_leads(monkeypatch):
    def fake_context(lead, contact):
        return {
            "businessName": lead.businessName,
            "industry": lead.category,
            "location": lead.location,
            "email": lead.email or "info@example.com",
            "phone": lead.phone,
            "website": lead.website,
            "summary": "A useful local business.",
            "serviceKeywords": [lead.category],
            "imagePrompts": ["clean business image"] * 5,
        }

    monkeypatch.setattr(main, "scrape_contact_details", lambda lead: {"email": lead.email, "phone": lead.phone, "website": lead.website})
    monkeypatch.setattr(main, "enrich_lead_with_gemini", fake_context)
    monkeypatch.setattr(
        main,
        "generate_site_content_with_groq",
        lambda context, template: {
            "headline": f"{context['businessName']} Website",
            "subheadline": "Modern local service landing page.",
            "about": "A concise about section.",
            "services": [
                {"title": "Service One", "description": "Description one."},
                {"title": "Service Two", "description": "Description two."},
                {"title": "Service Three", "description": "Description three."},
                {"title": "Service Four", "description": "Description four."},
            ],
            "ctaLabel": "Contact us",
            "contactIntro": "Get in touch.",
            "footerText": context["businessName"],
        },
    )
    monkeypatch.setattr(
        main,
        "generate_gemini_images",
        lambda prompts, accent: [main.fallback_image_data_uri("Mock", accent)] * 5,
    )
    monkeypatch.setattr(
        main,
        "deploy_site_to_netlify",
        lambda name, site_html: {
            "url": f"https://{main.slugify(name)}.netlify.app",
            "state": "ready",
            "mode": "production",
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
    assert payload["status"] == "COMPLETED"
    assert len(payload["results"]) == 2
    assert payload["results"][0]["deployment"]["url"].endswith(".netlify.app")
    assert payload["results"][1]["zendesk"]["ticketUrl"].endswith("/123")


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
