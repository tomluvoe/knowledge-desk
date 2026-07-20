from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from evidence_vault.util import (
    SCHEMA_VERSION,
    normalized_content,
    parse_frontmatter,
    sha256_file,
    source_id_for_hash,
)


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: list[str]
    checked: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {"valid": self.valid, "errors": self.errors, "checked": self.checked}


def load_schema(vault_root: Path, name: str) -> dict[str, Any]:
    return json.loads((vault_root / "system" / "schemas" / name).read_text(encoding="utf-8"))


def schema_errors(instance: object, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda item: "/".join(map(str, item.absolute_path)))
    ]


def validate_source_artifacts(vault_root: Path, artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = artifact_dir / "manifest.json"
    note_path = artifact_dir / "normalized.md"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["manifest.json: missing"]
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest.json: unreadable ({type(exc).__name__})"]
    errors.extend(f"manifest {message}" for message in schema_errors(manifest, load_schema(vault_root, "source-manifest.schema.json")))
    if not isinstance(manifest, dict):
        return errors + ["manifest root must be an object"]

    source_id = manifest.get("source_id")
    digest_value = manifest.get("content_hash", "")
    digest = digest_value.removeprefix("sha256:") if isinstance(digest_value, str) else ""
    if source_id != source_id_for_hash(digest):
        errors.append("manifest source_id is not derived from content_hash")
    if artifact_dir.name != source_id:
        errors.append("artifact directory name does not equal source_id")
    original_dir = artifact_dir / "original"
    if not original_dir.is_dir():
        errors.append("original/: missing directory")
        originals: list[Path] = []
    else:
        originals = [path for path in original_dir.iterdir() if path.is_file()]
    if len(originals) != 1:
        errors.append("source must contain exactly one immutable original file")
    elif sha256_file(originals[0]) != digest:
        errors.append("immutable original content hash does not match manifest")
    if len(originals) == 1:
        expected_original_path = f"sources/{source_id}/original/{originals[0].name}"
        if manifest.get("original_path") != expected_original_path:
            errors.append("manifest original_path does not identify the stored original")
    if manifest.get("normalized_path") != f"sources/{source_id}/normalized.md":
        errors.append("manifest normalized_path does not identify the stored normalized note")

    try:
        note_text = note_path.read_text(encoding="utf-8")
        note_metadata, note_body = parse_frontmatter(note_text)
        normalized_content(note_body)
    except FileNotFoundError:
        errors.append("normalized.md: missing")
        return errors
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(f"normalized.md: unreadable or incomplete ({type(exc).__name__})")
        return errors
    errors.extend(
        f"normalized note {message}"
        for message in schema_errors(note_metadata, load_schema(vault_root, "normalized-source-note.schema.json"))
    )
    shared_keys = set(note_metadata)
    for key in shared_keys:
        if manifest.get(key) != note_metadata[key]:
            errors.append(f"normalized note metadata {key!r} differs from manifest")

    status = manifest.get("extraction_status")
    media_type = manifest.get("media_type")
    content = normalized_content(note_body)
    if status == "needs_ocr":
        if media_type != "application/pdf":
            errors.append("needs_ocr is only valid for PDF sources")
        if not any("OCR" in warning or "ocr" in warning for warning in manifest.get("warnings", [])):
            errors.append("needs_ocr source must explain the OCR requirement in warnings")
    if media_type == "application/pdf":
        page_count = manifest.get("page_count")
        markers = re.findall(r"<!-- ev-page:([1-9][0-9]*) -->", content)
        expected = [str(index) for index in range(1, int(page_count or 0) + 1)]
        if markers != expected:
            errors.append("PDF normalized note does not contain exactly one ordered marker per page")
    elif status == "complete" and not content and originals and originals[0].stat().st_size:
        errors.append("complete text source has empty normalized content")
    return errors


def validate_vault(vault_root: Path) -> ValidationReport:
    vault_root = vault_root.resolve()
    errors: list[str] = []
    checked = {
        "schemas": 0,
        "examples": 0,
        "sources": 0,
        "observations": 0,
        "wiki_notes": 0,
        "memory_records": 0,
        "domain_packs": 0,
        "logs": 0,
    }
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted((vault_root / "system" / "schemas").glob("*.schema.json")):
        checked["schemas"] += 1
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            schemas[path.name] = schema
        except (OSError, json.JSONDecodeError, SchemaError) as exc:
            errors.append(f"{path.relative_to(vault_root)}: invalid schema: {exc}")

    example_map = {
        "source-manifest.example.json": "source-manifest.schema.json",
        "evidence-locator.example.json": "evidence-locator.schema.json",
        "reference.example.json": "reference.schema.json",
        "reference-topic.example.json": "reference.schema.json",
        "observation-ecology.example.json": "observation.schema.json",
        "observation-history.example.json": "observation.schema.json",
        "wiki-synthesis.example.json": "wiki-synthesis.schema.json",
        "memory-record.example.json": "memory-record.schema.json",
        "memory-conclusion.example.json": "memory-record.schema.json",
        "memory-decision.example.json": "memory-record.schema.json",
        "ingest-log-entry.example.json": "ingest-log-entry.schema.json",
        "domain-pack.example.json": "domain-pack.schema.json",
    }
    for filename, schema_name in example_map.items():
        path = vault_root / "system" / "examples" / filename
        checked["examples"] += 1
        try:
            instance = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.relative_to(vault_root)}: unreadable example: {exc}")
            continue
        if schema_name in schemas:
            errors.extend(
                f"{path.relative_to(vault_root)} {message}" for message in schema_errors(instance, schemas[schema_name])
            )

    normalized_example = vault_root / "system" / "examples" / "normalized-source-note.example.md"
    checked["examples"] += 1
    try:
        metadata, body = parse_frontmatter(normalized_example.read_text(encoding="utf-8"))
        normalized_content(body)
        if "normalized-source-note.schema.json" in schemas:
            errors.extend(
                f"{normalized_example.relative_to(vault_root)} {message}"
                for message in schema_errors(metadata, schemas["normalized-source-note.schema.json"])
            )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(f"{normalized_example.relative_to(vault_root)}: unreadable example: {exc}")

    source_ids: set[str] = set()
    source_manifests: list[tuple[Path, dict[str, Any]]] = []
    for artifact_dir in sorted((vault_root / "sources").glob("src-*")):
        if not artifact_dir.is_dir():
            continue
        checked["sources"] += 1
        source_id = artifact_dir.name
        if source_id in source_ids:
            errors.append(f"duplicate source ID: {source_id}")
        source_ids.add(source_id)
        errors.extend(f"{artifact_dir.relative_to(vault_root)}: {message}" for message in validate_source_artifacts(vault_root, artifact_dir))
        try:
            source_manifests.append(
                (artifact_dir / "manifest.json", json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8")))
            )
        except (OSError, json.JSONDecodeError):
            pass
    for manifest_path, manifest in source_manifests:
        revision_of = manifest.get("revision_of")
        if revision_of is not None and revision_of not in source_ids:
            errors.append(f"{manifest_path.relative_to(vault_root)}: revision_of target does not exist: {revision_of}")

    observation_ids: set[str] = set()
    observations: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((vault_root / "observations").glob("**/*.json")):
        checked["observations"] += 1
        try:
            observation = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.relative_to(vault_root)}: unreadable observation: {exc}")
            continue
        if "observation.schema.json" in schemas:
            errors.extend(
                f"{path.relative_to(vault_root)} {message}"
                for message in schema_errors(observation, schemas["observation.schema.json"])
            )
        if not isinstance(observation, dict):
            errors.append(f"{path.relative_to(vault_root)}: observation root must be an object")
            continue
        observation_id = observation.get("observation_id")
        if observation_id in observation_ids:
            errors.append(f"duplicate observation ID: {observation_id}")
        if isinstance(observation_id, str):
            observation_ids.add(observation_id)
        observations.append((path, observation))
        for locator in observation.get("evidence", []):
            if isinstance(locator, dict):
                errors.extend(f"{path.relative_to(vault_root)}: {message}" for message in validate_locator(vault_root, locator))
        horizon = observation.get("horizon")
        if isinstance(horizon, dict) and horizon.get("start") and horizon.get("end") and horizon["end"] < horizon["start"]:
            errors.append(f"{path.relative_to(vault_root)}: horizon end precedes start")
    observation_edges: list[tuple[str, str]] = []
    for path, observation in observations:
        source_observation = observation.get("observation_id")
        for relation in observation.get("relations", []):
            if not isinstance(relation, dict):
                continue
            target = relation.get("observation_id")
            if not isinstance(target, str):
                continue
            if target not in observation_ids:
                errors.append(f"{path.relative_to(vault_root)}: relation target does not exist: {target}")
                continue
            if isinstance(source_observation, str) and target == source_observation:
                errors.append(f"{path.relative_to(vault_root)}: relation cannot target the same observation")
                continue
            if isinstance(source_observation, str):
                observation_edges.append((source_observation, target))
    for cycle in _directed_cycles(observation_edges):
        errors.append(f"observation relation cycle: {' -> '.join(cycle)}")

    wiki_ids: set[str] = set()
    for path in sorted((vault_root / "wiki").glob("**/*.md")):
        if path.name == "README.md":
            continue
        checked["wiki_notes"] += 1
        record = _validate_markdown_record(vault_root, path, schemas.get("wiki-synthesis.schema.json"), errors)
        if not record:
            continue
        wiki_id = record.get("wiki_id")
        if wiki_id in wiki_ids:
            errors.append(f"{path.relative_to(vault_root)}: duplicate wiki ID {wiki_id}")
        if isinstance(wiki_id, str):
            wiki_ids.add(wiki_id)
        for observation_id in record.get("observation_ids", []):
            if observation_id not in observation_ids:
                errors.append(f"{path.relative_to(vault_root)}: observation target does not exist: {observation_id}")
        for locator in record.get("evidence", []):
            if isinstance(locator, dict):
                errors.extend(f"{path.relative_to(vault_root)}: {message}" for message in validate_locator(vault_root, locator))

    memory_ids: set[str] = set()
    memory_records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((vault_root / "memory").glob("**/*.md")):
        if path.name == "README.md":
            continue
        checked["memory_records"] += 1
        record = _validate_markdown_record(vault_root, path, schemas.get("memory-record.schema.json"), errors)
        if not record:
            continue
        memory_id = record.get("memory_id")
        if memory_id in memory_ids:
            errors.append(f"{path.relative_to(vault_root)}: duplicate memory ID {memory_id}")
        if isinstance(memory_id, str):
            memory_ids.add(memory_id)
        memory_records.append((path, record))
        for observation_id in record.get("observation_ids", []):
            if observation_id not in observation_ids:
                errors.append(f"{path.relative_to(vault_root)}: observation target does not exist: {observation_id}")
        for locator in record.get("evidence", []):
            if isinstance(locator, dict):
                errors.extend(f"{path.relative_to(vault_root)}: {message}" for message in validate_locator(vault_root, locator))
    memory_edges: list[tuple[str, str]] = []
    for path, record in memory_records:
        supersedes = record.get("supersedes")
        memory_id = record.get("memory_id")
        if supersedes is None:
            continue
        if supersedes not in memory_ids:
            errors.append(f"{path.relative_to(vault_root)}: supersedes target does not exist: {supersedes}")
            continue
        if isinstance(memory_id, str) and supersedes == memory_id:
            errors.append(f"{path.relative_to(vault_root)}: supersedes cannot target the same memory record")
            continue
        if isinstance(memory_id, str) and isinstance(supersedes, str):
            memory_edges.append((memory_id, supersedes))
    for cycle in _directed_cycles(memory_edges):
        errors.append(f"memory supersession cycle: {' -> '.join(cycle)}")

    domain_ids: set[str] = set()
    namespaces: set[str] = set()
    for path in sorted((vault_root / "domains").glob("*/manifest.json")):
        checked["domain_packs"] += 1
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.relative_to(vault_root)}: unreadable domain pack: {exc}")
            continue
        if "domain-pack.schema.json" in schemas:
            errors.extend(
                f"{path.relative_to(vault_root)} {message}"
                for message in schema_errors(pack, schemas["domain-pack.schema.json"])
            )
        if not isinstance(pack, dict):
            errors.append(f"{path.relative_to(vault_root)}: domain-pack root must be an object")
            continue
        domain_id = pack.get("domain_id")
        namespace = pack.get("namespace")
        if isinstance(domain_id, str):
            if domain_id in domain_ids:
                errors.append(f"{path.relative_to(vault_root)}: duplicate domain ID {domain_id}")
            domain_ids.add(domain_id)
        if isinstance(namespace, str):
            if namespace in namespaces:
                errors.append(f"{path.relative_to(vault_root)}: duplicate domain namespace {namespace}")
            namespaces.add(namespace)
        extension_schemas = pack.get("extension_schemas", [])
        if isinstance(extension_schemas, list):
            for relative_schema in extension_schemas:
                if isinstance(relative_schema, str) and not (vault_root / relative_schema).is_file():
                    errors.append(f"{path.relative_to(vault_root)}: extension schema does not exist: {relative_schema}")

    log_path = vault_root / "system" / "logs" / "ingest.jsonl"
    if log_path.exists():
        event_ids: set[str] = set()
        for line_number, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            checked["logs"] += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"system/logs/ingest.jsonl:{line_number}: invalid JSON: {exc}")
                continue
            if "ingest-log-entry.schema.json" in schemas:
                errors.extend(
                    f"system/logs/ingest.jsonl:{line_number} {message}"
                    for message in schema_errors(entry, schemas["ingest-log-entry.schema.json"])
                )
            if not isinstance(entry, dict):
                errors.append(f"system/logs/ingest.jsonl:{line_number}: entry root must be an object")
                continue
            event_id = entry.get("event_id")
            if event_id in event_ids:
                errors.append(f"system/logs/ingest.jsonl:{line_number}: duplicate event ID {event_id}")
            if isinstance(event_id, str):
                event_ids.add(event_id)
            source_id = entry.get("source_id")
            if isinstance(source_id, str) and not (vault_root / "sources" / source_id / "manifest.json").is_file():
                errors.append(f"system/logs/ingest.jsonl:{line_number}: source target does not exist")

    return ValidationReport(valid=not errors, errors=errors, checked=checked)


