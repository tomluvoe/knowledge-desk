from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evidence_vault import read_api
from evidence_vault.errors import EvidenceVaultError


def create_mcp_server(
    vault_root: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Create a read-only FastMCP server bound to a vault root.

    The server only exposes query helpers. It never writes observations, wiki,
    or queue proposals. Index rebuild may occur lazily for search if the
    disposable index is missing (derived state only).
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise EvidenceVaultError("mcp package is not installed; run `uv sync --locked`") from exc

    root = (vault_root or Path(os.environ.get("EVIDENCE_VAULT_ROOT", Path.cwd()))).resolve()

    server = FastMCP(
        name="evidence-vault",
        instructions=(
            "Read-only Evidence Vault MCP. Prefer sources and observations over wiki synthesis. "
            "Never treat missing evidence as neutral; status unknown means insufficient evidence. "
            "Do not present agent_inference as explicit_statement. "
            f"Vault root: {root}"
        ),
        host=host,
        port=port,
    )

    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @server.tool(name="search", description="Full-text search across source, observation, wiki, and memory layers")
    def search(
        query: str,
        layer: str | None = None,
        subject: str | None = None,
        topic: str | None = None,
        source_id: str | None = None,
        limit: int = 20,
    ) -> str:
        return _json(
            read_api.search(
                root,
                query,
                layer=layer,
                subject=subject,
                topic=topic,
                source_id=source_id,
                limit=limit,
            )
        )

    @server.tool(name="get_source", description="Get source manifest and normalized text by source_id")
    def get_source(source_id: str) -> str:
        return _json(read_api.get_source(root, source_id))

    @server.tool(name="get_evidence", description="Validate an evidence locator and report resolution status")
    def get_evidence(
        source_id: str,
        source_hash: str,
        normalized_path: str,
        locator_kind: str,
        selector_json: str,
        quote_sha256: str | None = None,
    ) -> str:
        try:
            selector = json.loads(selector_json)
        except json.JSONDecodeError as exc:
            return _json({"success": False, "message": f"invalid selector_json: {exc}"})
        locator: dict[str, Any] = {
            "source_id": source_id,
            "source_hash": source_hash,
            "normalized_path": normalized_path,
            "locator_kind": locator_kind,
            "selector": selector,
        }
        if quote_sha256:
            locator["quote_sha256"] = quote_sha256
        return _json(read_api.get_evidence(root, locator))

    @server.tool(name="get_entity", description="Get entity wiki page and related observations")
    def get_entity(entity_ref: str) -> str:
        return _json(read_api.get_entity(root, entity_ref))

    @server.tool(name="get_topic", description="Get topic wiki page and related observations")
    def get_topic(topic_ref: str) -> str:
        return _json(read_api.get_topic(root, topic_ref))

    @server.tool(name="get_synthesis", description="Get a wiki synthesis page by wiki_id or path")
    def get_synthesis(wiki_id_or_path: str) -> str:
        return _json(read_api.get_synthesis(root, wiki_id_or_path))

    @server.tool(name="get_observations", description="List or fetch temporal observations")
    def get_observations(
        subject: str | None = None,
        topic: str | None = None,
        source_id: str | None = None,
        observation_id: str | None = None,
        limit: int = 20,
    ) -> str:
        return _json(
            read_api.get_observations(
                root,
                subject=subject,
                topic=topic,
                source_id=source_id,
                observation_id=observation_id,
                limit=limit,
            )
        )

    @server.tool(name="get_perspective_at", description="Supported perspective for subject+topic as of a date")
    def get_perspective_at(subject: str, topic: str, as_of: str) -> str:
        return _json(read_api.get_perspective_at(root, subject, topic, as_of))

    @server.tool(name="get_perspective_timeline", description="Timeline of observation changes for subject+topic")
    def get_perspective_timeline(
        subject: str,
        topic: str,
        start: str | None = None,
        end: str | None = None,
    ) -> str:
        return _json(read_api.get_perspective_timeline(root, subject, topic, start=start, end=end))

    @server.tool(
        name="compare_perspectives",
        description="Compare two or more subjects on topics as of a date (dimensional, no opaque score)",
    )
    def compare_perspectives(subjects_csv: str, topics_csv: str, as_of: str) -> str:
        subjects = [part.strip() for part in subjects_csv.split(",") if part.strip()]
        topics = [part.strip() for part in topics_csv.split(",") if part.strip()]
        try:
            return _json(read_api.compare_perspectives_api(root, subjects, topics, as_of))
        except EvidenceVaultError as exc:
            return _json({"success": False, "message": str(exc)})

    @server.tool(name="explore_gaps", description="List sources missing observation and/or wiki coverage")
    def explore_gaps(source_id: str | None = None, topic: str | None = None) -> str:
        return _json(read_api.explore_gaps_api(root, source_id=source_id, topic=topic))

    @server.tool(
        name="explore_ask",
        description="Evidence-first Q&A from sources; returns insufficient_evidence when unsupported",
    )
    def explore_ask(question: str, limit: int = 5) -> str:
        return _json(read_api.explore_ask_api(root, question, limit=limit))

    return server


def run_mcp_server(
    vault_root: Path,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    server = create_mcp_server(vault_root, host=host, port=port)
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise EvidenceVaultError(f"unsupported MCP transport: {transport}")
    server.run(transport=transport)
