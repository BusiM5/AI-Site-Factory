import base64
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_gemini_output_missing_business_profile_is_replaced_with_personalized_page(monkeypatch):
    monkeypatch.setattr(
        main,
        "gemini_text_json",
        lambda *args, **kwargs: {
            "html": "<!doctype html><html><body><h1>Generic local service</h1></body></html>",
            "qaNotes": "Generic",
        },
    )

    result = main.generate_final_html_with_gemini(
        {
            "businessName": "G. Dimitriou Physiotherapy",
            "industry": "Local service",
            "location": "Gqeberha",
        }
    )

    assert "Move with confidence. Recover with care." in result["html"]
    assert "Physiotherapy Care" in result["html"]
    assert "backend generated" in result["qaNotes"]


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
    assert upgraded.count('data-ai-site-theme-version="3"') == 2
    assert "var(--ai-highlight-deep) 0%" in upgraded
    assert "var(--ai-highlight-soft) 100%" in upgraded
    assert 'root.style.setProperty("--ai-on-highlight", onHighlight)' in upgraded
    assert 'img[src^="data:image/svg+xml;base64,"]' in upgraded
    assert "aspect-ratio: 3 / 2 !important" in upgraded
    assert "object-fit: contain !important" in upgraded
    assert "overflow-wrap: anywhere" in upgraded
    assert "white-space: normal" in upgraded


def decode_svg_data_uri(data_uri):
    return base64.b64decode(data_uri.split(",", 1)[1]).decode("utf-8")


def svg_role_lines(svg, role):
    block = re.search(rf"<text data-role='{role}'[^>]*>(.*?)</text>", svg).group(1)
    return re.findall(r"<tspan[^>]*>(.*?)</tspan>", block)


def test_fallback_svg_wraps_all_business_copy_inside_bounded_regions():
    subtitle = "Independent accounting and bookkeeping specialists serving greater Durban"
    business_name = "The Founder's Architect Accounting and Advisory Practice"
    detail = "17 Erskine Terrace, Addington Beach, Durban, 4001, South Africa"

    svg = decode_svg_data_uri(
        main.fallback_image_data_uri(business_name, "#d35d89", subtitle, detail)
    )

    assert "ai-site-fallback-image-v2" in svg
    assert " ".join(svg_role_lines(svg, "subtitle")) == subtitle
    assert " ".join(svg_role_lines(svg, "business-name")) == business_name
    assert " ".join(svg_role_lines(svg, "detail")) == detail
    assert max(map(int, re.findall(r"textLength='(\d+)'", svg))) <= 436
    assert max(map(int, re.findall(r"<tspan x='626' y='(\d+)'", svg))) <= 618

    unbroken = "A" * 180
    lines, _, _ = main.fit_svg_text(unbroken, 436, 92, 24, 0.56)
    assert "".join(lines) == unbroken


def test_existing_legacy_fallback_svg_is_upgraded_without_touching_other_data_images():
    legacy_svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800' viewBox='0 0 1200 800'>"
        "<defs><linearGradient id='accent' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='#d35d89'/><stop offset='1' stop-color='#1d9bf0'/>"
        "</linearGradient></defs>"
        "<rect x='138' y='158' width='426' height='300' rx='30'/>"
        "<text x='626' y='197' font-family='Arial'>Accounting in Durban, South Africa</text>"
        "<text x='626' y='408' font-family='Arial'>The Founder&#x27;s Architect</text>"
        "<text x='626' y='472' font-family='Arial'>17 Erskine Terrace, Addington Beach, Durban</text>"
        "</svg>"
    )
    unrelated_svg = "<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800'><rect/></svg>"
    legacy_uri = "data:image/svg+xml;base64," + base64.b64encode(legacy_svg.encode()).decode()
    unrelated_uri = "data:image/svg+xml;base64," + base64.b64encode(unrelated_svg.encode()).decode()
    existing_html = f"<!doctype html><html><head></head><body><img src='{legacy_uri}'><img src='{unrelated_uri}'></body></html>"

    upgraded = main.ensure_required_site_features(existing_html)
    payloads = re.findall(r"data:image/svg\+xml;base64,([A-Za-z0-9+/]+={0,2})", upgraded)
    upgraded_svg = base64.b64decode(payloads[0]).decode()

    assert "ai-site-fallback-image-v2" in upgraded_svg
    assert "The Founder's Architect" in upgraded_svg
    assert " ".join(svg_role_lines(upgraded_svg, "detail")) == "17 Erskine Terrace, Addington Beach, Durban"
    assert base64.b64decode(payloads[1]).decode() == unrelated_svg

    upgraded_again = main.ensure_required_site_features(upgraded)
    assert upgraded_again == upgraded
    assert upgraded_again.count(payloads[0]) == 1


def test_uploaded_google_main_image_is_preserved_as_the_site_hero():
    main_image_url = "https://lh3.googleusercontent.com/example-business-photo=w1200-h800"
    lead = main.DiscoveredLead(
        leadKey="place-image-test",
        businessName="Image Test Restaurant",
        phone="+27 31 555 0100",
        category="Restaurant",
        location="Durban",
        raw={"imageUrl": main_image_url},
    )

    context = main.build_public_lead_context(lead, {}, "canonical-image-test")
    generated = main.ensure_generated_hero_and_working_links(
        '<!doctype html><html><body><img src="data:image/svg+xml;base64,OLD" alt="Generated hero"></body></html>',
        context,
    )
    fallback = main.build_bootstrap_gsap_landing_html(context, dict(main.FREEFORM_SITE_SPEC))

    assert context["mainImageUrl"] == main_image_url
    assert context["brandTheme"]["name"] == "hospitality"
    assert context["brandTheme"]["highlight"] == "#b45309"
    assert f'src="{main_image_url}"' in generated
    assert f'src="{main_image_url}"' in fallback
    assert "data-ai-business-main-image" in generated
    assert 'data-ai-default-highlight="#b45309"' in fallback


def test_business_profile_replaces_generic_campaign_copy_with_personalized_services():
    context = {
        "businessName": "G. Dimitriou Physiotherapy",
        "industry": "Local service",
        "category": "Local service",
        "location": "Gqeberha, South Africa",
        "summary": "G. Dimitriou Physiotherapy is a local Local service business.",
        "serviceKeywords": ["Local service"],
    }

    profile = main.personalized_business_profile(context)
    fallback = main.build_bootstrap_gsap_landing_html(context, dict(main.FREEFORM_SITE_SPEC))

    assert profile["industry"] == "Physiotherapy"
    assert profile["tagline"] == "Move with confidence. Recover with care."
    assert [service["title"] for service in profile["services"]] == [
        "Physiotherapy Care",
        "Mobility Support",
        "Recovery-Focused Support",
        "Movement Guidance",
    ]
    assert len({service["description"] for service in profile["services"]}) == 4
    assert "Move with confidence. Recover with care." in fallback
    assert "Physiotherapy support for movement and recovery" in fallback
    assert "Services designed for local customers" not in fallback
    assert ">Local Service<" not in fallback


def test_unknown_business_still_receives_name_and_location_specific_copy():
    profile = main.personalized_business_profile(
        {
            "businessName": "Mabaso Community Services",
            "industry": "Local service",
            "location": "Durban",
        }
    )

    assert profile["servicesHeading"] == "How Mabaso Community Services can help local customers"
    assert profile["services"][0]["title"] == "Local Business Enquiries"
    assert all("Mabaso Community Services" in service["description"] for service in profile["services"])


def test_compact_lead_cannot_discard_source_main_image_or_business_theme(monkeypatch):
    main_image_url = "https://lh3.googleusercontent.com/source-business-photo=w1200-h800"
    context = {
        "businessName": "Harbour Physiotherapy",
        "industry": "Physiotherapy",
        "location": "Gqeberha",
        "mainImageUrl": main_image_url,
        "brandTheme": main.business_theme_for_context(
            {"businessName": "Harbour Physiotherapy", "industry": "Physiotherapy"}
        ),
    }
    monkeypatch.setattr(
        main,
        "groq_chat_json",
        lambda *args, **kwargs: {
            "businessName": "Harbour Physiotherapy",
            "industry": "Physiotherapy",
            "mainImageUrl": None,
            "brandTheme": {"highlight": "#ff00ff"},
            "serviceKeywords": ["Physiotherapy"],
        },
    )

    brief = main.compact_lead_with_groq(context)

    assert brief["mainImageUrl"] == main_image_url
    assert brief["brandTheme"]["name"] == "healthcare"
    assert brief["brandTheme"]["highlight"] == "#0f766e"


def test_business_theme_defaults_are_industry_aligned_and_persist_on_upgrade():
    context = {
        "businessName": "Harbour Physiotherapy",
        "industry": "Physiotherapy",
        "location": "Gqeberha",
    }
    themed = main.ensure_required_site_features(
        "<!doctype html><html><head></head><body><main>Health</main></body></html>",
        context,
    )

    assert 'data-ai-theme-name="healthcare"' in themed
    assert 'data-ai-default-highlight="#0f766e"' in themed
    assert 'data-ai-default-background="#f0fdfa"' in themed
    assert 'var storageKey = "ai-site-factory-theme-v3-" + themeName' in themed

    upgraded = main.ensure_required_site_features(themed)
    assert 'data-ai-theme-name="healthcare"' in upgraded
    assert 'data-ai-default-highlight="#0f766e"' in upgraded
    assert 'data-ai-default-background="#f0fdfa"' in upgraded


def test_main_business_image_is_injected_without_replacing_a_logo():
    main_image_url = "https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=business&w=408&h=240"
    source = '<!doctype html><html><head></head><body><img class="logo" src="logo.svg" alt="Logo"><main>Content</main></body></html>'

    generated = main.ensure_generated_hero_and_working_links(
        source,
        {
            "businessName": "Green Garden Care",
            "industry": "Landscaping",
            "location": "Durban",
            "mainImageUrl": main_image_url,
        },
    )

    assert 'class="logo" src="logo.svg"' in generated
    assert 'src="https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=business&amp;w=408&amp;h=240"' in generated
    assert "data-ai-business-main-image-container" in generated


