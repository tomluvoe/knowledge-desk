from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0.0"
CONTENT_START = "<!-- ev-content-start -->"
CONTENT_END = "<!-- ev-content-end -->"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc_datetime(value: str, *, date_only_time: time = time.min) -> datetime:
    """Parse ISO/RFC3339 text and normalize it to an aware UTC datetime."""
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return datetime.combine(date.fromisoformat(text), date_only_time, tzinfo=timezone.utc)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_id_for_hash(digest: str) -> str:
    return f"src-{digest[:24]}"


def safe_filename(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip(".-")
    return sanitized or "source"


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_text_synced(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def write_json_synced(path: Path, value: Any) -> None:
    write_text_synced(path, json_text(value))


def append_jsonl_synced(path: Path, value: Any) -> None:
    """Append one durable JSON line; callers serialize logical multi-file transactions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"failed to append JSON line to {path}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def replace_text_synced(path: Path, value: str) -> None:
    """Durably replace a text file without truncating the prior version in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    staged = Path(temporary_name)
    try:
        write_text_synced(staged, value)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        staged.chmod(mode)
        os.replace(staged, path)
        fsync_directory(path.parent)
    finally:
        if staged.exists():
            staged.unlink()


def replace_json_synced(path: Path, value: Any) -> None:
    replace_text_synced(path, json_text(value))


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def confined_file(root: Path, candidate: Path) -> Path | None:
    """Resolve an existing file only when its real path stays under root."""
    lexical_root = root.absolute()
    try:
        root = lexical_root.resolve(strict=True)
    except OSError:
        return None
    if root != lexical_root:
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not resolved.is_relative_to(root):
        return None
    return resolved


def normalization_for_path(manifest: dict[str, Any], normalized_path: str) -> dict[str, Any] | None:
    normalization = manifest.get("normalization")
    if not isinstance(normalization, dict):
        return None
    revisions = normalization.get("revisions")
    if not isinstance(revisions, list):
        return None
    for revision in revisions:
        if isinstance(revision, dict) and revision.get("normalized_path") == normalized_path:
            return revision
    return None


def render_frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{key}: {encoded}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening front matter delimiter")
    metadata: dict[str, Any] = {}
    for index, raw_line in enumerate(lines[1:], start=1):
        if raw_line.strip() == "---":
            return metadata, "".join(lines[index + 1 :])
        if ":" not in raw_line:
            raise ValueError(f"invalid front matter line {index + 1}")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key or key in metadata:
            raise ValueError(f"invalid or duplicate front matter key at line {index + 1}")
        try:
            metadata[key] = json.loads(raw_value.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"front matter value for {key!r} is not JSON-compatible YAML") from exc
    raise ValueError("missing closing front matter delimiter")


def normalized_content(body: str) -> str:
    start = body.find(CONTENT_START)
    end = body.rfind(CONTENT_END)
    if start < 0 or end < start:
        raise ValueError("normalized note has no bounded content section")
    content = body[start + len(CONTENT_START) : end]
    return content.removeprefix("\n").removesuffix("\n")