def _validate_markdown_record(
    vault_root: Path,
    path: Path,
    schema: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any] | None:
    try:
        metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        errors.append(f"{path.relative_to(vault_root)}: invalid Markdown record: {exc}")
        return None
    if not body.strip():
        errors.append(f"{path.relative_to(vault_root)}: Markdown record has no readable body")
    if schema:
        errors.extend(
            f"{path.relative_to(vault_root)} {message}" for message in schema_errors(metadata, schema)
        )
    return metadata


def validate_locator(vault_root: Path, locator: dict[str, Any]) -> list[str]:
    """Validate a standalone or embedded evidence locator.

    Embedded citations under observations, wiki notes, and memory records omit
    ``schema_version`` (the parent artifact owns the schema). Standalone locator
    documents require it. Either form is accepted here.
    """
    errors: list[str] = []
    if not isinstance(locator, dict):
        return ["evidence locator root must be an object"]

    candidate = dict(locator)
    if "schema_version" not in candidate:
        candidate["schema_version"] = SCHEMA_VERSION

    schema = load_schema(vault_root, "evidence-locator.schema.json")
    errors.extend(f"evidence locator {message}" for message in schema_errors(candidate, schema))
    if errors:
        return errors

    source_id = locator["source_id"]
    manifest_path = vault_root / "sources" / source_id / "manifest.json"
    if not manifest_path.is_file():
        return [f"evidence target source does not exist: {source_id}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"evidence target source manifest is unreadable: {exc}"]
    if not isinstance(manifest, dict):
        return ["evidence target source manifest is not an object"]
    if locator["source_hash"] != manifest.get("content_hash"):
        errors.append("evidence source_hash differs from source manifest")
    if locator["normalized_path"] != manifest.get("normalized_path"):
        errors.append("evidence normalized_path differs from source manifest")

    kind = locator["locator_kind"]
    media_type = manifest.get("media_type")
    kind_errors = _locator_kind_media_errors(kind, media_type)
    errors.extend(kind_errors)
    if kind_errors:
        return errors

    note_path = vault_root / locator["normalized_path"]
    try:
        _, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
        content = normalized_content(body)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return errors + [f"evidence normalized target is unreadable ({type(exc).__name__})"]

    selector = locator.get("selector")
    if not isinstance(selector, dict):
        return errors + ["evidence selector must be an object"]

    selected: str | None = None
    if kind == "pdf_page":
        page = selector.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            errors.append("PDF page locator selector.page must be a positive integer")
        else:
            match = re.search(
                rf"<!-- ev-page:{page} -->\s*(.*?)(?=<a id=\"page-|<!-- ev-content-end -->|\Z)",
                content,
                flags=re.DOTALL,
            )
            if not match:
                errors.append(f"PDF page locator does not resolve: {page}")
            else:
                selected = match.group(1).strip()
    elif kind == "markdown_heading":
        heading = selector.get("heading")
        occurrence = selector.get("occurrence")
        if not isinstance(heading, str) or not heading:
            errors.append("Markdown heading locator selector.heading must be a non-empty string")
        elif not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
            errors.append("Markdown heading locator selector.occurrence must be a positive integer")
        else:
            matches = _heading_sections(content, heading)
            if occurrence > len(matches):
                errors.append(f"Markdown heading locator does not resolve: {heading!r} occurrence {occurrence}")
            else:
                selected = matches[occurrence - 1]
    elif kind == "line_range":
        start, end = selector.get("start_line"), selector.get("end_line")
        if not isinstance(start, int) or isinstance(start, bool) or start < 1:
            errors.append("line-range locator selector.start_line must be a positive integer")
        elif not isinstance(end, int) or isinstance(end, bool) or end < 1:
            errors.append("line-range locator selector.end_line must be a positive integer")
        elif end < start:
            errors.append("line-range end_line precedes start_line")
        else:
            lines = [line for line in content.splitlines() if not line.startswith("<!-- ev-block")]
            if end > len(lines):
                errors.append(f"line-range locator exceeds normalized content: {start}-{end}")
            else:
                selected = "\n".join(lines[start - 1 : end])
    elif kind == "block":
        block_id = selector.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            errors.append("block locator selector.block_id must be a non-empty string")
        else:
            match = re.search(
                rf"<!-- ev-block:{re.escape(block_id)} [^>]*-->\n?(.*?)<!-- ev-block-end:{re.escape(block_id)} [^>]*-->",
                content,
                flags=re.DOTALL,
            )
            if not match:
                errors.append(f"block locator does not resolve: {block_id}")
            else:
                selected = match.group(1).strip()
    else:
        errors.append(f"unsupported evidence locator_kind: {kind!r}")

    if selected is not None and "quote_sha256" in locator:
        import hashlib

        actual = hashlib.sha256(selected.encode("utf-8")).hexdigest()
        if actual != locator["quote_sha256"]:
            errors.append("evidence quote_sha256 does not match resolved content")
    return errors


def _heading_sections(content: str, wanted: str) -> list[str]:
    lines = content.splitlines()
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    fence = ""
    for index, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            char = marker.group(1)[0]
            if not in_fence:
                in_fence, fence = True, char
            elif char == fence:
                in_fence = False
            continue
        if in_fence:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2)))
    found: list[str] = []
    for position, (line_index, level, title) in enumerate(headings):
        if title != wanted:
            continue
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        found.append("\n".join(lines[line_index:end]).strip())
    return found


