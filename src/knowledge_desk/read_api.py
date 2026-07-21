from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.explore import explore_ask, explore_gaps
from knowledge_desk.index import rebuild_index, search_index
from knowledge_desk.observations import ObservationQuery, get_observation, list_observations
from knowledge_desk.perspective import compare_perspectives, perspective_at, perspective_timeline
from knowledge_desk.util import confined_file, normalized_content, parse_frontmatter
from knowledge_desk.validation import validate_locator


MAX_LIMIT = 50
MAX_TEXT = 4000
API_VERSION = "1.0.0"
SOURCE_ID_PATTERN = re.compile(r"^src-[0-9a-f]{24}$")
OBSERVATION_ID_PATTERN = re.compile(r"^obs-[0-9]{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")
REFERENCE_PATTERNS = {
    "entity": re.compile(r"^entity-[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "topic": re.compile(r"^topic-[a-z0-9]+(?:-[a-z0-9]+)*$"),
}


def _bound_limit(limit: int | None, default: int = 20) -> int:
    value = default if limit is None else int(limit)
    return max(1, min(value, MAX_LIMIT))


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def search(
    vault_root: Path,
    query: str,
    *,
    layer: str | None = None,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
    limit: int | None = 20,
    rebuild_if_missing: bool = True,
) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    bounded = _bound_limit(limit)
    result = search_index(
        vault_root,
        query,
        layer=layer,
        subject=subject,
        topic=topic,
        source_id=source_id,
        limit=bounded,
    )
    if result.message != "ok" and "index missing" in result.message and rebuild_if_missing:
        rebuild = rebuild_index(vault_root)
        if rebuild.status == "rebuilt":
            result = search_index(
                vault_root,
                query,
                layer=layer,
                subject=subject,
                topic=topic,
                source_id=source_id,
                limit=bounded,
            )
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = (
        f"{result.count} hit(s) for {query!r}"
        if result.message == "ok"
        else result.message
    )
    return payload


def get_source(vault_root: Path, source_id: str) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "message": "invalid source_id",
        }
    sources_root = vault_root / "sources"
    source_root = sources_root / source_id
    manifest_path = confined_file(sources_root, source_root / "manifest.json")
    if manifest_path is None:
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "message": "source not found",
        }
    manifest = _load_json(manifest_path)
    if manifest is None:
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "message": "source not found",
        }
    expected_normalized = f"sources/{source_id}/normalized.md"
    if manifest.get("source_id") != source_id or manifest.get("normalized_path") != expected_normalized:
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "manifest": manifest,
            "message": "source manifest identity or normalized_path is invalid",
        }
    note_path = confined_file(source_root, vault_root / expected_normalized)
    if note_path is None:
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "manifest": manifest,
            "message": "normalized note is missing or outside its source directory",
        }
    body = ""
    try:
        _, note_body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
        body = normalized_content(note_body)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            "api_version": API_VERSION,
            "success": False,
            "source_id": source_id,
            "manifest": manifest,
            "message": f"normalized note unreadable: {exc}",
        }
    return {
        "api_version": API_VERSION,
        "success": True,
        "source_id": source_id,
        "manifest": manifest,
        "normalized_path": note_path.relative_to(vault_root).as_posix(),
        "text": _clip(body),
        "summary": f"Source {source_id}: {manifest.get('title') or source_id}",
    }


def get_evidence(vault_root: Path, locator: dict[str, Any]) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    errors = validate_locator(vault_root, locator)
    if errors:
        return {
            "api_version": API_VERSION,
            "success": False,
            "locator": locator,
            "errors": errors,
            "message": "evidence locator does not resolve",
        }
    # Re-resolve selected text for the response (validate_locator already checked integrity).
    source = get_source(vault_root, str(locator.get("source_id")))
    return {
        "api_version": API_VERSION,
        "success": True,
        "locator": locator,
        "source_title": (source.get("manifest") or {}).get("title") if source.get("success") else None,
        "statement_basis_hint": "Resolve quote_sha256 and selector against immutable normalized content",
        "summary": f"Evidence from {locator.get('source_id')} via {locator.get('locator_kind')}",
        "message": "locator validated against immutable source",
    }


