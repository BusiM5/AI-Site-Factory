import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


client = TestClient(main.app)


def test_backend_browser_smoke_routes():
    assert client.get("/").status_code == 200
    assert client.get("/favicon.ico").status_code == 204


def test_gemini_probe_uses_auth_header(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return FakeResponse(payload={"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"]})

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-test")
    monkeypatch.setattr(main.requests, "get", fake_get)

    result = main.probe_gemini()

    assert "key=" not in captured["url"]
    assert captured["headers"] == {"x-goog-api-key": "test-gemini-key"}
    assert result["model"] == "models/gemini-test"


def test_groq_auth_failure_is_actionable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: FakeResponse(status_code=401))

    with pytest.raises(RuntimeError, match="Replace GROQ_API_KEY"):
        main.groq_chat_json("prompt", "system")


def test_gemini_rate_limit_uses_local_final_html_fallback(monkeypatch):
    monkeypatch.setattr(
        main,
        "gemini_text_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            main.GeminiRateLimitError("Gemini rate limit reached.")
        ),
    )

    result = main.generate_final_html_with_gemini(
        {
            "businessName": "STYLE BY RN",
            "industry": "Beauty",
            "location": "Durban",
            "summary": "Local beauty services.",
            "serviceKeywords": ["Beauty"],
        }
    )

    assert result["html"].lower().startswith("<!doctype html>")
    assert "bootstrap@5.3.8" in result["html"].lower()
    assert "gsap@3.15" in result["html"].lower()
    assert "fallback" in result["qaNotes"].lower()