# Locator kinds that are only meaningful for particular source media types.
# line_range is intentionally media-agnostic: every normalized note has lines.
LOCATOR_KIND_MEDIA_TYPES: dict[str, frozenset[str]] = {
    "pdf_page": frozenset({"application/pdf"}),
    "markdown_heading": frozenset({"text/markdown"}),
    "block": frozenset({"text/plain"}),
    "media_timestamp": frozenset(),  # no media adapter yet
}


def _locator_kind_media_errors(kind: object, media_type: object) -> list[str]:
    if not isinstance(kind, str):
        return []
    allowed = LOCATOR_KIND_MEDIA_TYPES.get(kind)
    if allowed is None:
        return []
    if kind == "media_timestamp":
        return ["media timestamp locators require a future media adapter"]
    if not isinstance(media_type, str):
        return [f"evidence locator_kind {kind!r} requires a source media_type"]
    if media_type not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        return [f"evidence locator_kind {kind!r} is incompatible with media_type {media_type!r} (allowed: {allowed_text})"]
    return []


def _directed_cycles(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Return each simple directed cycle once, as a closed node path (A -> B -> A)."""
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, [])

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in adjacency}
    path: list[str] = []
    cycles: list[list[str]] = []
    seen_cycle_keys: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in adjacency.get(node, []):
            if color.get(neighbor, WHITE) == GRAY:
                start = path.index(neighbor)
                cycle_nodes = path[start:]
                key = _cycle_key(cycle_nodes)
                if key not in seen_cycle_keys:
                    seen_cycle_keys.add(key)
                    cycles.append(cycle_nodes + [neighbor])
            elif color.get(neighbor, WHITE) == WHITE:
                visit(neighbor)
        path.pop()
        color[node] = BLACK

    for node in sorted(adjacency):
        if color[node] == WHITE:
            visit(node)
    return cycles


def _cycle_key(nodes: list[str]) -> tuple[str, ...]:
    if not nodes:
        return ()
    rotate = min(range(len(nodes)), key=lambda index: nodes[index])
    return tuple(nodes[rotate:] + nodes[:rotate])