def get_entity(vault_root: Path, entity_ref: str) -> dict[str, Any]:
    return _get_wiki_or_observations(vault_root, kind="entity", ref=entity_ref)


def get_topic(vault_root: Path, topic_ref: str) -> dict[str, Any]:
    return _get_wiki_or_observations(vault_root, kind="topic", ref=topic_ref)


def get_synthesis(vault_root: Path, wiki_id_or_path: str) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    path = _find_wiki_path(vault_root, wiki_id_or_path)
    if path is None:
        return {
            "api_version": API_VERSION,
            "success": False,
            "query": wiki_id_or_path,
            "message": "wiki synthesis page not found",
        }
    try:
        metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            "api_version": API_VERSION,
            "success": False,
            "query": wiki_id_or_path,
            "message": f"unreadable wiki page: {exc}",
        }
    return {
        "api_version": API_VERSION,
        "success": True,
        "path": path.relative_to(vault_root).as_posix(),
        "metadata": metadata,
        "body": _clip(body),
        "summary": f"Wiki {metadata.get('wiki_id') or path.stem}: {metadata.get('title') or path.stem}",
        "epistemic_note": "Wiki prose is revisable synthesis, not primary evidence",
    }


def get_observations(
    vault_root: Path,
    *,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
    observation_id: str | None = None,
    limit: int | None = 20,
) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    if observation_id:
        if not OBSERVATION_ID_PATTERN.fullmatch(observation_id):
            return {
                "api_version": API_VERSION,
                "success": False,
                "observation_id": observation_id,
                "message": "invalid observation_id",
            }
        record = get_observation(vault_root, observation_id)
        if record is None:
            return {
                "api_version": API_VERSION,
                "success": False,
                "observation_id": observation_id,
                "message": "observation not found",
            }
        return {
            "api_version": API_VERSION,
            "success": True,
            "count": 1,
            "observations": [_observation_payload(record.observation, record.path)],
            "summary": f"Observation {observation_id}",
        }
    records = list_observations(
        vault_root,
        ObservationQuery(subject=subject, topic=topic, source_id=source_id),
    )
    bounded = _bound_limit(limit)
    sliced = records[:bounded]
    return {
        "api_version": API_VERSION,
        "success": True,
        "count": len(sliced),
        "total_matched": len(records),
        "observations": [_observation_payload(r.observation, r.path) for r in sliced],
        "summary": f"{len(sliced)} observation(s) (of {len(records)} matched)",
    }


def get_perspective_at(
    vault_root: Path,
    subject: str,
    topic: str,
    as_of: str,
) -> dict[str, Any]:
    result = perspective_at(vault_root, subject, topic, as_of)
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = (
        f"Perspective for {subject}/{topic} as of {as_of}: {result.status} "
        f"({result.orientation or 'no orientation'})"
    )
    if result.statement_basis:
        payload["statement_basis_note"] = (
            f"Primary statement_basis is {result.statement_basis}; "
            "do not present agent_inference as explicit_statement"
        )
    return payload


