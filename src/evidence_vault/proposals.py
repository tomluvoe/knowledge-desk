from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evidence_vault.errors import EvidenceVaultError, ValidationError
from evidence_vault.observe import _append_observation_unlocked
from evidence_vault.util import render_frontmatter, safe_filename, utc_now, write_text_synced
from evidence_vault.writer import vault_write_lock


QUEUE_DIR = "system/update-queue"
APPLIED_DIR = "system/update-queue/applied"
REJECTED_DIR = "system/update-queue/rejected"


@dataclass
class ProposalResult:
    operation: str = "proposal"
    status: str = "failed"
    path: str | None = None
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def list_proposals(vault_root: Path) -> dict[str, object]:
    vault_root = vault_root.resolve()
    queue = vault_root / QUEUE_DIR
    items: list[dict[str, object]] = []
    if queue.is_dir():
        for path in sorted(queue.glob("*.json")):
            if path.name in {"README.md"}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {"unreadable": True}
            items.append(
                {
                    "path": path.relative_to(vault_root).as_posix(),
                    "kind": payload.get("kind") if isinstance(payload, dict) else None,
                    "status": payload.get("status") if isinstance(payload, dict) else None,
                    "created_at": payload.get("created_at") if isinstance(payload, dict) else None,
                }
            )
    return {
        "operation": "proposal.list",
        "count": len(items),
        "proposals": items,
        "message": "queue entries are review-only until applied",
    }


def apply_proposal(vault_root: Path, proposal_path: Path) -> ProposalResult:
    """Apply a reviewable proposal under the exclusive writer lock."""
    vault_root = vault_root.resolve()
    result = ProposalResult(operation="proposal.apply")
    path = proposal_path if proposal_path.is_absolute() else vault_root / proposal_path
    result.path = str(path.relative_to(vault_root)) if path.is_relative_to(vault_root) else str(path)
    try:
        with vault_write_lock(vault_root):
            payload = _load_proposal(path)
            kind = payload.get("kind")
            applied: dict[str, object] = {}
            if kind == "explore_ask_proposal":
                applied = _apply_explore_ask(vault_root, payload)
            elif kind == "explore_gaps_proposal":
                applied = {
                    "action": "acknowledged",
                    "note": "gaps proposals are informational; no canonical write performed",
                }
            elif kind == "observation_proposal" and isinstance(payload.get("observation"), dict):
                obs_result = _append_observation_unlocked(vault_root, payload["observation"])
                if obs_result.status not in {"created", "noop"}:
                    raise ValidationError(obs_result.message)
                applied = {"observation": obs_result.to_dict()}
            else:
                raise EvidenceVaultError(f"unsupported or incomplete proposal kind: {kind!r}")

            payload["status"] = "applied"
            payload["applied_at"] = utc_now()
            payload["apply_result"] = applied
            dest = _archive(vault_root, path, APPLIED_DIR, payload)
            result.status = "applied"
            result.details = {"archived_to": dest, **applied}
            result.message = f"proposal applied and archived to {dest}"
            return result
    except (EvidenceVaultError, OSError, json.JSONDecodeError, ValueError) as exc:
        result.message = str(exc)
        return result


def reject_proposal(vault_root: Path, proposal_path: Path, *, reason: str | None = None) -> ProposalResult:
    vault_root = vault_root.resolve()
    result = ProposalResult(operation="proposal.reject")
    path = proposal_path if proposal_path.is_absolute() else vault_root / proposal_path
    result.path = str(path.relative_to(vault_root)) if path.is_relative_to(vault_root) else str(path)
    try:
        with vault_write_lock(vault_root):
            payload = _load_proposal(path)
            payload["status"] = "rejected"
            payload["rejected_at"] = utc_now()
            if reason:
                payload["reject_reason"] = reason
            dest = _archive(vault_root, path, REJECTED_DIR, payload)
            result.status = "rejected"
            result.details = {"archived_to": dest}
            result.message = f"proposal rejected and archived to {dest}"
            return result
    except (EvidenceVaultError, OSError, json.JSONDecodeError, ValueError) as exc:
        result.message = str(exc)
        return result


def _load_proposal(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvidenceVaultError(f"proposal not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceVaultError(f"unreadable proposal: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceVaultError("proposal root must be an object")
    return payload


def _archive(vault_root: Path, path: Path, relative_dir: str, payload: dict[str, Any]) -> str:
    dest_dir = vault_root / relative_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if path.resolve() != dest.resolve() and path.is_file():
        path.unlink()
    return dest.relative_to(vault_root).as_posix()


def _apply_explore_ask(vault_root: Path, payload: dict[str, Any]) -> dict[str, object]:
    details: dict[str, object] = {}
    observation = payload.get("proposed_observation_stub")
    if isinstance(observation, dict):
        # Refuse incomplete TODO stubs to avoid polluting the vault.
        subjects = observation.get("subjects") if isinstance(observation.get("subjects"), list) else []
        topics = observation.get("topics") if isinstance(observation.get("topics"), list) else []
        subject_ids = [s.get("ref_id") for s in subjects if isinstance(s, dict)]
        topic_ids = [t.get("ref_id") for t in topics if isinstance(t, dict)]
        if "entity-todo" in subject_ids or "topic-todo" in topic_ids:
            details["observation"] = {
                "status": "skipped",
                "message": "observation stub still has TODO subjects/topics; edit before apply",
            }
        else:
            obs_result = _append_observation_unlocked(vault_root, observation)
            if obs_result.status not in {"created", "noop"}:
                raise ValidationError(obs_result.message)
            details["observation"] = obs_result.to_dict()

    memory = payload.get("proposed_memory_open_question")
    if isinstance(memory, dict) and payload.get("ask_status") == "insufficient_evidence":
        details["memory"] = _write_memory_record(vault_root, memory)
    return details


def _write_memory_record(vault_root: Path, record: dict[str, Any]) -> dict[str, object]:
    memory_id = record.get("memory_id")
    if not isinstance(memory_id, str):
        raise ValidationError("memory proposal missing memory_id")
    kind = record.get("kind") or "open_question"
    subdir = {
        "user_conclusion": "conclusions",
        "user_decision": "decisions",
        "open_question": "open-questions",
    }.get(str(kind), "open-questions")
    path = vault_root / "memory" / subdir / f"{safe_filename(memory_id)}.md"
    if path.exists():
        return {"status": "noop", "path": path.relative_to(vault_root).as_posix()}
    body = render_frontmatter(record) + f"\n# {record.get('title') or memory_id}\n\n{record.get('statement') or ''}\n"
    write_text_synced(path, body)
    return {"status": "created", "path": path.relative_to(vault_root).as_posix()}
