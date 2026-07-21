from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from knowledge_desk import __version__
from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.layout import BACKUP_ROOTS, init_vault
from knowledge_desk.util import fsync_directory, utc_now
from knowledge_desk.validation import validate_vault
from knowledge_desk.writer import vault_write_lock


MANIFEST_NAME = "knowledge-desk-backup.json"
BACKUP_SCHEMA_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000


@dataclass
class BackupResult:
    operation: str = "backup"
    status: str = "failed"
    archive: str | None = None
    paths: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class RestoreResult:
    operation: str = "restore"
    status: str = "failed"
    archive: str | None = None
    paths: list[str] = field(default_factory=list)
    recovery_archive: str | None = None
    recovery_path: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ArchivePlan:
    manifest: dict[str, Any]
    members: tuple[tarfile.TarInfo, ...]
    roots: tuple[str, ...]


def backup_vault(
    vault_root: Path,
    output: Path,
    *,
    include_index: bool = False,
) -> BackupResult:
    """Write a consistent tar.gz snapshot of durable desk data."""
    vault_root = vault_root.resolve()
    result = BackupResult()
    output = _absolute_path(output)
    staged: Path | None = None

    try:
        with vault_write_lock(vault_root):
            members = _backup_members(vault_root, include_index=include_index)
            if not members:
                raise KnowledgeDeskError("nothing to back up; run `knowledge-desk init` and add data first")
            _refuse_output_inside_members(output, members)

            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor, staged_name = tempfile.mkstemp(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
            )
            os.close(descriptor)
            staged = Path(staged_name)
            archived_relpaths = _write_archive(
                vault_root,
                staged,
                members,
                include_index=include_index,
            )
            with staged.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(staged, output)
            staged = None
            fsync_directory(output.parent)

        result.status = "created"
        result.archive = str(output)
        result.paths = archived_relpaths
        result.message = f"consistent backup written to {output}"
        return result
    except (OSError, KnowledgeDeskError, tarfile.TarError, ValueError) as exc:
        result.message = str(exc)
        return result
    finally:
        if staged is not None and staged.exists():
            staged.unlink()


