from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError, ValidationError
from knowledge_desk.observe import (
    _append_observation_unlocked,
    _append_observations_atomic_unlocked,
)
from knowledge_desk.util import (
    json_text,
    render_frontmatter,
    replace_text_synced,
    safe_filename,
    utc_now,
    write_text_synced,
)
from knowledge_desk.writer import vault_write_lock


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
            path = _require_pending_proposal_path(vault_root, path)
            result.path = path.relative_to(vault_root).as_posix()
            payload = _load_proposal(path)
            _require_pending_status(payload)
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
            elif kind == "workspace_refine_proposal":
                from knowledge_desk.workspace import apply_workspace_refine_proposal

                workspace_result = apply_workspace_refine_proposal(vault_root, payload)
                if workspace_result.get("status") != "refined":
                    raise ValidationError(
                        workspace_result.get("message")
                        or f"workspace refine failed: {workspace_result.get('status')}"
                    )
                applied = {"workspace": workspace_result}
            elif kind == "compile_from_ask_proposal":
                applied = _apply_compile_from_ask(vault_root, payload)
            else:
                raise KnowledgeDeskError(f"unsupported or incomplete proposal kind: {kind!r}")

            payload["status"] = "applied"
            payload["applied_at"] = utc_now()
            payload["apply_result"] = applied
            dest = _archive(vault_root, path, APPLIED_DIR, payload)
            result.status = "applied"
            result.details = {"archived_to": dest, **applied}
            result.message = f"proposal applied and archived to {dest}"
            return result
    except (KnowledgeDeskError, OSError, json.JSONDecodeError, ValueError) as exc:
        result.message = str(exc)
        return result


def reject_proposal(vault_root: Path, proposal_path: Path, *, reason: str | None = None) -> ProposalResult:
    vault_root = vault_root.resolve()
    result = ProposalResult(operation="proposal.reject")
    path = proposal_path if proposal_path.is_absolute() else vault_root / proposal_path
    result.path = str(path.relative_to(vault_root)) if path.is_relative_to(vault_root) else str(path)
    try:
        with vault_write_lock(vault_root):
            path = _require_pending_proposal_path(vault_root, path)
            result.path = path.relative_to(vault_root).as_posix()
            payload = _load_proposal(path)
            _require_pending_status(payload)
            payload["status"] = "rejected"
            payload["rejected_at"] = utc_now()
            if reason:
                payload["reject_reason"] = reason
            dest = _archive(vault_root, path, REJECTED_DIR, payload)
            result.status = "rejected"
            result.details = {"archived_to": dest}
            result.message = f"proposal rejected and archived to {dest}"
            return result
    except (KnowledgeDeskError, OSError, json.JSONDecodeError, ValueError) as exc:
        result.message = str(exc)
        return result


