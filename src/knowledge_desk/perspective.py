from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.observations import ObservationQuery, list_observations


@dataclass
class PerspectiveResult:
    operation: str = "perspective.at"
    subject: str = ""
    topic: str = ""
    as_of: str = ""
    status: str = "unknown"  # supported | unknown | conflicted
    reason: str = ""
    orientation: str | None = None
    assertion: str | None = None
    observation_id: str | None = None
    statement_basis: str | None = None
    epistemic_class: str | None = None
    confidence: float | None = None
    freshness: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    supporting_observation_ids: list[str] = field(default_factory=list)
    superseded_observation_ids: list[str] = field(default_factory=list)
    conflicting_observation_ids: list[str] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    observation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class TimelineEvent:
    at: str
    observation_id: str
    change: str  # introduced | confirms | refines | contradicts | supersedes
    orientation: str | None
    assertion: str
    related_observation_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class TimelineResult:
    operation: str = "perspective.timeline"
    subject: str = ""
    topic: str = ""
    start: str | None = None
    end: str | None = None
    status: str = "unknown"
    reason: str = ""
    events: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_as_of(value: str) -> datetime:
    """Parse a date (YYYY-MM-DD) or RFC3339/ISO datetime into an aware UTC datetime."""
    text = value.strip()
    if not text:
        raise KnowledgeDeskError("as_of must be a non-empty date or datetime")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise KnowledgeDeskError(f"invalid as_of date: {value}") from exc
        return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KnowledgeDeskError(f"invalid as_of datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def effective_time(observation: dict[str, Any]) -> datetime | None:
    """When the observation begins to speak for a perspective."""
    for key in ("valid_at", "expressed_at", "publication_date", "recorded_at"):
        value = observation.get(key)
        if value is None or value == "":
            continue
        try:
            if isinstance(value, str) and len(value) == 10 and value[4] == "-":
                day = date.fromisoformat(value)
                return datetime.combine(day, time.min, tzinfo=timezone.utc)
            return parse_as_of(str(value))
        except (KnowledgeDeskError, ValueError):
            continue
    return None


def horizon_covers(observation: dict[str, Any], as_of: datetime) -> bool:
    horizon = observation.get("horizon")
    if horizon is None:
        return True
    if not isinstance(horizon, dict):
        return True
    as_of_day = as_of.date()
    start = horizon.get("start")
    end = horizon.get("end")
    if isinstance(start, str) and start:
        try:
            if as_of_day < date.fromisoformat(start):
                return False
        except ValueError:
            return False
    if isinstance(end, str) and end:
        try:
            if as_of_day > date.fromisoformat(end):
                return False
        except ValueError:
            return False
    return True


def applies_at(observation: dict[str, Any], as_of: datetime) -> bool:
    when = effective_time(observation)
    if when is None:
        return False
    if when > as_of:
        return False
    return horizon_covers(observation, as_of)


def perspective_at(
    vault_root: Path,
    subject: str,
    topic: str,
    as_of: str | datetime,
) -> PerspectiveResult:
    """Return the supported perspective for subject+topic as of a time.

    Missing evidence yields status ``unknown`` with reason ``insufficient_evidence``.
    Orientation ``unknown`` on a real observation is still ``supported`` (explicit unknown).
    """
    if isinstance(as_of, datetime):
        as_of_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        as_of_label = as_of_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        as_of_dt = parse_as_of(as_of)
        as_of_label = as_of if isinstance(as_of, str) else str(as_of)

    result = PerspectiveResult(subject=subject, topic=topic, as_of=as_of_label)
    records = list_observations(vault_root, ObservationQuery(subject=subject, topic=topic))
    applying = [record.observation for record in records if applies_at(record.observation, as_of_dt)]
    if not applying:
        result.status = "unknown"
        result.reason = "insufficient_evidence"
        return result

    by_id = {
        obs["observation_id"]: obs
        for obs in applying
        if isinstance(obs.get("observation_id"), str)
    }
    superseded: set[str] = set()
    for obs in applying:
        obs_id = obs.get("observation_id")
        if not isinstance(obs_id, str):
            continue
        for relation in obs.get("relations", []):
            if not isinstance(relation, dict):
                continue
            if relation.get("type") != "supersedes":
                continue
            target = relation.get("observation_id")
            if isinstance(target, str) and target in by_id:
                superseded.add(target)

    active = [obs for obs_id, obs in by_id.items() if obs_id not in superseded]
    if not active:
        result.status = "unknown"
        result.reason = "insufficient_evidence"
        result.superseded_observation_ids = sorted(superseded)
        return result

    active.sort(key=lambda obs: (effective_time(obs) or datetime.min.replace(tzinfo=timezone.utc), obs.get("observation_id") or ""))
    primary = active[-1]
    primary_id = str(primary["observation_id"])

    conflicts = [
        obs
        for obs in active
        if obs.get("observation_id") != primary_id
        and _is_conflict(primary, obs)
    ]
    supporting = [
        obs
        for obs in active
        if obs.get("observation_id") != primary_id and not _is_conflict(primary, obs)
    ]

    result.observation_id = primary_id
    result.observation = primary
    result.assertion = primary.get("assertion") if isinstance(primary.get("assertion"), str) else None
    result.orientation = primary.get("orientation") if isinstance(primary.get("orientation"), str) else None
    result.statement_basis = primary.get("statement_basis") if isinstance(primary.get("statement_basis"), str) else None
    result.epistemic_class = primary.get("epistemic_class") if isinstance(primary.get("epistemic_class"), str) else None
    confidence = primary.get("confidence")
    result.confidence = float(confidence) if isinstance(confidence, (int, float)) else None
    freshness = primary.get("freshness")
    result.freshness = dict(freshness) if isinstance(freshness, dict) else None
    evidence = primary.get("evidence")
    result.evidence = list(evidence) if isinstance(evidence, list) else []
    result.supporting_observation_ids = [
        str(obs["observation_id"]) for obs in supporting if isinstance(obs.get("observation_id"), str)
    ]
    result.superseded_observation_ids = sorted(superseded)
    result.conflicting_observation_ids = [
        str(obs["observation_id"]) for obs in conflicts if isinstance(obs.get("observation_id"), str)
    ]
    result.related = _related_edges(primary, by_id)

    if conflicts:
        result.status = "conflicted"
        result.reason = "multiple_active_perspectives"
    else:
        result.status = "supported"
        result.reason = "observation_applies"
    return result


def perspective_timeline(
    vault_root: Path,
    subject: str,
    topic: str,
    start: str | None = None,
    end: str | None = None,
) -> TimelineResult:
    """List meaningful perspective changes for subject+topic over an optional range."""
    result = TimelineResult(subject=subject, topic=topic, start=start, end=end)
    start_dt = parse_as_of(start) if start else None
    end_dt = parse_as_of(end) if end else None
    records = list_observations(vault_root, ObservationQuery(subject=subject, topic=topic))
    if not records:
        result.status = "unknown"
        result.reason = "insufficient_evidence"
        return result

    events: list[TimelineEvent] = []
    for record in records:
        obs = record.observation
        when = effective_time(obs)
        if when is None:
            continue
        if start_dt and when < start_dt:
            continue
        if end_dt and when > end_dt:
            continue
        obs_id = obs.get("observation_id")
        if not isinstance(obs_id, str):
            continue
        change = "introduced"
        related_id: str | None = None
        for relation in obs.get("relations", []):
            if not isinstance(relation, dict):
                continue
            rel_type = relation.get("type")
            target = relation.get("observation_id")
            if rel_type in {"confirms", "refines", "contradicts", "supersedes"} and isinstance(target, str):
                change = str(rel_type)
                related_id = target
                break
        events.append(
            TimelineEvent(
                at=when.isoformat().replace("+00:00", "Z"),
                observation_id=obs_id,
                change=change,
                orientation=obs.get("orientation") if isinstance(obs.get("orientation"), str) else None,
                assertion=str(obs.get("assertion") or ""),
                related_observation_id=related_id,
            )
        )
    events.sort(key=lambda item: (item.at, item.observation_id))
    result.events = [event.to_dict() for event in events]
    if not events:
        result.status = "unknown"
        result.reason = "insufficient_evidence"
    else:
        result.status = "supported"
        result.reason = "timeline_events"
    return result


def _is_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when two active observations disagree in orientation or explicitly contradict."""
    for obs, other in ((left, right), (right, left)):
        other_id = other.get("observation_id")
        for relation in obs.get("relations", []):
            if (
                isinstance(relation, dict)
                and relation.get("type") == "contradicts"
                and relation.get("observation_id") == other_id
            ):
                return True
    left_orientation = left.get("orientation")
    right_orientation = right.get("orientation")
    if (
        isinstance(left_orientation, str)
        and isinstance(right_orientation, str)
        and left_orientation != right_orientation
        and left_orientation != "unknown"
        and right_orientation != "unknown"
        and {left_orientation, right_orientation} != {"neutral", "mixed"}
    ):
        # Distinct non-unknown orientations without a confirms/refines link are conflicted.
        if not _soft_agreement(left, right):
            return True
    return False


def _soft_agreement(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for obs, other in ((left, right), (right, left)):
        other_id = other.get("observation_id")
        for relation in obs.get("relations", []):
            if (
                isinstance(relation, dict)
                and relation.get("type") in {"confirms", "refines"}
                and relation.get("observation_id") == other_id
            ):
                return True
    return False


def _related_edges(primary: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for relation in primary.get("relations", []):
        if not isinstance(relation, dict):
            continue
        rel_type = relation.get("type")
        target = relation.get("observation_id")
        if isinstance(rel_type, str) and isinstance(target, str) and target in by_id:
            edges.append({"type": rel_type, "observation_id": target})
    return edges


@dataclass
class CompareResult:
    operation: str = "perspective.compare"
    topic: str = ""
    as_of: str = ""
    status: str = "unknown"  # compared | partial | unknown
    reason: str = ""
    subjects: list[dict[str, object]] = field(default_factory=list)
    dimensions: list[dict[str, object]] = field(default_factory=list)
    agreements: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    insufficient: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_perspectives(
    vault_root: Path,
    subjects: list[str],
    topic: str,
    as_of: str | datetime,
    *,
    topics: list[str] | None = None,
) -> CompareResult:
    """Compare two or more subjects on a topic (or topics) as of a date.

    Returns visible dimensions rather than a single opaque similarity score.
    Missing evidence is listed under ``insufficient`` — never filled with neutral.
    """
    if len(subjects) < 2:
        raise KnowledgeDeskError("compare requires at least two --subject values")
    topic_list = topics or [topic]
    if not topic_list or not all(topic_list):
        raise KnowledgeDeskError("compare requires a topic")

    if isinstance(as_of, datetime):
        as_of_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        as_of_label = as_of_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        as_of_label = as_of
        as_of_dt = parse_as_of(as_of)

    # Primary topic drives top-level result; multi-topic expands dimensions.
    primary_topic = topic_list[0]
    result = CompareResult(topic=primary_topic if len(topic_list) == 1 else ",".join(topic_list), as_of=as_of_label)

    subject_rows: list[dict[str, object]] = []
    perspectives: dict[str, dict[str, PerspectiveResult]] = {}
    for subject in subjects:
        perspectives[subject] = {}
        for topic_name in topic_list:
            perspective = perspective_at(vault_root, subject, topic_name, as_of_dt)
            perspectives[subject][topic_name] = perspective
        # Summarize primary topic for subject row.
        primary = perspectives[subject][primary_topic]
        subject_rows.append(
            {
                "subject": subject,
                "status": primary.status,
                "reason": primary.reason,
                "orientation": primary.orientation,
                "assertion": primary.assertion,
                "observation_id": primary.observation_id,
                "statement_basis": primary.statement_basis,
                "epistemic_class": primary.epistemic_class,
                "confidence": primary.confidence,
                "freshness": primary.freshness,
            }
        )
        if primary.status == "unknown":
            result.insufficient.append(subject)

    result.subjects = subject_rows

    dimensions: list[dict[str, object]] = []
    for topic_name in topic_list:
        for dimension in (
            "evidence_status",
            "orientation",
            "assertion",
            "statement_basis",
            "epistemic_class",
            "confidence",
            "mechanisms",
            "conditions",
            "risks",
            "implications",
            "horizon",
            "freshness_status",
        ):
            values: dict[str, object] = {}
            for subject in subjects:
                perspective = perspectives[subject][topic_name]
                values[subject] = _dimension_value(perspective, dimension)
            agreement = _values_agree(values)
            dimensions.append(
                {
                    "topic": topic_name,
                    "dimension": dimension,
                    "values": values,
                    "agreement": agreement,  # agree | disagree | mixed | insufficient
                }
            )
            label = f"{topic_name}:{dimension}"
            if agreement == "agree":
                result.agreements.append(label)
            elif agreement == "disagree":
                result.disagreements.append(label)
            elif agreement == "insufficient":
                pass  # already tracked per subject

    result.dimensions = dimensions
    supported = [row for row in subject_rows if row["status"] in {"supported", "conflicted"}]
    if not supported:
        result.status = "unknown"
        result.reason = "insufficient_evidence"
    elif len(supported) < len(subjects):
        result.status = "partial"
        result.reason = "some_subjects_lack_evidence"
    else:
        result.status = "compared"
        result.reason = "dimensions_populated"
    return result


def _dimension_value(perspective: PerspectiveResult, dimension: str) -> object:
    if perspective.status == "unknown":
        return None
    observation = perspective.observation or {}
    if dimension == "evidence_status":
        return perspective.status
    if dimension == "orientation":
        return perspective.orientation
    if dimension == "assertion":
        return perspective.assertion
    if dimension == "statement_basis":
        return perspective.statement_basis
    if dimension == "epistemic_class":
        return perspective.epistemic_class
    if dimension == "confidence":
        return perspective.confidence
    if dimension == "mechanisms":
        return list(observation.get("mechanisms") or [])
    if dimension == "conditions":
        return list(observation.get("conditions") or [])
    if dimension == "risks":
        return list(observation.get("risks") or [])
    if dimension == "implications":
        return list(observation.get("implications") or [])
    if dimension == "horizon":
        return observation.get("horizon")
    if dimension == "freshness_status":
        freshness = perspective.freshness or {}
        return freshness.get("status")
    return None


def _values_agree(values: dict[str, object]) -> str:
    present = [(subject, value) for subject, value in values.items() if value is not None]
    if len(present) < 2:
        return "insufficient"
    first = present[0][1]
    # Normalize lists for comparison.
    def normalize(value: object) -> object:
        if isinstance(value, list):
            return tuple(sorted(str(item) for item in value))
        if isinstance(value, dict):
            return json_dumps_stable(value)
        return value

    first_n = normalize(first)
    if all(normalize(value) == first_n for _, value in present):
        return "agree"
    # Special-case empty lists as agreement with empty.
    if all(value == [] or value is None for _, value in present):
        return "agree"
    return "disagree"


def json_dumps_stable(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
