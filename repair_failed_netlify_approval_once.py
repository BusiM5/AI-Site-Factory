#!/usr/bin/env python
"""
One-time repair for a failed AI Site Factory Netlify approval.

What it does:
1. Finds one DEPLOY_FAILED approval in the local pipeline SQLite database.
2. Detects whether the old Git-linked run already created a same-name Netlify site.
3. Reconnects that orphaned Netlify site to site_registry when necessary.
4. Runs the new direct Netlify ZIP deployment path against the saved HTML.
5. Marks the approval as APPROVED and records the deployment.

It deliberately does NOT generate outreach or create a Zendesk ticket.
Use this only against the same environment/database where the failed approval exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
if not BACKEND_DIR.exists():
    raise SystemExit("Run this script from the AI-Site-Factory repository root.")

sys.path.insert(0, str(BACKEND_DIR))

try:
    import main as factory
except Exception as error:
    raise SystemExit(f"Could not import backend/main.py: {error}") from error


NETLIFY_SITES_URL = "https://api.netlify.com/api/v1/sites"


def safe_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def list_failed_approvals() -> List[Any]:
    with factory.get_pipeline_db() as db:
        return db.execute(
            """
            SELECT id, business_name, canonical_lead_key, status, created_at, updated_at
            FROM approval_records
            WHERE status = 'DEPLOY_FAILED'
            ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()


def choose_approval(approval_id: Optional[str]) -> Any:
    with factory.get_pipeline_db() as db:
        if approval_id:
            row = db.execute(
                "SELECT * FROM approval_records WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if not row:
                raise SystemExit(f"No approval record exists with id: {approval_id}")
            if row["status"] != "DEPLOY_FAILED":
                raise SystemExit(
                    f"Approval {approval_id} is {row['status']}, not DEPLOY_FAILED. "
                    "This one-off repair intentionally only targets failed deployments."
                )
            return row

    rows = list_failed_approvals()
    if not rows:
        raise SystemExit(
            "No DEPLOY_FAILED approval exists in this local pipeline database.\n"
            "That usually means the failed record lives on Render rather than in backend/data/pipeline.db."
        )

    if len(rows) > 1:
        print("More than one failed approval was found. Re-run with --approval-id <id>:\n")
        for row in rows:
            print(
                f"  {row['id']} | {row['business_name']} | "
                f"{row['created_at']} | {row['canonical_lead_key']}"
            )
        raise SystemExit(2)

    return rows[0]


def expected_site_name(business_name: str, canonical_key: str) -> str:
    return f"ai-site-{factory.slugify(business_name, 32)}-{canonical_key[:8]}"


def netlify_headers() -> Dict[str, str]:
    token = os.getenv("NETLIFY_AUTH_TOKEN")
    if not token:
        raise SystemExit(
            "NETLIFY_AUTH_TOKEN is missing. Put it in backend/.env or your current environment."
        )
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "AI-Site-Factory-One-Time-Repair",
    }


def find_remote_site(site_name: str) -> Optional[Dict[str, Any]]:
    response = requests.get(
        NETLIFY_SITES_URL,
        headers=netlify_headers(),
        params={
            "name": site_name,
            "filter": "all",
            "page": 1,
            "per_page": 100,
        },
        timeout=45,
    )

    if not response.ok:
        raise SystemExit(
            f"Could not list Netlify sites: HTTP {response.status_code}\n{response.text[:800]}"
        )

    payload = response.json()
    if not isinstance(payload, list):
        raise SystemExit("Netlify returned an unexpected response while listing sites.")

    exact_matches = [
        site
        for site in payload
        if site.get("name") == site_name
        or site.get("url", "").rstrip("/").endswith(f"//{site_name}.netlify.app")
        or site.get("ssl_url", "").rstrip("/").endswith(f"//{site_name}.netlify.app")
    ]

    if len(exact_matches) > 1:
        raise SystemExit(
            f"Found {len(exact_matches)} Netlify sites matching {site_name}. "
            "Stop here and inspect them manually so we do not deploy into the wrong site."
        )

    return exact_matches[0] if exact_matches else None


def site_registry_row(canonical_key: str) -> Optional[Any]:
    with factory.get_pipeline_db() as db:
        return db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = ?",
            (canonical_key,),
        ).fetchone()


