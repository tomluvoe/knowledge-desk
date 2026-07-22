from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from knowledge_desk.adapters import adapter_for_suffix
from knowledge_desk.adapters.base import IngestionAdapter
from knowledge_desk.errors import KnowledgeDeskError, ValidationError
from knowledge_desk.util import (
    SCHEMA_VERSION,
    confined_file,
    normalized_content,
    parse_frontmatter,
    replace_json_synced as atomic_replace_json_synced,
    replace_text_synced as atomic_replace_text_synced,
    render_frontmatter,
    safe_filename,
    sha256_file,
    sha256_text,
    source_id_for_hash,
    utc_now,
    write_json_synced,
    write_text_synced,
)


@dataclass
class IngestMetadata:
    title: str | None = None
    creator: str | None = None
    publication_date: str | None = None
    canonical_url: str | None = None
    language: str | None = None
    subject_refs: list[str] = field(default_factory=list)
    topic_refs: list[str] = field(default_factory=list)
    extensions: dict[str, object] = field(default_factory=dict)


@dataclass
class OperationResult:
    operation: str = "ingest"
    status: str = "failed"
    input_path: str = ""
    source_id: str | None = None
    content_hash: str | None = None
    manifest_path: str | None = None
    normalized_path: str | None = None
    extraction_status: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def ingest_path(
    vault_root: Path,
    input_path: Path,
    metadata: IngestMetadata,
    *,
    renormalize: bool = False,
) -> list[OperationResult]:
    vault_root = vault_root.resolve()
    input_path = input_path.resolve()
    if input_path.is_dir():
        # Non-recursive by design. Skip dotfiles (e.g. .DS_Store, .hidden.txt).
        files = sorted(
            (
                path
                for path in input_path.iterdir()
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda path: path.name.casefold(),
        )
        if not files:
            return [OperationResult(input_path=str(input_path), message="directory contains no ingestible files")]
        return [ingest_file(vault_root, path, metadata, renormalize=renormalize) for path in files]
    return [ingest_file(vault_root, input_path, metadata, renormalize=renormalize)]


def ingest_file(
    vault_root: Path,
    input_path: Path,
    metadata: IngestMetadata,
    *,
    renormalize: bool = False,
) -> OperationResult:
    from knowledge_desk.writer import vault_write_lock

    with vault_write_lock(vault_root):
        return _ingest_file_unlocked(vault_root, input_path, metadata, renormalize=renormalize)


def _ingest_file_unlocked(
    vault_root: Path,
    input_path: Path,
    metadata: IngestMetadata,
    *,
    renormalize: bool = False,
) -> OperationResult:
    vault_root = vault_root.resolve()
    input_path = input_path.resolve()
    result = OperationResult(input_path=str(input_path))
    staging_parent: Path | None = None
    try:
        if not input_path.is_file():
            raise KnowledgeDeskError(f"input is not a regular file: {input_path}")
        adapter = adapter_for_suffix(input_path.suffix)
        digest = sha256_file(input_path)
        content_hash = f"sha256:{digest}"
        source_id = source_id_for_hash(digest)
        result.source_id = source_id
        result.content_hash = content_hash

        sources_root = vault_root / "sources"
        final_dir = sources_root / source_id
        existing_manifest = final_dir / "manifest.json"
        if final_dir.exists():
            if not final_dir.is_dir() or final_dir.is_symlink():
                raise ValidationError(f"source ID collision or incomplete canonical directory: {final_dir}")
            resolved_manifest = confined_file(final_dir, existing_manifest)
            if resolved_manifest is None:
                raise ValidationError(f"source ID collision or incomplete canonical directory: {final_dir}")
            existing = json.loads(resolved_manifest.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValidationError(f"existing manifest for {source_id} is not an object")
            if existing.get("content_hash") != content_hash:
                raise ValidationError(f"source ID collision for {source_id}")
            original_path = existing.get("original_path")
            expected_prefix = f"sources/{source_id}/original/"
            if not isinstance(original_path, str) or not original_path.startswith(expected_prefix):
                raise ValidationError(f"existing manifest for {source_id} has an invalid original_path")
            original = confined_file(final_dir, vault_root / original_path)
            if original is None or sha256_file(original) != digest:
                raise ValidationError(f"existing immutable original for {source_id} failed hash verification")
            if renormalize:
                return _renormalize_source(
                    vault_root,
                    input_path,
                    adapter,
                    existing_manifest,
                    existing,
                    result,
                )
            result.status = "noop"
            result.manifest_path = existing_manifest.relative_to(vault_root).as_posix()
            result.normalized_path = existing["normalized_path"]
            result.extraction_status = existing["extraction_status"]
            result.warnings = list(existing.get("warnings", []))
            result.message = "duplicate content already ingested; no files changed"
            return result

        extracted = adapter.extract(input_path)
        now = utc_now()
        original_name = safe_filename(input_path.name)
        original_rel = f"sources/{source_id}/original/{original_name}"
        normalized_rel = f"sources/{source_id}/normalized.md"
        revision_of = _find_revision(vault_root, input_path.name, source_id)
        title = metadata.title or extracted.title or input_path.stem
        creator = metadata.creator or extracted.creator
        warnings = sorted(set(extracted.warnings))
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "source_id": source_id,
            "media_type": extracted.media_type,
            "content_hash": content_hash,
            "original_filename": input_path.name,
            "original_path": original_rel,
            "normalized_path": normalized_rel,
            "title": title,
            "creator": creator,
            "publication_date": metadata.publication_date,
            "ingested_at": now,
            "canonical_url": metadata.canonical_url,
            "language": metadata.language,
            "subject_refs": sorted(set(metadata.subject_refs)),
            "topic_refs": sorted(set(metadata.topic_refs)),
            "extraction_status": extracted.extraction_status,
            "warnings": warnings,
            "revision_of": revision_of,
            "extensions": dict(metadata.extensions),
        }
        if extracted.page_count is not None:
            manifest["page_count"] = extracted.page_count

        note_metadata = {
            key: manifest[key]
            for key in (
                "schema_version",
                "source_id",
                "content_hash",
                "media_type",
                "title",
                "creator",
                "publication_date",
                "ingested_at",
                "canonical_url",
                "language",
                "subject_refs",
                "topic_refs",
                "extraction_status",
                "warnings",
            )
        }
        if extracted.page_count is not None:
            note_metadata["page_count"] = extracted.page_count
        normalized_note = render_frontmatter(note_metadata) + "\n" + extracted.markdown_body
        normalized_hash = f"sha256:{sha256_text(normalized_note)}"
        normalization_revision = _normalization_revision(
            adapter=adapter,
            normalized_path=normalized_rel,
            normalized_hash=normalized_hash,
            created_at=now,
            supersedes=None,
            extraction_status=extracted.extraction_status,
            warnings=warnings,
            page_count=extracted.page_count,
        )
        manifest["normalized_hash"] = normalized_hash
        manifest["normalization"] = {
            "current_revision": normalization_revision["revision_id"],
            "revisions": [normalization_revision],
        }

        staging_root = vault_root / "system" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(tempfile.mkdtemp(prefix="ingest-", dir=staging_root))
        staged_source = staging_parent / source_id
        staged_original = staged_source / "original" / original_name
        staged_original.parent.mkdir(parents=True)
        shutil.copyfile(input_path, staged_original)
        with staged_original.open("rb") as stream:
            os.fsync(stream.fileno())
        write_json_synced(staged_source / "manifest.json", manifest)
        write_text_synced(staged_source / "normalized.md", normalized_note)

        from knowledge_desk.validation import validate_source_artifacts

        errors = validate_source_artifacts(vault_root, staged_source)
        if errors:
            raise ValidationError("staged artifacts failed validation: " + "; ".join(errors))

        sources_root.mkdir(parents=True, exist_ok=True)
        os.replace(staged_source, final_dir)
        _append_ingest_log(
            vault_root,
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": f"ing-{uuid.uuid4().hex}",
                "occurred_at": now,
                "operation": "ingest",
                "status": "revision" if revision_of else "created",
                "source_id": source_id,
                "content_hash": content_hash,
                "input_filename": input_path.name,
                "warnings": warnings,
            },
        )
        result.status = "revision" if revision_of else "created"
        result.manifest_path = f"sources/{source_id}/manifest.json"
        result.normalized_path = normalized_rel
        result.extraction_status = extracted.extraction_status
        result.warnings = warnings
        result.message = "canonical source artifacts created and validated"
        return result
    except (KnowledgeDeskError, OSError, ValueError, json.JSONDecodeError) as exc:
        result.message = str(exc)
        return result
    finally:
        if staging_parent and staging_parent.exists():
            shutil.rmtree(staging_parent, ignore_errors=True)