def test_existing_theme_widget_is_upgraded_to_control_site_variables():
    legacy_html = """<!doctype html><html><head>
    <style id="ai-site-theme-widget-style">old</style>
    </head><body>
    <aside data-ai-site-theme-widget></aside>
    <script id="ai-site-theme-widget-script">old</script>
    </body></html>"""

    upgraded = main.ensure_required_site_features(legacy_html)

    assert upgraded.count('id="ai-site-theme-widget-style"') == 1
    assert upgraded.count('id="ai-site-theme-widget-script"') == 1
    assert 'data-theme-color="text"' in upgraded
    assert 'data-theme-color="background"' in upgraded
    assert 'data-theme-color="highlight"' in upgraded
    assert 'root.style.setProperty("--ai-text", text)' in upgraded
    assert 'root.style.setProperty("--ai-background", background)' in upgraded
    assert 'root.style.setProperty("--ai-highlight", highlight)' in upgraded


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
        "website": None,
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
        "compact_lead_with_groq",
        lambda context: calls.append("groq_compact") or {
            **context,
            "serviceKeywords": [context.get("industry", "service")],
            "contactType": "email" if context.get("email") else "phone",
        },
    )
    monkeypatch.setattr(
        main,
        "generate_final_html_with_gemini",
        lambda lead_brief: calls.append("gemini_final") or {
            "html": "<!doctype html><html><head><title>Final</title></head><body><main>Final</main></body></html>",
            "qaNotes": "Final",
            "stylingLibraries": ["Bootstrap", "Tailwind CSS", "Animate.css"],
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


def fake_direct_deploy_with_history(
    canonical_key,
    business_name,
    site_html,
    pipeline_id,
    approval_id,
    approved_by,
    github_export,
):
    result = fake_git_deploy_with_history(
        canonical_key,
        business_name,
        pipeline_id,
        approval_id,
        approved_by,
        github_export,
    )
    result["publishMode"] = "direct-netlify"
    result["deploymentMode"] = "Direct Netlify"
    history_id = result["deploymentHistoryId"]
    with main.get_pipeline_db() as db:
        db.execute(
            "UPDATE deployment_history SET publish_mode = ?, raw_json = ? WHERE id = ?",
            ("direct-netlify", main.json.dumps(result, default=str), history_id),
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
    assert calls == [("family restaurants in Gauteng, South Africa", "Gauteng, South Africa", 10)]


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


def test_discover_filters_out_website_present_leads(monkeypatch):
    with_website = apify_item(1, province="Gauteng")
    with_website["website"] = "https://already-online.example.com"
    no_website = apify_item(2, province="Gauteng")
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location="South Africa": [with_website, no_website])

    response = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["leads"]) == 1
    assert payload["leads"][0]["businessName"] == "Lead Business 2"
    assert payload["provinceStats"]["South Africa"]["websitesSkipped"] == 1


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
    assert "cdn.tailwindcss.com" in result["pendingPreviewHtml"]
    assert "animate.css" in result["pendingPreviewHtml"].lower()
    assert "data-ai-site-theme-widget" in result["pendingPreviewHtml"]
    assert model_calls == ["groq_compact", "gemini_final"]
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
    assert "bootstrap@5.3.8" in lower
    assert "sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" in html
    assert "cdn.tailwindcss.com" in lower
    assert "animate.css" in lower
    assert "data-ai-site-theme-widget" in lower
    assert "gsap@3.15" in lower
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


def test_pipeline_skips_website_present_lead_before_model_calls(monkeypatch):
    model_calls, export_calls = stub_generation(monkeypatch)
    lead = lead_payload()
    lead["website"] = "https://alpha.example.com"

    response = client.post("/api/pipeline/run", json={"leads": [lead]})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "SKIPPED_WEBSITE_PRESENT"
    assert model_calls == []
    assert export_calls == []


def test_pipeline_skips_duplicate_selected_leads(monkeypatch):
    model_calls, export_calls = stub_generation(monkeypatch)
    first = lead_payload()
    second = {**lead_payload(), "leadKey": "lead-duplicate", "businessName": "Alpha Plumbing Duplicate"}

    response = client.post("/api/pipeline/run", json={"leads": [first, second]})

    assert response.status_code == 200
    payload = response.json()
    statuses = [result["status"] for result in payload["results"]]
    assert statuses == ["PENDING_APPROVAL", "SKIPPED_DUPLICATE"]
    assert model_calls == ["groq_compact", "gemini_final"]
    assert len(export_calls) == 1


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
    assert approved["publishMode"] == "github-netlify"
    assert approved["zendesk"]["ticketId"] == 123

    detail = client.get(f"/api/approvals/{approval_id}?includeHtml=true").json()
    assert detail["pendingPreviewHtml"] is None
    assert detail["previewAvailable"] is False

    steps = client.get(f"/api/pipeline/runs/{pipeline['pipelineId']}").json()["steps"]
    step_names = [step["step"] for step in steps]
    assert step_names.index("github_export") < step_names.index("netlify_git_deploy")


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

    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", fake_git_deploy_with_history)
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"})
    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", lambda *args, **kwargs: {"syncStatus": "TICKET_CREATED", "ticketId": 123})

    retry = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})

    assert retry.status_code == 200
    assert retry.json()["status"] == "APPROVED"


def test_approval_requires_successful_github_export_before_deploy():
    approval_id = main.create_approval_record(
        pipeline_id="pipeline-no-export",
        canonical_key="canonical-no-export",
        lead_key="lead-no-export",
        business_name="No Export Plumbing",
        site_html="<!doctype html><html><body>No Export</body></html>",
        context={"businessName": "No Export Plumbing"},
        site_content={"finalHtmlChecksum": "checksum"},
        template={"id": "default-service"},
    )

    response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})

    assert response.status_code == 409
    assert "Retry Export" in response.json()["detail"]


def test_approval_does_not_auto_fallback_to_direct_netlify_when_git_deploy_fails(monkeypatch):
    stub_generation(monkeypatch)

    def failed_git_deploy(*args, **kwargs):
        raise RuntimeError("Host key verification failed / Could not read from remote repository")

    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", failed_git_deploy)
    monkeypatch.setattr(
        main,
        "deploy_direct_netlify_fallback_for_lead",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct fallback must be explicitly requested")),
    )

    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]
    response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})

    assert response.status_code == 502
    detail = client.get(f"/api/approvals/{approval_id}").json()
    assert detail["status"] == "DEPLOY_FAILED"
    assert detail["publishMode"] == "github-netlify"
    assert detail["deploymentHistory"] is None