def get_perspective_timeline(
    vault_root: Path,
    subject: str,
    topic: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    result = perspective_timeline(vault_root, subject, topic, start=start, end=end)
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = f"Timeline {subject}/{topic}: {result.status}, {len(result.events)} event(s)"
    return payload


def compare_perspectives_api(
    vault_root: Path,
    subjects: list[str],
    topics: list[str],
    as_of: str,
) -> dict[str, Any]:
    if len(subjects) < 2:
        raise KnowledgeDeskError("compare_perspectives requires at least two subjects")
    if not topics:
        raise KnowledgeDeskError("compare_perspectives requires at least one topic")
    result = compare_perspectives(vault_root, subjects, topics[0], as_of, topics=topics)
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = (
        f"Compare {', '.join(subjects)} on {', '.join(topics)} as of {as_of}: {result.status}"
    )
    return payload


def explore_gaps_api(
    vault_root: Path,
    *,
    source_id: str | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    # Read-only: never propose from MCP.
    result = explore_gaps(vault_root, source_id=source_id, topic=topic, propose=False)
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = result.message
    return payload


def explore_ask_api(
    vault_root: Path,
    question: str,
    *,
    limit: int | None = 5,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    result = explore_ask(
        vault_root,
        question,
        limit=_bound_limit(limit, 5),
        propose=False,
        subject=subject,
        topic=topic,
        source_id=source_id,
    )
    payload = result.to_dict()
    payload["api_version"] = API_VERSION
    payload["summary"] = result.message or result.status
    return payload


def _observation_payload(observation: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "path": path,
        "observation_id": observation.get("observation_id"),
        "assertion": observation.get("assertion"),
        "epistemic_class": observation.get("epistemic_class"),
        "statement_basis": observation.get("statement_basis"),
        "orientation": observation.get("orientation"),
        "confidence": observation.get("confidence"),
        "freshness": observation.get("freshness"),
        "subjects": observation.get("subjects"),
        "topics": observation.get("topics"),
        "evidence": observation.get("evidence"),
        "relations": observation.get("relations"),
        "explicit_vs_inferred": observation.get("statement_basis"),
    }


def _get_wiki_or_observations(vault_root: Path, *, kind: str, ref: str) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    wiki_path = _find_wiki_by_ref(vault_root, kind=kind, ref=ref)
    if kind == "entity":
        observations = list_observations(vault_root, ObservationQuery(subject=ref))
    else:
        observations = list_observations(vault_root, ObservationQuery(topic=ref))
    wiki_payload = None
    if wiki_path is not None:
        try:
            metadata, body = parse_frontmatter(wiki_path.read_text(encoding="utf-8"))
            wiki_payload = {
                "path": wiki_path.relative_to(vault_root).as_posix(),
                "metadata": metadata,
                "body": _clip(body),
            }
        except (OSError, UnicodeDecodeError, ValueError):
            wiki_payload = None
    return {
        "api_version": API_VERSION,
        "success": wiki_payload is not None or bool(observations),
        "kind": kind,
        "ref": ref,
        "wiki": wiki_payload,
        "observations": [
            _observation_payload(record.observation, record.path) for record in observations[:MAX_LIMIT]
        ],
        "summary": (
            f"{kind} {ref}: "
            f"{'wiki page found' if wiki_payload else 'no wiki page'}; "
            f"{len(observations)} observation(s)"
        ),
        "epistemic_note": "Prefer observation evidence over wiki synthesis when they differ",
    }


def _find_wiki_path(vault_root: Path, wiki_id_or_path: str) -> Path | None:
    wiki_root = vault_root / "wiki"
    requested = Path(wiki_id_or_path)
    if not requested.is_absolute() and ".." not in requested.parts:
        candidate = confined_file(wiki_root, vault_root / requested)
        if candidate is not None:
            return candidate
    for path in _wiki_markdown_paths(wiki_root):
        if path.name == "README.md":
            continue
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if metadata.get("wiki_id") == wiki_id_or_path or path.stem == wiki_id_or_path:
            return path
    return None


def _find_wiki_by_ref(vault_root: Path, *, kind: str, ref: str) -> Path | None:
    wiki_root = vault_root / "wiki"
    directory = "entities" if kind == "entity" else "topics"
    pattern = REFERENCE_PATTERNS[kind]
    if pattern.fullmatch(ref):
        slug = ref.removeprefix(f"{kind}-")
        direct = confined_file(wiki_root, wiki_root / directory / f"{slug}.md")
        if direct is not None:
            return direct
    # Broader scan by title/ref substring.
    for path in _wiki_markdown_paths(wiki_root):
        if path.name == "README.md":
            continue
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if metadata.get("kind") != kind:
            continue
        title = str(metadata.get("title") or "")
        wiki_id = str(metadata.get("wiki_id") or "")
        if ref.casefold() in title.casefold() or ref.casefold() in wiki_id.casefold() or ref.casefold() in path.stem.casefold():
            return path
    return None


def _wiki_markdown_paths(wiki_root: Path) -> list[Path]:
    if not wiki_root.is_dir():
        return []
    paths: list[Path] = []
    for candidate in sorted(wiki_root.glob("**/*.md")):
        path = confined_file(wiki_root, candidate)
        if path is not None:
            paths.append(path)
    return paths