def test_refresh_deployed_business_media_updates_existing_html_and_redeploys(monkeypatch):
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "refresh-secret")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    approval_id = main.create_approval_record(
        pipeline_id="pipeline-refresh",
        canonical_key="canonical-refresh",
        lead_key="lead-refresh",
        business_name="Harbour Physiotherapy",
        site_html='<!doctype html><html><head></head><body><header class="hero"><img src="old.svg" alt="Generated banner"></header></body></html>',
        context={
            "businessName": "Harbour Physiotherapy",
            "industry": "Physiotherapy",
            "location": "Gqeberha",
            "contactChannel": "phone",
        },
        site_content={},
        template=dict(main.FREEFORM_SITE_SPEC),
        status="APPROVED",
        approval_id="approval-refresh",
    )
    row = main.get_approval_or_404(approval_id)
    contract = {
        "fieldIds": {
            "approvalId": "1",
            "canonicalLeadKey": "2",
            "businessName": "3",
            "liveUrl": "4",
            "deployRequested": "5",
            "industry": "6",
            "location": "7",
            "address": "8",
        }
    }
    ticket = {
        "id": 6001,
        "status": "open",
        "tags": ["asf_deployed", "asf_channel_phone"],
        "custom_fields": [
            {"id": 1, "value": approval_id},
            {"id": 2, "value": "canonical-refresh"},
            {"id": 3, "value": "Harbour Physiotherapy"},
            {"id": 4, "value": "https://harbour.netlify.app"},
            {"id": 5, "value": True},
            {"id": 6, "value": "Physiotherapy"},
            {"id": 7, "value": "Gqeberha"},
            {"id": 8, "value": "1 Harbour Road"},
        ],
    }
    image_url = "https://images.example.com/harbour.jpg"
    captured = {}

    class ImageResponse(FakeResponse):
        def close(self):
            return None

    monkeypatch.setattr(
        main.requests,
        "get",
        lambda *args, **kwargs: ImageResponse(200, headers={"Content-Type": "image/jpeg"}),
    )
    monkeypatch.setattr(main, "zendesk_api_request", lambda *args, **kwargs: {"ticket": ticket})
    monkeypatch.setattr(main, "require_zendesk_ticket_contract", lambda channel: contract)
    monkeypatch.setattr(main, "resolve_webhook_approval", lambda request: row)
    monkeypatch.setattr(
        main,
        "get_remote_github_repo",
        lambda *args, **kwargs: {
            "default_branch": "main",
            "html_url": "https://github.com/owner/site-refresh",
            "private": False,
        },
    )
    monkeypatch.setattr(
        main,
        "get_github_text_file",
        lambda *args, **kwargs: {
            "content": row["html"],
            "sha": "old-index-sha",
            "htmlUrl": "https://github.com/owner/site-refresh/blob/main/index.html",
        },
    )

    def update_file(owner, repo, branch, path, content, message, headers):
        captured["html"] = content
        return {
            "action": "UPDATED",
            "contentSha": "new-index-sha",
            "commitSha": "new-commit-sha",
            "htmlUrl": "https://github.com/owner/site-refresh/blob/main/index.html",
        }

    monkeypatch.setattr(main, "put_github_file", update_file)
    monkeypatch.setattr(
        main,
        "deploy_direct_netlify_fallback_for_lead",
        lambda **kwargs: {
            "state": "ready",
            "url": "https://harbour.netlify.app",
            "deploymentHistoryId": "history-refresh",
        },
    )
    monkeypatch.setattr(main, "zendesk_custom_fields", lambda values: [])
    monkeypatch.setattr(main, "update_zendesk_ticket_comment", lambda *args, **kwargs: ticket)
    monkeypatch.setattr(main, "update_zendesk_ticket_tags", lambda *args, **kwargs: ticket["tags"])

    response = client.post(
        "/api/deployments/refresh-business-media",
        headers={"x-ai-site-factory-secret": "refresh-secret"},
        json={
            "zendeskTicketId": 6001,
            "mainImageUrl": image_url,
            "githubRepoFullName": "owner/site-refresh",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REFRESHED"
    assert response.json()["businessProfile"]["industry"] == "Physiotherapy"
    assert image_url in captured["html"]
    assert 'data-ai-default-highlight="#0f766e"' in captured["html"]
    assert "Move with confidence. Recover with care." in captured["html"]
    assert "Physiotherapy Care" in captured["html"]
    assert "Services designed for local customers" not in captured["html"]
    with main.get_pipeline_db() as db:
        updated = db.execute("SELECT * FROM approval_records WHERE id = ?", (approval_id,)).fetchone()
    assert updated["status"] == "APPROVED"
    assert image_url in updated["html"]


@pytest.fixture(autouse=True)
def isolated_pipeline_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("ENABLE_LEGACY_PIPELINE_RUN", "true")
    main.init_pipeline_db()
    main.LEADS_DB.clear()
    main.CONTENT_DB.clear()
    main.PREVIEW_DB.clear()
    main.DISCOVERY_DB.clear()
    main.PIPELINE_DB.clear()
    main.LOG_BUFFER.clear()
    main.RUNTIME_INTEGRATION_OVERRIDES.clear()
    yield


def test_empty_deployment_restores_configuration_without_demo_activity():
    first_restore = main.restore_pipeline_seed_if_empty()

    assert first_restore["restored"] is True
    assert first_restore["restoredCounts"]["campaigns"] == 0
    assert first_restore["restoredCounts"]["campaign_email_leads"] == 0
    assert first_restore["restoredCounts"]["campaign_call_leads"] == 0
    assert first_restore["restoredCounts"]["campaign_deployments"] == 0
    assert first_restore["restoredCounts"]["zendesk_field_settings"] == 20
    assert first_restore["restoredCounts"]["zendesk_provisioned_resources"] == 30
    restored_fields = main.get_zendesk_field_settings()
    assert restored_fields["canonicalLeadKey"] == "28939446436508"
    assert restored_fields["pipelineId"] == "28939465524508"
    assert restored_fields["approvalId"] == "28939458660636"
    assert restored_fields["deployRequested"] == "28939474364188"
    assert restored_fields["liveUrl"] == "28939458703388"

    response = client.get("/api/campaigns?limit=200")
    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["campaigns"] == 0
    assert payload["totals"]["leads"] == 0
    assert payload["campaigns"] == []

    second_restore = main.restore_pipeline_seed_if_empty()
    assert second_restore["restored"] is False
    assert second_restore["reason"] == "seed_empty"


def test_empty_deployment_bootstraps_only_zendesk_config(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "cxsupporthub")

    restored = main.restore_zendesk_config_seed_if_empty()

    assert restored["restored"] is True
    assert restored["restoredCounts"]["zendesk_field_settings"] == 20
    assert restored["restoredCounts"]["zendesk_provisioned_resources"] == 23
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM zendesk_ticket_links").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM zendesk_provisioned_resources WHERE resource_key LIKE 'trigger:%'").fetchone()[0] == 0

    replay = main.restore_zendesk_config_seed_if_empty()
    assert replay["restored"] is False
    assert replay["reason"] == "zendesk_config_not_empty"


def test_startup_pipeline_seed_restore_is_opt_in(monkeypatch):
    monkeypatch.delenv("ENABLE_PIPELINE_SEED_RESTORE", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(
        main,
        "restore_pipeline_seed_if_empty",
        lambda: (_ for _ in ()).throw(AssertionError("disabled bootstrap must not read the seed")),
    )

    assert main.bootstrap_pipeline_seed_on_startup() == {
        "restored": False,
        "reason": "pipeline_seed_restore_disabled",
    }


def test_startup_pipeline_seed_restore_runs_when_enabled(monkeypatch):
    expected = {"restored": True, "reason": "seed_restored"}
    monkeypatch.setenv("ENABLE_PIPELINE_SEED_RESTORE", "true")
    monkeypatch.setattr(main, "restore_pipeline_seed_if_empty", lambda: expected)

    assert main.bootstrap_pipeline_seed_on_startup() == expected


def test_startup_pipeline_seed_restore_defaults_on_for_render(monkeypatch):
    expected = {"restored": True, "reason": "seed_restored"}
    monkeypatch.delenv("ENABLE_PIPELINE_SEED_RESTORE", raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(main, "restore_pipeline_seed_if_empty", lambda: expected)

    assert main.bootstrap_pipeline_seed_on_startup() == expected


def test_operational_data_cleanup_retains_zendesk_configuration():
    restored = main.restore_pipeline_seed_if_empty()
    assert restored["restored"] is True
    timestamp = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO campaigns (
                id, name, industry, query, location, requested_count, discovered_count,
                channel_filter, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "campaign-cleanup-test",
                "Disposable demo campaign",
                "Testing",
                "test leads",
                "Durban",
                1,
                0,
                "email",
                "INTAKE_READY",
                timestamp,
                timestamp,
            ),
        )

    result = main.clear_pipeline_operational_data("test-demo-reset")

    assert result["cleared"] is True
    assert result["deletedCounts"]["campaigns"] == 1
    with main.get_pipeline_db() as db:
        for table in main.PIPELINE_OPERATIONAL_TABLES:
            assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM zendesk_field_settings").fetchone()[0] == 20
        assert db.execute("SELECT COUNT(*) FROM zendesk_provisioned_resources").fetchone()[0] == 30
        marker = db.execute(
            "SELECT value FROM app_metadata WHERE key = 'pipeline_data_cleanup_version'"
        ).fetchone()
        assert marker["value"] == "test-demo-reset"


def test_render_operational_cleanup_runs_once(monkeypatch):
    main.restore_pipeline_seed_if_empty()
    monkeypatch.setenv("RENDER", "true")

    first = main.cleanup_pipeline_operational_data_on_startup()
    second = main.cleanup_pipeline_operational_data_on_startup()

    assert first["cleared"] is True
    assert first["cleanupVersion"] == main.PIPELINE_DATA_CLEANUP_VERSION
    assert second == {
        "cleared": False,
        "reason": "cleanup_already_applied",
        "cleanupVersion": main.PIPELINE_DATA_CLEANUP_VERSION,
    }


def test_startup_zendesk_bootstrap_defaults_on(monkeypatch):
    calls = []
    expected = {"restored": True, "reason": "zendesk_config_restored"}
    monkeypatch.delenv("ENABLE_ZENDESK_CONFIG_BOOTSTRAP", raising=False)
    monkeypatch.setattr(
        main,
        "restore_zendesk_config_seed_if_empty",
        lambda: calls.append("zendesk") or expected,
    )

    assert main.bootstrap_zendesk_config_on_startup() == expected
    assert calls == ["zendesk"]


def test_startup_zendesk_bootstrap_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ZENDESK_CONFIG_BOOTSTRAP", "false")
    monkeypatch.setattr(
        main,
        "restore_zendesk_config_seed_if_empty",
        lambda: (_ for _ in ()).throw(AssertionError("disabled bootstrap must not read the seed")),
    )

    assert main.bootstrap_zendesk_config_on_startup() == {
        "restored": False,
        "reason": "zendesk_config_bootstrap_disabled",
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def apify_item(index, category="Restaurant", province="Gauteng", website=None, email=None, phone=None, address=None):
    item = {
        "title": f"Lead Business {index}",
        "website": None,
        "phone": f"+27 11 000 {index:04d}",
        "categoryName": category,
        "address": address if address is not None else f"{index} Market Street, {province}, South Africa",
        "countryCode": "ZA",
        "rating": 4.5,
        "reviewsCount": 20 + index,
        "googleMapsUrl": f"https://maps.example.com/{index}",
    }
    if website is not None:
        item["website"] = website
    return item


def lead_payload():
    return main.DiscoveredLead(
        leadKey="lead-1",
        businessName="Alpha Plumbing",
        email="alpha@example.com",
        category="Plumbing",
        location="Durban",
    ).model_dump()


def test_legacy_pipeline_is_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("ENABLE_LEGACY_PIPELINE_RUN", raising=False)

    response = client.post("/api/pipeline/run", json={"leads": [lead_payload()]})

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "LEGACY_PIPELINE_DISABLED"


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


def test_discover_leads_accepts_custom_industry_and_search_intent(monkeypatch):
    calls = []

    def fake_apify(query, limit, location="South Africa"):
        calls.append((query, location, limit))
        return [apify_item(12, category="Solar Energy", province="Western Cape")]

    monkeypatch.setattr(main, "run_apify_google_maps", fake_apify)
    response = client.post(
        "/api/leads/discover",
        json={
            "presetId": "custom",
            "industry": "Solar Energy",
            "location": "Cape Town, South Africa",
            "query": "commercial solar installers",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["preset"]["id"] == "custom"
    assert payload["preset"]["industry"] == "Solar Energy"
    assert payload["leads"][0]["category"] == "Solar Energy"
    assert calls == [("commercial solar installers in Cape Town, South Africa", "Cape Town, South Africa", 5)]


def test_custom_discovery_requires_industry_and_search_intent():
    response = client.post(
        "/api/leads/discover",
        json={"presetId": "custom", "industry": "", "location": "South Africa", "query": ""},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Industry is required for a custom campaign."


def test_discover_force_refresh_can_reuse_discovered_leads(monkeypatch):
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location="South Africa": [apify_item(1, province="Gauteng")])

    first = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1})
    second = client.post("/api/leads/discover", json={"presetId": "restaurants", "location": "South Africa", "limit": 1, "forceRefresh": True})

    assert first.status_code == 200
    assert len(first.json()["leads"]) == 1
    assert second.status_code == 200
    assert second.json()["cached"] is False
    assert len(second.json()["leads"]) == 1
    assert second.json()["leads"][0]["businessName"] == "Lead Business 1"
    assert second.json()["duplicatesSkipped"] == 0


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


def test_failed_github_netlify_deployment_uses_direct_fallback(monkeypatch):
    stub_generation(monkeypatch)

    def failed_deploy(*args, **kwargs):
        raise RuntimeError("netlify build failed")

    def fallback_deploy(**kwargs):
        return {
            "deployAction": "DIRECT_FALLBACK_CREATED",
            "siteCreated": True,
            "siteReused": False,
            "siteId": f"site-{kwargs['canonical_key']}",
            "siteName": "ai-site-alpha-fallback",
            "buildId": None,
            "deployId": f"deploy-{kwargs['canonical_key']}",
            "state": "ready",
            "url": "https://alpha-fallback.netlify.app",
            "deploymentHistoryId": None,
            "publishMode": "direct-netlify-fallback",
            "deploymentMode": "Direct Netlify fallback",
            "githubExport": kwargs["github_export"],
        }

    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", failed_deploy)
    monkeypatch.setattr(main, "deploy_direct_netlify_fallback_for_lead", fallback_deploy)
    monkeypatch.setattr(main, "generate_outreach_with_groq", lambda context, site_url: {"subject": "Preview", "body": f"See {site_url}"})
    monkeypatch.setattr(main, "create_zendesk_outreach_ticket", lambda *args, **kwargs: {"syncStatus": "TICKET_CREATED", "ticketId": 123})
    pipeline = client.post("/api/pipeline/run", json={"templateId": "default-service", "leads": [lead_payload()]}).json()
    approval_id = pipeline["results"][0]["pendingApprovalId"]

    response = client.post(f"/api/approvals/{approval_id}/approve", json={"approvedBy": "Ops"})
    detail = client.get(f"/api/approvals/{approval_id}?includeHtml=true").json()

    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert response.json()["deployment"]["publishMode"] == "direct-netlify-fallback"
    assert detail["status"] == "APPROVED"
    assert detail["errors"][0]["retryable"] is True


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
    outreach = {"subject": "Website preview", "body": "See https://alpha.netlify.app"}
    result = main.create_zendesk_outreach_ticket(context, deployment, outreach, "pipeline-1")

    assert result["ticketId"] == 999
    assert result["contactType"] == "phone"
    assert result["liveLink"] == "https://alpha.netlify.app"
    assert "ai_site_no_website" in result["tags"]
    assert len(captured_posts) == 2


def test_discovery_accepts_large_limit_and_returns_contactable_mixed_leads(monkeypatch):
    items = [
        {**apify_item(1, "Plumbing"), "email": "one@example.com"},
        {**apify_item(2, "Plumbing"), "phone": None, "email": "two@example.com"},
        {**apify_item(3, "Plumbing"), "email": None},
        {**apify_item(4, "Plumbing"), "website": "https://has-site.example"},
        {**apify_item(5, "Plumbing"), "phone": None, "email": None},
    ]
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location: items)

    result = client.post(
        "/api/leads/discover",
        json={"presetId": "plumbers", "location": "Durban, South Africa", "limit": 25, "forceRefresh": True},
    )

    assert result.status_code == 200
    payload = result.json()
    assert payload["requestedCount"] == 25
    assert payload["rawFetched"] == 5
    assert payload["eligibleReturned"] == 3
    assert payload["websitesSkipped"] == 1
    assert payload["noContactSkipped"] == 1
    assert payload["emailLeads"] == 2
    assert payload["phoneLeads"] == 2
    assert payload["emailAndPhoneLeads"] == 1
    assert [lead["businessName"] for lead in payload["leads"]] == [
        "Lead Business 1",
        "Lead Business 2",
        "Lead Business 3",
    ]


def test_discovered_but_not_generated_lead_can_reappear(monkeypatch):
    item = {**apify_item(11, "Plumbing"), "email": "repeat@example.com"}
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location: [item])

    first = client.post(
        "/api/leads/discover",
        json={"presetId": "plumbers", "location": "Durban, South Africa", "limit": 5, "forceRefresh": True},
    )
    second = client.post(
        "/api/leads/discover",
        json={"presetId": "plumbers", "location": "Durban, South Africa", "limit": 5, "forceRefresh": True},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["leads"][0]["businessName"] == "Lead Business 11"
    assert second.json()["leads"][0]["businessName"] == "Lead Business 11"


def test_already_generated_lead_is_skipped_in_discovery(monkeypatch):
    item = {**apify_item(12, "Plumbing"), "email": "generated@example.com"}
    lead = main.normalize_apify_items([item], "Plumbing", "Durban, South Africa", 1)[0]
    canonical_key = main.canonical_lead_key_for_lead(lead)
    main.create_approval_record(
        pipeline_id="pipeline-generated",
        canonical_key=canonical_key,
        lead_key=lead.leadKey,
        business_name=lead.businessName,
        site_html="<!doctype html><html><body>Generated</body></html>",
        context=lead.model_dump(),
        site_content={},
        template={},
        status="PENDING",
    )
    monkeypatch.setattr(main, "run_apify_google_maps", lambda query, limit, location: [item])

    result = client.post(
        "/api/leads/discover",
        json={"presetId": "plumbers", "location": "Durban, South Africa", "limit": 5, "forceRefresh": True},
    )

    assert result.status_code == 200
    payload = result.json()
    assert payload["leads"] == []
    assert payload["generatedDuplicatesSkipped"] == 1


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


def test_github_export_retries_transient_repo_and_file_failures(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    monkeypatch.setenv("GITHUB_API_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("GITHUB_API_RETRY_BASE_SECONDS", "0")
    repo_posts = []
    file_puts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        repo_posts.append(json["name"])
        if len(repo_posts) == 1:
            return FakeResponse(503, {"message": "Service unavailable"})
        return FakeResponse(
            201,
            {
                "id": 91,
                "name": json["name"],
                "full_name": f"owner/{json['name']}",
                "html_url": f"https://github.com/owner/{json['name']}",
                "default_branch": "main",
                "private": False,
            },
        )

    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse(404, {})

    def fake_put(url, headers=None, json=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        file_puts.append(path)
        if path == "README.md" and file_puts.count("README.md") == 1:
            return FakeResponse(503, {"message": "Service unavailable"})
        return FakeResponse(
            201,
            {
                "content": {"sha": f"{path}-sha", "html_url": f"https://github.test/{path}"},
                "commit": {"sha": f"{path}-commit"},
            },
        )

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)

    result = main.export_site_to_github(
        "canonical-transient",
        "Transient Plumbing",
        "<html>transient</html>",
        "pipeline-transient",
        "approval-transient",
    )

    assert result["commitSha"] == "index.html-commit"
    assert len(repo_posts) == 2
    assert len(set(repo_posts)) == 1
    assert file_puts == ["README.md", "README.md", "index.html"]


def test_github_export_recovers_partial_remote_repo_without_duplicate(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_OWNER", "owner")
    canonical_key = "canonical-partial"
    suffix = main.hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()[:8]
    repo_name = f"ai-site-partial-plumbing-20260720002015-{suffix}"
    posts = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url == "https://api.github.com/user/repos":
            return FakeResponse(
                200,
                [
                    {
                        "id": 92,
                        "name": repo_name,
                        "full_name": f"owner/{repo_name}",
                        "html_url": f"https://github.com/owner/{repo_name}",
                        "default_branch": "main",
                        "private": False,
                        "created_at": "2026-07-20T00:20:21Z",
                        "owner": {"login": "owner"},
                    }
                ],
            )
        if "/contents/" in url:
            return FakeResponse(404, {})
        raise AssertionError(url)

    def fake_put(url, headers=None, json=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        return FakeResponse(
            201,
            {
                "content": {"sha": f"{path}-sha", "html_url": f"https://github.test/{path}"},
                "commit": {"sha": f"{path}-commit"},
            },
        )

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: posts.append(args) or (_ for _ in ()).throw(AssertionError("duplicate repo")))

    result = main.export_site_to_github(
        canonical_key,
        "Partial Plumbing",
        "<html>partial</html>",
        "pipeline-partial",
        "approval-partial",
    )

    assert result["exportAction"] == "RECOVERED"
    assert result["repository"] == f"owner/{repo_name}"
    assert result["commitSha"] == "index.html-commit"
    assert posts == []


def test_netlify_github_installation_resolution_is_scoped_and_overridable(monkeypatch):
    monkeypatch.delenv("NETLIFY_GITHUB_INSTALLATION_ID", raising=False)
    assert main.resolve_netlify_github_installation("BusiM5/generated-site") == (99999032, "owner_default")
    assert main.resolve_netlify_github_installation("another-owner/generated-site") == (None, "unmatched_owner")

    monkeypatch.setenv("NETLIFY_GITHUB_INSTALLATION_ID", "123456")
    assert main.resolve_netlify_github_installation("another-owner/generated-site") == (123456, "environment")


@pytest.mark.parametrize("invalid_id", ["0", "-1", "not-a-number"])
def test_netlify_github_installation_rejects_non_positive_or_invalid_values(monkeypatch, invalid_id):
    monkeypatch.setenv("NETLIFY_GITHUB_INSTALLATION_ID", invalid_id)

    with pytest.raises(RuntimeError, match="positive numeric"):
        main.resolve_netlify_github_installation("BusiM5/generated-site")


def test_netlify_auto_build_selection_requires_target_sha_and_fresh_build_id():
    builds = [
        {"id": "old-target", "sha": "target-sha", "done": True},
        {"id": "new-wrong", "sha": "different-sha", "done": True},
    ]
    assert main.netlify_current_build_from_list(builds, "target-sha", "old-target") is None

    expected = {"id": "new-target", "sha": "target-sha", "done": True}
    assert main.netlify_current_build_from_list([expected, *builds], "target-sha", "old-target") == expected


def test_site_registry_publish_mode_migration_backfills_latest_deployment_mode():
    timestamp = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute("DROP TABLE site_registry")
        db.execute(
            """
            CREATE TABLE site_registry (
                canonical_lead_key TEXT PRIMARY KEY,
                site_id TEXT NOT NULL,
                site_name TEXT,
                url TEXT,
                admin_url TEXT,
                github_repo_full_name TEXT,
                github_repo_url TEXT,
                last_commit_sha TEXT,
                last_build_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_deploy_id TEXT,
                last_deploy_state TEXT,
                deployment_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, created_at, updated_at, last_deploy_state
            ) VALUES ('legacy-fallback', 'legacy-site', ?, ?, 'ready')
            """,
            (timestamp, timestamp),
        )
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, deployed_at, publish_mode
            ) VALUES ('legacy-history', 'legacy-fallback', ?, 'direct-netlify-fallback')
            """,
            (timestamp,),
        )

    main.init_pipeline_db()

    with main.get_pipeline_db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(site_registry)").fetchall()}
        registry = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = 'legacy-fallback'"
        ).fetchone()
    assert "publish_mode" in columns
    assert registry["publish_mode"] == "direct-netlify-fallback"


def test_netlify_git_deploy_uses_build_api_not_zip_upload(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    monkeypatch.delenv("NETLIFY_GITHUB_INSTALLATION_ID", raising=False)
    calls = []
    github_export = fake_github_export("canonical-one", "Alpha Plumbing", "<html>one</html>", "pipeline-1", "approval-1")
    github_export["repository"] = "BusiM5/ai-site-canonical-one"
    github_export["repoUrl"] = "https://github.com/BusiM5/ai-site-canonical-one"

    def fake_post(url, headers=None, json=None, params=None, data=None, timeout=None):
        calls.append(("post", url, json, data))
        assert data is None
        if url.endswith("/api/v1/sites"):
            assert json["repo"]["repo_path"] == github_export["repository"]
            assert json["repo"]["installation_id"] == 99999032
            assert "repo_url" not in json["repo"]
            assert "build_settings" not in json
            return FakeResponse(
                201,
                {
                    "id": "site-1",
                    "name": "ai-site-alpha",
                    "ssl_url": "https://alpha.netlify.app",
                    "admin_url": "https://app.netlify.com/sites/alpha",
                    "build_settings": json["repo"],
                },
            )
        if url.endswith("/builds"):
            return FakeResponse(201, {"id": "build-1", "deploy_id": "deploy-1", "done": True})
        raise AssertionError(url)

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(("get", url, None, None))
        if url.endswith("/sites/site-1/builds"):
            assert params == {"per_page": 10}
            return FakeResponse(
                200,
                [
                    {
                        "id": "build-1",
                        "deploy_id": "deploy-1",
                        "sha": github_export["commitSha"],
                        "done": True,
                    }
                ],
            )
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
    assert not any(call[1].endswith("/builds") and call[0] == "post" for call in calls)
    assert not any("/deploys" in call[1] and call[0] == "post" for call in calls)
    history = client.get("/api/deployments/history").json()["deployments"]
    assert history[0]["github_repo_url"] == github_export["repoUrl"]
    assert history[0]["build_id"] == "build-1"
    installation_log = next(
        entry for entry in main.LOG_BUFFER if entry["event"] == "provider.netlify.github_installation_selected"
    )
    assert installation_log["details"]["source"] == "owner_default"


def test_netlify_git_deploy_reads_back_linkage_and_posts_build_only_when_auto_build_missing(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    monkeypatch.setenv("NETLIFY_GITHUB_INSTALLATION_ID", "123456")
    monkeypatch.setenv("NETLIFY_AUTOBUILD_WAIT_SECONDS", "0")
    github_export = fake_github_export("canonical-manual", "Manual Build", "<html>manual</html>")
    posts = []
    expected_site_name = "ai-site-manual-build-canonica"
    expected_repo = {
        "provider": "github",
        "repo_path": github_export["repository"],
        "repo_branch": "main",
        "dir": "",
        "cmd": "",
        "public_repo": True,
        "installation_id": 123456,
    }

    def fake_post(url, headers=None, json=None, params=None, data=None, timeout=None):
        posts.append(url)
        if url.endswith("/api/v1/sites"):
            assert json == {
                "name": expected_site_name,
                "processing_settings": {"html": {"pretty_urls": True}},
                "repo": expected_repo,
            }
            return FakeResponse(201, {"id": "site-manual", "name": json["name"]})
        if url.endswith("/sites/site-manual/builds"):
            return FakeResponse(
                201,
                {"id": "build-manual", "deploy_id": "deploy-manual", "sha": github_export["commitSha"], "done": True},
            )
        raise AssertionError(url)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/sites/site-manual"):
            return FakeResponse(
                200,
                {
                    "id": "site-manual",
                    "name": expected_site_name,
                    "ssl_url": "https://manual.netlify.app",
                    "build_settings": expected_repo,
                },
            )
        if url.endswith("/sites/site-manual/builds"):
            assert params == {"per_page": 10}
            return FakeResponse(200, [])
        if url.endswith("/deploys/deploy-manual"):
            return FakeResponse(200, {"id": "deploy-manual", "state": "ready", "ssl_url": "https://manual.netlify.app"})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)

    deployment = main.deploy_github_repo_to_netlify_for_lead(
        "canonical-manual",
        "Manual Build",
        "pipeline-manual",
        "approval-manual",
        "Ops",
        github_export,
    )

    assert deployment["state"] == "ready"
    assert deployment["buildId"] == "build-manual"
    assert sum(url.endswith("/builds") for url in posts) == 1


def test_netlify_git_deploy_rejects_unverified_repository_linkage(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    monkeypatch.setenv("NETLIFY_GITHUB_INSTALLATION_ID", "123456")
    github_export = fake_github_export("canonical-unlinked", "Unlinked", "<html>unlinked</html>")
    wrong_link = {
        "provider": "github",
        "repo_path": "another-owner/wrong-repo",
        "repo_branch": "main",
        "installation_id": 999,
    }
    build_posts = []

    def fake_post(url, headers=None, json=None, params=None, data=None, timeout=None):
        if url.endswith("/api/v1/sites"):
            return FakeResponse(201, {"id": "site-unlinked", "name": json["name"], "build_settings": wrong_link})
        build_posts.append(url)
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(
        main.requests,
        "get",
        lambda url, **kwargs: FakeResponse(200, {"id": "site-unlinked", "build_settings": wrong_link}),
    )

    with pytest.raises(RuntimeError, match="did not confirm"):
        main.deploy_github_repo_to_netlify_for_lead(
            "canonical-unlinked",
            "Unlinked",
            "pipeline-unlinked",
            "approval-unlinked",
            "Ops",
            github_export,
        )

    assert build_posts == []
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM site_registry").fetchone()["count"] == 0


def test_netlify_git_deploy_relinks_fallback_registry_instead_of_reusing_it(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    monkeypatch.setenv("NETLIFY_GITHUB_INSTALLATION_ID", "123456")
    github_export = fake_github_export("canonical-fallback", "Fallback Site", "<html>fallback</html>")
    timestamp = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, admin_url,
                github_repo_full_name, github_repo_url, last_commit_sha, last_build_id,
                created_at, updated_at, last_deploy_id, last_deploy_state,
                deployment_count, publish_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "canonical-fallback", "site-fallback", "fallback-site",
                "https://fallback.netlify.app", "https://app.netlify.com/sites/fallback-site",
                github_export["repository"], github_export["repoUrl"], github_export["commitSha"],
                "old-build", timestamp, timestamp, "old-deploy", "ready", 1,
                "direct-netlify-fallback",
            ),
        )
    patches = []

    def fake_patch(url, headers=None, json=None, timeout=None):
        patches.append(json)
        assert "build_settings" not in json
        return FakeResponse(
            200,
            {
                "id": "site-fallback",
                "name": "fallback-site",
                "ssl_url": "https://fallback.netlify.app",
                "admin_url": "https://app.netlify.com/sites/fallback-site",
                "build_settings": json["repo"],
            },
        )

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/sites/site-fallback/builds"):
            return FakeResponse(
                200,
                [{"id": "new-build", "deploy_id": "new-deploy", "sha": github_export["commitSha"], "done": True}],
            )
        if url.endswith("/deploys/new-deploy"):
            return FakeResponse(200, {"id": "new-deploy", "state": "ready", "ssl_url": "https://fallback.netlify.app"})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "patch", fake_patch)
    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("automatic linked build must be reused")),
    )

    deployment = main.deploy_github_repo_to_netlify_for_lead(
        "canonical-fallback",
        "Fallback Site",
        "pipeline-fallback",
        "approval-fallback",
        "Ops",
        github_export,
    )

    assert deployment["deployAction"] == "REDEPLOYED"
    assert len(patches) == 1
    with main.get_pipeline_db() as db:
        registry = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = 'canonical-fallback'"
        ).fetchone()
        assert registry["publish_mode"] == "github-netlify"
        assert registry["last_build_id"] == "new-build"


def test_direct_netlify_recovery_enables_disabled_duplicate_site_before_upload(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(("get", url))
        if url.endswith("/api/v1/sites"):
            return FakeResponse(
                200,
                [
                    {
                        "id": "site-disabled",
                        "name": "ai-site-disabled-business-canonica",
                        "state": "disabled",
                        "ssl_url": "https://ai-site-disabled-business-canonica.netlify.app",
                    }
                ],
            )
        raise AssertionError(url)

    def fake_put(url, headers=None, params=None, timeout=None):
        calls.append(("put", url))
        assert url.endswith("/sites/site-disabled/enable")
        return FakeResponse(204, {})

    def fake_post(url, headers=None, json=None, data=None, timeout=None):
        calls.append(("post", url))
        if url.endswith("/api/v1/sites"):
            return FakeResponse(422, {"errors": {"subdomain": ["must be unique"]}})
        if url.endswith("/sites/site-disabled/deploys"):
            return FakeResponse(
                201,
                {
                    "id": "deploy-recovered",
                    "state": "ready",
                    "ssl_url": "https://ai-site-disabled-business-canonica.netlify.app",
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)
    monkeypatch.setattr(main.requests, "post", fake_post)

    result = main.deploy_direct_netlify_fallback_for_lead(
        canonical_key="canonical-disabled",
        business_name="Disabled Business",
        site_html="<!doctype html><html><body>Recovered</body></html>",
        pipeline_id="pipeline-disabled",
        approval_id="approval-disabled",
        approved_by="Ops",
        github_export=fake_github_export(
            "canonical-disabled", "Disabled Business", "<html>Recovered</html>"
        ),
        git_error=RuntimeError("Git-linked site name already exists."),
    )

    assert result["state"] == "ready"
    assert result["siteId"] == "site-disabled"
    assert result["deployAction"] == "DIRECT_FALLBACK_REDEPLOYED"
    assert calls.index(("put", "https://api.netlify.com/api/v1/sites/site-disabled/enable")) < calls.index(
        ("post", "https://api.netlify.com/api/v1/sites/site-disabled/deploys")
    )


def test_direct_netlify_redeploy_uses_remote_disabled_state_after_registry_restore(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-secret")
    timestamp = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, created_at, updated_at,
                last_deploy_state, deployment_count, publish_mode
            ) VALUES (?, ?, ?, ?, ?, ?, 'uploaded', 1, 'direct-netlify-fallback')
            """,
            (
                "canonical-restored",
                "site-restored",
                "restored-site",
                "https://restored-site.netlify.app",
                timestamp,
                timestamp,
            ),
        )
    enabled = []

    monkeypatch.setattr(
        main.requests,
        "get",
        lambda url, **kwargs: FakeResponse(
            200,
            {
                "id": "site-restored",
                "name": "restored-site",
                "state": "disabled",
                "ssl_url": "https://restored-site.netlify.app",
            },
        ),
    )
    monkeypatch.setattr(
        main.requests,
        "put",
        lambda url, **kwargs: enabled.append(url) or FakeResponse(204, {}),
    )
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda url, **kwargs: FakeResponse(
            201,
            {
                "id": "deploy-restored",
                "state": "ready",
                "ssl_url": "https://restored-site.netlify.app",
            },
        ),
    )

    result = main.deploy_direct_netlify_fallback_for_lead(
        canonical_key="canonical-restored",
        business_name="Restored Business",
        site_html="<!doctype html><html><body>Restored</body></html>",
        pipeline_id="pipeline-restored",
        approval_id="approval-restored",
        approved_by="Ops",
        github_export=fake_github_export(
            "canonical-restored", "Restored Business", "<html>Restored</html>"
        ),
        git_error=RuntimeError("Git linkage unavailable."),
    )

    assert result["state"] == "ready"
    assert enabled == ["https://api.netlify.com/api/v1/sites/site-restored/enable"]


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
            "SELECT site_id, url, publish_mode FROM site_registry WHERE canonical_lead_key = ?",
            ("canonical-one",),
        ).fetchone()
    assert registry["site_id"] == "new-account-site"
    assert registry["url"] == "https://new-account.netlify.app"
    assert registry["publish_mode"] == "direct-netlify"


def test_cancel_netlify_site_disables_live_site_and_clears_registry_url(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-token")
    timestamp = main.now_iso()
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, created_at, updated_at,
                last_deploy_state, deployment_count, publish_mode
            ) VALUES (?, ?, ?, ?, ?, ?, 'ready', 1, 'github-netlify')
            """,
            (
                "canonical-cancel",
                "site-cancel",
                "cancel-site",
                "https://cancel-site.netlify.app",
                timestamp,
                timestamp,
            ),
        )
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, site_id, site_name, url, state,
                deployed_at, approval_status, raw_json
            ) VALUES (?, ?, ?, ?, ?, 'ready', ?, 'APPROVED', ?)
            """,
            (
                "history-cancel",
                "canonical-cancel",
                "site-cancel",
                "cancel-site",
                "https://cancel-site.netlify.app",
                timestamp,
                '{"state":"ready","url":"https://cancel-site.netlify.app"}',
            ),
        )

    calls = []

    def fake_put(url, headers=None, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(204, {})

    monkeypatch.setattr(main.requests, "put", fake_put)

    result = main.cancel_netlify_site_for_lead("canonical-cancel")

    assert result["status"] == "CANCELLED"
    assert result["previousUrl"] == "https://cancel-site.netlify.app"
    assert calls == [
        (
            "https://api.netlify.com/api/v1/sites/site-cancel/disable",
            {"reason": "AI Site Factory deployment checkbox was unchecked in Zendesk."},
        )
    ]
    with main.get_pipeline_db() as db:
        site = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = 'canonical-cancel'"
        ).fetchone()
        history = db.execute("SELECT * FROM deployment_history WHERE id = 'history-cancel'").fetchone()
    assert site["url"] is None
    assert site["last_deploy_state"] == "cancelled"
    assert history["state"] == "cancelled"
    assert history["approval_status"] == "CANCELLED"