def _load_proposal(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise KnowledgeDeskError(f"proposal not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeDeskError(f"unreadable proposal: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeDeskError("proposal root must be an object")
    return payload


def _require_pending_proposal_path(vault_root: Path, path: Path) -> Path:
    """Return a direct pending queue entry, rejecting traversal and symlinks."""
    vault_root = vault_root.resolve()
    queue_path = vault_root / QUEUE_DIR
    queue = queue_path.resolve()
    if not queue.is_relative_to(vault_root) or queue != queue_path:
        raise KnowledgeDeskError("proposal queue must be a real directory inside the vault")
    if path.is_symlink():
        raise KnowledgeDeskError("proposal path must not be a symlink")
    resolved = path.resolve()
    if resolved.parent != queue or resolved.suffix != ".json":
        raise KnowledgeDeskError(
            f"proposal must be a direct pending JSON file under {QUEUE_DIR}/"
        )
    return resolved


def _require_pending_status(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    if status not in {"pending", "proposed"}:
        raise KnowledgeDeskError(
            f"proposal status must be pending or proposed before apply/reject; got {status!r}"
        )


def _archive(vault_root: Path, path: Path, relative_dir: str, payload: dict[str, Any]) -> str:
    dest_dir = vault_root / relative_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _available_archive_path(dest_dir, path.name)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".proposal-archive-", dir=dest_dir)
    os.close(descriptor)
    staged = Path(temporary_name)
    try:
        write_text_synced(staged, json_text(payload))
        os.replace(staged, dest)
    finally:
        if staged.exists():
            staged.unlink()
    path.unlink()
    return dest.relative_to(vault_root).as_posix()


def _available_archive_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    source = Path(filename)
    for index in range(2, 10_000):
        candidate = dest_dir / f"{source.stem}-{index}{source.suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise KnowledgeDeskError(f"cannot allocate collision-safe archive name for {filename}")


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


def _apply_compile_from_ask(vault_root: Path, payload: dict[str, Any]) -> dict[str, object]:
    """Apply compile-from-ask after all complete observation stubs pass preflight."""
    details: dict[str, object] = {"action": "compile_from_ask"}
    observations = payload.get("proposed_observations")
    if not isinstance(observations, list):
        observations = []
        single = payload.get("proposed_observation_stub") or payload.get("observation")
        if isinstance(single, dict):
            observations = [single]

    complete_observations: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ValidationError(f"proposed_observations/{index} must be an object")
        subjects = observation.get("subjects") if isinstance(observation.get("subjects"), list) else []
        topics = observation.get("topics") if isinstance(observation.get("topics"), list) else []
        subject_ids = [s.get("ref_id") for s in subjects if isinstance(s, dict)]
        topic_ids = [t.get("ref_id") for t in topics if isinstance(t, dict)]
        if "entity-todo" in subject_ids or "topic-todo" in topic_ids:
            raise ValidationError(
                f"proposed_observations/{index} still has TODO subjects/topics; "
                "edit the proposal before applying the all-or-nothing batch"
            )
        complete_observations.append(observation)

    applied_obs = [
        result.to_dict()
        for result in _append_observations_atomic_unlocked(vault_root, complete_observations)
    ]
    details["observations"] = applied_obs

    evolve_scope = payload.get("wiki_evolve") if isinstance(payload.get("wiki_evolve"), dict) else {}
    run_evolve = bool(payload.get("run_wiki_evolve", True))
    if run_evolve:
        from knowledge_desk.wiki import _evolve_wiki_unlocked

        subject = evolve_scope.get("subject") if isinstance(evolve_scope.get("subject"), str) else None
        topic = evolve_scope.get("topic") if isinstance(evolve_scope.get("topic"), str) else None
        # Union proposal scope with successfully applied observation IDs so new
        # compile stubs are never dropped when older citation IDs are listed.
        scope_ids = {
            i
            for i in (evolve_scope.get("observation_ids") or [])
            if isinstance(i, str)
        }
        applied_ids = {
            item.get("observation_id")
            for item in applied_obs
            if isinstance(item, dict)
            and item.get("status") in {"created", "noop"}
            and isinstance(item.get("observation_id"), str)
        }
        observation_ids = sorted(scope_ids | applied_ids) or None
        evolve_result = _evolve_wiki_unlocked(
            vault_root,
            observation_ids=observation_ids,
            subject=subject if not observation_ids else None,
            topic=topic if not observation_ids else None,
        )
        # If ID-scoped evolve was a noop but subject/topic exist, evolve full scope.
        if (
            evolve_result.status == "noop"
            and (subject or topic)
            and observation_ids
        ):
            evolve_result = _evolve_wiki_unlocked(
                vault_root,
                observation_ids=None,
                subject=subject,
                topic=topic,
            )
        details["wiki_evolve"] = evolve_result.to_dict()
    else:
        details["wiki_evolve"] = {"status": "skipped", "message": "run_wiki_evolve=false"}
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
    replace_text_synced(path, body)
    return {"status": "created", "path": path.relative_to(vault_root).as_posix()}
