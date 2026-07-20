from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from evidence_vault.adapters import adapter_for_suffix
from evidence_vault.errors import EvidenceVaultError, ValidationError
from evidence_vault.util import (
    SCHEMA_VERSION,
    render_frontmatter,
    safe_filename,
    sha256_file,
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


def ingest_path(vault_root: Path, input_path: Path, metadata: IngestMetadata) -> list[OperationResult]:
    vault_root = vault_root.resolve()
    input_path = input_path.resolve()
    if input_path.is_dir():
        files = sorted((path for path in input_path.iterdir() if path.is_file()), key=lambda path: path.name.casefold())
        if not files:
            return [OperationResult(input_path=str(input_path), message="directory contains no files")]
        return [ingest_file(vault_root, path, metadata) for path in files]
    return [ingest_file(vault_root, input_path, metadata)]


def ingest_file(vault_root: Path, input_path: Path, metadata: IngestMetadata) -> OperationResult:
    result = OperationResult(input_path=str(input_path))
    staging_parent: Path | None = None
    try:
        if not input_path.is_file():
            raise EvidenceVaultError(f"input is not a regular file: {input_path}")
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
            if not existing_manifest.is_file():
                raise ValidationError(f"source ID collision or incomplete canonical directory: {final_dir}")
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValidationError(f"existing manifest for {source_id} is not an object")
            if existing.get("content_hash") != content_hash:
                raise ValidationError(f"source ID collision for {source_id}")
            original = vault_root / existing["original_path"]
            if not original.is_file() or sha256_file(original) != digest:
                raise ValidationError(f"existing immutable original for {source_id} failed hash verification")
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
            "extraction_status": extracted.extraction_status,
            "warnings": warnings,
            "revision_of": revision_of,
            "extensions": {},
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
                "extraction_status",
                "warnings",
            )
        }
        if extracted.page_count is not None:
            note_metadata["page_count"] = extracted.page_count
        normalized_note = render_frontmatter(note_metadata) + "\n" + extracted.markdown_body

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

        from evidence_vault.validation import validate_source_artifacts

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
    except (EvidenceVaultError, OSError, ValueError, json.JSONDecodeError) as exc:
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


def successful(results: Iterable[OperationResult]) -> bool:
    return all(result.status in {"created", "revision", "noop"} for result in results)
