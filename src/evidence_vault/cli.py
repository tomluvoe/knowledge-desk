from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evidence_vault.ingest import IngestMetadata, ingest_path, successful as ingest_successful
from evidence_vault.observe import append_observation_path, successful as observe_successful
from evidence_vault.validation import validate_vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evidence-vault", description="Maintain a trustworthy local Evidence Vault")
    parser.add_argument("--vault", type=Path, default=Path.cwd(), help="vault repository root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="ingest a file or directory")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--title")
    ingest.add_argument("--creator")
    ingest.add_argument("--published", dest="publication_date")
    ingest.add_argument("--url", dest="canonical_url")
    ingest.add_argument("--language")

    observe = subparsers.add_parser(
        "observe",
        help="append a validated temporal observation (never rewrites an existing observation_id)",
    )
    observe.add_argument("path", type=Path, help="JSON observation document")

    subparsers.add_parser("validate", help="validate canonical vault artifacts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vault_root = args.vault.resolve()
    if args.command == "ingest":
        metadata = IngestMetadata(
            title=args.title,
            creator=args.creator,
            publication_date=args.publication_date,
            canonical_url=args.canonical_url,
            language=args.language,
        )
        results = ingest_path(vault_root, args.path, metadata)
        payload = {
            "operation": "ingest",
            "success": ingest_successful(results),
            "results": [result.to_dict() for result in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["success"] else 1
    if args.command == "observe":
        result = append_observation_path(vault_root, args.path)
        payload = {
            "operation": "observe",
            "success": observe_successful([result]),
            "results": [result.to_dict()],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["success"] else 1
    report = validate_vault(vault_root)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.valid else 1


if __name__ == "__main__":
    sys.exit(main())