def restore_vault(
    vault_root: Path,
    archive_path: Path,
    *,
    force: bool = False,
) -> RestoreResult:
    """Validate, stage, and recoverably publish a Knowledge Desk backup."""
    vault_root = vault_root.resolve()
    archive_path = archive_path.expanduser().resolve()
    result = RestoreResult(archive=str(archive_path))
    staging_parent: Path | None = None
    preserve_staging = False

    try:
        if not archive_path.is_file():
            raise KnowledgeDeskError(f"backup archive not found: {archive_path}")
        _require_validation_contracts(vault_root)

        staging_parent = Path(
            tempfile.mkdtemp(prefix=".knowledge-desk-restore-", dir=vault_root.parent)
        )
        candidate = staging_parent / "candidate"
        candidate.mkdir()

        with tarfile.open(archive_path, "r:gz") as archive:
            plan = _preflight_archive(archive)
            archive.extractall(candidate, members=plan.members, filter="data")

        _copy_validation_contracts(vault_root, candidate)
        init_result = init_vault(candidate, write_readmes=False)
        if init_result.status != "initialized":
            raise KnowledgeDeskError(f"failed to prepare staged restore: {init_result.message}")
        validation = validate_vault(candidate)
        if not validation.valid:
            detail = "; ".join(validation.errors[:8])
            suffix = "…" if len(validation.errors) > 8 else ""
            raise KnowledgeDeskError(f"staged restore failed vault validation: {detail}{suffix}")

        with vault_write_lock(vault_root):
            conflicts = _restore_conflicts(vault_root, list(plan.roots))
            if conflicts and not force:
                raise KnowledgeDeskError(
                    "restore refused: non-empty data paths already exist "
                    f"({', '.join(conflicts[:8])}{'…' if len(conflicts) > 8 else ''}); "
                    "use --force to replace whole roots recoverably"
                )
            if conflicts and force:
                recovery = _recovery_archive_path(vault_root)
                recovery_result = backup_vault(
                    vault_root,
                    recovery,
                    include_index="system/.index" in plan.roots,
                )
                if recovery_result.status != "created":
                    raise KnowledgeDeskError(
                        f"forced restore could not create recovery backup: {recovery_result.message}"
                    )
                result.recovery_archive = recovery_result.archive

            rollback = staging_parent / "rollback"
            failed_publish = staging_parent / "failed-publish"
            try:
                _publish_roots(vault_root, candidate, plan.roots, rollback, failed_publish)
            except Exception as exc:
                preserve_staging = rollback.exists() and any(rollback.rglob("*"))
                if preserve_staging:
                    result.recovery_path = str(staging_parent)
                    detail = f"restore publication failed; manual recovery is preserved at {staging_parent}"
                else:
                    detail = "restore publication failed and the prior roots were restored"
                raise KnowledgeDeskError(f"{detail}: {exc}") from exc

        result.status = "restored"
        result.paths = list(plan.roots)
        recovery_note = (
            f"; pre-restore recovery archive: {result.recovery_archive}"
            if result.recovery_archive
            else ""
        )
        result.message = (
            f"validated and restored {len(result.paths)} whole data root(s) into {vault_root}"
            f"{recovery_note}"
        )
        return result
    except (
        EOFError,
        OSError,
        UnicodeDecodeError,
        KnowledgeDeskError,
        tarfile.TarError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        result.message = str(exc)
        return result
    finally:
        if staging_parent is not None and staging_parent.exists() and not preserve_staging:
            shutil.rmtree(staging_parent, ignore_errors=True)


def _absolute_path(path: Path) -> Path:
    path = path.expanduser()
    return (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()


def _backup_members(vault_root: Path, *, include_index: bool) -> list[Path]:
    members = [vault_root / relative for relative in BACKUP_ROOTS if (vault_root / relative).exists()]
    index_dir = vault_root / "system" / ".index"
    if include_index and index_dir.exists():
        members.append(index_dir)
    return members


def _refuse_output_inside_members(output: Path, members: list[Path]) -> None:
    for member in members:
        root = member.resolve()
        if output == root or output.is_relative_to(root):
            raise KnowledgeDeskError(
                f"backup output must be outside archived root {root}: {output}"
            )


def _write_archive(
    vault_root: Path,
    output: Path,
    members: list[Path],
    *,
    include_index: bool,
) -> list[str]:
    archived_relpaths: list[str] = []
    with tarfile.open(output, "w:gz") as archive:
        for member in members:
            arcname = member.relative_to(vault_root).as_posix()
            archive.add(member, arcname=arcname, filter=_exclude_disposable)
            archived_relpaths.append(arcname + ("/" if member.is_dir() else ""))

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "kind": "knowledge_desk_backup",
            "created_at": utc_now(),
            "tool_version": __version__,
            "vault_label": vault_root.name,
            "paths": archived_relpaths,
            "include_index": include_index,
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        info = tarfile.TarInfo(name=MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(manifest_bytes))
    return archived_relpaths + [MANIFEST_NAME]


def _exclude_disposable(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = PurePosixPath(tarinfo.name).parts
    blocked = {".staging", ".locks", ".venv", "__pycache__"}
    if any(part in blocked for part in parts):
        return None
    if not (tarinfo.isdir() or tarinfo.isfile()):
        raise KnowledgeDeskError(f"backup refuses non-regular member: {tarinfo.name}")
    return tarinfo


def _preflight_archive(archive: tarfile.TarFile) -> ArchivePlan:
    members = archive.getmembers()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise KnowledgeDeskError(f"archive has too many members: {len(members)}")
    names: set[str] = set()
    manifest_member: tarfile.TarInfo | None = None
    data_members: list[tarfile.TarInfo] = []
    for member in members:
        name = member.name
        pure = PurePosixPath(name)
        if (
            not name
            or name == "."
            or name.startswith("/")
            or name.startswith("./")
            or "\\" in name
            or ".." in pure.parts
            or pure.as_posix() != name.rstrip("/")
        ):
            raise KnowledgeDeskError(f"archive contains unsafe or non-canonical path: {name}")
        if name in names:
            raise KnowledgeDeskError(f"archive contains duplicate path: {name}")
        names.add(name)
        if name == MANIFEST_NAME:
            if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
                raise KnowledgeDeskError("backup manifest must be one bounded regular file")
            manifest_member = member
            continue
        if not (member.isdir() or member.isfile()):
            raise KnowledgeDeskError(f"archive contains unsupported member type: {name}")
        data_members.append(member)

    if manifest_member is None:
        raise KnowledgeDeskError(f"archive is missing required {MANIFEST_NAME}")
    manifest_stream = archive.extractfile(manifest_member)
    if manifest_stream is None:
        raise KnowledgeDeskError("backup manifest is unreadable")
    manifest = json.loads(manifest_stream.read().decode("utf-8"))
    roots = _validate_backup_manifest(manifest, data_members)
    return ArchivePlan(manifest=manifest, members=tuple(data_members), roots=roots)


def _validate_backup_manifest(
    manifest: object,
    members: list[tarfile.TarInfo],
) -> tuple[str, ...]:
    if not isinstance(manifest, dict):
        raise KnowledgeDeskError("backup manifest root must be an object")
    required = {
        "schema_version",
        "kind",
        "created_at",
        "tool_version",
        "vault_label",
        "paths",
        "include_index",
    }
    if set(manifest) != required:
        raise KnowledgeDeskError("backup manifest fields do not match the supported contract")
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise KnowledgeDeskError(
            f"unsupported backup schema_version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("kind") != "knowledge_desk_backup":
        raise KnowledgeDeskError(f"unsupported backup kind: {manifest.get('kind')!r}")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise KnowledgeDeskError("backup manifest created_at must be RFC3339")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeDeskError("backup manifest created_at must be RFC3339") from exc
    if parsed_created_at.tzinfo is None:
        raise KnowledgeDeskError("backup manifest created_at must include a timezone")
    tool_version = manifest.get("tool_version")
    if not isinstance(tool_version, str) or _compatibility_series(tool_version) != _compatibility_series(
        __version__
    ):
        raise KnowledgeDeskError(
            f"backup tool_version {tool_version!r} is incompatible with {__version__!r}"
        )
    if not isinstance(manifest.get("vault_label"), str) or not manifest["vault_label"]:
        raise KnowledgeDeskError("backup manifest vault_label must be a non-empty string")
    include_index = manifest.get("include_index")
    if not isinstance(include_index, bool):
        raise KnowledgeDeskError("backup manifest include_index must be boolean")
    declared = manifest.get("paths")
    if not isinstance(declared, list) or not declared:
        raise KnowledgeDeskError("backup manifest paths must be a non-empty array")

    allowed = tuple(BACKUP_ROOTS) + (("system/.index",) if include_index else ())
    roots: list[str] = []
    for item in declared:
        if not isinstance(item, str):
            raise KnowledgeDeskError("backup manifest paths must contain strings")
        root = item.removesuffix("/")
        if root not in allowed or item != root + "/" or root in roots:
            raise KnowledgeDeskError(f"backup manifest contains invalid root declaration: {item!r}")
        roots.append(root)

    discovered: set[str] = set()
    for member in members:
        matches = [root for root in allowed if member.name == root or member.name.startswith(root + "/")]
        if not matches:
            raise KnowledgeDeskError(f"archive contains disallowed path: {member.name}")
        discovered.add(max(matches, key=len))
    if discovered != set(roots):
        raise KnowledgeDeskError("backup manifest paths do not match archived data roots")
    for root in roots:
        root_member = next((member for member in members if member.name == root), None)
        if root_member is None or not root_member.isdir():
            raise KnowledgeDeskError(f"archive root must be an explicit directory: {root}")
    return tuple(roots)


def _compatibility_series(version: str) -> tuple[int, int]:
    parts = version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise KnowledgeDeskError(f"invalid tool version: {version!r}")
    return int(parts[0]), int(parts[1])


def _require_validation_contracts(vault_root: Path) -> None:
    for relative in ("system/schemas", "system/examples"):
        if not (vault_root / relative).is_dir():
            raise KnowledgeDeskError(f"restore target is missing product validation contracts: {relative}")


def _copy_validation_contracts(vault_root: Path, candidate: Path) -> None:
    for relative in ("system/schemas", "system/examples"):
        source = vault_root / relative
        destination = candidate / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)


def _restore_conflicts(vault_root: Path, roots: list[str]) -> list[str]:
    conflicts: list[str] = []
    for root in roots:
        path = vault_root / root
        if not path.exists():
            continue
        if path.is_file() or path.is_symlink():
            conflicts.append(root)
            continue
        has_data = any(child.is_file() and child.name != "README.md" for child in path.rglob("*"))
        if has_data:
            conflicts.append(root)
    return conflicts


def _recovery_archive_path(vault_root: Path) -> Path:
    stamp = utc_now().replace(":", "").replace("-", "")
    return vault_root.parent / f"knowledge-desk-pre-restore-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"


def _publish_roots(
    vault_root: Path,
    candidate: Path,
    roots: tuple[str, ...],
    rollback: Path,
    failed_publish: Path,
) -> None:
    moved_old: list[str] = []
    installed: list[str] = []
    try:
        for relative in roots:
            staged_root = candidate / relative
            if not staged_root.is_dir():
                raise KnowledgeDeskError(f"staged archive root is missing: {relative}")
            destination = vault_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                rollback_root = rollback / relative
                rollback_root.parent.mkdir(parents=True, exist_ok=True)
                _move_path(destination, rollback_root)
                moved_old.append(relative)
            _move_path(staged_root, destination)
            installed.append(relative)
        fsync_directory(vault_root)
        fsync_directory(vault_root / "system")
    except Exception:
        rollback_errors: list[str] = []
        for relative in reversed(installed):
            destination = vault_root / relative
            if destination.exists() or destination.is_symlink():
                failed_root = failed_publish / relative
                failed_root.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _move_path(destination, failed_root)
                except OSError as exc:
                    rollback_errors.append(f"remove new {relative}: {exc}")
        for relative in reversed(moved_old):
            rollback_root = rollback / relative
            destination = vault_root / relative
            if rollback_root.exists() or rollback_root.is_symlink():
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _move_path(rollback_root, destination)
                except OSError as exc:
                    rollback_errors.append(f"restore old {relative}: {exc}")
        if rollback_errors:
            raise KnowledgeDeskError(
                "restore rollback requires manual recovery: " + "; ".join(rollback_errors)
            )
        raise


def _move_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)