def test_direct_netlify_deploy_is_available_only_when_explicitly_requested(monkeypatch):
    stub_generation(monkeypatch)
    monkeypatch.setattr(main, "deploy_direct_netlify_for_lead", fake_direct_deploy_with_history)
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"})
    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", lambda *args, **kwargs: {"syncStatus": "TICKET_CREATED", "ticketId": 456})

    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]
    approved = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops", "publishMode": "direct-netlify"}).json()

    assert approved["status"] == "APPROVED"
    assert approved["publishMode"] == "direct-netlify"
    assert approved["deployment"]["deploymentMode"] == "Direct Netlify"


def test_zendesk_phone_lead_ticket_is_private_tagged_and_has_no_fake_email(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    captured_posts = []

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        captured_posts.append((url, json))
        if url.endswith("/organizations.json"):
            return FakeResponse(201, {"organization": {"id": 42}})
        if url.endswith("/tickets.json"):
            ticket = json["ticket"]
            assert "requester_id" not in ticket
            assert ticket["status"] == "new"
            assert ticket["comment"]["public"] is False
            assert "ai_site_phone_lead" in ticket["tags"]
            assert "https://alpha.netlify.app" in ticket["comment"]["body"]
            return FakeResponse(201, {"ticket": {"id": 999, "status": "new"}})
        raise AssertionError(url)

    def fake_get(*args, **kwargs):
        raise AssertionError("Phone-only Zendesk flow must not search or create a fake email requester.")

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)

    context = {
        "businessName": "Alpha Plumbing",
        "industry": "Plumbing",
        "location": "Durban",
        "phone": "+27 31 000 0000",
        "source": "Google Maps",
    }
    deployment = {"url": "https://alpha.netlify.app"}
    outreach = main.generate_outreach_with_groq(context, deployment["url"])
    result = main.create_zendesk_outreach_ticket(context, deployment, outreach, "pipeline-1")

    assert result["ticketId"] == 999
    assert result["contactType"] == "phone"
    assert result["liveLink"] == "https://alpha.netlify.app"
    assert "ai_site_no_website" in result["tags"]
    assert len(captured_posts) == 2


