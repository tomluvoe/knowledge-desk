from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from knowledge_desk.backup import backup_vault, restore_vault
from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.explore import compile_from_ask, explore_ask, explore_gaps
from knowledge_desk.index import rebuild_index, search_index
from knowledge_desk.ingest import IngestMetadata, ingest_path, successful as ingest_successful
from knowledge_desk.layout import init_vault
from knowledge_desk.lint import lint_vault
from knowledge_desk.mcp_server import run_mcp_server
from knowledge_desk.observe import append_observation_path, successful as observe_successful
from knowledge_desk.observations import get_observation, list_observations_result, parse_observation_query
from knowledge_desk.perspective import compare_perspectives, perspective_at, perspective_timeline
from knowledge_desk.composition import compose_with_vault, composition_contract
from knowledge_desk.maintain import (
    default_steps_from_env,
    last_run as maintain_last_run,
    parse_steps,
    run_maintain_cycle,
    run_maintain_loop,
)
from knowledge_desk.proposals import apply_proposal, list_proposals, reject_proposal
from knowledge_desk.subscribe import add_subscription, list_subscriptions, poll_subscriptions
from knowledge_desk.validation import validate_vault
from knowledge_desk.wiki import evolve_wiki, refine_validate_wiki
from knowledge_desk.workspace import (
    add_page as workspace_add_page,
    benchtest_workspace,
    get_workspace,
    init_workspace,
    list_workspaces,
    refine_workspace,
)
from knowledge_desk.fetch_page import fetch_and_ingest_page, fetch_page
from knowledge_desk.youtube_transcript import (
    fetch_and_ingest_youtube_transcript,
    fetch_youtube_transcript,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge-desk", description="Maintain a trustworthy local Knowledge Desk")
    parser.add_argument("--vault", type=Path, default=Path.cwd(), help="desk/vault root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_cmd = subparsers.add_parser(
        "init",
        help="create empty local data directories (sources, wiki, …) without overwriting existing data",
    )
    init_cmd.add_argument(
        "--no-readmes",
        action="store_true",
        help="do not write local README placeholders",
    )

    backup_cmd = subparsers.add_parser("backup", help="write a tar.gz archive of durable desk data")
    backup_cmd.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output archive path (e.g. backups/desk-2026-07-21.tar.gz)",
    )
    backup_cmd.add_argument(
        "--include-index",
        action="store_true",
        help="also include disposable system/.index (default: data only)",
    )

    restore_cmd = subparsers.add_parser("restore", help="restore durable desk data from a backup tar.gz")
    restore_cmd.add_argument("archive", type=Path, help="backup archive path")
    restore_cmd.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing non-empty data directories",
    )

    ingest = subparsers.add_parser("ingest", help="ingest a file or directory")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--title")
    ingest.add_argument("--creator")
    ingest.add_argument("--published", dest="publication_date")
    ingest.add_argument("--url", dest="canonical_url")
    ingest.add_argument("--language")
    ingest.add_argument(
        "--subject-ref",
        action="append",
        dest="subject_refs",
        default=[],
        help="catalog entity ref for the source; repeatable",
    )
    ingest.add_argument(
        "--topic-ref",
        action="append",
        dest="topic_refs",
        default=[],
        help="catalog topic ref for the source; repeatable",
    )
    ingest.add_argument(
        "--renormalize",
        action="store_true",
        help="explicitly create a normalization revision when adapter output changed",
    )

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

    perspective_compare_cmd = perspective_sub.add_parser(
        "compare",
        help="compare two or more subjects on a topic as of a date (dimensional, not a single score)",
    )
    perspective_compare_cmd.add_argument(
        "--subject",
        action="append",
        dest="subjects",
        required=True,
        help="subject ref_id or label (repeat; at least two)",
    )
    perspective_compare_cmd.add_argument(
        "--topic",
        action="append",
        dest="topics",
        required=True,
        help="topic ref_id or label (repeat for multi-topic dimensions)",
    )
    perspective_compare_cmd.add_argument("--as-of", required=True, dest="as_of")

    subscribe = subparsers.add_parser(
        "subscribe",
        help="YouTube channel/playlist subscriptions (poll new videos since a start date)",
    )
    subscribe_sub = subscribe.add_subparsers(dest="subscribe_command", required=True)
    sub_add = subscribe_sub.add_parser("add", help="register a channel or playlist subscription")
    sub_add.add_argument("--url", required=True, help="channel or playlist URL")
    sub_add.add_argument("--since", required=True, help="only videos on/after this date (YYYY-MM-DD)")
    sub_add.add_argument("--label", help="human label for the subscription")
    sub_add.add_argument("--subject-ref", dest="subject_ref", help="default entity ref for delta briefings")
    sub_add.add_argument("--topic-ref", dest="topic_ref", help="default topic ref for delta briefings")
    sub_add.add_argument("--language", default="en", help="caption language preference (default en)")
    subscribe_sub.add_parser("list", help="list local subscriptions")
    sub_poll = subscribe_sub.add_parser(
        "poll",
        help="poll subscriptions for new videos, fetch transcripts, ingest, write delta briefings",
    )
    sub_poll.add_argument("--id", dest="subscription_id", help="poll only this subscription_id")
    sub_poll.add_argument("--max-videos", type=int, default=10, dest="max_videos")

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
        "--subject-ref",
        action="append",
        dest="subject_refs",
        default=[],
        help="catalog entity ref when using --ingest; repeatable",
    )
    fetch_transcript.add_argument(
        "--topic-ref",
        action="append",
        dest="topic_refs",
        default=[],
        help="catalog topic ref when using --ingest; repeatable",
    )
    fetch_transcript.add_argument(
        "--no-timestamps",
        action="store_true",
        help="omit [mm:ss] prefixes from transcript lines",
    )
    fetch_transcript.add_argument(
        "--ingest",
        action="store_true",
        help="after writing the transcript file, run knowledge-desk ingest on it",
    )

    fetch_page_cmd = subparsers.add_parser(
        "fetch-page",
        help="fetch a public web page to cleaned inbox Markdown (network; optional --ingest)",
    )
    fetch_page_cmd.add_argument("url", help="http(s) URL of a public HTML page")
    fetch_page_cmd.add_argument(
        "--out",
        type=Path,
        help="output path (default: inbox/web-<host>-<hash>.md)",
    )
    fetch_page_cmd.add_argument("--title", help="optional document title override")
    fetch_page_cmd.add_argument("--creator", help="optional creator for ingest metadata")
    fetch_page_cmd.add_argument("--language", help="optional language for ingest metadata")
    fetch_page_cmd.add_argument(
        "--subject-ref",
        action="append",
        dest="subject_refs",
        default=[],
        help="catalog entity ref when using --ingest; repeatable",
    )
    fetch_page_cmd.add_argument(
        "--topic-ref",
        action="append",
        dest="topic_refs",
        default=[],
        help="catalog topic ref when using --ingest; repeatable",
    )
    fetch_page_cmd.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout seconds (default 30)",
    )
    fetch_page_cmd.add_argument(
        "--max-bytes",
        type=int,
        default=5 * 1024 * 1024,
        dest="max_bytes",
        help="max response body size in bytes (default 5 MiB)",
    )
    fetch_page_cmd.add_argument(
        "--ingest",
        action="store_true",
        help="after writing the Markdown file, run knowledge-desk ingest on it",
    )

    index = subparsers.add_parser("index", help="manage the disposable rebuildable search index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_sub.add_parser("rebuild", help="rebuild SQLite FTS index from canonical vault content")

    search = subparsers.add_parser("search", help="full-text search over the disposable index")
    search.add_argument("query", help="FTS5 query string")
    search.add_argument("--layer", choices=["source", "observation", "wiki", "memory"])
    search.add_argument("--subject", help="filter by exact subject ref_id")
    search.add_argument("--topic", help="filter by exact topic ref_id")
    search.add_argument("--source-id", dest="source_id")
    search.add_argument("--epistemic-class", dest="epistemic_class")
    search.add_argument("--orientation")
    search.add_argument("--limit", type=int, default=20)

    wiki = subparsers.add_parser("wiki", help="evolve and refine-validate the living wiki")
    wiki_sub = wiki.add_subparsers(dest="wiki_command", required=True)
    wiki_evolve = wiki_sub.add_parser(
        "evolve",
        help="compile living wiki pages (entities, topics, source summaries, comparisons, events)",
    )
    wiki_evolve.add_argument(
        "--observation",
        action="append",
        dest="observation_ids",
        help="limit to observation_id (repeatable)",
    )
    wiki_evolve.add_argument("--subject", help="limit to subject ref/label")
    wiki_evolve.add_argument("--topic", help="limit to topic ref/label")
    wiki_sub.add_parser(
        "refine-validate",
        help="vault validate plus wiki citation/orphan/duplicate findings",
    )

    explore = subparsers.add_parser(
        "explore",
        help="source-gap detection and evidence-first Q&A (proposals stay in update-queue)",
    )
    explore_sub = explore.add_subparsers(dest="explore_command", required=True)
    explore_gaps_cmd = explore_sub.add_parser(
        "gaps",
        help="list sources missing observation and/or wiki coverage",
    )
    explore_gaps_cmd.add_argument("--source-id", dest="source_id", help="limit to one source_id")
    explore_gaps_cmd.add_argument("--topic", help="limit to sources related to a topic term")
    explore_gaps_cmd.add_argument(
        "--propose",
        action="store_true",
        help="write a review-only proposal under system/update-queue/",
    )
    explore_ask_cmd = explore_sub.add_parser(
        "ask",
        help="answer from source passages with citations, or explicit insufficient evidence",
    )
    explore_ask_cmd.add_argument("question", help="natural-language question")
    explore_ask_cmd.add_argument("--limit", type=int, default=5, help="max citations (default 5)")
    explore_ask_cmd.add_argument("--subject", help="restrict to subject ref_id or label (e.g. entity-…)")
    explore_ask_cmd.add_argument("--topic", help="restrict to topic ref_id or label (e.g. topic-…)")
    explore_ask_cmd.add_argument("--source-id", dest="source_id", help="restrict to one source_id")
    explore_ask_cmd.add_argument(
        "--propose",
        action="store_true",
        help="write observation stub or open-question proposal to system/update-queue/",
    )
    explore_compile = explore_sub.add_parser(
        "compile-from-ask",
        help="evidence ask + thin/missing wiki → review-only compile proposal (not MCP auto-write)",
    )
    explore_compile.add_argument("question", help="natural-language question")
    explore_compile.add_argument("--limit", type=int, default=5)
    explore_compile.add_argument("--subject", help="subject ref_id or label for scope and wiki health")
    explore_compile.add_argument("--topic", help="topic ref_id or label for scope and wiki health")
    explore_compile.add_argument("--source-id", dest="source_id")
    explore_compile.add_argument(
        "--no-propose",
        action="store_true",
        help="classify only; do not write update-queue proposal",
    )

    mcp = subparsers.add_parser("mcp", help="read-only MCP server (stdio or network transport)")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="start the read-only MCP server")
    mcp_serve.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    mcp_serve.add_argument("--host", default="127.0.0.1", help="bind host for sse/http (default 127.0.0.1)")
    mcp_serve.add_argument("--port", type=int, default=8000, help="bind port for sse/http (default 8000)")

    proposal = subparsers.add_parser("proposal", help="list/apply/reject review-only update-queue proposals")
    proposal_sub = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_sub.add_parser("list", help="list pending proposals in system/update-queue/")
    proposal_apply = proposal_sub.add_parser("apply", help="apply a proposal under the single-writer lock")
    proposal_apply.add_argument("path", type=Path, help="proposal JSON path")
    proposal_reject = proposal_sub.add_parser("reject", help="reject and archive a proposal")
    proposal_reject.add_argument("path", type=Path)
    proposal_reject.add_argument("--reason", help="optional rejection reason")

    workspace = subparsers.add_parser(
        "workspace",
        help="user-owned memory workspaces (thesis/framework/…); not auto-evolved",
    )
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    ws_init = workspace_sub.add_parser("init", help="create memory/workspaces/<id>/")
    ws_init.add_argument("--title", required=True)
    ws_init.add_argument(
        "--kind",
        default="thesis",
        choices=sorted(["thesis", "framework", "prediction_set", "research_program", "process", "other"]),
    )
    ws_init.add_argument("--id", dest="workspace_id", help="workspace id (ws-…)")
    ws_init.add_argument("--status", default="active", choices=["active", "draft", "superseded", "archived"])
    ws_init.add_argument("--as-of", dest="as_of")
    ws_init.add_argument("--subject", action="append", dest="subjects", default=[])
    ws_init.add_argument("--topic", action="append", dest="topics", default=[])
    ws_init.add_argument("--statement", help="initial spine stance text")
    ws_init.add_argument("--observation", action="append", dest="observation_ids", default=[])
    workspace_sub.add_parser("list", help="list workspaces under memory/workspaces/")
    ws_get = workspace_sub.add_parser("get", help="show workspace spine, pages, changelog tail")
    ws_get.add_argument("--id", dest="workspace_id", required=True)
    ws_page = workspace_sub.add_parser("add-page", help="add pillar/prediction/note page")
    ws_page.add_argument("--id", dest="workspace_id", required=True)
    ws_page.add_argument("--title", required=True)
    ws_page.add_argument(
        "--page-kind",
        dest="page_kind",
        default="pillar",
        choices=["pillar", "prediction", "framework", "note", "invalidation", "other"],
    )
    ws_page.add_argument("--page-id", dest="page_id")
    ws_page.add_argument("--body", help="markdown body")
    ws_page.add_argument("--observation", action="append", dest="observation_ids", default=[])
    ws_page.add_argument("--prior", action="store_true", help="mark as intentional prior (not evidence-backed)")
    ws_refine = workspace_sub.add_parser("refine", help="explicit refine with changelog entry")
    ws_refine.add_argument("--id", dest="workspace_id", required=True)
    ws_refine.add_argument("--summary", required=True, help="changelog summary of what changed")
    ws_refine.add_argument("--page-id", dest="page_id", help="page to refine (default: spine)")
    ws_refine.add_argument("--body", help="replacement markdown body")
    ws_refine.add_argument("--title")
    ws_refine.add_argument("--status", choices=["active", "draft", "superseded", "archived"])
    ws_refine.add_argument("--as-of", dest="as_of")
    ws_refine.add_argument("--observation", action="append", dest="observation_ids", default=[])
    ws_refine.add_argument("--reason", help="why this refine (optional)")
    ws_bench = workspace_sub.add_parser(
        "benchtest",
        help="stress-test workspace claims against corpus (does not auto-mutate pages)",
    )
    ws_bench.add_argument("--id", dest="workspace_id", required=True)
    ws_bench.add_argument("--since", help="only consider observations on/after this timestamp")
    ws_bench.add_argument("--source-id", dest="source_id")
    ws_bench.add_argument(
        "--no-persist",
        action="store_true",
        help="do not write benchtests/*.json or changelog entry",
    )

    compose = subparsers.add_parser(
        "compose",
        help="cross-MCP composition: join external context with vault evidence (read-only)",
    )
    compose_sub = compose.add_subparsers(dest="compose_command", required=True)
    compose_sub.add_parser(
        "contract",
        help="print the domain-neutral cross-MCP composition contract",
    )
    compose_join = compose_sub.add_parser(
        "join",
        help="join external claims JSON with vault perspective/ask (nothing written to vault)",
    )
    compose_join.add_argument("question", help="composition question for the reasoning layer")
    compose_join.add_argument(
        "--external",
        type=Path,
        help="path to JSON file of external claims (or {\"claims\": [...]})",
    )
    compose_join.add_argument(
        "--external-json",
        dest="external_json",
        help="inline JSON string of external claims (alternative to --external)",
    )
    compose_join.add_argument("--subject", help="optional vault subject for perspective/ask scope")
    compose_join.add_argument("--topic", help="optional vault topic for perspective/ask scope")
    compose_join.add_argument("--as-of", dest="as_of", help="as-of date for perspective_at")
    compose_join.add_argument(
        "--no-ask",
        action="store_true",
        help="do not run explore_ask for vault claims",
    )
    compose_join.add_argument("--ask-limit", type=int, default=5, dest="ask_limit")

    maintain = subparsers.add_parser(
        "maintain",
        help="unattended maintainer: inbox ingest, wiki evolve, lint, index, gap proposals",
    )
    maintain_sub = maintain.add_subparsers(dest="maintain_command", required=True)
    maintain_once = maintain_sub.add_parser("once", help="run one maintenance cycle and exit")
    maintain_once.add_argument(
        "--steps",
        help="comma-separated steps (default: inbox_ingest,subscribe_poll,wiki_evolve,lint,index_rebuild,explore_gaps)",
    )
    maintain_once.add_argument("--max-inbox", type=int, dest="max_inbox", help="cap inbox files this cycle")
    maintain_once.add_argument(
        "--no-subscribe",
        action="store_true",
        help="skip YouTube subscription poll even if subscriptions exist",
    )
    maintain_once.add_argument(
        "--no-propose-gaps",
        action="store_true",
        help="run explore gaps without writing update-queue proposals",
    )
    maintain_loop = maintain_sub.add_parser("loop", help="run maintain once on an interval (container worker)")
    maintain_loop.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="seconds between cycles (default 300)",
    )
    maintain_loop.add_argument("--steps", help="comma-separated steps override")
    maintain_loop.add_argument("--max-inbox", type=int, dest="max_inbox")
    maintain_loop.add_argument("--max-cycles", type=int, dest="max_cycles", help="stop after N cycles (tests)")
    maintain_loop.add_argument("--no-subscribe", action="store_true")
    maintain_loop.add_argument("--no-propose-gaps", action="store_true")
    maintain_sub.add_parser("status", help="show last maintainer run from system/jobs/")

    subparsers.add_parser("validate", help="validate canonical vault artifacts")
    subparsers.add_parser(
        "lint",
        help="structured semantic/structural findings (review suggestions; does not auto-fix)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vault_root = args.vault.resolve()
    if args.command == "init":
        result = init_vault(vault_root, write_readmes=not args.no_readmes)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "initialized" else 1
    if args.command == "backup":
        result = backup_vault(vault_root, args.out, include_index=args.include_index)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "created" else 1
    if args.command == "restore":
        result = restore_vault(vault_root, args.archive, force=args.force)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "restored" else 1
    if args.command == "ingest":
        metadata = IngestMetadata(
            title=args.title,
            creator=args.creator,
            publication_date=args.publication_date,
            canonical_url=args.canonical_url,
            language=args.language,
            subject_refs=args.subject_refs,
            topic_refs=args.topic_refs,
        )
        results = ingest_path(vault_root, args.path, metadata, renormalize=args.renormalize)
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
    if args.command == "subscribe":
        return _subscribe_command(vault_root, args)
    if args.command == "fetch-transcript":
        return _fetch_transcript_command(vault_root, args)
    if args.command == "fetch-page":
        return _fetch_page_command(vault_root, args)
    if args.command == "index":
        return _index_command(vault_root, args)
    if args.command == "search":
        return _search_command(vault_root, args)
    if args.command == "wiki":
        return _wiki_command(vault_root, args)
    if args.command == "explore":
        return _explore_command(vault_root, args)
    if args.command == "mcp":
        return _mcp_command(vault_root, args)
    if args.command == "lint":
        report = lint_vault(vault_root)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.valid else 1
    if args.command == "proposal":
        return _proposal_command(vault_root, args)
    if args.command == "maintain":
        return _maintain_command(vault_root, args)
    if args.command == "compose":
        return _compose_command(vault_root, args)
    if args.command == "workspace":
        return _workspace_command(vault_root, args)
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
        from knowledge_desk.observations import relation_graph

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
        if args.perspective_command == "compare":
            topics = args.topics or []
            result = compare_perspectives(
                vault_root,
                args.subjects,
                topics[0],
                args.as_of,
                topics=topics,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status in {"compared", "partial"} else 2
    except KnowledgeDeskError as exc:
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


def _wiki_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.wiki_command == "evolve":
        result = evolve_wiki(
            vault_root,
            observation_ids=args.observation_ids,
            subject=args.subject,
            topic=args.topic,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status in {"evolved", "noop"} else 1
    if args.wiki_command == "refine-validate":
        result = refine_validate_wiki(vault_root)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.valid else 1
    return 2


def _proposal_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.proposal_command == "list":
        print(json.dumps(list_proposals(vault_root), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.proposal_command == "apply":
        result = apply_proposal(vault_root, args.path)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "applied" else 1
    if args.proposal_command == "reject":
        result = reject_proposal(vault_root, args.path, reason=args.reason)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "rejected" else 1
    return 2


def _workspace_command(vault_root: Path, args: argparse.Namespace) -> int:
    try:
        if args.workspace_command == "init":
            result = init_workspace(
                vault_root,
                title=args.title,
                kind=args.kind,
                workspace_id=args.workspace_id,
                status=args.status,
                as_of=args.as_of,
                subject_refs=args.subjects or None,
                topic_refs=args.topics or None,
                statement=args.statement,
                observation_ids=args.observation_ids or None,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status == "created" else 1
        if args.workspace_command == "list":
            print(json.dumps(list_workspaces(vault_root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.workspace_command == "get":
            payload = get_workspace(vault_root, args.workspace_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if payload.get("success") else 1
        if args.workspace_command == "add-page":
            result = workspace_add_page(
                vault_root,
                args.workspace_id,
                title=args.title,
                page_kind=args.page_kind,
                body=args.body,
                page_id=args.page_id,
                observation_ids=args.observation_ids or None,
                prior=args.prior,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status == "created" else 1
        if args.workspace_command == "refine":
            result = refine_workspace(
                vault_root,
                args.workspace_id,
                summary=args.summary,
                page_id=args.page_id,
                body=args.body,
                title=args.title,
                observation_ids=args.observation_ids or None,
                status=args.status,
                as_of=args.as_of,
                reason=args.reason,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status == "refined" else 1
        if args.workspace_command == "benchtest":
            report = benchtest_workspace(
                vault_root,
                args.workspace_id,
                since=args.since,
                source_id=args.source_id,
                persist=not args.no_persist,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if report.get("status") == "ok" else 1
    except KnowledgeDeskError as exc:
        print(
            json.dumps(
                {
                    "operation": f"workspace.{getattr(args, 'workspace_command', '?')}",
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


def _compose_command(vault_root: Path, args: argparse.Namespace) -> int:
    try:
        if args.compose_command == "contract":
            print(json.dumps(composition_contract(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.compose_command == "join":
            external_raw: object | None = None
            if args.external is not None:
                path = args.external if args.external.is_absolute() else Path.cwd() / args.external
                external_raw = json.loads(path.read_text(encoding="utf-8"))
            elif args.external_json:
                external_raw = args.external_json
            result = compose_with_vault(
                vault_root,
                question=args.question,
                external_context=external_raw,
                subject=args.subject,
                topic=args.topic,
                as_of=args.as_of,
                include_ask=not args.no_ask,
                ask_limit=args.ask_limit,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status == "composed" else 1
    except (KnowledgeDeskError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "operation": f"compose.{getattr(args, 'compose_command', '?')}",
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


def _maintain_command(vault_root: Path, args: argparse.Namespace) -> int:
    try:
        if args.maintain_command == "status":
            print(json.dumps(maintain_last_run(vault_root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        steps = parse_steps(args.steps) if getattr(args, "steps", None) else default_steps_from_env()
        common = {
            "steps": steps,
            "max_inbox_files": getattr(args, "max_inbox", None),
            "propose_gaps": not getattr(args, "no_propose_gaps", False),
            "poll_subscriptions": not getattr(args, "no_subscribe", False),
        }
        if args.maintain_command == "once":
            result = run_maintain_cycle(vault_root, **common)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status in {"ok", "noop"} else 1
        if args.maintain_command == "loop":
            result = run_maintain_loop(
                vault_root,
                interval_seconds=args.interval,
                max_cycles=args.max_cycles,
                **common,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.status in {"ok", "noop", "partial"} else 1
    except KnowledgeDeskError as exc:
        print(
            json.dumps(
                {
                    "operation": f"maintain.{getattr(args, 'maintain_command', '?')}",
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


def _mcp_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.mcp_command != "serve":
        return 2
    try:
        run_mcp_server(
            vault_root,
            transport=args.transport,
            host=args.host,
            port=args.port,
        )
    except KnowledgeDeskError as exc:
        print(
            json.dumps(
                {"operation": "mcp.serve", "success": False, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    return 0


def _explore_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.explore_command == "gaps":
        result = explore_gaps(
            vault_root,
            source_id=args.source_id,
            topic=args.topic,
            propose=args.propose,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "ok" else 1
    if args.explore_command == "ask":
        result = explore_ask(
            vault_root,
            args.question,
            limit=args.limit,
            propose=args.propose,
            subject=args.subject,
            topic=args.topic,
            source_id=args.source_id,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status == "answered" else 2
    if args.explore_command == "compile-from-ask":
        result = compile_from_ask(
            vault_root,
            args.question,
            limit=args.limit,
            subject=args.subject,
            topic=args.topic,
            source_id=args.source_id,
            propose=not args.no_propose,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.status in {"proposed", "noop"} else 2
    return 2


def _subscribe_command(vault_root: Path, args: argparse.Namespace) -> int:
    try:
        if args.subscribe_command == "add":
            result = add_subscription(
                vault_root,
                args.url,
                since=args.since,
                label=args.label,
                subject_ref=args.subject_ref,
                topic_ref=args.topic_ref,
                language=args.language,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.get("status") == "created" else 1
        if args.subscribe_command == "list":
            print(json.dumps(list_subscriptions(vault_root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.subscribe_command == "poll":
            result = poll_subscriptions(
                vault_root,
                subscription_id=args.subscription_id,
                max_videos=args.max_videos,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result.get("status") == "ok" else 1
    except KnowledgeDeskError as exc:
        print(
            json.dumps(
                {"operation": f"subscribe.{args.subscribe_command}", "success": False, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    return 2


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
            subject_refs=args.subject_refs,
            topic_refs=args.topic_refs,
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


def _fetch_page_command(vault_root: Path, args: argparse.Namespace) -> int:
    if args.ingest:
        result = fetch_and_ingest_page(
            vault_root,
            args.url,
            output_path=args.out,
            title=args.title,
            creator=args.creator,
            language=args.language,
            subject_refs=args.subject_refs,
            topic_refs=args.topic_refs,
            timeout_seconds=args.timeout,
            max_bytes=args.max_bytes,
        )
    else:
        result = fetch_page(
            vault_root,
            args.url,
            output_path=args.out,
            title=args.title,
            timeout_seconds=args.timeout,
            max_bytes=args.max_bytes,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if result.status != "created":
        return 1
    if args.ingest and not (result.ingest or {}).get("success"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