def register_orphan_site(
    *,
    canonical_key: str,
    remote_site: Dict[str, Any],
    github_export: Dict[str, Any],
) -> None:
    timestamp = factory.now_iso()
    site_id = remote_site.get("id") or remote_site.get("name")
    site_name = remote_site.get("name")
    site_url = (
        remote_site.get("ssl_url")
        or remote_site.get("url")
        or f"https://{site_name}.netlify.app"
    )
    published_deploy = remote_site.get("published_deploy") or {}

    if not site_id or not site_name:
        raise SystemExit("The matching Netlify site did not contain an id or name.")

    with factory.get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, admin_url,
                github_repo_full_name, github_repo_url, last_commit_sha, last_build_id,
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_lead_key) DO UPDATE SET
                site_id = excluded.site_id,
                site_name = excluded.site_name,
                url = excluded.url,
                admin_url = COALESCE(excluded.admin_url, site_registry.admin_url),
                github_repo_full_name = excluded.github_repo_full_name,
                github_repo_url = excluded.github_repo_url,
                last_commit_sha = excluded.last_commit_sha,
                last_build_id = excluded.last_build_id,
                updated_at = excluded.updated_at,
                last_deploy_id = excluded.last_deploy_id,
                last_deploy_state = excluded.last_deploy_state
            """,
            (
                canonical_key,
                site_id,
                site_name,
                site_url,
                remote_site.get("admin_url"),
                github_export.get("repository"),
                github_export.get("repoUrl"),
                github_export.get("commitSha"),
                None,
                timestamp,
                timestamp,
                published_deploy.get("id"),
                "ORPHAN_RECONCILED",
                0,
            ),
        )


def mark_approval_repaired(row: Any, deployment: Dict[str, Any], github_export: Dict[str, Any]) -> None:
    previous_notes = (row["notes"] or "").strip()
    repair_note = (
        f"One-time direct Netlify repair completed at {factory.now_iso()}. "
        f"Previous deployment failure was replayed without creating Zendesk outreach."
    )
    notes = f"{previous_notes}\n{repair_note}".strip()
    state = deployment.get("state") or "unknown"
    saved_html = None if state == "ready" else row["html"]

    with factory.get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = ?, updated_at = ?, approved_by = ?, notes = ?, html = ?,
                deployment_history_id = ?, publish_mode = ?, github_export_json = ?, errors_json = ?
            WHERE id = ?
            """,
            (
                "APPROVED",
                factory.now_iso(),
                "One-time Netlify Repair",
                notes,
                saved_html,
                deployment.get("deploymentHistoryId"),
                deployment.get("publishMode", "direct-netlify"),
                json.dumps(github_export, default=str),
                json.dumps([], default=str),
                row["id"],
            ),
        )

    factory.refresh_pipeline_run_status_from_approvals(row["pipeline_id"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair exactly one failed Netlify approval using the new direct deployment path."
    )
    parser.add_argument(
        "--approval-id",
        help="Failed approval ID. Required only when there is more than one failed approval.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the Netlify deploy. Without this flag, the script only inspects.",
    )
    args = parser.parse_args()

    if not hasattr(factory, "deploy_direct_netlify_for_lead"):
        raise SystemExit(
            "The direct Netlify patch is not present in backend/main.py. "
            "Pull commit 85c14fe first."
        )

    row = choose_approval(args.approval_id)
    github_export = safe_json(row["github_export_json"], {})
    if not isinstance(github_export, dict) or not github_export.get("repository"):
        raise SystemExit(
            "This failed approval has no successful GitHub export metadata, so there is nothing safe to replay."
        )

    if not row["html"]:
        raise SystemExit(
            "This failed approval no longer contains saved HTML, so it cannot be replayed safely."
        )

    canonical_key = row["canonical_lead_key"]
    business_name = row["business_name"]
    site_name = expected_site_name(business_name, canonical_key)
    existing_registry = site_registry_row(canonical_key)
    remote_site = None if existing_registry else find_remote_site(site_name)

    print("\nOne-time Netlify repair plan")
    print(f"  Approval:      {row['id']}")
    print(f"  Business:      {business_name}")
    print(f"  Expected site: {site_name}")
    print(f"  Existing DB:   {'yes' if existing_registry else 'no'}")
    print(f"  Netlify site:  {'found' if remote_site else 'not found'}")
    print(f"  Repository:    {github_export.get('repository')}")

    if not args.execute:
        print("\nDry run only. Nothing changed.")
        print("Re-run with: python repair_failed_netlify_approval_once.py --execute")
        return

    if remote_site:
        register_orphan_site(
            canonical_key=canonical_key,
            remote_site=remote_site,
            github_export=github_export,
        )
        print(f"Recovered orphan Netlify site: {remote_site.get('name')}")

    deployment = factory.deploy_direct_netlify_for_lead(
        canonical_key=canonical_key,
        business_name=business_name,
        site_html=row["html"],
        pipeline_id=row["pipeline_id"],
        approval_id=row["id"],
        approved_by="One-time Netlify Repair",
        github_export=github_export,
    )
    mark_approval_repaired(row, deployment, github_export)

    print("\nRepair completed.")
    print(f"  State: {deployment.get('state')}")
    print(f"  URL:   {deployment.get('url')}")
    print("  Zendesk: intentionally skipped for this repair run.")


if __name__ == "__main__":
    main()
