from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evidence_vault.ingest import IngestMetadata, ingest_path, successful as ingest_successful
from evidence_vault.observe import append_observation_path, successful as observe_successful
from evidence_vault.observations import get_observation, list_observations_result, parse_observation_query
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

    observations = subparsers.add_parser("observations", help="query append-only temporal observations")
    observations_sub = observations.add_subparsers(dest="observations_command", required=True)

    obs_list = observations_sub.add_parser("list", help="list observations with optional filters")
    obs_list.add_argument("--subject", help="match subject ref_id or label substring")
    obs_list.add_argument("--topic", help="match topic ref_id or label substring")
    obs_list.add_argument("--source-id", dest="source_id", help="match evidence source_id exactly")
    obs_list.add_argument("--orientation", help="exact orientation enum value")
    obs_list.add_argument("--epistemic-class", dest="epistemic_class", help="exact epistemic_class value")
    obs_list.add_argument("--statement-basis", dest="statement_basis", help="exact statement_basis value")
    obs_list.add_argument("--id-prefix", dest="id_prefix", help="observation_id prefix")

    obs_get = observations_sub.add_parser("get", help="get one observation by observation_id")
    obs_get.add_argument("observation_id")

    obs_graph = observations_sub.add_parser("relations", help="list outgoing relation edges for all observations")

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
    if args.command == "observations":
        return _observations_command(vault_root, args)
    report = validate_vault(vault_root)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.valid else 1


def _observations_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.observations_command == "list":
        result = list_observations_result(vault_root, parse_observation_query(args))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.observations_command == "get":
        record = get_observation(vault_root, args.observation_id)
        if record is None:
            payload = {
                "operation": "observations.get",
                "success": False,
                "observation_id": args.observation_id,
                "message": "observation not found",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
        payload = {
            "operation": "observations.get",
            "success": True,
            "path": record.path,
            "observation": record.observation,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.observations_command == "relations":
        from evidence_vault.observations import relation_graph

        graph = relation_graph(vault_root)
        payload = {
            "operation": "observations.relations",
            "count": len(graph),
            "relations": graph,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