def _find_revision(vault_root: Path, original_filename: str, new_source_id: str) -> str | None:
    candidates: list[tuple[str, str]] = []
    for path in (vault_root / "sources").glob("src-*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("original_filename") == original_filename and manifest.get("source_id") != new_source_id:
            candidates.append((str(manifest.get("ingested_at", "")), str(manifest["source_id"])))
    return max(candidates)[1] if candidates else None


def _renormalize_source(
    vault_root: Path,
    input_path: Path,
    adapter: IngestionAdapter,
    manifest_path: Path,
    manifest: dict[str, object],
    result: OperationResult,
) -> OperationResult:
    extracted = adapter.extract(input_path)
    working = dict(manifest)
    history = working.get("normalization")
    had_history = isinstance(history, dict) and isinstance(history.get("revisions"), list)
    if not had_history:
        working = _with_legacy_normalization_history(vault_root, manifest_path.parent, working)
        history = working["normalization"]
    assert isinstance(history, dict)
    revisions = list(history["revisions"])
    current_revision = str(history["current_revision"])

    warnings = sorted(set(extracted.warnings))
    working["extraction_status"] = extracted.extraction_status
    working["warnings"] = warnings
    if extracted.page_count is None:
        working.pop("page_count", None)
    else:
        working["page_count"] = extracted.page_count
    note_metadata = _normalized_note_metadata(working)
    normalized_note = render_frontmatter(note_metadata) + "\n" + extracted.markdown_body
    normalized_hash = f"sha256:{sha256_text(normalized_note)}"

    if normalized_hash == working.get("normalized_hash") and not had_history:
        _validate_normalization_update(vault_root, working, normalized_note)
        _replace_json_synced(manifest_path, working)
        _append_normalization_log(vault_root, input_path, working, warnings)
        result.status = "normalization_revision"
        result.message = "existing normalization anchored with integrity metadata"
        result.manifest_path = manifest_path.relative_to(vault_root).as_posix()
        result.normalized_path = str(working["normalized_path"])
        result.extraction_status = str(working["extraction_status"])
        result.warnings = warnings
        return result

    now = utc_now()
    revision_id = _normalization_revision_id(
        normalized_hash,
        adapter=adapter,
        created_at=now,
        supersedes=current_revision,
    )
    normalized_rel = f"sources/{result.source_id}/normalizations/{revision_id}.md"
    if any(isinstance(item, dict) and item.get("revision_id") == revision_id for item in revisions):
        raise ValidationError(f"normalization revision collision for {revision_id}")
    revision = _normalization_revision(
        adapter=adapter,
        normalized_path=normalized_rel,
        normalized_hash=normalized_hash,
        created_at=now,
        supersedes=current_revision,
        extraction_status=extracted.extraction_status,
        warnings=warnings,
        page_count=extracted.page_count,
        revision_id=revision_id,
    )
    revisions.append(revision)
    working["normalized_path"] = normalized_rel
    working["normalized_hash"] = normalized_hash
    working["normalization"] = {"current_revision": revision_id, "revisions": revisions}
    _validate_normalization_update(vault_root, working, normalized_note)

    destination = vault_root / normalized_rel
    if destination.exists():
        raise ValidationError(f"normalization revision destination already exists: {normalized_rel}")
    published_note = False
    try:
        _replace_text_synced(destination, normalized_note)
        published_note = True
        _replace_json_synced(manifest_path, working)
    except Exception:
        if published_note and destination.is_file():
            destination.unlink()
        raise
    _append_normalization_log(vault_root, input_path, working, warnings)
    result.status = "normalization_revision"
    result.manifest_path = manifest_path.relative_to(vault_root).as_posix()
    result.normalized_path = normalized_rel
    result.extraction_status = extracted.extraction_status
    result.warnings = warnings
    result.message = f"normalization revision {revision_id} created; prior locators remain resolvable"
    return result


def _with_legacy_normalization_history(
    vault_root: Path,
    source_dir: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    working = dict(manifest)
    normalized_path = str(working.get("normalized_path") or "")
    source_id = working.get("source_id")
    expected_prefix = f"sources/{source_id}/"
    if not normalized_path.startswith(expected_prefix):
        raise ValidationError("legacy normalized_path is outside its source")
    note_path = confined_file(source_dir, vault_root / normalized_path)
    if note_path is None:
        raise ValidationError("legacy normalized note is missing or outside its source")
    normalized_hash = f"sha256:{sha256_file(note_path)}"
    revision = _normalization_revision(
        adapter=None,
        normalized_path=normalized_path,
        normalized_hash=normalized_hash,
        created_at=str(working.get("ingested_at") or utc_now()),
        supersedes=None,
        extraction_status=str(working.get("extraction_status") or "complete"),
        warnings=list(working.get("warnings") or []),
        page_count=working.get("page_count") if isinstance(working.get("page_count"), int) else None,
    )
    working["normalized_hash"] = normalized_hash
    working["normalization"] = {"current_revision": revision["revision_id"], "revisions": [revision]}
    return working


def _normalized_note_metadata(manifest: dict[str, object]) -> dict[str, object]:
    metadata = {
        key: manifest[key]
        for key in (
            "schema_version",
            "source_id",
            "content_hash",
            "media_type",
            "title",
            "creator",
            "publication_date",
            "ingested_at",
            "canonical_url",
            "language",
        )
    }
    for key in ("subject_refs", "topic_refs"):
        if isinstance(manifest.get(key), list):
            metadata[key] = manifest[key]
    metadata["extraction_status"] = manifest["extraction_status"]
    metadata["warnings"] = manifest["warnings"]
    if isinstance(manifest.get("page_count"), int):
        metadata["page_count"] = manifest["page_count"]
    return metadata


def _normalization_revision(
    *,
    adapter: IngestionAdapter | None,
    normalized_path: str,
    normalized_hash: str,
    created_at: str,
    supersedes: str | None,
    extraction_status: str,
    warnings: list[str],
    page_count: int | None,
    revision_id: str | None = None,
) -> dict[str, object]:
    revision: dict[str, object] = {
        "revision_id": revision_id or _normalization_revision_id(normalized_hash),
        "normalized_path": normalized_path,
        "normalized_hash": normalized_hash,
        "adapter": getattr(adapter, "adapter_id", "knowledge-desk.legacy"),
        "adapter_version": getattr(adapter, "adapter_version", "unknown"),
        "created_at": created_at,
        "supersedes": supersedes,
        "extraction_status": extraction_status,
        "warnings": warnings,
    }
    if page_count is not None:
        revision["page_count"] = page_count
    return revision


def _normalization_revision_id(
    normalized_hash: str,
    *,
    adapter: IngestionAdapter | None = None,
    created_at: str | None = None,
    supersedes: str | None = None,
) -> str:
    if adapter is None or created_at is None:
        digest = normalized_hash.removeprefix("sha256:")
    else:
        digest = sha256_text(
            "\0".join(
                (
                    normalized_hash,
                    adapter.adapter_id,
                    adapter.adapter_version,
                    created_at,
                    supersedes or "",
                )
            )
        )
    return f"norm-{digest[:16]}"


def _validate_normalization_update(
    vault_root: Path,
    manifest: dict[str, object],
    normalized_note: str,
) -> None:
    from knowledge_desk.validation import load_schema, schema_errors

    errors = schema_errors(manifest, load_schema(vault_root, "source-manifest.schema.json"))
    try:
        note_metadata, note_body = parse_frontmatter(normalized_note)
        normalized_content(note_body)
    except ValueError as exc:
        errors.append(f"normalized note: {exc}")
    else:
        errors.extend(
            f"normalized note {message}"
            for message in schema_errors(
                note_metadata,
                load_schema(vault_root, "normalized-source-note.schema.json"),
            )
        )
    if errors:
        raise ValidationError("normalization update failed validation: " + "; ".join(errors))


def _replace_text_synced(path: Path, value: str) -> None:
    atomic_replace_text_synced(path, value)


def _replace_json_synced(path: Path, value: object) -> None:
    atomic_replace_json_synced(path, value)


def _append_normalization_log(
    vault_root: Path,
    input_path: Path,
    manifest: dict[str, object],
    warnings: list[str],
) -> None:
    _append_ingest_log(
        vault_root,
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"ing-{uuid.uuid4().hex}",
            "occurred_at": utc_now(),
            "operation": "ingest",
            "status": "normalization_revision",
            "source_id": manifest["source_id"],
            "content_hash": manifest["content_hash"],
            "input_filename": input_path.name,
            "warnings": warnings,
        },
    )


def _append_ingest_log(vault_root: Path, entry: dict[str, object]) -> None:
    log_path = vault_root / "system" / "logs" / "ingest.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retag_source(
    vault_root: Path,
    source_id: str,
    *,
    subject_refs: list[str] | None = None,
    topic_refs: list[str] | None = None,
    clear_subjects: bool = False,
    clear_topics: bool = False,
) -> OperationResult:
    """Update catalog associations on an existing source without re-ingesting bytes.

    Immutable original content and source_id stay fixed. Only subject_refs /
    topic_refs (and the matching normalized front matter + hash) change.
    """
    from knowledge_desk.writer import vault_write_lock

    with vault_write_lock(vault_root):
        return _retag_source_unlocked(
            vault_root,
            source_id,
            subject_refs=subject_refs,
            topic_refs=topic_refs,
            clear_subjects=clear_subjects,
            clear_topics=clear_topics,
        )