def test_cancel_netlify_site_recovers_registry_from_zendesk_live_url(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "netlify-token")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url == "https://api.netlify.com/api/v1/sites"
        assert params == {"per_page": 100, "page": 1}
        return FakeResponse(
            200,
            [
                {
                    "id": "site-recovered",
                    "name": "recovered-site",
                    "ssl_url": "https://recovered-site.netlify.app",
                    "url": "http://recovered-site.netlify.app",
                    "admin_url": "https://app.netlify.com/sites/recovered-site",
                }
            ],
        )

    def fake_put(url, headers=None, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(204, {})

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)

    result = main.cancel_netlify_site_for_lead(
        "canonical-recovered",
        "https://recovered-site.netlify.app",
    )

    assert result["status"] == "CANCELLED"
    assert result["recoveredFromLiveUrl"] is True
    assert result["siteId"] == "site-recovered"
    assert calls[0][0].endswith("/sites/site-recovered/disable")
    with main.get_pipeline_db() as db:
        site = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = 'canonical-recovered'"
        ).fetchone()
    assert site["site_id"] == "site-recovered"
    assert site["url"] is None
    assert site["last_deploy_state"] == "cancelled"


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

    client.put("/api/settings/zendesk-fields", json={"fields": {"approvalId": "1003"}})
    merged = client.get("/api/settings/zendesk-fields").json()["fields"]
    assert merged["canonicalLeadKey"] == "1001"
    assert merged["pipelineId"] == "1002"
    assert merged["approvalId"] == "1003"


