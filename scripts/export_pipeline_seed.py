"""Export the non-secret SQLite application state used to seed empty deployments."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "backend" / "data" / "pipeline.db"
DEFAULT_OUTPUT = ROOT / "backend" / "data" / "pipeline.seed.json"

# Keep this dependency order aligned with PIPELINE_SEED_TABLES in backend/main.py.
TABLES = [
    "lead_registry",
    "lead_identity_index",
    "discovery_batches",
    "pipeline_runs",
    "pipeline_steps",
    "site_registry",
    "github_site_repos",
    "deployment_history",
    "approval_records",
    "campaigns",
    "campaign_deployments",
    "campaign_email_leads",
    "campaign_call_leads",
    "zendesk_field_settings",
    "zendesk_provisioned_resources",
    "zendesk_ticket_links",
    "zendesk_webhook_events",
]

SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?)?(token|secret|password|authorization|access_key|private_key)($|_)",
    re.IGNORECASE,
)


def assert_no_sensitive_keys(value: Any, path: str) -> None:
    """Reject structured values that accidentally contain credential-shaped keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY.search(str(key)):
                raise ValueError(f"Refusing to export credential-shaped key: {child_path}")
            assert_no_sensitive_keys(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_sensitive_keys(item, f"{path}[{index}]")


def inspect_json_columns(row: dict[str, Any], table: str) -> None:
    for column, value in row.items():
        if not column.endswith("_json") or not value:
            continue
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            continue
        assert_no_sensitive_keys(decoded, f"{table}.{column}")


def export_seed(source: Path, output: Path) -> dict[str, int]:
    if not source.exists():
        raise FileNotFoundError(f"SQLite database not found: {source}")

    connection = sqlite3.connect(source)
    connection.row_factory = sqlite3.Row
    try:
        tables: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}
        for table in TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not exists:
                tables[table] = []
                counts[table] = 0
                continue
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]
            for row in rows:
                inspect_json_columns(row, table)
            tables[table] = rows
            counts[table] = len(rows)
    finally:
        connection.close()

    payload = {
        "schemaVersion": 1,
        "description": "Previous AI Site Factory application data for empty deployments.",
        "counts": counts,
        "tables": tables,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    counts = export_seed(args.source.resolve(), args.output.resolve())
    print(f"Exported {sum(counts.values())} records to {args.output.resolve()}")


if __name__ == "__main__":
    main()