def _retag_source_unlocked(
    vault_root: Path,
    source_id: str,
    *,
    subject_refs: list[str] | None,
    topic_refs: list[str] | None,
    clear_subjects: bool,
    clear_topics: bool,
) -> OperationResult:
    vault_root = vault_root.resolve()
    result = OperationResult(operation="source-retag", input_path=source_id, source_id=source_id)
    try:
        if clear_subjects and subject_refs:
            raise ValidationError("use either --clear-subjects or --subject-ref, not both")
        if clear_topics and topic_refs:
            raise ValidationError("use either --clear-topics or --topic-ref, not both")
        if not clear_subjects and subject_refs is None and not clear_topics and topic_refs is None:
            raise ValidationError(
                "provide --subject-ref / --clear-subjects and/or --topic-ref / --clear-topics"
            )

        source_dir = vault_root / "sources" / source_id
        if not source_dir.is_dir():
            raise ValidationError(f"unknown source_id: {source_id}")
        manifest_path = source_dir / "manifest.json"
        resolved_manifest = confined_file(source_dir, manifest_path)
        if resolved_manifest is None or not resolved_manifest.is_file():
            raise ValidationError(f"missing or unreadable manifest for {source_id}")
        manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValidationError(f"manifest for {source_id} is not an object")
        if manifest.get("source_id") != source_id:
            raise ValidationError(f"manifest source_id mismatch for {source_id}")

        current_subjects = _catalog_ref_list(manifest.get("subject_refs"))
        current_topics = _catalog_ref_list(manifest.get("topic_refs"))
        if clear_subjects:
            next_subjects = []
        elif subject_refs is not None:
            next_subjects = sorted(set(subject_refs))
        else:
            next_subjects = current_subjects
        if clear_topics:
            next_topics = []
        elif topic_refs is not None:
            next_topics = sorted(set(topic_refs))
        else:
            next_topics = current_topics

        result.content_hash = str(manifest.get("content_hash") or "") or None
        result.manifest_path = manifest_path.relative_to(vault_root).as_posix()
        result.normalized_path = str(manifest.get("normalized_path") or "") or None
        result.extraction_status = str(manifest.get("extraction_status") or "") or None
        result.warnings = list(manifest.get("warnings") or []) if isinstance(manifest.get("warnings"), list) else []

        if next_subjects == current_subjects and next_topics == current_topics:
            result.status = "noop"
            result.message = "catalog associations already match requested values"
            return result

        working = dict(manifest)
        working["subject_refs"] = next_subjects
        working["topic_refs"] = next_topics

        normalized_rel = str(working.get("normalized_path") or "")
        if not normalized_rel.startswith(f"sources/{source_id}/"):
            raise ValidationError("normalized_path is outside its source")
        note_path = confined_file(source_dir, vault_root / normalized_rel)
        if note_path is None or not note_path.is_file():
            raise ValidationError("current normalized note is missing or outside its source")
        prior_note = note_path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(prior_note)
        note_metadata = _normalized_note_metadata(working)
        # parse_frontmatter returns the body after the closing --- line (may start with \n).
        if body.startswith("\n") or body == "":
            normalized_note = render_frontmatter(note_metadata) + body
        else:
            normalized_note = render_frontmatter(note_metadata) + "\n" + body
        normalized_hash = f"sha256:{sha256_text(normalized_note)}"
        working["normalized_hash"] = normalized_hash
        history = working.get("normalization")
        if isinstance(history, dict) and isinstance(history.get("revisions"), list):
            current_revision = history.get("current_revision")
            revisions = []
            for item in history["revisions"]:
                if not isinstance(item, dict):
                    revisions.append(item)
                    continue
                entry = dict(item)
                # Keep integrity anchors for the published current path in sync with the note.
                if entry.get("normalized_path") == normalized_rel:
                    entry["normalized_hash"] = normalized_hash
                revisions.append(entry)
            working["normalization"] = {
                "current_revision": current_revision,
                "revisions": revisions,
            }

        _validate_normalization_update(vault_root, working, normalized_note)

        original_rel = str(working.get("original_path") or "")
        if original_rel:
            original_path = confined_file(source_dir, vault_root / original_rel)
            if original_path is not None and original_path.is_file() and working.get("content_hash"):
                actual = f"sha256:{sha256_file(original_path)}"
                if actual != working["content_hash"]:
                    raise ValidationError("original content hash mismatch; refusing retag")

        published_note = False
        try:
            _replace_text_synced(note_path, normalized_note)
            published_note = True
            _replace_json_synced(manifest_path, working)
        except Exception:
            if published_note:
                _replace_text_synced(note_path, prior_note)
            raise

        result.status = "updated"
        result.normalized_path = normalized_rel
        result.message = (
            f"updated catalog associations "
            f"(subjects={next_subjects or '[]'}, topics={next_topics or '[]'}); "
            "rebuild the search index to refresh FTS"
        )
        return result
    except (KnowledgeDeskError, ValidationError, OSError, json.JSONDecodeError, ValueError) as exc:
        result.status = "failed"
        result.message = str(exc)
        return result


def _catalog_ref_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if isinstance(item, str)})


def successful(results: Iterable[OperationResult]) -> bool:
    return all(
        result.status in {"created", "revision", "normalization_revision", "updated", "noop"}
        for result in results
    )
