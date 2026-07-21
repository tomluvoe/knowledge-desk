from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError, ValidationError
from knowledge_desk.util import write_json_synced
from knowledge_desk.validation import (
    load_schema,
    reference_identity_errors,
    schema_errors,
    validate_locator,
)


@dataclass
class ObserveResult:
    operation: str = "observe"
    status: str = "failed"
    observation_id: str | None = None
    path: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def observation_path(vault_root: Path, observation_id: str) -> Path:
    return vault_root / "observations" / f"{observation_id}.json"


def load_observation_document(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeDeskError(f"cannot read observation document {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeDeskError(f"observation document root must be an object: {path}")
    return payload


def validate_observation_document(vault_root: Path, observation: dict[str, Any]) -> list[str]:
    """Return structural and evidence errors for a single observation payload."""
    errors = schema_errors(observation, load_schema(vault_root, "observation.schema.json"))
    for field, expected_kind in (("subjects", "entity"), ("topics", "topic")):
        references = observation.get(field)
        if not isinstance(references, list):
            continue
        for index, reference in enumerate(references):
            errors.extend(
                f"{field}/{index}: {message}"
                for message in reference_identity_errors(reference, expected_kind)
            )
    if errors:
        return errors
    observation_id = observation.get("observation_id")
    for index, locator in enumerate(observation.get("evidence", [])):
        if not isinstance(locator, dict):
            errors.append(f"evidence/{index}: locator must be an object")
            continue
        errors.extend(f"evidence/{index}: {message}" for message in validate_locator(vault_root, locator))
    for index, relation in enumerate(observation.get("relations", [])):
        if not isinstance(relation, dict):
            errors.append(f"relations/{index}: relation must be an object")
            continue
        target = relation.get("observation_id")
        if not isinstance(target, str):
            continue
        if target == observation_id:
            errors.append(f"relations/{index}: relation cannot target the same observation")
            continue
        target_path = observation_path(vault_root, target)
        if not target_path.is_file():
            errors.append(f"relations/{index}: relation target does not exist: {target}")
    horizon = observation.get("horizon")
    if isinstance(horizon, dict) and horizon.get("start") and horizon.get("end") and horizon["end"] < horizon["start"]:
        errors.append("horizon end precedes start")
    return errors


def append_observation(vault_root: Path, observation: dict[str, Any]) -> ObserveResult:
    """Append an observation if it is new; never rewrite an existing record."""
    from knowledge_desk.writer import vault_write_lock

    with vault_write_lock(vault_root):
        return _append_observation_unlocked(vault_root, observation)


def _append_observation_unlocked(vault_root: Path, observation: dict[str, Any]) -> ObserveResult:
    vault_root = vault_root.resolve()
    result = ObserveResult()
    staging_parent: Path | None = None
    try:
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            raise ValidationError("observation_id is required")
        result.observation_id = observation_id

        errors = validate_observation_document(vault_root, observation)
        if errors:
            raise ValidationError("observation failed validation: " + "; ".join(errors))

        final_path = observation_path(vault_root, observation_id)
        relative = final_path.relative_to(vault_root).as_posix()
        result.path = relative
        if final_path.exists():
            existing = load_observation_document(final_path)
            if existing == observation:
                result.status = "noop"
                result.message = "identical observation already present; no files changed"
                return result
            raise ValidationError(
                f"observation {observation_id} already exists with different content; "
                "append a new observation instead of rewriting history"
            )

        # Cycle check against existing relation graph when this observation is added.
        cycle_error = _relation_cycle_error(vault_root, observation)
        if cycle_error:
            raise ValidationError(cycle_error)

        staging_root = vault_root / "system" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_parent = Path(tempfile.mkdtemp(prefix="observe-", dir=staging_root))
        staged = staging_parent / f"{observation_id}.json"
        write_json_synced(staged, observation)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, final_path)
        result.status = "created"
        result.message = "observation appended and validated"
        return result
    except (KnowledgeDeskError, OSError, ValueError, json.JSONDecodeError) as exc:
        result.message = str(exc)
        return result
    finally:
        if staging_parent and staging_parent.exists():
            shutil.rmtree(staging_parent, ignore_errors=True)


def append_observation_path(vault_root: Path, document_path: Path) -> ObserveResult:
    try:
        observation = load_observation_document(document_path.resolve())
    except KnowledgeDeskError as exc:
        return ObserveResult(message=str(exc))
    return append_observation(vault_root, observation)


def successful(results: list[ObserveResult]) -> bool:
    return all(result.status in {"created", "noop"} for result in results)


def _relation_cycle_error(vault_root: Path, observation: dict[str, Any]) -> str | None:
    observation_id = observation["observation_id"]
    edges: list[tuple[str, str]] = []
    for path in sorted((vault_root / "observations").glob("**/*.json")):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(existing, dict):
            continue
        source_id = existing.get("observation_id")
        if not isinstance(source_id, str):
            continue
        for relation in existing.get("relations", []):
            if isinstance(relation, dict) and isinstance(relation.get("observation_id"), str):
                edges.append((source_id, relation["observation_id"]))
    for relation in observation.get("relations", []):
        if isinstance(relation, dict) and isinstance(relation.get("observation_id"), str):
            edges.append((observation_id, relation["observation_id"]))

    # Detect whether adding these edges creates a cycle involving the new observation.
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        if start == goal:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(neighbor, goal, seen) for neighbor in adjacency.get(start, []))

    for relation in observation.get("relations", []):
        if not isinstance(relation, dict):
            continue
        target = relation.get("observation_id")
        if isinstance(target, str) and reaches(target, observation_id, set()):
            return f"observation relation would create a cycle involving {observation_id}"
    return None
