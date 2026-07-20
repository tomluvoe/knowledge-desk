from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evidence_vault.errors import EvidenceVaultError
from evidence_vault.index import rebuild_index, search_index
from evidence_vault.ingest import IngestMetadata, ingest_path, successful as ingest_successful
from evidence_vault.observe import append_observation_path, successful as observe_successful
from evidence_vault.observations import get_observation, list_observations_result, parse_observation_query
from evidence_vault.perspective import perspective_at, perspective_timeline
from evidence_vault.validation import validate_vault
from evidence_vault.youtube_transcript import (
    fetch_and_ingest_youtube_transcript,
    fetch_youtube_transcript,
)


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

    observations_sub.add_parser("relations", help="list outgoing relation edges for all observations")

    perspective = subparsers.add_parser("perspective", help="temporal perspective queries over observations")
    perspective_sub = perspective.add_subparsers(dest="perspective_command", required=True)

    perspective_at_cmd = perspective_sub.add_parser(
        "at",
        help="supported perspective for a subject+topic as of a date (unknown if insufficient evidence)",
    )
    perspective_at_cmd.add_argument("--subject", required=True, help="subject ref_id or label substring")
    perspective_at_cmd.add_argument("--topic", required=True, help="topic ref_id or label substring")
    perspective_at_cmd.add_argument(
        "--as-of",
        required=True,
        dest="as_of",
        help="date (YYYY-MM-DD) or RFC3339 datetime",
    )

    perspective_timeline_cmd = perspective_sub.add_parser(
        "timeline",
        help="timeline of meaningful observation changes for a subject+topic",
    )
    perspective_timeline_cmd.add_argument("--subject", required=True)
    perspective_timeline_cmd.add_argument("--topic", required=True)
    perspective_timeline_cmd.add_argument("--from", dest="start", help="range start date/datetime")
    perspective_timeline_cmd.add_argument("--to", dest="end", help="range end date/datetime")

    fetch_transcript = subparsers.add_parser(
        "fetch-transcript",
        help="download a YouTube transcript as plain Markdown under inbox/ (optional --ingest)",
    )
    fetch_transcript.add_argument("url", help="YouTube URL or 11-character video id")
    fetch_transcript.add_argument(
        "--out",
        type=Path,
        help="output path (default: inbox/youtube-<video-id>.md)",
    )
    fetch_transcript.add_argument(
        "--language",
        action="append",
        dest="languages",
        help="preferred caption language code; repeatable, descending priority (default: en)",
    )
    fetch_transcript.add_argument("--title", help="optional document title")
    fetch_transcript.add_argument("--creator", help="optional creator for ingest metadata")
    fetch_transcript.add_argument(
        "--no-timestamps",
        action="store_true",
        help="omit [mm:ss] prefixes from transcript lines",
    )
    fetch_transcript.add_argument(
        "--ingest",
        action="store_true",
        help="after writing the transcript file, run evidence-vault ingest on it",
    )

    index = subparsers.add_parser("index", help="manage the disposable rebuildable search index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_sub.add_parser("rebuild", help="rebuild SQLite FTS index from canonical vault content")

    search = subparsers.add_parser("search", help="full-text search over the disposable index")
    search.add_argument("query", help="FTS5 query string")
    search.add_argument("--layer", choices=["source", "observation", "wiki", "memory"])
    search.add_argument("--subject", help="filter by subject ref_id substring")
    search.add_argument("--topic", help="filter by topic ref_id substring")
    search.add_argument("--source-id", dest="source_id")
    search.add_argument("--epistemic-class", dest="epistemic_class")
    search.add_argument("--orientation")
    search.add_argument("--limit", type=int, default=20)

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
    if args.command == "perspective":
        return _perspective_command(vault_root, args)
    if args.command == "fetch-transcript":
        return _fetch_transcript_command(vault_root, args)
    if args.command == "index":
        return _index_command(vault_root, args)
    if args.command == "search":
        return _search_command(vault_root, args)
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


def _perspective_command(vault_root: Path, args: argparse.Namespace) -> int:
    try:
        if args.perspective_command == "at":
            result = perspective_at(vault_root, args.subject, args.topic, args.as_of)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status != "unknown" else 2
        if args.perspective_command == "timeline":
            result = perspective_timeline(vault_root, args.subject, args.topic, start=args.start, end=args.end)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status != "unknown" else 2
    except EvidenceVaultError as exc:
        print(
            json.dumps(
                {
                    "operation": f"perspective.{args.perspective_command}",
                    "success": False,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    return 2


def _index_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.index_command == "rebuild":
        result = rebuild_index(vault_root)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "rebuilt" else 1
    return 2


def _search_command(vault_root: Path, args: argparse.Namespace) -> int:
    result = search_index(
        vault_root,
        args.query,
        layer=args.layer,
        subject=args.subject,
        topic=args.topic,
        source_id=args.source_id,
        epistemic_class=args.epistemic_class,
        orientation=args.orientation,
        limit=args.limit,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.message == "ok" else 1


def _fetch_transcript_command(vault_root: Path, args: argparse.Namespace) -> int:
    languages = args.languages or ["en"]
    if args.ingest:
        result = fetch_and_ingest_youtube_transcript(
            vault_root,
            args.url,
            output_path=args.out,
            languages=languages,
            include_timestamps=not args.no_timestamps,
            title=args.title,
            creator=args.creator,
            language=languages[0] if languages else None,
        )
    else:
        result = fetch_youtube_transcript(
            vault_root,
            args.url,
            output_path=args.out,
            languages=languages,
            include_timestamps=not args.no_timestamps,
            title=args.title,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if result.status != "created":
        return 1
    if args.ingest and not (result.ingest or {}).get("success"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
