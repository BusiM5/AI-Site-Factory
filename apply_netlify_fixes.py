#!/usr/bin/env python3
"""One-time patch for AI-Site-Factory backend/main.py.

Run from the repository root:
    python apply_netlify_fixes.py

The script creates backend/main.py.bak, then makes these changes:
1) Direct ZIP deployment becomes the primary Netlify route.
2) The Git-linked path uses an HTTPS clone URL and optional Netlify GitHub installation ID.
3) Direct deploys reuse an orphaned same-name Netlify site after a 422 instead of failing.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys

TARGET = Path("backend/main.py")


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: Run this from the repository root. Missing {TARGET}")
        return 1

    text = TARGET.read_text(encoding="utf-8")
    original = text

    def replace_once(old: str, new: str, label: str) -> None:
        nonlocal text
        count = text.count(old)
        if count != 1:
            raise RuntimeError(f"{label}: expected exactly one matching block, found {count}.")
        text = text.replace(old, new, 1)

    replace_once(
        'VALID_PUBLISH_MODES = {"github-netlify", "direct-netlify-fallback"}',
        'VALID_PUBLISH_MODES = {"github-netlify", "direct-netlify", "direct-netlify-fallback"}',
        "publish mode set",
    )

    replace_once(
        '''    repo_settings = {
        "provider": "github",
        "repo_path": repo_full_name,
        "repo_branch": branch,
        "repo_url": repo_url,
        "dir": "",
        "cmd": "",
        "public_repo": not bool(github_export.get("private")),
    }
''',
        '''    netlify_installation_id_raw = compact_text(os.getenv("NETLIFY_GITHUB_INSTALLATION_ID"))
    netlify_installation_id: Optional[int] = None
    if netlify_installation_id_raw:
        try:
            netlify_installation_id = int(netlify_installation_id_raw)
        except ValueError as error:
            raise RuntimeError(
                "NETLIFY_GITHUB_INSTALLATION_ID must be a numeric Netlify GitHub installation id."
            ) from error

    repo_settings = {
        "provider": "github",
        "repo_path": repo_full_name,
        "repo_branch": branch,
        "repo_url": f"https://github.com/{repo_full_name}.git",
        "dir": "",
        "cmd": "",
        "public_repo": not bool(github_export.get("private")),
    }
    if netlify_installation_id is not None:
        repo_settings["installation_id"] = netlify_installation_id
''',
        "Netlify Git repository settings",
    )

    anchor = "\ndef deploy_direct_netlify_fallback_for_lead(\n"
    if text.count(anchor) != 1:
        raise RuntimeError("Could not find the direct Netlify deployment function exactly once.")

    wrapper = '''
def deploy_direct_netlify_for_lead(
    canonical_key: str,
    business_name: str,
    site_html: str,
    pipeline_id: Optional[str],
    approval_id: Optional[str],
    approved_by: Optional[str],
    github_export: Dict[str, Any],
) -> Dict[str, Any]:
    """Deploy generated HTML directly to Netlify while retaining GitHub as the source archive."""
    result = deploy_direct_netlify_fallback_for_lead(
        canonical_key=canonical_key,
        business_name=business_name,
        site_html=site_html,
        pipeline_id=pipeline_id,
        approval_id=approval_id,
        approved_by=approved_by,
        github_export=github_export,
        git_error=RuntimeError("Direct Netlify deployment selected by pipeline configuration."),
    )
    result["publishMode"] = "direct-netlify"
    result["deploymentMode"] = "Direct Netlify"
    result.pop("fallbackReason", None)

    deployment_history_id = result.get("deploymentHistoryId")
    if deployment_history_id:
        with get_pipeline_db() as db:
            db.execute(
                "UPDATE deployment_history SET publish_mode = ?, raw_json = ? WHERE id = ?",
                ("direct-netlify", json.dumps(result, default=str), deployment_history_id),
            )

    log_event(
        "info",
        "provider.netlify.direct_deploy_finish",
        "Direct Netlify deployment recorded.",
        siteName=result.get("siteName"),
        state=result.get("state"),
        url=result.get("url"),
    )
    return result

'''
    text = text.replace(anchor, "\n" + wrapper + "def deploy_direct_netlify_fallback_for_lead(\n", 1)

    replace_once(
        '''        create_response = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "name": site_name,
                "processing_settings": {"html": {"pretty_urls": True}},
            },
            timeout=45,
        )
        create_response.raise_for_status()
        site = create_response.json()
        site_id = site.get("id") or site.get("name")
        site_name = site.get("name") or site_name
        site_created = True
        if not site_id:
            raise RuntimeError("Netlify did not return a site id for fallback deployment.")
''',
        '''        create_response = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "name": site_name,
                "processing_settings": {"html": {"pretty_urls": True}},
            },
            timeout=45,
        )
        if create_response.status_code == 422:
            # A previous Git-linked attempt may have created the site but failed before
            # our database recorded it. Reuse that Netlify site instead of treating the
            # duplicate name as a second deployment failure.
            sites_response = requests.get(
                "https://api.netlify.com/api/v1/sites",
                headers=headers,
                params={"per_page": 100},
                timeout=45,
            )
            sites_response.raise_for_status()
            matching_site = next(
                (item for item in sites_response.json() if item.get("name") == site_name),
                None,
            )
            if not matching_site:
                create_response.raise_for_status()
            site = matching_site
            site_id = site.get("id") or site.get("name")
            site_name = site.get("name") or site_name
            site_reused = True
            deploy_action = "DIRECT_FALLBACK_REDEPLOYED"
        else:
            create_response.raise_for_status()
            site = create_response.json()
            site_id = site.get("id") or site.get("name")
            site_name = site.get("name") or site_name
            site_created = True
        if not site_id:
            raise RuntimeError("Netlify did not return a site id for direct deployment.")
''',
        "direct Netlify duplicate-site recovery",
    )

    replace_once(
        '''    try:
        try:
            deployment = run_approval_step(
                "netlify_deploy",
                "netlify",
                lambda: deploy_github_repo_to_netlify_for_lead(
                    canonical_key=row["canonical_lead_key"],
                    business_name=row["business_name"],
                    pipeline_id=row["pipeline_id"],
                    approval_id=approval_id,
                    approved_by=approved_by,
                    regenerate_existing_site=request.regenerateExistingSite,
                    github_export=github_export,
                ),
            )
        except Exception as git_deploy_error:
            errors.append(structured_pipeline_error("netlify_deploy", git_deploy_error, provider="netlify", retryable=True))
            log_event(
                "warning",
                "approval.netlify.git_failed_fallback",
                "Git-linked Netlify deployment failed; attempting direct deploy fallback.",
                approvalId=approval_id,
                reason=sanitize_message(git_deploy_error),
            )
            deployment = run_approval_step(
                "netlify_direct_fallback",
                "netlify",
                lambda: deploy_direct_netlify_fallback_for_lead(
                    canonical_key=row["canonical_lead_key"],
                    business_name=row["business_name"],
                    site_html=site_html,
                    pipeline_id=row["pipeline_id"],
                    approval_id=approval_id,
                    approved_by=approved_by,
                    github_export=github_export,
                    git_error=git_deploy_error,
                ),
            )
        effective_publish_mode = deployment.get("publishMode", publish_mode)
''',
        '''    try:
        deployment = run_approval_step(
            "netlify_direct_deploy",
            "netlify",
            lambda: deploy_direct_netlify_for_lead(
                canonical_key=row["canonical_lead_key"],
                business_name=row["business_name"],
                site_html=site_html,
                pipeline_id=row["pipeline_id"],
                approval_id=approval_id,
                approved_by=approved_by,
                github_export=github_export,
            ),
        )
        effective_publish_mode = deployment.get("publishMode", "direct-netlify")
''',
        "approval deployment flow",
    )

    if text == original:
        print("No changes made.")
        return 0

    backup = TARGET.with_suffix(TARGET.suffix + ".bak")
    shutil.copy2(TARGET, backup)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched {TARGET}")
    print(f"Backup created at {backup}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