def test_sites_endpoint_filters_by_live_email_leads(monkeypatch):
    stub_generation(monkeypatch)
    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", fake_git_deploy_with_history)
    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", lambda context, deployment, outreach, pipeline_id: {"syncStatus": "TICKET_CREATED", "ticketId": 123, "liveLink": deployment["url"], "contactType": "email", "tags": ["ai_site_email_lead"]})

    pipeline = client.post("/api/pipeline/run", json={"leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]
    client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})

    response = client.get("/api/sites?status=deployed&contactType=email&q=Alpha&page=1&pageSize=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["sites"][0]["businessName"] == "Alpha Plumbing"
    assert payload["sites"][0]["contactType"] == "email"
    assert payload["sites"][0]["liveUrl"] == "https://alpha.netlify.app"


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
    assert calls == ["groq_compact", "gemini_final"]


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


def test_direct_netlify_deploy_migrates_stale_site_to_current_account(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "new-account-token")
    now = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, admin_url,
                github_repo_full_name, github_repo_url, last_commit_sha, last_build_id,
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "canonical-one",
                "old-account-site",
                "ai-site-alpha-plumbing-canonical",
                "https://old-account.netlify.app",
                "https://app.netlify.com/sites/old-account",
                "owner/ai-site-alpha",
                "https://github.com/owner/ai-site-alpha",
                "old-commit",
                None,
                now,
                now,
                None,
                "ready",
                1,
            ),
        )

    create_names = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/sites/old-account-site"):
            return FakeResponse(403, {})
        if url.endswith("/api/v1/sites"):
            return FakeResponse(200, [])
        raise AssertionError(url)

    def fake_post(url, headers=None, json=None, data=None, timeout=None):
        if url.endswith("/api/v1/sites"):
            create_names.append(json["name"])
            if len(create_names) == 1:
                return FakeResponse(422, {})
            return FakeResponse(
                201,
                {
                    "id": "new-account-site",
                    "name": json["name"],
                    "ssl_url": "https://new-account.netlify.app",
                    "admin_url": "https://app.netlify.com/sites/new-account",
                },
            )
        if url.endswith("/sites/new-account-site/deploys"):
            return FakeResponse(
                201,
                {
                    "id": "new-deploy",
                    "state": "ready",
                    "ssl_url": "https://new-account.netlify.app",
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "post", fake_post)

    result = main.deploy_direct_netlify_for_lead(
        canonical_key="canonical-one",
        business_name="Alpha Plumbing",
        site_html="<!doctype html><html><body>Alpha</body></html>",
        pipeline_id="pipeline-one",
        approval_id="approval-one",
        approved_by="Ops",
        github_export=fake_github_export("canonical-one", "Alpha Plumbing", "<html>Alpha</html>"),
    )

    assert result["accountMigration"] is True
    assert result["siteId"] == "new-account-site"
    assert result["url"] == "https://new-account.netlify.app"
    assert len(create_names) == 2
    assert create_names[1] != create_names[0]
    with main.get_pipeline_db() as db:
        registry = db.execute(
            "SELECT site_id, url FROM site_registry WHERE canonical_lead_key = ?",
            ("canonical-one",),
        ).fetchone()
    assert registry["site_id"] == "new-account-site"
    assert registry["url"] == "https://new-account.netlify.app"


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


def create_pending_approval_for_webhook():
    approval_id = main.create_approval_record(
        pipeline_id="pipeline-webhook",
        canonical_key="canonical-webhook",
        lead_key="lead-webhook",
        business_name="Webhook Plumbing",
        site_html="<!doctype html><html><body>Webhook Plumbing</body></html>",
        context={
            "canonicalLeadKey": "canonical-webhook",
            "businessName": "Webhook Plumbing",
            "industry": "Plumbing",
            "location": "Durban",
            "email": "owner@example.com",
            "phone": "+27 31 000 0000",
            "sourceUrl": "https://maps.example.com/webhook",
        },
        site_content={"finalHtmlChecksum": "checksum"},
        template={"id": "default-service"},
    )
    with main.get_pipeline_db() as db:
        db.execute(
            "UPDATE approval_records SET status = ?, github_export_json = ? WHERE id = ?",
            ("PENDING", main.json.dumps(fake_github_export("canonical-webhook", "Webhook Plumbing", "html"), default=str), approval_id),
        )
    return approval_id


def test_zendesk_field_settings_roundtrip():
    response = client.put(
        "/api/settings/zendesk-fields",
        json={"fields": {"canonicalLeadKey": "1001", "pipelineId": "1002", "unknown": "ignored"}},
    )

    assert response.status_code == 200
    payload = client.get("/api/settings/zendesk-fields").json()
    assert payload["fields"]["canonicalLeadKey"] == "1001"
    assert payload["fields"]["pipelineId"] == "1002"
    assert "unknown" not in payload["fields"]


def test_zendesk_intake_creates_email_and_phone_tickets_once(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    main.save_zendesk_field_settings({"approvalId": "2001", "contactChannel": "2002"})
    ticket_posts = []

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        if url.endswith("/organizations.json"):
            return FakeResponse(201, {"organization": {"id": 42}})
        if url.endswith("/users.json"):
            return FakeResponse(201, {"user": {"id": 77, "email": "alpha@example.com"}})
        if url.endswith("/tickets.json"):
            ticket_posts.append(json["ticket"])
            return FakeResponse(201, {"ticket": {"id": 900 + len(ticket_posts), "status": "new"}})
        raise AssertionError(url)

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        if url.endswith("/users/search.json"):
            return FakeResponse(200, {"users": []})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)
    context = {
        "canonicalLeadKey": "canonical-intake",
        "businessName": "Alpha Plumbing",
        "industry": "Plumbing",
        "location": "Durban",
        "email": "alpha@example.com",
        "phone": "+27 31 000 0000",
        "sourceUrl": "https://maps.example.com/1",
    }

    first = main.create_zendesk_intake_tickets("approval-intake", context, "pipeline-intake", "batch-1")
    second = main.create_zendesk_intake_tickets("approval-intake", context, "pipeline-intake", "batch-1")

    assert len(first) == 2
    assert len(second) == 2
    assert len(ticket_posts) == 2
    assert {ticket["tags"][2] for ticket in ticket_posts} == {"ai_site_email_lead", "ai_site_phone_lead"}
    assert ticket_posts[0]["custom_fields"]


def test_zendesk_webhook_rejects_invalid_secret(monkeypatch):
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "expected-secret")

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "phone_status", "approvalId": "missing"},
        headers={"x-ai-site-factory-secret": "wrong"},
    )

    assert response.status_code == 401


