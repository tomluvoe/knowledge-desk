from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from evidence_vault.errors import EvidenceVaultError
from evidence_vault.observe import load_observation_document, observation_path


@dataclass(frozen=True)
class ObservationQuery:
    """Filters for listing observations. All provided filters are ANDed."""

    subject: str | None = None  # ref_id or label substring (casefold)
    topic: str | None = None
    source_id: str | None = None
    orientation: str | None = None
    epistemic_class: str | None = None
    statement_basis: str | None = None
    observation_id_prefix: str | None = None


@dataclass
class ObservationRecord:
    path: str
    observation: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "observation": self.observation}


@dataclass
class ObservationListResult:
    operation: str = "observations.list"
    count: int = 0
    observations: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def iter_observation_paths(vault_root: Path) -> Iterable[Path]:
    root = vault_root.resolve() / "observations"
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("**/*.json") if path.is_file())


def load_all_observations(vault_root: Path) -> list[ObservationRecord]:
    vault_root = vault_root.resolve()
    records: list[ObservationRecord] = []
    for path in iter_observation_paths(vault_root):
        try:
            observation = load_observation_document(path)
        except EvidenceVaultError:
            continue
        records.append(
            ObservationRecord(
                path=path.relative_to(vault_root).as_posix(),
                observation=observation,
            )
        )
    return records


def get_observation(vault_root: Path, observation_id: str) -> ObservationRecord | None:
    vault_root = vault_root.resolve()
    path = observation_path(vault_root, observation_id)
    if path.is_file():
        return ObservationRecord(
            path=path.relative_to(vault_root).as_posix(),
            observation=load_observation_document(path),
        )
    # Nested layouts: search by observation_id field.
    for record in load_all_observations(vault_root):
        if record.observation.get("observation_id") == observation_id:
            return record
    return None


def list_observations(vault_root: Path, query: ObservationQuery | None = None) -> list[ObservationRecord]:
    query = query or ObservationQuery()
    matches = [record for record in load_all_observations(vault_root) if _matches(record.observation, query)]
    matches.sort(key=lambda record: _sort_key(record.observation))
    return matches


def list_observations_result(vault_root: Path, query: ObservationQuery | None = None) -> ObservationListResult:
    records = list_observations(vault_root, query)
    return ObservationListResult(
        count=len(records),
        observations=[record.to_dict() for record in records],
    )


def relation_graph(vault_root: Path) -> dict[str, list[dict[str, str]]]:
    """Map observation_id -> outgoing relations [{type, observation_id}]."""
    graph: dict[str, list[dict[str, str]]] = {}
    for record in load_all_observations(vault_root):
        observation_id = record.observation.get("observation_id")
        if not isinstance(observation_id, str):
            continue
        outgoing: list[dict[str, str]] = []
        for relation in record.observation.get("relations", []):
            if not isinstance(relation, dict):
                continue
            rel_type = relation.get("type")
            target = relation.get("observation_id")
            if isinstance(rel_type, str) and isinstance(target, str):
                outgoing.append({"type": rel_type, "observation_id": target})
        graph[observation_id] = outgoing
    return graph


def _matches(observation: dict[str, Any], query: ObservationQuery) -> bool:
    observation_id = observation.get("observation_id")
    if query.observation_id_prefix and (
        not isinstance(observation_id, str) or not observation_id.startswith(query.observation_id_prefix)
    ):
        return False
    if query.orientation and observation.get("orientation") != query.orientation:
        return False
    if query.epistemic_class and observation.get("epistemic_class") != query.epistemic_class:
        return False
    if query.statement_basis and observation.get("statement_basis") != query.statement_basis:
        return False
    if query.subject and not _refs_match(observation.get("subjects"), query.subject):
        return False
    if query.topic and not _refs_match(observation.get("topics"), query.topic):
        return False
    if query.source_id and not _has_source(observation, query.source_id):
        return False
    return True


def _refs_match(refs: object, needle: str) -> bool:
    if not isinstance(refs, list):
        return False
    needle_cf = needle.casefold()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_id = ref.get("ref_id")
        label = ref.get("label")
        if isinstance(ref_id, str) and (ref_id == needle or needle_cf in ref_id.casefold()):
            return True
        if isinstance(label, str) and needle_cf in label.casefold():
            return True
    return False


def _has_source(observation: dict[str, Any], source_id: str) -> bool:
    for locator in observation.get("evidence", []):
        if isinstance(locator, dict) and locator.get("source_id") == source_id:
            return True
    return False


def _sort_key(observation: dict[str, Any]) -> tuple[str, str]:
    valid_at = observation.get("valid_at") or observation.get("expressed_at") or observation.get("recorded_at") or ""
    observation_id = observation.get("observation_id") or ""
    return (str(valid_at), str(observation_id))


def parse_observation_query(args: Any) -> ObservationQuery:
    """Build a query from argparse namespace attributes used by the CLI."""
    return ObservationQuery(
        subject=getattr(args, "subject", None),
        topic=getattr(args, "topic", None),
        source_id=getattr(args, "source_id", None),
        orientation=getattr(args, "orientation", None),
        epistemic_class=getattr(args, "epistemic_class", None),
        statement_basis=getattr(args, "statement_basis", None),
        observation_id_prefix=getattr(args, "id_prefix", None),
    )
