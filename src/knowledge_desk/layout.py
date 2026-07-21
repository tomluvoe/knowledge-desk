from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from knowledge_desk.util import write_text_synced

# Durable per-desk data (not tracked in the product Git repo).
DATA_DIRECTORIES: tuple[str, ...] = (
    "inbox",
    "sources",
    "observations",
    "wiki",
    "wiki/entities",
    "wiki/topics",
    "wiki/events",
    "wiki/comparisons",
    "wiki/syntheses",
    "memory",
    "memory/conclusions",
    "memory/decisions",
    "memory/open-questions",
    "domains",
    "system/logs",
    "system/update-queue",
    "system/update-queue/applied",
    "system/update-queue/rejected",
)

# Paths archived by backup (directory roots relative to vault root).
BACKUP_ROOTS: tuple[str, ...] = (
    "inbox",
    "sources",
    "observations",
    "wiki",
    "memory",
    "domains",
    "system/logs",
    "system/update-queue",
)

README_BLURBS: dict[str, str] = {
    "inbox": "Review-only candidate files. Not truth until `knowledge-desk ingest` publishes them under sources/.",
    "sources": "Immutable originals, manifests, and normalized Markdown. Content-addressed under src-*.",
    "observations": "Append-only temporal observations (JSON). Never rewrite history; append related records instead.",
    "wiki": "Revisable cited synthesis (entities, topics, events, comparisons, syntheses). Not primary evidence.",
    "wiki/entities": "Entity wiki pages.",
    "wiki/topics": "Topic wiki pages.",
    "wiki/events": "Event wiki pages.",
    "wiki/comparisons": "Comparison wiki pages.",
    "wiki/syntheses": "Cross-source synthesis pages.",
    "memory": "User conclusions, decisions, and open questions (distinct from source evidence).",
    "memory/conclusions": "User conclusions.",
    "memory/decisions": "User decisions.",
    "memory/open-questions": "Open questions.",
    "domains": "Optional installed domain packs for this desk instance (not part of the product Git repo).",
    "system/logs": "Append-only operational logs (e.g. ingest.jsonl).",
    "system/update-queue": "Review-only proposals. Apply/reject with `knowledge-desk proposal`.",
    "system/update-queue/applied": "Archived applied proposals.",
    "system/update-queue/rejected": "Archived rejected proposals.",
}


@dataclass
class InitResult:
    operation: str = "init"
    status: str = "failed"
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def init_vault(vault_root: Path, *, write_readmes: bool = True) -> InitResult:
    """Create empty durable data directories without overwriting existing content."""
    vault_root = vault_root.resolve()
    result = InitResult()
    try:
        for relative in DATA_DIRECTORIES:
            path = vault_root / relative
            if path.is_dir():
                result.skipped.append(f"{relative}/")
            else:
                path.mkdir(parents=True, exist_ok=True)
                result.created.append(f"{relative}/")
            if write_readmes and relative in README_BLURBS:
                readme = path / "README.md"
                if not readme.exists():
                    write_text_synced(readme, f"# {relative}\n\n{README_BLURBS[relative]}\n")
                    result.created.append(readme.relative_to(vault_root).as_posix())
                else:
                    result.skipped.append(readme.relative_to(vault_root).as_posix())
        # Ensure product support dirs used at runtime always exist.
        for relative in ("system/schemas", "system/templates", "system/examples", "system/.staging"):
            path = vault_root / relative
            if not path.is_dir():
                # schemas/templates/examples should come from Git; only create if missing for bare data roots.
                path.mkdir(parents=True, exist_ok=True)
                result.created.append(f"{relative}/")
        result.status = "initialized"
        result.message = (
            f"desk data layout ready ({len(result.created)} created, {len(result.skipped)} already present)"
        )
        return result
    except OSError as exc:
        result.message = str(exc)
        return result