def zendesk_setup_fake(monkeypatch):
    state = {
        "ticket_fields": [
            {"id": 1, "type": "subject", "title": "Subject"},
            {"id": 2, "type": "description", "title": "Description"},
            {"id": 3, "type": "status", "title": "Status"},
            {"id": 4, "type": "priority", "title": "Priority"},
            {"id": 5, "type": "tickettype", "title": "Type"},
            {"id": 6, "type": "group", "title": "Group"},
            {"id": 7, "type": "assignee", "title": "Assignee"},
        ],
        "ticket_forms": [],
        "views": [],
        "brands": [{"id": 88, "name": "Default brand", "subdomain": "supporthub", "default": True}],
        "webhooks": [],
        "triggers": [],
    }
    calls = []
    counters = {"ticket_field": 1000, "ticket_form": 2000, "view": 3000, "webhook": 4000, "trigger": 5000}

    def collection_for_url(url):
        if "/ticket_fields" in url:
            return "ticket_fields", "ticket_field"
        if "/ticket_forms" in url:
            return "ticket_forms", "ticket_form"
        if "/views" in url:
            return "views", "view"
        if "/brands" in url:
            return "brands", "brand"
        if "/webhooks" in url:
            return "webhooks", "webhook"
        if "/triggers" in url:
            return "triggers", "trigger"
        raise AssertionError(url)

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        collection, _ = collection_for_url(url)
        return FakeResponse(200, {collection: state[collection], "next_page": None})

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        collection, root = collection_for_url(url)
        calls.append(("post", collection))
        counters[root] += 1
        item = {"id": counters[root], **json[root]}
        state[collection].append(item)
        return FakeResponse(201, {root: item})

    def fake_put(url, json=None, auth=None, headers=None, timeout=None):
        collection, root = collection_for_url(url)
        calls.append(("put", collection))
        resource_id = url.rstrip("/").split("/")[-1].removesuffix(".json")
        existing = next(item for item in state[collection] if str(item["id"]) == resource_id)
        existing.update(json[root])
        return FakeResponse(200, {root: existing})

    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "put", fake_put)
    return state, calls


