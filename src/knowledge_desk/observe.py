from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError, ValidationError
from knowledge_desk.util import fsync_directory, write_json_synced
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


def _append_observations_atomic_unlocked(
    vault_root: Path,
    observations: list[dict[str, Any]],
) -> list[ObserveResult]:
    """Preflight and append a batch, rolling back every created file on failure."""
    vault_root = vault_root.resolve()
    observation_ids: set[str] = set()
    planned: list[tuple[dict[str, Any], Path, ObserveResult, bool]] = []
    staging_parent: Path | None = None

    for index, observation in enumerate(observations):
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            raise ValidationError(f"proposed_observations/{index}: observation_id is required")
        if observation_id in observation_ids:
            raise ValidationError(
                f"proposed_observations/{index}: duplicate observation_id in batch: {observation_id}"
            )
        observation_ids.add(observation_id)

        errors = validate_observation_document(vault_root, observation)
        if errors:
            raise ValidationError(
                f"proposed_observations/{index} failed validation: " + "; ".join(errors)
            )
        cycle_error = _relation_cycle_error(vault_root, observation)
        if cycle_error:
            raise ValidationError(f"proposed_observations/{index}: {cycle_error}")

        final_path = observation_path(vault_root, observation_id)
        result = ObserveResult(
            observation_id=observation_id,
            path=final_path.relative_to(vault_root).as_posix(),
        )
        noop = False
        if final_path.exists():
            existing = load_observation_document(final_path)
            if existing != observation:
                raise ValidationError(
                    f"observation {observation_id} already exists with different content; "
                    "the complete compile batch was not written"
                )
            result.status = "noop"
            result.message = "identical observation already present; no files changed"
            noop = True
        planned.append((observation, final_path, result, noop))

    creates = [item for item in planned if not item[3]]
    if not creates:
        return [item[2] for item in planned]

    staging_root = vault_root / "system" / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix="observe-batch-", dir=staging_root))
    staged_paths: dict[str, Path] = {}
    installed: list[Path] = []
    try:
        for observation, _final_path, _result, noop in planned:
            if noop:
                continue
            observation_id = str(observation["observation_id"])
            staged = staging_parent / f"{observation_id}.json"
            write_json_synced(staged, observation)
            staged_paths[observation_id] = staged

        observations_root = vault_root / "observations"
        observations_root.mkdir(parents=True, exist_ok=True)
        try:
            for observation, final_path, result, noop in planned:
                if noop:
                    continue
                if final_path.exists() or final_path.is_symlink():
                    raise ValidationError(
                        f"observation {observation['observation_id']} appeared after preflight; "
                        "the complete compile batch was not written"
                    )
                _publish_observation(staged_paths[str(observation["observation_id"])], final_path)
                installed.append(final_path)
                result.status = "created"
                result.message = "observation appended in atomic compile batch"
            fsync_directory(observations_root)
        except Exception as exc:
            rollback_errors: list[str] = []
            for final_path in reversed(installed):
                try:
                    final_path.unlink()
                except OSError as rollback_exc:
                    rollback_errors.append(f"{final_path.name}: {rollback_exc}")
            fsync_directory(observations_root)
            if rollback_errors:
                raise KnowledgeDeskError(
                    "compile observation rollback requires manual recovery: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
        return [item[2] for item in planned]
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent, ignore_errors=True)


def _publish_observation(staged: Path, final_path: Path) -> None:
    os.replace(staged, final_path)


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