def test_zendesk_webhook_phone_status_updates_ticket_without_deploy(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(approval_id, "canonical-webhook", "pipeline-webhook", "phone", "intake", 321, "https://zendesk.test/321", "new", ["ai_site_phone_lead"], {})
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    captured = {}

    def fake_put(url, json=None, auth=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse(200, {"ticket": {"id": 321, "status": "open"}})

    monkeypatch.setattr(main.requests, "put", fake_put)

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "phone_status", "approvalId": approval_id, "channel": "phone", "zendeskTicketId": 321, "value": "CALL_BACK"},
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 200
    assert captured["payload"]["ticket"]["comment"]["public"] is False
    assert "CALL_BACK" in captured["payload"]["ticket"]["comment"]["body"]


def test_zendesk_webhook_rejects_phone_status_for_email_ticket(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(approval_id, "canonical-webhook", "pipeline-webhook", "email", "intake", 654, "https://zendesk.test/654", "new", ["ai_site_email_lead"], {})
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "phone_status", "approvalId": approval_id, "channel": "email", "zendeskTicketId": 654, "value": "CALL_BACK"},
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 409
    assert "phone-channel" in response.json()["detail"]


def test_zendesk_webhook_email_adds_public_reply(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(approval_id, "canonical-webhook", "pipeline-webhook", "email", "intake", 654, "https://zendesk.test/654", "new", ["ai_site_email_lead"], {})
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": "Public email body"})
    captured = {}

    def fake_put(url, json=None, auth=None, timeout=None):
        captured["payload"] = json
        return FakeResponse(200, {"ticket": {"id": 654, "status": "open"}})

    monkeypatch.setattr(main.requests, "put", fake_put)

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "send_email", "approvalId": approval_id, "channel": "email", "zendeskTicketId": 654},
        headers={"x-zendesk-webhook-secret": "secret"},
    )

    assert response.status_code == 200
    assert captured["payload"]["ticket"]["comment"]["public"] is True
    assert "Public email body" in captured["payload"]["ticket"]["comment"]["body"]


def test_zendesk_webhook_rejects_email_send_for_phone_ticket(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(approval_id, "canonical-webhook", "pipeline-webhook", "phone", "intake", 321, "https://zendesk.test/321", "new", ["ai_site_phone_lead"], {})
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "send_email", "approvalId": approval_id, "channel": "phone", "zendeskTicketId": 321},
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 409
    assert "email-channel" in response.json()["detail"]


def test_zendesk_webhook_deploy_triggers_existing_approval_path(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(approval_id, "canonical-webhook", "pipeline-webhook", "email", "intake", 777, "https://zendesk.test/777", "new", ["ai_site_email_lead"], {})
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", fake_git_deploy_with_history)
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": "Preview body"})
    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", lambda context, deployment, outreach, pipeline_id: {"ticketId": 123, "syncStatus": "TICKET_CREATED"})

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "deploy_site", "approvalId": approval_id, "channel": "email", "zendeskTicketId": 777},
        headers={"x-zendesk-webhook-secret": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["deployment"]["status"] == "APPROVED"
    assert response.json()["result"]["deployment"]["publishMode"] == "github-netlify"


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
