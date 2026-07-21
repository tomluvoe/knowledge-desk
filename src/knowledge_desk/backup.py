from __future__ import annotations

import json
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from knowledge_desk import __version__
from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.layout import BACKUP_ROOTS, init_vault
from knowledge_desk.util import utc_now


MANIFEST_NAME = "knowledge-desk-backup.json"


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
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def backup_vault(
    vault_root: Path,
    output: Path,
    *,
    include_index: bool = False,
) -> BackupResult:
    """Write a tar.gz of durable desk data (not product code or disposable caches)."""
    vault_root = vault_root.resolve()
    result = BackupResult()
    output = output.expanduser()
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve()
    else:
        output = output.resolve()

    try:
        members: list[Path] = []
        for relative in BACKUP_ROOTS:
            path = vault_root / relative
            if path.exists():
                members.append(path)
        if include_index:
            index_dir = vault_root / "system" / ".index"
            if index_dir.exists():
                members.append(index_dir)

        if not members:
            raise KnowledgeDeskError("nothing to back up; run `knowledge-desk init` and add data first")

        output.parent.mkdir(parents=True, exist_ok=True)
        archived_relpaths: list[str] = []
        with tarfile.open(output, "w:gz") as archive:
            for member in members:
                arcname = member.relative_to(vault_root).as_posix()
                archive.add(member, arcname=arcname, filter=_exclude_disposable)
                archived_relpaths.append(arcname + ("/" if member.is_dir() else ""))

            manifest = {
                "schema_version": "1.0.0",
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
            import io

            archive.addfile(info, io.BytesIO(manifest_bytes))
            archived_relpaths.append(MANIFEST_NAME)

        result.status = "created"
        result.archive = str(output)
        result.paths = archived_relpaths
        result.message = f"backup written to {output}"
        return result
    except (OSError, KnowledgeDeskError, tarfile.TarError) as exc:
        result.message = str(exc)
        return result


def restore_vault(
    vault_root: Path,
    archive_path: Path,
    *,
    force: bool = False,
) -> RestoreResult:
    """Restore durable desk data from a tar.gz produced by backup_vault."""
    vault_root = vault_root.resolve()
    result = RestoreResult(archive=str(archive_path))
    archive_path = archive_path.expanduser().resolve()
    try:
        if not archive_path.is_file():
            raise KnowledgeDeskError(f"backup archive not found: {archive_path}")

        with tarfile.open(archive_path, "r:gz") as archive:
            names = archive.getnames()
            data_names = [name for name in names if name != MANIFEST_NAME]
            if not force:
                conflicts = _restore_conflicts(vault_root, data_names)
                if conflicts:
                    raise KnowledgeDeskError(
                        "restore refused: non-empty data paths already exist "
                        f"({', '.join(conflicts[:8])}{'…' if len(conflicts) > 8 else ''}); use --force to overwrite"
                    )

            # Safety: only extract known roots and the backup manifest.
            allowed_prefixes = tuple(f"{root}/" for root in BACKUP_ROOTS) + BACKUP_ROOTS + (
                "system/.index",
                "system/.index/",
                MANIFEST_NAME,
            )
            for member in archive.getmembers():
                name = member.name
                if name in (".", ""):
                    continue
                if name != MANIFEST_NAME and not any(
                    name == prefix.rstrip("/") or name.startswith(prefix if prefix.endswith("/") else prefix + "/")
                    for prefix in allowed_prefixes
                ):
                    raise KnowledgeDeskError(f"archive contains disallowed path: {name}")
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise KnowledgeDeskError(f"archive contains unsafe path: {name}")

            archive.extractall(vault_root, filter="data")
            result.paths = sorted(data_names)

        init_vault(vault_root, write_readmes=False)
        result.status = "restored"
        result.message = f"restored {len(result.paths)} path(s) into {vault_root}"
        return result
    except (OSError, KnowledgeDeskError, tarfile.TarError) as exc:
        result.message = str(exc)
        return result


def _exclude_disposable(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(tarinfo.name).parts
    blocked = {".staging", ".locks", ".venv", "__pycache__"}
    if any(part in blocked for part in parts):
        return None
    if parts[:2] == ("system", ".index") and False:
        # index only included when caller adds the root explicitly
        pass
    return tarinfo


def _restore_conflicts(vault_root: Path, names: list[str]) -> list[str]:
    conflicts: list[str] = []
    # Check each backup root separately (not top-level "system/", which also holds product schemas).
    for root in BACKUP_ROOTS + ("system/.index",):
        if not any(name == root or name.startswith(root + "/") for name in names):
            continue
        path = vault_root / root
        if not path.exists():
            continue
        if path.is_file():
            conflicts.append(root)
            continue
        # README-only layout from `init` is OK without --force.
        has_data = any(child.is_file() and child.name != "README.md" for child in path.rglob("*"))
        if has_data:
            conflicts.append(root)
    return conflicts