def test_zendesk_setup_inspection_reports_matches_missing_resources_and_brands(monkeypatch):
    state, _ = zendesk_setup_fake(monkeypatch)
    definition = main.ZENDESK_FIELD_BLUEPRINT[0]
    state["ticket_fields"].append(
        {
            "id": 999,
            "title": definition["title"],
            "type": definition["type"],
            "agent_description": f"[AI Site Factory key={definition['key']}] managed",
        }
    )

    response = client.post("/api/settings/zendesk-setup/inspect", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["inspected"] is True
    assert payload["fields"][0]["status"] == "ready"
    assert payload["fields"][0]["resourceId"] == 999
    assert sum(field["status"] == "missing" for field in payload["fields"]) == len(main.ZENDESK_FIELD_BLUEPRINT) - 1
    assert payload["forms"][0]["status"] == "missing"
    assert payload["brands"] == [{"id": "88", "name": "Default brand", "subdomain": "supporthub", "default": True}]


def test_zendesk_setup_page_loads_existing_brands_without_manual_inspection(monkeypatch):
    zendesk_setup_fake(monkeypatch)

    response = client.get("/api/settings/zendesk-setup")

    assert response.status_code == 200
    payload = response.json()
    assert payload["brandsLoaded"] is True
    assert payload["brands"] == [
        {
            "id": "88",
            "name": "Default brand",
            "subdomain": "supporthub",
            "default": True,
            "active": True,
        }
    ]


def test_zendesk_setup_adopts_compatible_string_fields_but_rejects_unsafe_types(monkeypatch):
    state, _ = zendesk_setup_fake(monkeypatch)
    status_definition = next(item for item in main.ZENDESK_FIELD_BLUEPRINT if item["key"] == "leadStatus")
    deploy_definition = next(item for item in main.ZENDESK_FIELD_BLUEPRINT if item["key"] == "deployRequested")
    state["ticket_fields"].extend(
        [
            {"id": 991, "title": status_definition["title"], "type": "text"},
            {"id": 992, "title": deploy_definition["title"], "type": "text"},
        ]
    )

    payload = client.post("/api/settings/zendesk-setup/inspect", json={}).json()
    status_field = next(item for item in payload["fields"] if item["key"] == "leadStatus")
    deploy_field = next(item for item in payload["fields"] if item["key"] == "deployRequested")

    assert status_field["status"] == "ready"
    assert status_field["adaptedType"] is True
    assert deploy_field["status"] == "conflict"


def test_zendesk_field_inspection_does_not_trust_stale_saved_id_for_unrelated_field(monkeypatch):
    state, _ = zendesk_setup_fake(monkeypatch)
    definition = next(item for item in main.ZENDESK_FIELD_BLUEPRINT if item["key"] == "phoneCallStatus")
    stale_id = 28777280257052
    managed_id = 28779999999999
    state["ticket_fields"].extend(
        [
            {
                "id": stale_id,
                "title": "Legacy call disposition",
                "type": "tagger",
                "agent_description": "Legacy options: no_answer, call_back, interested",
            },
            {
                "id": managed_id,
                "title": definition["title"],
                "type": "tagger",
                "agent_description": "[AI Site Factory key=phoneCallStatus] Managed call workflow.",
                "custom_field_options": definition["custom_field_options"],
            },
        ]
    )
    main.save_zendesk_field_settings({"phoneCallStatus": str(stale_id)})
    main.save_zendesk_provisioned_resource(
        "field:phoneCallStatus", "ticket_field", str(stale_id), "Legacy call disposition", {"type": "tagger"}
    )

    payload = client.post("/api/settings/zendesk-setup/inspect", json={}).json()
    inspected = next(item for item in payload["fields"] if item["key"] == "phoneCallStatus")

    assert inspected["resourceId"] == managed_id
    assert inspected["matchSource"] == "marker"
    assert inspected["status"] == "ready"


def test_zendesk_setup_provisions_fields_before_forms_and_reruns_idempotently(monkeypatch):
    state, calls = zendesk_setup_fake(monkeypatch)
    request = {
        "confirm": True,
        "brandId": "88",
        "createViews": True,
        "createAutomation": False,
        "emailFormName": "ASF Email Leads",
        "callFormName": "ASF Call Leads",
    }

    first = client.post("/api/settings/zendesk-setup/provision", json=request)
    second = client.post("/api/settings/zendesk-setup/provision", json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(state["ticket_fields"]) == 7 + len(main.ZENDESK_FIELD_BLUEPRINT)
    assert len(state["ticket_forms"]) == 2
    assert len(state["views"]) == 3
    first_form_call = next(index for index, call in enumerate(calls) if call == ("post", "ticket_forms"))
    assert all(call == ("post", "ticket_fields") for call in calls[:first_form_call])
    assert first_form_call == len(main.ZENDESK_FIELD_BLUEPRINT)
    assert all(form["restricted_brand_ids"] == [88] for form in state["ticket_forms"])
    assert all(form["in_all_brands"] is False for form in state["ticket_forms"])
    assert all(form["ticket_field_ids"] for form in state["ticket_forms"])
    assert second.json()["fields"][0]["status"] == "ready"
    assert main.get_zendesk_field_settings()["approvalId"]


def test_zendesk_setup_provisions_inactive_live_only_cancellation_triggers(monkeypatch):
    state, _ = zendesk_setup_fake(monkeypatch)
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "webhook-secret")

    response = client.post(
        "/api/settings/zendesk-setup/provision",
        json={
            "confirm": True,
            "brandId": "88",
            "createViews": False,
            "createAutomation": True,
            "webhookUrl": "https://backend.example/api/zendesk/webhook",
        },
    )

    assert response.status_code == 200, response.text
    cancellation_triggers = [
        trigger for trigger in state["triggers"]
        if trigger["title"] in {
            "AI Site Factory - Cancel email deployment",
            "AI Site Factory - Cancel call deployment",
        }
    ]
    assert len(cancellation_triggers) == 2
    for trigger in cancellation_triggers:
        assert trigger["active"] is False
        conditions = trigger["conditions"]["all"]
        assert any(condition["field"].startswith("custom_fields_") and condition["value"] == "false" for condition in conditions)
        assert {condition["value"] for condition in conditions if condition["field"] == "current_tags"} >= {
            "asf_deployed"
        }
        webhook_action = next(action for action in trigger["actions"] if action["field"] == "notification_webhook")
        assert '"action":"cancel_deployment"' in webhook_action["value"][1]


def test_live_zendesk_field_reconciliation_replaces_stale_restored_ids(monkeypatch):
    current_ids = configure_managed_zendesk_contract()
    stale_ids = {key: str(8000 + index) for index, key in enumerate(main.ZENDESK_FIELD_KEYS)}
    main.save_zendesk_field_settings(stale_ids)
    live_fields = [
        {
            "id": current_ids[definition["key"]],
            "title": definition["title"],
            "type": definition["type"],
            "agent_description": f"[AI Site Factory key={definition['key']}] {definition['description']}",
        }
        for definition in main.ZENDESK_FIELD_BLUEPRINT
    ]
    canonical_definition = next(
        definition for definition in main.ZENDESK_FIELD_BLUEPRINT if definition["key"] == "canonicalLeadKey"
    )
    live_fields.insert(
        0,
        {
            "id": stale_ids["canonicalLeadKey"],
            "title": canonical_definition["title"],
            "type": canonical_definition["type"],
            "agent_description": "Legacy duplicate without the managed marker.",
        },
    )
    monkeypatch.setattr(main, "zendesk_connection_snapshot", lambda: {"connected": True})
    monkeypatch.setattr(
        main,
        "zendesk_list_all",
        lambda path, key: live_fields if path == "/ticket_fields.json" and key == "ticket_fields" else [],
    )

    result = main.reconcile_zendesk_field_settings_from_live_instance()

    assert result["reconciled"] is True
    assert result["resolvedCount"] == len(main.ZENDESK_FIELD_KEYS)
    assert set(result["changed"]) == set(main.ZENDESK_FIELD_KEYS)
    assert main.get_zendesk_field_settings() == current_ids


def test_managed_zendesk_fields_encode_dropdown_values_and_route_channel_forms():
    main.save_zendesk_field_settings({"contactChannel": "1001", "leadStatus": "1002", "phoneCallStatus": "1003"})
    main.save_zendesk_provisioned_resource("field:contactChannel", "ticket_field", "1001", "Channel", {"type": "tagger"})
    main.save_zendesk_provisioned_resource("field:leadStatus", "ticket_field", "1002", "Status", {"type": "tagger"})
    main.save_zendesk_provisioned_resource("field:phoneCallStatus", "ticket_field", "1003", "Call status", {"type": "tagger"})
    main.save_zendesk_provisioned_resource("form:email", "ticket_form", "2001", "Email", {"channel": "email"})
    main.save_zendesk_provisioned_resource("form:phone", "ticket_form", "2002", "Phone", {"channel": "phone"})
    main.save_zendesk_provisioned_resource("configuration", "configuration", "supporthub", "Setup", {"brandId": "88"})

    fields = main.zendesk_custom_fields({"contactChannel": "phone", "leadStatus": "DEPLOYED", "phoneCallStatus": "No answer"})

    assert fields == [
        {"id": 1001, "value": "asf_cf_channel_phone"},
        {"id": 1002, "value": "asf_cf_status_deployed"},
        {"id": 1003, "value": "asf_cf_call_no_answer"},
    ]
    assert main.zendesk_ticket_routing_fields("email") == {"ticket_form_id": 2001, "brand_id": 88}
    assert main.zendesk_ticket_routing_fields("phone") == {"ticket_form_id": 2002, "brand_id": 88}


def configure_managed_zendesk_contract():
    field_ids = {}
    for index, definition in enumerate(main.ZENDESK_FIELD_BLUEPRINT, start=1):
        field_id = str(3000 + index)
        field_ids[definition["key"]] = field_id
        main.save_zendesk_provisioned_resource(
            f"field:{definition['key']}",
            "ticket_field",
            field_id,
            definition["title"],
            {"type": definition["type"], "forms": definition["forms"]},
        )
    main.save_zendesk_field_settings(field_ids)
    main.save_zendesk_provisioned_resource(
        "form:email", "ticket_form", "5001", "Email", {"channel": "email", "brandId": "88"}
    )
    main.save_zendesk_provisioned_resource(
        "form:phone", "ticket_form", "5002", "Phone", {"channel": "phone", "brandId": "88"}
    )
    main.save_zendesk_provisioned_resource(
        "configuration", "configuration", "supporthub", "Setup", {"brandId": "88"}
    )
    return field_ids


def managed_orphan_zendesk_ticket(
    field_ids,
    *,
    ticket_id=5806,
    brand_id=88,
    channel="email",
    campaign_id="campaign-orphan",
    campaign_name="Recovered Durban campaign",
    canonical_key="canonical-orphan",
    pipeline_id=None,
    approval_id="8852628b-orphan-approval",
    business_name="Recovered Plumbing",
    contact_email=None,
    contact_phone=None,
    live_url=None,
):
    pipeline_id = pipeline_id or campaign_id
    contact_email = contact_email or ("recovered@example.com" if channel == "email" else None)
    contact_phone = contact_phone or ("+27 31 555 0101" if channel == "phone" else None)
    values = {
        "campaignId": campaign_id,
        "campaignName": campaign_name,
        "canonicalLeadKey": canonical_key,
        "pipelineId": pipeline_id,
        "approvalId": approval_id,
        "batchId": "batch-orphan",
        "businessName": business_name,
        "contactName": "Recovered Owner",
        "contactEmail": contact_email,
        "contactPhone": contact_phone,
        "industry": "Plumbing",
        "location": "Durban",
        "address": "1 Recovery Road",
        "contactChannel": channel,
        "leadStatus": "AWAITING_DEPLOYMENT",
        "deployRequested": True,
        "emailSendRequested": False,
        "phoneCallStatus": "NEW" if channel == "phone" else None,
        "liveUrl": live_url,
        "sourceUrl": "https://maps.example.com/recovered",
    }
    form_id = 5001 if channel == "email" else 5002
    form_tag = "asf_form_email_lead" if channel == "email" else "asf_form_call_lead"
    return {
        "id": ticket_id,
        "external_id": f"asf:{campaign_id}:{canonical_key}:{channel}:intake",
        "brand_id": brand_id,
        "ticket_form_id": form_id,
        "status": "open",
        "tags": [
            "ai_site_factory", "asf_managed", "asf_intake", f"asf_channel_{channel}",
            form_tag, "asf_deploy_requested", "asf_source_apify_google_maps",
        ],
        "custom_fields": main.zendesk_custom_fields(values),
        "_field_ids": field_ids,
        "_approval_id": approval_id,
        "_canonical_key": canonical_key,
        "_campaign_id": campaign_id,
    }


def test_zendesk_intake_creates_email_and_phone_tickets_once(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    ticket_posts = []
    ticket_headers = []
    requester_posts = []
    remote_tickets = {}

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        if url.endswith("/organizations.json"):
            return FakeResponse(201, {"organization": {"id": 42}})
        if url.endswith("/users/create_or_update.json"):
            user = json["user"]
            requester_posts.append(user)
            assert user["name"] == "Alpha Plumbing"
            assert user["email"] == "alpha@example.com"
            assert user["phone"] == "+27 31 000 0000"
            assert user["role"] == "end-user"
            assert user["skip_verify_email"] is True
            assert user["external_id"].startswith("asf-requester-")
            return FakeResponse(200, {"user": {**user, "id": 77}})
        if url.endswith("/tickets.json"):
            ticket_posts.append(json["ticket"])
            ticket_headers.append(headers)
            ticket = {**json["ticket"], "id": 900 + len(ticket_posts), "status": "new"}
            remote_tickets[ticket["id"]] = ticket
            return FakeResponse(
                201,
                {"ticket": ticket},
            )
        raise AssertionError(url)

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        if url.endswith("/tickets.json"):
            return FakeResponse(200, {"tickets": []})
        if "/tickets/" in url:
            ticket_id = int(url.rsplit("/", 1)[-1].removesuffix(".json"))
            return FakeResponse(200, {"ticket": remote_tickets[ticket_id]})
        if url.endswith("/users/search.json"):
            return FakeResponse(200, {"users": []})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)
    context = {
        "campaignId": "campaign-intake",
        "campaignName": "Durban Plumbers - July",
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
    assert len(requester_posts) == 1
    assert {(ticket["brand_id"], ticket["ticket_form_id"]) for ticket in ticket_posts} == {(88, 5001), (88, 5002)}
    assert {ticket["external_id"] for ticket in ticket_posts} == {
        "asf:campaign-intake:canonical-intake:email:intake",
        "asf:campaign-intake:canonical-intake:phone:intake",
    }
    assert {next(tag for tag in ticket["tags"] if tag in {"ai_site_email_lead", "ai_site_phone_lead"}) for ticket in ticket_posts} == {"ai_site_email_lead", "ai_site_phone_lead"}
    assert all("asf_managed" in ticket["tags"] for ticket in ticket_posts)
    assert all(ticket["requester_id"] == 77 for ticket in ticket_posts)
    assert len({headers["Idempotency-Key"] for headers in ticket_headers}) == 2
    assert all(headers["Idempotency-Key"].startswith("asf-") for headers in ticket_headers)
    for ticket in ticket_posts:
        values = {str(item["id"]): item["value"] for item in ticket["custom_fields"]}
        assert values[field_ids["campaignId"]] == "campaign-intake"
        assert values[field_ids["campaignName"]] == "Durban Plumbers - July"
        assert values[field_ids["approvalId"]] == "approval-intake"
        assert values[field_ids["businessName"]] == "Alpha Plumbing"
        assert values[field_ids["industry"]] == "Plumbing"
        assert values[field_ids["location"]] == "Durban"


def test_zendesk_intake_creates_phone_only_business_requester_without_fake_email(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    configure_managed_zendesk_contract()
    ticket_posts = []

    def fake_post(url, json=None, auth=None, headers=None, timeout=None):
        if url.endswith("/organizations.json"):
            return FakeResponse(201, {"organization": {"id": 42}})
        if url.endswith("/users/create_or_update.json"):
            user = json["user"]
            assert user["name"] == "Phone Only Solar"
            assert user["phone"] == "+27 31 000 0000"
            assert "email" not in user
            return FakeResponse(200, {"user": {**user, "id": 78}})
        if url.endswith("/tickets.json"):
            ticket_posts.append(json["ticket"])
            return FakeResponse(201, {"ticket": {**json["ticket"], "id": 902, "status": "new"}})
        raise AssertionError(url)

    def fake_get(url, **kwargs):
        if url.endswith("/tickets.json"):
            return FakeResponse(200, {"tickets": []})
        raise AssertionError(url)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", fake_get)

    result = main.create_zendesk_intake_tickets(
        "approval-phone-requester",
        {
            "campaignId": "campaign-phone-requester",
            "campaignName": "Durban Solar - July",
            "canonicalLeadKey": "canonical-phone-requester",
            "businessName": "Phone Only Solar",
            "industry": "Solar",
            "location": "Durban",
            "phone": "+27 31 000 0000",
        },
        "pipeline-phone-requester",
        requested_channels=["phone"],
    )

    assert len(result) == 1
    assert len(ticket_posts) == 1
    assert ticket_posts[0]["requester_id"] == 78
    assert result[0]["payload"]["userId"] == 78
    assert result[0]["payload"]["requesterName"] == "Phone Only Solar"


def test_zendesk_intake_rejects_missing_contract_before_http(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    monkeypatch.setattr(
        main.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not run")),
    )
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not run")),
    )

    with pytest.raises(main.HTTPException) as error:
        main.create_zendesk_intake_tickets(
            "approval-intake",
            {
                "campaignId": "campaign-intake",
                "campaignName": "Durban Plumbers - July",
                "canonicalLeadKey": "canonical-intake",
                "businessName": "Alpha Plumbing",
                "email": "alpha@example.com",
            },
            "pipeline-intake",
            requested_channels=["email"],
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ZENDESK_TICKET_CONTRACT_INVALID"


def test_zendesk_intake_adopts_and_repairs_ticket_by_external_id_without_posting(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    external_id = "asf:campaign-intake:canonical-intake:email:intake"
    repairs = []

    def fake_get(url, params=None, **kwargs):
        assert url.endswith("/tickets.json")
        assert params["external_id"] == external_id
        return FakeResponse(
            200,
            {
                "tickets": [
                        {
                            "id": 8123,
                            "external_id": external_id,
                            "brand_id": 999,
                            "ticket_form_id": 9999,
                            "status": "new",
                            "tags": [],
                            "custom_fields": [],
                        }
                ]
            },
        )

    def fake_put(url, json=None, **kwargs):
        assert url.endswith("/tickets/8123.json")
        repairs.append(json["ticket"])
        return FakeResponse(200, {"ticket": {**json["ticket"], "id": 8123, "status": "new"}})

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main.requests, "put", fake_put)
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ticket must be adopted")),
    )

    result = main.create_zendesk_intake_tickets(
        "approval-intake",
        {
            "campaignId": "campaign-intake",
            "campaignName": "Durban Plumbers - July",
            "canonicalLeadKey": "canonical-intake",
            "businessName": "Alpha Plumbing",
            "industry": "Plumbing",
            "location": "Durban",
            "email": "alpha@example.com",
        },
        "pipeline-intake",
        requested_channels=["email"],
    )

    assert result[0]["ticketId"] == 8123
    assert result[0]["externalId"] == external_id
    assert result[0]["payload"]["adoptedByExternalId"] is True
    assert result[0]["payload"]["repaired"] is True
    assert repairs[0]["brand_id"] == 88
    assert repairs[0]["ticket_form_id"] == 5001
    values = {str(item["id"]): item["value"] for item in repairs[0]["custom_fields"]}
    assert values[field_ids["campaignName"]] == "Durban Plumbers - July"
    assert values[field_ids["businessName"]] == "Alpha Plumbing"
    assert "asf_managed" in repairs[0]["tags"]


def test_zendesk_field_comparison_normalizes_checkbox_values():
    assert main.zendesk_ticket_field_value_matches(False, "false") is True
    assert main.zendesk_ticket_field_value_matches(True, "true") is True
    assert main.zendesk_ticket_field_value_matches(False, True) is False
    assert main.zendesk_ticket_field_value_matches("asf_cf_channel_email", "asf_cf_channel_email") is True


def test_live_zendesk_contract_rejects_form_on_wrong_brand(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()

    def fake_api(method, path, **kwargs):
        if path == "/brands/88.json":
            return {"brand": {"id": 88, "active": True}}
        if path == "/ticket_forms/5001.json":
            return {
                "ticket_form": {
                    "id": 5001,
                    "active": True,
                    "in_all_brands": False,
                    "restricted_brand_ids": [99],
                    "ticket_field_ids": [int(value) for value in field_ids.values()],
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(main, "zendesk_api_request", fake_api)

    with pytest.raises(main.HTTPException) as error:
        main.require_zendesk_ticket_contract("email", verify_live=True)

    assert error.value.status_code == 409
    assert "not available on the selected brand" in " ".join(error.value.detail["problems"])


def test_webhook_recovers_missing_deferred_approval_from_exact_managed_ticket(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    ticket = managed_orphan_zendesk_ticket(field_ids)
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda method, path, **kwargs: {"ticket": ticket} if path == "/tickets/5806.json" else (_ for _ in ()).throw(AssertionError(path)),
    )

    request = main.ZendeskWebhookRequest(
        action="deploy_site",
        approvalId=ticket["_approval_id"],
        canonicalLeadKey=ticket["_canonical_key"],
        zendeskTicketId=5806,
        channel="email",
    )
    recovered = main.resolve_webhook_approval(request)
    replay = main.resolve_webhook_approval(request)

    assert recovered["id"] == ticket["_approval_id"]
    assert replay["id"] == ticket["_approval_id"]
    context = main.safe_json_loads(recovered["context_json"], {})
    assert context["intakeDeferred"] is True
    assert context["recoveredFromZendeskTicketId"] == 5806
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"] == 1
        assert db.execute("SELECT COUNT(*) AS count FROM approval_records").fetchone()["count"] == 1
        lead = db.execute("SELECT * FROM campaign_email_leads").fetchone()
        assert lead["ticket_id"] == 5806
        assert lead["approval_id"] == ticket["_approval_id"]
    link = main.get_zendesk_ticket_link(ticket["_approval_id"], "email", "intake", 5806)
    assert link["externalId"] == ticket["external_id"]
    assert link["payload"]["recoveredFromManagedTicket"] is True


def test_cancellation_webhook_recovers_missing_state_and_live_url(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    ticket = managed_orphan_zendesk_ticket(
        field_ids,
        live_url="https://recovered-site.netlify.app",
    )
    ticket["tags"] = [
        tag for tag in ticket["tags"] if tag != "asf_deploy_requested"
    ] + ["asf_deployed", "asf_stage_live"]
    for field in ticket["custom_fields"]:
        if str(field["id"]) == field_ids["deployRequested"]:
            field["value"] = False
    main.save_zendesk_field_settings(
        {
            "canonicalLeadKey": "88001",
            "pipelineId": "88002",
            "approvalId": "88003",
            "deployRequested": "88004",
            "liveUrl": "88005",
        }
    )
    live_fields = [
        {
            "id": field_ids[definition["key"]],
            "title": definition["title"],
            "type": definition["type"],
            "agent_description": f"[AI Site Factory key={definition['key']}] {definition['description']}",
        }
        for definition in main.ZENDESK_FIELD_BLUEPRINT
    ]
    monkeypatch.setattr(main, "zendesk_connection_snapshot", lambda: {"connected": True})
    monkeypatch.setattr(
        main,
        "zendesk_list_all",
        lambda path, key: live_fields if path == "/ticket_fields.json" and key == "ticket_fields" else [],
    )
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda method, path, **kwargs: {"ticket": ticket}
        if path == "/tickets/5806.json"
        else (_ for _ in ()).throw(AssertionError(path)),
    )

    recovered = main.resolve_webhook_approval(
        main.ZendeskWebhookRequest(
            action="cancel_deployment",
            approvalId=ticket["_approval_id"],
            canonicalLeadKey=ticket["_canonical_key"],
            zendeskTicketId=5806,
            channel="email",
        )
    )

    assert recovered["id"] == ticket["_approval_id"]
    assert main.get_zendesk_field_settings()["canonicalLeadKey"] == field_ids["canonicalLeadKey"]
    assert main.get_zendesk_field_settings()["approvalId"] == field_ids["approvalId"]
    link = main.get_zendesk_ticket_link(ticket["_approval_id"], "email", "intake", 5806)
    assert link["payload"]["liveUrl"] == "https://recovered-site.netlify.app"


def test_orphan_recovery_keeps_same_campaign_and_pipeline_counts_for_multiple_tickets(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    first = managed_orphan_zendesk_ticket(field_ids)
    second = managed_orphan_zendesk_ticket(
        field_ids,
        ticket_id=5807,
        canonical_key="canonical-orphan-two",
        approval_id="8852628b-orphan-approval-two",
        business_name="Recovered Electrical",
        contact_email="electrical@example.com",
    )
    tickets = {first["id"]: first, second["id"]: second}
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda method, path, **kwargs: {"ticket": tickets[int(path.split("/")[2].split(".")[0])]},
    )

    for ticket in (first, second):
        recovered = main.recover_managed_zendesk_webhook_approval(
            main.ZendeskWebhookRequest(
                action="deploy_site",
                approvalId=ticket["_approval_id"],
                canonicalLeadKey=ticket["_canonical_key"],
                zendeskTicketId=ticket["id"],
                channel="email",
            )
        )
        assert recovered["id"] == ticket["_approval_id"]

    with main.get_pipeline_db() as db:
        campaign = db.execute("SELECT * FROM campaigns WHERE id = ?", (first["_campaign_id"],)).fetchone()
        pipeline = db.execute("SELECT * FROM pipeline_runs WHERE pipeline_id = ?", (first["_campaign_id"],)).fetchone()
        assert campaign["discovered_count"] == 2
        assert campaign["requested_count"] == 2
        assert pipeline["lead_count"] == 2
        assert db.execute("SELECT COUNT(*) AS count FROM approval_records").fetchone()["count"] == 2
        assert db.execute("SELECT COUNT(*) AS count FROM campaign_email_leads").fetchone()["count"] == 2
        assert db.execute("SELECT COUNT(*) AS count FROM zendesk_ticket_links").fetchone()["count"] == 2


def test_orphan_recovery_rolls_back_every_insert_when_ticket_link_identity_conflicts(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    ticket = managed_orphan_zendesk_ticket(field_ids)
    main.save_zendesk_ticket_link(
        approval_id="existing-approval",
        canonical_key="existing-canonical",
        pipeline_id="existing-pipeline",
        channel="email",
        stage="intake",
        ticket_id=9999,
        ticket_url="https://supporthub.zendesk.com/agent/tickets/9999",
        status="open",
        tags=["existing"],
        payload={"existing": True},
        external_id=ticket["external_id"],
    )
    monkeypatch.setattr(main, "zendesk_api_request", lambda *args, **kwargs: {"ticket": ticket})

    with pytest.raises(main.HTTPException) as error:
        main.recover_managed_zendesk_webhook_approval(
            main.ZendeskWebhookRequest(
                action="deploy_site",
                approvalId=ticket["_approval_id"],
                canonicalLeadKey=ticket["_canonical_key"],
                zendeskTicketId=ticket["id"],
                channel="email",
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ZENDESK_ORPHAN_RECOVERY_CONFLICT"
    assert error.value.detail["entity"] == "ticket_link"
    with main.get_pipeline_db() as db:
        for table in (
            "lead_registry",
            "campaigns",
            "pipeline_runs",
            "approval_records",
            "campaign_deployments",
            "campaign_email_leads",
        ):
            assert db.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"] == 0
        links = db.execute("SELECT * FROM zendesk_ticket_links").fetchall()
        assert len(links) == 1
        assert links[0]["approval_id"] == "existing-approval"


def test_webhook_refuses_orphan_recovery_on_wrong_brand(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    ticket = managed_orphan_zendesk_ticket(field_ids, brand_id=999)
    monkeypatch.setattr(main, "zendesk_api_request", lambda *args, **kwargs: {"ticket": ticket})

    with pytest.raises(main.HTTPException) as error:
        main.resolve_webhook_approval(
            main.ZendeskWebhookRequest(
                action="deploy_site",
                approvalId=ticket["_approval_id"],
                canonicalLeadKey=ticket["_canonical_key"],
                zendeskTicketId=5806,
                channel="email",
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ZENDESK_ORPHAN_TICKET_CONTRACT_INVALID"
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM approval_records").fetchone()["count"] == 0


def test_webhook_rejects_mismatched_approval_and_ticket_link():
    first = create_pending_approval_for_webhook()
    second = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(
        second, "canonical-webhook", "pipeline-webhook", "email", "intake", 777,
        "https://zendesk.test/777", "new", ["asf_managed"], {},
    )

    with pytest.raises(main.HTTPException) as error:
        main.resolve_webhook_approval(
            main.ZendeskWebhookRequest(
                action="deploy_site", approvalId=first, zendeskTicketId=777, channel="email"
            )
        )

    assert error.value.status_code == 409
    assert "identities do not match" in error.value.detail


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

    def fake_tag_update(ticket_id, *, add=None, remove=None):
        captured["tagTicketId"] = ticket_id
        captured["addedTags"] = list(add or [])
        captured["removedTags"] = list(remove or [])
        return ["admin_owned", "asf_deploy_email_fired", *(add or [])]

    monkeypatch.setattr(main, "update_zendesk_ticket_tags", fake_tag_update)

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
    assert "tags" not in captured["payload"]["ticket"]
    assert captured["tagTicketId"] == 654
    assert "asf_email_sent" in captured["addedTags"]
    link = main.get_zendesk_ticket_link(approval_id, "email", "intake", 654)
    assert "admin_owned" in link["tags"]
    assert "asf_deploy_email_fired" in link["tags"]


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


def test_zendesk_tag_mutation_preserves_remote_fired_and_admin_tags(monkeypatch):
    calls = []

    def fake_api(method, path, *, payload=None, params=None):
        calls.append((method, path, payload))
        if method == "delete":
            return {"tags": ["admin_owned", "asf_deploy_phone_fired"]}
        if method == "put":
            return {
                "tags": ["admin_owned", "asf_deploy_phone_fired", *(payload or {}).get("tags", [])]
            }
        raise AssertionError((method, path))

    monkeypatch.setattr(main, "zendesk_api_request", fake_api)

    tags = main.update_zendesk_ticket_tags(
        5808,
        remove=["asf_deploy_requested", "asf_stage_generating"],
        add=["asf_deployed", "asf_stage_live"],
    )

    assert calls == [
        (
            "delete",
            "/tickets/5808/tags.json",
            {"tags": ["asf_deploy_requested", "asf_stage_generating"]},
        ),
        (
            "put",
            "/tickets/5808/tags.json",
            {"tags": ["asf_deployed", "asf_stage_live"]},
        ),
    ]
    assert "admin_owned" in tags
    assert "asf_deploy_phone_fired" in tags


def test_apply_email_cancellation_macro_renders_and_persists_existing_macro(monkeypatch):
    macro_id = 28965347718172
    monkeypatch.setattr(
        main,
        "zendesk_list_all",
        lambda path, key: [
            {
                "id": macro_id,
                "title": main.EMAIL_CANCELLATION_MACRO_TITLE,
                "active": True,
                "actions": [
                    {"field": "comment_mode_is_public", "value": "true"},
                    {"field": "custom_fields_28939474364188", "value": "false"},
                    {"field": "remove_tags", "value": "asf_10_day_clock_started"},
                    {"field": "current_tags", "value": "asf_10_day_cancellation_sent"},
                    {"field": "status", "value": "solved"},
                ],
            }
        ],
    )
    captured = {}

    def fake_api(method, path, *, payload=None, params=None):
        if method == "get":
            assert path == f"/tickets/5871/macros/{macro_id}/apply.json"
            assert params == {"normalize_comment": "true"}
            return {
                "result": {
                    "ticket": {
                        "status": "solved",
                        "comment": {
                            "html_body": "<p>Your site has been cancelled and undeployed.</p>",
                            "public": True,
                        },
                    }
                }
            }
        if method == "put":
            captured["path"] = path
            captured["payload"] = payload
            return {"ticket": {"id": 5871, "status": "solved"}}
        raise AssertionError((method, path))

    monkeypatch.setattr(main, "zendesk_api_request", fake_api)
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_tags",
        lambda ticket_id, *, add=None, remove=None: captured.update(
            {"ticketId": ticket_id, "add": list(add or []), "remove": list(remove or [])}
        ) or ["asf_10_day_cancellation_sent"],
    )

    result = main.apply_zendesk_macro_to_ticket(5871, main.EMAIL_CANCELLATION_MACRO_TITLE)

    assert result["macroId"] == macro_id
    assert result["public"] is True
    assert result["status"] == "solved"
    assert "asf_10_day_cancellation_due" in captured["remove"]
    assert "asf_10_day_clock_started" in captured["remove"]
    assert captured["add"] == ["asf_10_day_cancellation_sent"]
    assert captured["path"] == "/tickets/5871.json"
    ticket = captured["payload"]["ticket"]
    assert ticket["comment"]["public"] is True
    assert ticket["comment"]["html_body"] == "<p>Your site has been cancelled and undeployed.</p>"
    assert ticket["custom_fields"] == [{"id": 28939474364188, "value": "false"}]


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


def test_zendesk_webhook_cancellation_resets_ticket_and_deploy_claim(monkeypatch):
    configure_managed_zendesk_contract()
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(
        approval_id,
        "canonical-webhook",
        "pipeline-webhook",
        "email",
        "intake",
        778,
        "https://zendesk.test/778",
        "open",
        ["asf_managed", "asf_deployed", "asf_stage_live", "asf_deploy_email_fired"],
        {"deployRequested": True, "liveUrl": "https://cancel.example"},
    )
    with main.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO deploy_webhook_claims (
                approval_id, state, attempt_count, completed_at, result_json, updated_at
            ) VALUES (?, 'COMPLETED', 1, ?, '{}', ?)
            """,
            (approval_id, main.now_iso(), main.now_iso()),
        )

    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda method, path, **kwargs: {"ticket": {"id": 778, "tags": ["asf_deployed"]}},
    )
    monkeypatch.setattr(
        main,
        "cancel_netlify_site_for_lead",
        lambda canonical_key, live_url=None: {
            "status": "CANCELLED",
            "siteId": "site-cancel",
            "previousUrl": "https://cancel.example",
        },
    )
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_tags",
        lambda ticket_id, remove=None, add=None: ["asf_managed", "asf_deployment_cancelled", *list(add or [])],
    )
    captured = {}

    def fake_comment(ticket_id, body, public, extra_ticket_fields=None):
        captured["ticketId"] = ticket_id
        captured["body"] = body
        captured["public"] = public
        captured["fields"] = extra_ticket_fields
        return {"id": ticket_id, "status": "open"}

    monkeypatch.setattr(main, "update_zendesk_ticket_comment", fake_comment)

    response = client.post(
        "/api/zendesk/webhook",
        json={
            "action": "cancel_deployment",
            "approvalId": approval_id,
            "canonicalLeadKey": "canonical-webhook",
            "channel": "email",
            "zendeskTicketId": 778,
        },
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["cancellation"]["status"] == "CANCELLED"
    assert response.json()["result"]["cancellation"]["scheduled"] is False
    assert captured["ticketId"] == 778
    assert captured["public"] is False
    assert "Netlify site has been disabled" in captured["body"]
    field_values = {str(field["id"]): field["value"] for field in captured["fields"]["custom_fields"]}
    settings = main.get_zendesk_field_settings()
    assert field_values[settings["deployRequested"]] is False
    assert field_values[settings["liveUrl"]] is None
    with main.get_pipeline_db() as db:
        approval = db.execute("SELECT * FROM approval_records WHERE id = ?", (approval_id,)).fetchone()
        claim = db.execute("SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)).fetchone()
    assert approval["status"] == "PENDING"
    assert claim is None
    link = main.get_zendesk_ticket_link(approval_id, "email", "intake", 778)
    assert link["payload"]["deployRequested"] is False
    assert link["payload"]["liveUrl"] is None


def test_cancellation_webhook_marks_due_tag_as_scheduled(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(
        approval_id,
        "canonical-webhook",
        "pipeline-webhook",
        "email",
        "intake",
        5871,
        "https://zendesk.test/5871",
        "open",
        ["asf_managed", "asf_deployed"],
        {"deployRequested": True, "liveUrl": "https://scheduled.example"},
    )
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda method, path, **kwargs: {
            "ticket": {
                "id": 5871,
                "tags": ["asf_deployed", "asf_10_day_cancellation_due"],
            }
        },
    )
    captured = {}

    def fake_cancel(row, ticket_id, channel, *, scheduled=False):
        captured.update(
            {
                "approvalId": row["id"],
                "ticketId": ticket_id,
                "channel": channel,
                "scheduled": scheduled,
            }
        )
        return {"status": "CANCELLED", "scheduled": scheduled}

    monkeypatch.setattr(main, "cancel_approval_deployment", fake_cancel)

    response = client.post(
        "/api/zendesk/webhook",
        json={
            "action": "cancel_deployment",
            "approvalId": approval_id,
            "canonicalLeadKey": "canonical-webhook",
            "channel": "email",
            "zendeskTicketId": 5871,
        },
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 200, response.text
    assert captured == {
        "approvalId": approval_id,
        "ticketId": 5871,
        "channel": "email",
        "scheduled": True,
    }


def test_webhook_carries_invoking_ticket_into_approval(monkeypatch):
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(
        approval_id, "canonical-webhook", "pipeline-webhook", "email", "intake", 777,
        "https://zendesk.test/777", "new", ["asf_managed", "asf_channel_email"], {},
    )
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(main, "reuse_existing_live_deployment", lambda *args, **kwargs: None)
    captured = {}

    def fake_approve(resolved_approval_id, action_request):
        captured["approvalId"] = resolved_approval_id
        captured["ticketId"] = action_request.zendeskTicketId
        main.save_zendesk_ticket_link(
            resolved_approval_id,
            "canonical-webhook",
            "pipeline-webhook",
            "email",
            "intake",
            777,
            "https://zendesk.test/777",
            "open",
            ["asf_managed", "asf_deployed", "asf_deploy_email_fired", "admin_owned"],
            {
                "deployRequested": True,
                "deployedAt": "2026-07-16T08:00:00+00:00",
                "liveUrl": "https://alpha.netlify.app",
            },
        )
        return main.ApprovalActionResponse(
            approvalId=resolved_approval_id,
            status="APPROVED",
            canonicalLeadKey="canonical-webhook",
            businessName="Webhook Plumbing",
            deployment={"url": "https://alpha.netlify.app", "state": "ready"},
        )

    monkeypatch.setattr(main, "approve_generated_site", fake_approve)

    response = client.post(
        "/api/zendesk/webhook",
        json={"action": "deploy_site", "approvalId": approval_id, "channel": "email", "zendeskTicketId": 777},
        headers={"x-ai-site-factory-secret": "secret"},
    )

    assert response.status_code == 200
    assert captured == {"approvalId": approval_id, "ticketId": 777}
    link = main.get_zendesk_ticket_link(approval_id, "email", "intake", 777)
    assert link["payload"]["liveUrl"] == "https://alpha.netlify.app"
    assert link["payload"]["deployedAt"] == "2026-07-16T08:00:00+00:00"
    assert link["payload"]["deployWebhookAt"]
    assert "asf_deployed" in link["tags"]
    assert "asf_deploy_email_fired" in link["tags"]
    assert "admin_owned" in link["tags"]


def test_deployment_update_writes_and_verifies_live_url_on_exact_route(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    field_ids = configure_managed_zendesk_contract()
    approval_id = main.create_approval_record(
        pipeline_id="campaign-live-url",
        canonical_key="canonical-live-url",
        lead_key="lead-live-url",
        business_name="Live URL Plumbing",
        site_html=None,
        context={
            "campaignId": "campaign-live-url",
            "campaignName": "Live URL Campaign",
            "canonicalLeadKey": "canonical-live-url",
            "businessName": "Live URL Plumbing",
            "industry": "Plumbing",
            "location": "Durban",
            "email": "live@example.com",
            "contactChannel": "email",
            "intakeDeferred": True,
        },
        site_content={"deferredGeneration": True},
        template=dict(main.FREEFORM_SITE_SPEC),
        status="PENDING",
    )
    main.save_zendesk_ticket_link(
        approval_id,
        "canonical-live-url",
        "campaign-live-url",
        "email",
        "intake",
        5806,
        "https://zendesk.test/5806",
        "open",
        ["asf_managed", "asf_deploy_email_fired", "admin_owned"],
        {},
    )
    row = main.get_approval_or_404(approval_id)
    captured = {"events": []}

    def fake_update(ticket_id, body, public, extra_ticket_fields=None):
        captured["events"].append("ticket_fields")
        captured["ticketId"] = ticket_id
        captured["body"] = body
        captured["fields"] = extra_ticket_fields
        return {"id": ticket_id, "status": "open", **extra_ticket_fields}

    monkeypatch.setattr(main, "update_zendesk_ticket_comment", fake_update)
    def fake_tags(ticket_id, *, add=None, remove=None):
        captured["events"].append("deployed_tags")
        captured["addedTags"] = list(add or [])
        return ["asf_managed", "asf_deploy_email_fired", "admin_owned", *(add or [])]

    monkeypatch.setattr(main, "update_zendesk_ticket_tags", fake_tags)

    result = main.update_existing_intake_ticket(
        row,
        {"url": "https://alpha.netlify.app", "githubRepoUrl": "https://github.com/acme/site"},
        {"subject": "Preview", "body": "Preview body"},
        ticket_id_override=5806,
    )

    assert result["liveLink"] == "https://alpha.netlify.app"
    assert captured["events"] == ["ticket_fields", "deployed_tags"]
    assert "asf_deploy_requested" in captured["addedTags"]
    assert "asf_deployed" in captured["addedTags"]
    assert captured["ticketId"] == 5806
    assert captured["fields"]["brand_id"] == 88
    assert captured["fields"]["ticket_form_id"] == 5001
    assert "tags" not in captured["fields"]
    values = {str(item["id"]): item["value"] for item in captured["fields"]["custom_fields"]}
    assert values[field_ids["liveUrl"]] == "https://alpha.netlify.app"
    link = main.get_zendesk_ticket_link(approval_id, "email", "intake", 5806)
    assert "asf_deploy_email_fired" in link["tags"]
    assert "admin_owned" in link["tags"]


def test_failed_deployment_lifecycle_unchecks_field_without_retriggering(monkeypatch):
    field_ids = configure_managed_zendesk_contract()
    approval_id = create_pending_approval_for_webhook()
    main.save_zendesk_ticket_link(
        approval_id,
        "canonical-webhook",
        "pipeline-webhook",
        "email",
        "intake",
        5883,
        "https://zendesk.test/5883",
        "open",
        ["asf_managed", "asf_deploy_email_fired", "asf_stage_generating"],
        {"deployRequested": True},
    )
    row = main.get_approval_or_404(approval_id)
    captured = {}

    def fake_tags(ticket_id, *, add=None, remove=None):
        captured["removed"] = list(remove or [])
        captured["added"] = list(add or [])
        return ["asf_managed", *(add or [])]

    def fake_comment(ticket_id, body, public, extra_ticket_fields=None):
        captured["fields"] = extra_ticket_fields
        return {"id": ticket_id, "status": "open"}

    monkeypatch.setattr(main, "update_zendesk_ticket_tags", fake_tags)
    monkeypatch.setattr(main, "update_zendesk_ticket_comment", fake_comment)

    result = main.update_zendesk_deployment_lifecycle(
        row,
        5883,
        "GENERATION_FAILED",
        "Temporary GitHub failure.",
    )

    values = {str(item["id"]): item["value"] for item in captured["fields"]["custom_fields"]}
    assert values[field_ids["deployRequested"]] is False
    assert "asf_deploy_email_fired" in captured["removed"]
    assert {"asf_generation_failed", "asf_stage_failed"}.issubset(captured["added"])
    assert result["payload"]["deployRequested"] is False


def test_deployment_update_refuses_unlinked_ticket_override(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    configure_managed_zendesk_contract()
    approval_id = create_pending_approval_for_webhook()
    row = main.get_approval_or_404(approval_id)
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_tags",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unlinked ticket was mutated")),
    )
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_comment",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unlinked ticket was mutated")),
    )

    with pytest.raises(main.HTTPException) as error:
        main.update_existing_intake_ticket(
            row,
            {"url": "https://alpha.netlify.app"},
            {"subject": "Preview", "body": "Preview body"},
            ticket_id_override=9999,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ZENDESK_TICKET_LINK_MISMATCH"
    assert error.value.detail["ticketId"] == 9999


def test_deployment_update_fails_when_zendesk_drops_live_url(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "supporthub")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    configure_managed_zendesk_contract()
    approval_id = main.create_approval_record(
        pipeline_id="campaign-live-url-fail",
        canonical_key="canonical-live-url-fail",
        lead_key="lead-live-url-fail",
        business_name="Dropped URL Plumbing",
        site_html=None,
        context={
            "campaignId": "campaign-live-url-fail",
            "campaignName": "Dropped URL Campaign",
            "canonicalLeadKey": "canonical-live-url-fail",
            "businessName": "Dropped URL Plumbing",
            "email": "drop@example.com",
            "contactChannel": "email",
            "intakeDeferred": True,
        },
        site_content={"deferredGeneration": True},
        template=dict(main.FREEFORM_SITE_SPEC),
        status="PENDING",
    )
    main.save_zendesk_ticket_link(
        approval_id,
        "canonical-live-url-fail",
        "campaign-live-url-fail",
        "email",
        "intake",
        5806,
        "https://zendesk.test/5806",
        "open",
        ["asf_managed", "asf_deploy_email_fired", "admin_owned"],
        {},
    )
    row = main.get_approval_or_404(approval_id)
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_tags",
        lambda ticket_id, *, add=None, remove=None: [
            "asf_managed", "asf_deploy_email_fired", "admin_owned", *(add or [])
        ],
    )
    monkeypatch.setattr(
        main,
        "update_zendesk_ticket_comment",
        lambda ticket_id, body, public, extra_ticket_fields=None: {
            "id": ticket_id, "status": "open", "brand_id": 88, "ticket_form_id": 5001, "custom_fields": []
        },
    )
    monkeypatch.setattr(
        main,
        "zendesk_api_request",
        lambda *args, **kwargs: {
            "ticket": {"id": 5806, "brand_id": 88, "ticket_form_id": 5001, "custom_fields": []}
        },
    )

    with pytest.raises(main.HTTPException) as error:
        main.update_existing_intake_ticket(
            row,
            {"url": "https://alpha.netlify.app"},
            {"subject": "Preview", "body": "Preview body"},
            ticket_id_override=5806,
        )

    assert error.value.status_code == 502
    assert error.value.detail["code"] == "ZENDESK_LIVE_URL_UPDATE_REJECTED"


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


def test_campaign_intake_splits_email_and_call_records_without_generation(monkeypatch):
    item = apify_item(301, province="KwaZulu-Natal")
    item["email"] = "campaign@example.com"
    model_calls = []
    discovery_calls = []
    monkeypatch.setattr(main, "run_apify_google_maps", lambda *args, **kwargs: discovery_calls.append(True) or [item])
    monkeypatch.setattr(main, "compact_lead_with_groq", lambda *args: model_calls.append("groq"))
    monkeypatch.setattr(main, "generate_final_html_with_gemini", lambda *args: model_calls.append("gemini"))
    monkeypatch.setattr(main, "export_site_to_github", lambda *args, **kwargs: model_calls.append("github"))
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})
    monkeypatch.setattr(main, "zendesk_connection_snapshot", lambda: {"connected": True, "workspaceReady": True})
    ticket_ids = {"email": 7301, "phone": 7302}
    monkeypatch.setattr(
        main,
        "create_zendesk_intake_tickets",
        lambda **kwargs: [{"ticketId": ticket_ids[kwargs["requested_channels"][0]]}],
    )

    request = {
        "campaignName": "Durban Services - July",
        "presetId": "plumbers",
        "industry": "Plumbing",
        "location": "Durban, South Africa",
        "limit": 1,
        "channels": ["email", "phone"],
        "syncZendesk": True,
    }
    response = client.post("/api/campaigns/intake", json=request)
    replay = client.post("/api/campaigns/intake", json=request)

    assert response.status_code == 200
    payload = response.json()
    assert payload["metrics"]["emailLeads"] == 1
    assert payload["metrics"]["callLeads"] == 1
    assert payload["metrics"]["aiGenerations"] == 0
    assert payload["metrics"]["reposCreated"] == 0
    assert payload["emailLeads"][0]["status"] == "TICKET_READY"
    assert payload["callLeads"][0]["status"] == "TICKET_READY"
    assert replay.status_code == 200
    assert replay.json()["campaignId"] == payload["campaignId"]
    assert replay.json()["idempotentReplay"] is True
    assert len(discovery_calls) == 1
    assert model_calls == []


def test_campaign_replay_resumes_partial_zendesk_intake_without_duplicates(monkeypatch):
    item = apify_item(401, province="KwaZulu-Natal")
    item["email"] = "resume@example.com"
    discovery_calls = []
    attempts = {"email": 0, "phone": 0}
    monkeypatch.setattr(main, "run_apify_google_maps", lambda *args, **kwargs: discovery_calls.append(True) or [item])
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})

    def intake(**kwargs):
        channel = kwargs["requested_channels"][0]
        attempts[channel] += 1
        if channel == "phone" and attempts[channel] == 1:
            raise RuntimeError("temporary Zendesk timeout")
        return [{"ticketId": 9101 if channel == "email" else 9102}]

    monkeypatch.setattr(main, "create_zendesk_intake_tickets", intake)
    request = {
        "campaignName": "Resumable campaign",
        "presetId": "plumbers",
        "industry": "Plumbing",
        "location": "Durban, South Africa",
        "limit": 1,
        "channels": ["email", "phone"],
        "syncZendesk": True,
    }

    first = client.post("/api/campaigns/intake", json=request)
    second = client.post("/api/campaigns/intake", json=request)

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "INTAKE_PARTIAL"
    assert first.json()["sync"]["pending"] == 1
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ACTIVE"
    assert second.json()["idempotentReplay"] is True
    assert second.json()["sync"]["pending"] == 0
    assert len(discovery_calls) == 1
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM campaign_deployments").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM campaign_email_leads").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM campaign_call_leads").fetchone()[0] == 1


def test_campaign_final_state_uses_saved_ticket_facts_not_stale_worker_error(monkeypatch):
    item = apify_item(402, province="KwaZulu-Natal")
    item["email"] = "race@example.com"
    item["phone"] = None
    monkeypatch.setattr(main, "run_apify_google_maps", lambda *args, **kwargs: [item])
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})

    def stale_worker(**kwargs):
        with main.get_pipeline_db() as db:
            db.execute(
                "UPDATE campaign_email_leads SET ticket_id = 9301, status = 'TICKET_READY' WHERE approval_id = ?",
                (kwargs["approval_id"],),
            )
        raise RuntimeError("stale worker timeout after another retry succeeded")

    monkeypatch.setattr(main, "create_zendesk_intake_tickets", stale_worker)
    response = client.post(
        "/api/campaigns/intake",
        json={
            "campaignName": "Concurrent retry facts",
            "presetId": "plumbers",
            "location": "Durban, South Africa",
            "limit": 1,
            "channels": ["email"],
            "syncZendesk": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ACTIVE"
    assert response.json()["sync"]["pending"] == 0
    assert response.json()["sync"]["errors"] == []
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT error FROM campaign_deployments").fetchone()["error"] is None


def test_campaign_creation_is_locked_until_zendesk_workspace_is_provisioned():
    response = client.post(
        "/api/campaigns/intake",
        json={
            "campaignName": "Locked campaign",
            "presetId": "plumbers",
            "location": "Durban",
            "limit": 1,
            "channels": ["email"],
            "syncZendesk": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ZENDESK_SETUP_REQUIRED"


def test_uploaded_campaign_processes_durable_chunks_without_generating_sites(monkeypatch):
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})
    ticket_ids = iter([8101, 8102])
    monkeypatch.setattr(main, "create_zendesk_intake_tickets", lambda **kwargs: [{"ticketId": next(ticket_ids)}])
    monkeypatch.setattr(main, "compact_lead_with_groq", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI must wait for deploy_site")))
    monkeypatch.setattr(main, "generate_final_html_with_gemini", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("AI must wait for deploy_site")))
    monkeypatch.setattr(main, "export_site_to_github", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GitHub must wait for deploy_site")))

    csv_data = (
        "businessName,email,phone,industry,location,sourceUrl\n"
        "Email Lead,email@example.com,,Plumbing,Durban,https://maps.example.com/email\n"
        "Call Lead,,+27 31 555 0123,Plumbing,Durban,https://maps.example.com/call\n"
    )
    queued = client.post(
        "/api/campaigns/import",
        data={
            "campaignName": "Uploaded leads",
            "industry": "Plumbing",
            "location": "Durban",
            "channels": "email,phone",
            "chunkSize": "1",
        },
        files={"file": ("leads.csv", csv_data, "text/csv")},
    )

    assert queued.status_code == 200, queued.text
    job = queued.json()
    assert job["status"] == "QUEUED"
    assert job["fileRetained"] is True
    assert job["totalRows"] == 2

    replay = client.post(
        "/api/campaigns/import",
        data={
            "campaignName": "Uploaded leads",
            "industry": "Plumbing",
            "location": "Durban",
            "channels": "email,phone",
            "chunkSize": "1",
        },
        files={"file": ("leads.csv", csv_data, "text/csv")},
    )
    assert replay.status_code == 200
    assert replay.json()["jobId"] == job["jobId"]
    assert replay.json()["idempotentReplay"] is True

    first = client.post(f"/api/campaigns/imports/{job['jobId']}/process")
    assert first.status_code == 200, first.text
    assert first.json()["processedRows"] == 1
    assert first.json()["fileRetained"] is True

    second = client.post(f"/api/campaigns/imports/{job['jobId']}/process")
    assert second.status_code == 200, second.text
    completed = second.json()
    assert completed["status"] == "COMPLETED"
    assert completed["fileRetained"] is False
    assert completed["succeededRows"] == 2
    assert completed["campaign"]["metrics"]["emailLeads"] == 1
    assert completed["campaign"]["metrics"]["callLeads"] == 1
    assert completed["campaign"]["metrics"]["aiGenerations"] == 0


def test_uploaded_generic_row_ids_do_not_create_distinct_lead_identities():
    first = main.normalize_uploaded_lead(
        {"id": "row-1", "businessName": "Alpha Plumbing", "email": "alpha@example.com", "location": "Durban"},
        1,
        "Plumbing",
        "Durban",
    )
    second = main.normalize_uploaded_lead(
        {"id": "row-2", "businessName": "Alpha Plumbing", "email": "alpha@example.com", "location": "Durban"},
        2,
        "Plumbing",
        "Durban",
    )

    assert first.canonicalLeadKey == second.canonicalLeadKey
    assert first.leadKey == second.leadKey


def test_uploaded_identity_is_skipped_across_campaigns(monkeypatch):
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})
    ticket_calls = []

    def intake(**kwargs):
        ticket_calls.append(kwargs)
        return [{"ticketId": 8201}]

    monkeypatch.setattr(main, "create_zendesk_intake_tickets", intake)

    def queue(name, business):
        csv_data = f"businessName,email,industry,location\n{business},same@example.com,Plumbing,Durban\n"
        response = client.post(
            "/api/campaigns/import",
            data={
                "campaignName": name,
                "industry": "Plumbing",
                "location": "Durban",
                "channels": "email",
                "chunkSize": "5",
            },
            files={"file": ("leads.csv", csv_data, "text/csv")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    first_job = queue("First upload", "Alpha Plumbing")
    first_result = client.post(f"/api/campaigns/imports/{first_job['jobId']}/process")
    second_job = queue("Second upload", "Alpha Plumbing (Pty) Ltd")
    second_result = client.post(f"/api/campaigns/imports/{second_job['jobId']}/process")

    assert first_result.status_code == 200, first_result.text
    assert first_result.json()["succeededRows"] == 1
    assert second_result.status_code == 200, second_result.text
    assert second_result.json()["status"] == "COMPLETED"
    assert second_result.json()["succeededRows"] == 0
    assert second_result.json()["skippedRows"] == 1
    assert second_result.json()["campaign"]["metrics"]["emailLeads"] == 0
    assert len(ticket_calls) == 1
    with main.get_pipeline_db() as db:
        assert db.execute("SELECT COUNT(*) FROM campaign_email_leads").fetchone()[0] == 1
        item = db.execute(
            "SELECT * FROM campaign_import_items WHERE job_id = ?", (second_job["jobId"],)
        ).fetchone()
        assert item["status"] == "SKIPPED"
        assert main.safe_json_loads(item["ticket_ids_json"], []) == [8201]


def test_uploaded_identity_claim_is_atomic_across_campaigns():
    timestamp = main.now_iso()
    campaign_ids = ["claim-campaign-a", "claim-campaign-b"]
    with main.get_pipeline_db() as db:
        for campaign_id in campaign_ids:
            db.execute(
                """
                INSERT INTO campaigns (
                    id, idempotency_key, name, preset_id, industry, query, location, requested_count,
                    discovered_count, channel_filter, status, warnings_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'uploaded-leads', 'Plumbing', 'leads.csv', 'Durban', 1, 0, 'email',
                          'IMPORT_PROCESSING', '[]', ?, ?)
                """,
                (campaign_id, f"upload:{campaign_id}", campaign_id, timestamp, timestamp),
            )

    leads = [
        main.normalize_uploaded_lead(
            {"businessName": business, "email": "atomic@example.com", "location": "Durban"},
            index,
            "Plumbing",
            "Durban",
        )
        for index, business in enumerate(["Atomic Plumbing", "Atomic Plumbing Pty Ltd"], start=1)
    ]
    barrier = threading.Barrier(2)

    def claim(index):
        barrier.wait(timeout=5)
        return main.claim_uploaded_campaign_lead_identity(campaign_ids[index], leads[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [0, 1]))

    assert sum(result is None for result in results) == 1
    assert sum(result is not None for result in results) == 1
    with main.get_pipeline_db() as db:
        email_claims = db.execute(
            "SELECT COUNT(*) AS count FROM campaign_lead_identity_claims WHERE identity_key = 'email:atomic@example.com'"
        ).fetchone()["count"]
        assert email_claims == 1


def test_campaign_deploy_webhook_generates_once_then_deploys(monkeypatch):
    item = apify_item(302, province="KwaZulu-Natal")
    item["email"] = "deploy@example.com"
    monkeypatch.setattr(main, "run_apify_google_maps", lambda *args, **kwargs: [item])
    model_calls, export_calls = stub_generation(monkeypatch)
    monkeypatch.setattr(main, "deploy_github_repo_to_netlify_for_lead", fake_git_deploy_with_history)
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "campaign-webhook-secret")
    monkeypatch.setattr(main, "require_zendesk_workspace_ready", lambda: {"workspaceReady": True})
    monkeypatch.setattr(main, "verify_zendesk_ticket_contracts", lambda channels: {})
    monkeypatch.setattr(main, "zendesk_connection_snapshot", lambda: {"connected": True, "workspaceReady": True})
    monkeypatch.setattr(main, "create_zendesk_intake_tickets", lambda **kwargs: [{"ticketId": 7401}])
    monkeypatch.setattr(main, "update_zendesk_deployment_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main,
        "update_existing_intake_ticket",
        lambda row, deployment, outreach, ticket_id_override=None: {
            "ticketId": ticket_id_override or 7401,
            "syncStatus": "TICKET_UPDATED",
            "liveLink": deployment.get("url"),
        },
    )

    campaign = client.post(
        "/api/campaigns/intake",
        json={
            "campaignName": "Deferred Deploy",
            "presetId": "plumbers",
            "location": "Durban, South Africa",
            "limit": 1,
            "channels": ["email"],
            "syncZendesk": True,
        },
    ).json()
    approval_id = campaign["emailLeads"][0]["approvalId"]

    deployed = client.post(
        "/api/zendesk/webhook",
        headers={"x-ai-site-factory-secret": "campaign-webhook-secret"},
        json={
            "action": "deploy_site",
            "approvalId": approval_id,
            "channel": "email",
            "actor": "Zendesk test agent",
        },
    )

    assert deployed.status_code == 200
    detail = client.get(f"/api/campaigns/{campaign['campaignId']}").json()
    assert detail["metrics"]["aiGenerations"] == 1
    assert detail["metrics"]["reposCreated"] == 1
    assert detail["metrics"]["deployed"] == 1
    assert detail["deployments"][0]["liveUrl"] == "https://alpha.netlify.app"
    assert model_calls == ["groq_compact", "gemini_final"]
    assert len(export_calls) == 1


def create_deferred_webhook_approval(approval_id: str = "approval-concurrent-deploy") -> str:
    return main.create_approval_record(
        pipeline_id="pipeline-concurrent-deploy",
        canonical_key="canonical-concurrent-deploy",
        lead_key="lead-concurrent-deploy",
        business_name="Concurrent Plumbing",
        site_html=None,
        context={
            "businessName": "Concurrent Plumbing",
            "canonicalLeadKey": "canonical-concurrent-deploy",
            "contactChannel": "phone",
            "phone": "+27 31 555 0199",
            "intakeDeferred": True,
        },
        site_content={"deferredGeneration": True},
        template={},
        status="AWAITING_DEPLOYMENT",
        approval_id=approval_id,
    )


def test_deferred_github_retry_reuses_persisted_html_without_model_calls(monkeypatch):
    approval_id = create_deferred_webhook_approval("approval-deferred-export-retry")
    model_calls = []
    export_calls = []

    def compact(context):
        model_calls.append("groq")
        return {
            "businessName": context["businessName"],
            "industry": "Plumbing",
            "location": "Durban",
            "summary": "Local plumbing services.",
            "serviceKeywords": ["Plumbing"],
        }

    def generate(brief):
        model_calls.append("gemini")
        return {
            "html": "<!doctype html><html><head></head><body>Saved artifact</body></html>",
            "qaNotes": "ok",
            "stylingLibraries": ["Bootstrap"],
        }

    def export(canonical_key, business_name, site_html, pipeline_id=None, approval_id=None):
        export_calls.append(site_html)
        if len(export_calls) == 1:
            raise RuntimeError("temporary github 503")
        return fake_github_export(canonical_key, business_name, site_html, pipeline_id, approval_id)

    monkeypatch.setattr(main, "compact_lead_with_groq", compact)
    monkeypatch.setattr(main, "generate_final_html_with_gemini", generate)
    monkeypatch.setattr(main, "export_site_to_github", export)

    with pytest.raises(main.HTTPException, match="Deferred site export failed"):
        main.prepare_deferred_approval(approval_id)

    failed = main.get_approval_or_404(approval_id)
    assert failed["status"] == "EXPORT_FAILED"
    assert "Saved artifact" in failed["html"]

    retried = main.prepare_deferred_approval(approval_id)

    assert retried["status"] == "PENDING"
    assert main.safe_json_loads(retried["github_export_json"], {})["commitSha"]
    assert model_calls == ["groq", "gemini"]
    assert len(export_calls) == 2


def test_deploy_webhook_claim_is_atomic_for_concurrent_deliveries():
    approval_id = create_deferred_webhook_approval()
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return main.acquire_deploy_webhook_claim(approval_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))

    assert sorted(item["disposition"] for item in claims) == ["ACQUIRED", "IN_PROGRESS"]
    owner = next(item for item in claims if item["disposition"] == "ACQUIRED")
    assert main.complete_deploy_webhook_claim(
        approval_id,
        owner["token"],
        {"approvalId": approval_id, "deployment": {"url": "https://atomic.example"}},
    ) is True

    replay = main.acquire_deploy_webhook_claim(approval_id)
    assert replay["disposition"] == "ALREADY_PROCESSED"
    assert replay["result"]["deployment"]["url"] == "https://atomic.example"
    with main.get_pipeline_db() as db:
        row = db.execute(
            "SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    assert row["state"] == "COMPLETED"
    assert row["attempt_count"] == 1


def test_expired_deploy_webhook_lease_can_be_reclaimed():
    approval_id = create_deferred_webhook_approval("approval-expired-lease")
    first = main.acquire_deploy_webhook_claim(approval_id)
    assert first["disposition"] == "ACQUIRED"
    with main.get_pipeline_db() as db:
        db.execute(
            "UPDATE deploy_webhook_claims SET lease_expires_at = ? WHERE approval_id = ?",
            (0, approval_id),
        )

    replacement = main.acquire_deploy_webhook_claim(approval_id)
    assert replacement["disposition"] == "ACQUIRED"
    assert replacement["token"] != first["token"]
    assert replacement["attemptCount"] == 2
    assert main.complete_deploy_webhook_claim(
        approval_id,
        first["token"],
        {"approvalId": approval_id, "owner": "expired"},
    ) is False
    assert main.complete_deploy_webhook_claim(
        approval_id,
        replacement["token"],
        {"approvalId": approval_id, "owner": "replacement"},
    ) is True


def test_concurrent_deploy_webhooks_run_generation_and_deployment_once(monkeypatch):
    approval_id = create_deferred_webhook_approval("approval-concurrent-endpoint")
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "concurrent-secret")
    monkeypatch.setattr(main, "safe_update_zendesk_deployment_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "update_campaign_workflow", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "reuse_existing_live_deployment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_pipeline_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_zendesk_webhook_event", lambda *args, **kwargs: None)

    generation_started = threading.Event()
    release_generation = threading.Event()
    generation_calls = []
    deployment_calls = []

    def prepare(approval_id_value):
        generation_calls.append(approval_id_value)
        generation_started.set()
        assert release_generation.wait(timeout=5)
        with main.get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = 'PENDING', html = '<!doctype html><html></html>',
                    github_export_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    '{"repository":"owner/repo","commitSha":"abc123"}',
                    main.now_iso(),
                    approval_id_value,
                ),
            )
        return main.get_approval_or_404(approval_id_value)

    def deploy(approval_id_value, request):
        deployment_calls.append(approval_id_value)
        return main.ApprovalActionResponse(
            approvalId=approval_id_value,
            status="APPROVED",
            canonicalLeadKey="canonical-concurrent-deploy",
            businessName="Concurrent Plumbing",
            deployment={"url": "https://concurrent.example", "state": "ready"},
        )

    monkeypatch.setattr(main, "prepare_deferred_approval", prepare)
    monkeypatch.setattr(main, "approve_generated_site", deploy)

    def deliver():
        request = main.ZendeskWebhookRequest(
            action="deploy_site",
            approvalId=approval_id,
            canonicalLeadKey="canonical-concurrent-deploy",
            channel="phone",
        )
        http_request = main.Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/zendesk/webhook",
                "headers": [(b"x-ai-site-factory-secret", b"concurrent-secret")],
            }
        )
        return main.zendesk_webhook(request, http_request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deliver)
        assert generation_started.wait(timeout=5)
        second = pool.submit(deliver)
        second_result = second.result(timeout=5)
        release_generation.set()
        first_result = first.result(timeout=5)

    assert {first_result["status"], second_result["status"]} == {"COMPLETED", "IN_PROGRESS"}
    assert generation_calls == [approval_id]
    assert deployment_calls == [approval_id]

    replay = deliver()
    assert replay["status"] == "ALREADY_PROCESSED"
    assert replay["result"]["deployment"]["deployment"]["url"] == "https://concurrent.example"
    assert generation_calls == [approval_id]
    assert deployment_calls == [approval_id]


def test_failed_deploy_webhook_claim_releases_for_retry(monkeypatch):
    approval_id = create_deferred_webhook_approval("approval-retry-deploy")
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", "retry-secret")
    monkeypatch.setattr(main, "safe_update_zendesk_deployment_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "update_campaign_workflow", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "reuse_existing_live_deployment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_pipeline_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_zendesk_webhook_event", lambda *args, **kwargs: None)
    prepare_calls = []
    deployment_calls = []

    def prepare(approval_id_value):
        prepare_calls.append(approval_id_value)
        if len(prepare_calls) == 1:
            raise main.HTTPException(status_code=502, detail="temporary generation failure")
        with main.get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = 'PENDING', html = '<!doctype html><html></html>',
                    github_export_json = ?, updated_at = ?
                WHERE id = ?
                """,
                ('{"repository":"owner/repo","commitSha":"retry123"}', main.now_iso(), approval_id_value),
            )
        return main.get_approval_or_404(approval_id_value)

    def deploy(approval_id_value, request):
        deployment_calls.append(approval_id_value)
        return main.ApprovalActionResponse(
            approvalId=approval_id_value,
            status="APPROVED",
            canonicalLeadKey="canonical-concurrent-deploy",
            businessName="Concurrent Plumbing",
            deployment={"url": "https://retry.example", "state": "ready"},
        )

    monkeypatch.setattr(main, "prepare_deferred_approval", prepare)
    monkeypatch.setattr(main, "approve_generated_site", deploy)

    request = main.ZendeskWebhookRequest(
        action="deploy_site",
        approvalId=approval_id,
        canonicalLeadKey="canonical-concurrent-deploy",
        channel="phone",
    )
    http_request = main.Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/zendesk/webhook",
            "headers": [(b"x-ai-site-factory-secret", b"retry-secret")],
        }
    )

    with pytest.raises(main.HTTPException, match="temporary generation failure"):
        main.zendesk_webhook(request, http_request)
    with main.get_pipeline_db() as db:
        failed = db.execute(
            "SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    assert failed["state"] == "FAILED"
    assert failed["claim_token"] is None
    assert failed["lease_expires_at"] is None

    retried = main.zendesk_webhook(request, http_request)
    assert retried["status"] == "COMPLETED"
    assert prepare_calls == [approval_id, approval_id]
    assert deployment_calls == [approval_id]
    with main.get_pipeline_db() as db:
        completed = db.execute(
            "SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    assert completed["state"] == "COMPLETED"
    assert completed["attempt_count"] == 2


def test_zendesk_connection_endpoint_validates_and_masks_token(monkeypatch):
    monkeypatch.setattr(
        main.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"user": {"id": 42, "email": "agent@example.com"}}),
    )

    response = client.put(
        "/api/settings/zendesk-connection",
        json={
            "subdomain": "supporthub.zendesk.com",
            "username": "agent@example.com",
            "apiToken": "zendesk-secret-token",
            "validateConnection": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is True
    assert payload["subdomain"] == "supporthub"
    assert payload["username"] == "agent@example.com"
    assert payload["maskedToken"] != "zendesk-secret-token"
    assert "apiToken" not in payload


def test_legacy_pipeline_backfill_populates_campaign_dashboard_idempotently():
    lead = main.DiscoveredLead(**lead_payload())
    lead.phone = "+27 31 555 0101"
    canonical_key = main.canonical_lead_key_for_lead(lead)
    lead.canonicalLeadKey = canonical_key
    main.upsert_lead_registry(lead)
    main.record_discovery_batch(
        batch_id="legacy-batch-1",
        preset_id="plumbers",
        query="plumbers in Durban, South Africa",
        location="Durban, South Africa",
        lead_count=1,
        duplicates_skipped=0,
        leads=[lead],
        province_stats={},
        warnings=[],
    )
    main.save_pipeline_run(
        pipeline_id="legacy-pipeline-1",
        status="PENDING_APPROVAL",
        template_id=main.FREEFORM_TEMPLATE_ID,
        source_batch_id="legacy-batch-1",
        lead_count=1,
        completed_count=0,
        pending_count=1,
        failed_count=0,
        warnings=[],
    )
    main.create_approval_record(
        pipeline_id="legacy-pipeline-1",
        canonical_key=canonical_key,
        lead_key=lead.leadKey,
        business_name=lead.businessName,
        site_html="<!doctype html><html><body>Legacy site</body></html>",
        context=main.build_public_lead_context(lead, {}, canonical_key),
        site_content={"legacy": True},
        template=dict(main.FREEFORM_SITE_SPEC),
        status="PENDING",
    )

    first = main.backfill_legacy_campaign_data()
    second = main.backfill_legacy_campaign_data()
    dashboard = main.list_campaigns(20, includeLegacy=True)

    assert first == {
        "campaignsCreated": 1,
        "emailLeadsCreated": 1,
        "callLeadsCreated": 1,
        "deploymentsCreated": 1,
    }
    assert second == {
        "campaignsCreated": 0,
        "emailLeadsCreated": 0,
        "callLeadsCreated": 0,
        "deploymentsCreated": 0,
    }
    assert dashboard["totals"]["campaigns"] == 1
    assert dashboard["totals"]["leads"] == 2
    assert dashboard["totals"]["pending"] == 1
    assert dashboard["totals"]["aiGenerations"] == 1
