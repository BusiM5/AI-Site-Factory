"""Clear operational demo data while preserving Zendesk configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import main  # noqa: E402


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        required=True,
        help="Must be exactly DELETE-DEMO-DATA.",
    )
    args = parser.parse_args()
    if args.confirm != "DELETE-DEMO-DATA":
        raise SystemExit("Refusing cleanup: pass --confirm DELETE-DEMO-DATA")
    result = main.clear_pipeline_operational_data()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    run()
