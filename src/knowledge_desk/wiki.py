from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.observations import ObservationQuery, list_observations, load_all_observations
from knowledge_desk.util import (
    SCHEMA_VERSION,
    parse_frontmatter,
    render_frontmatter,
    replace_text_synced,
    utc_now,
)
from knowledge_desk.validation import validate_vault


KIND_DIRS = {
    "entity": "entities",
    "topic": "topics",
    "event": "events",
    "comparison": "comparisons",
    "synthesis": "syntheses",
}


@dataclass
class WikiPageChange:
    status: str  # created | updated | unchanged
    path: str
    wiki_id: str
    kind: str
    title: str
    observation_ids: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class WikiEvolveResult:
    operation: str = "wiki.evolve"
    status: str = "failed"
    pages: list[dict[str, object]] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class WikiFinding:
    severity: str  # error | warning | info
    path: str
    code: str
    message: str
    suggested_action: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class WikiRefineResult:
    operation: str = "wiki.refine-validate"
    status: str = "failed"
    valid: bool = False
    vault_valid: bool = False
    findings: list[dict[str, object]] = field(default_factory=list)
    checked_pages: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evolve_wiki(
    vault_root: Path,
    *,
    observation_ids: list[str] | None = None,
    subject: str | None = None,
    topic: str | None = None,
) -> WikiEvolveResult:
    """Compile sources/observations into a cited living wiki.

    Creates/updates entity and topic pages, source summaries, cross-source
    topic syntheses, and comparison pages when subjects disagree. Mechanical
    (no LLM prose): cites observations, preserves source-specific positions,
    and separates consensus / disagreement / uncertainty.
    """
    from knowledge_desk.writer import vault_write_lock

    with vault_write_lock(vault_root):
        return _evolve_wiki_unlocked(
            vault_root,
            observation_ids=observation_ids,
            subject=subject,
            topic=topic,
        )


def _evolve_wiki_unlocked(
    vault_root: Path,
    *,
    observation_ids: list[str] | None = None,
    subject: str | None = None,
    topic: str | None = None,
) -> WikiEvolveResult:
    vault_root = vault_root.resolve()
    result = WikiEvolveResult()
    try:
        if observation_ids:
            records = []
            for observation_id in observation_ids:
                matches = list_observations(vault_root, ObservationQuery(observation_id_prefix=observation_id))
                records.extend(
                    record for record in matches if record.observation.get("observation_id") == observation_id
                )
        else:
            records = list_observations(
                vault_root,
                ObservationQuery(subject=subject, topic=topic),
            )
        if not records:
            result.status = "noop"
            result.message = "no matching observations to evolve from"
            return result

        observations = [record.observation for record in records]

        # Group observations by subject and topic refs.
        pages: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for obs in observations:
            for ref in obs.get("subjects", []) if isinstance(obs.get("subjects"), list) else []:
                if isinstance(ref, dict) and ref.get("kind") == "entity" and isinstance(ref.get("ref_id"), str):
                    key = ("entity", ref["ref_id"], str(ref.get("label") or ref["ref_id"]))
                    pages.setdefault(key, []).append(obs)
            for ref in obs.get("topics", []) if isinstance(obs.get("topics"), list) else []:
                if isinstance(ref, dict) and ref.get("kind") == "topic" and isinstance(ref.get("ref_id"), str):
                    key = ("topic", ref["ref_id"], str(ref.get("label") or ref["ref_id"]))
                    pages.setdefault(key, []).append(obs)

        changes: list[WikiPageChange] = []
        for (kind, ref_id, label), obs_group in sorted(pages.items(), key=lambda item: item[0][1]):
            changes.append(
                _write_wiki_page(
                    vault_root,
                    kind=kind,
                    ref_id=ref_id,
                    title=label,
                    observations=obs_group,
                )
            )

        # Living-wiki compile products beyond entity/topic pages.
        changes.extend(_write_source_summaries(vault_root, observations))
        changes.extend(_write_topic_syntheses(vault_root, observations))
        changes.extend(_write_comparisons(vault_root, observations))
        changes.extend(_write_event_pages(vault_root, observations))

        result.pages = [change.to_dict() for change in changes]
        result.status = "evolved" if any(change.status in {"created", "updated"} for change in changes) else "noop"
        result.message = f"processed {len(changes)} wiki page(s) from observations"
        return result
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result.message = str(exc)
        return result


def refine_validate_wiki(vault_root: Path) -> WikiRefineResult:
    """Run vault validate plus wiki-focused semantic/structural findings."""
    vault_root = vault_root.resolve()
    result = WikiRefineResult()
    report = validate_vault(vault_root)
    result.vault_valid = report.valid
    findings: list[WikiFinding] = []
    if not report.valid:
        for error in report.errors:
            if error.startswith("wiki/") or "wiki" in error:
                findings.append(
                    WikiFinding(
                        severity="error",
                        path=_path_from_error(error),
                        code="vault_validate",
                        message=error,
                        suggested_action="Fix the schema/locator issue reported by validate",
                    )
                )

    observation_ids = {
        record.observation.get("observation_id")
        for record in load_all_observations(vault_root)
        if isinstance(record.observation.get("observation_id"), str)
    }
    pages = list(_iter_wiki_pages(vault_root))
    result.checked_pages = len(pages)
    titles: dict[str, list[str]] = {}

    for path, metadata, body in pages:
        rel = path.relative_to(vault_root).as_posix()
        title = str(metadata.get("title") or path.stem)
        titles.setdefault(title.casefold(), []).append(rel)
        obs_ids = metadata.get("observation_ids") if isinstance(metadata.get("observation_ids"), list) else []
        evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), list) else []
        material = _material_body(body)

        if material and not obs_ids and not evidence:
            findings.append(
                WikiFinding(
                    severity="error",
                    path=rel,
                    code="unsupported_synthesis",
                    message="wiki body has material prose without observation_ids or evidence",
                    suggested_action="Link observations/evidence or remove unsupported claims",
                )
            )
        if not obs_ids and not evidence:
            findings.append(
                WikiFinding(
                    severity="warning",
                    path=rel,
                    code="orphan_page",
                    message="wiki page has no observation_ids and no evidence locators",
                    suggested_action="Attach observations or delete/merge the page",
                )
            )
        for observation_id in obs_ids:
            if not isinstance(observation_id, str):
                continue
            if observation_id not in observation_ids:
                findings.append(
                    WikiFinding(
                        severity="error",
                        path=rel,
                        code="dangling_observation",
                        message=f"observation_ids references missing observation {observation_id}",
                        suggested_action="Remove the id or append the observation",
                    )
                )
        if obs_ids and not evidence:
            findings.append(
                WikiFinding(
                    severity="warning",
                    path=rel,
                    code="missing_evidence",
                    message="observation_ids present but evidence array is empty",
                    suggested_action="Copy evidence locators from linked observations",
                )
            )
        freshness = metadata.get("freshness")
        if freshness == "unknown" and obs_ids:
            findings.append(
                WikiFinding(
                    severity="info",
                    path=rel,
                    code="freshness_unknown",
                    message="freshness is unknown while observations are linked",
                    suggested_action="Set freshness from the latest observation status when known",
                )
            )
        wiki_id = metadata.get("wiki_id")
        if isinstance(wiki_id, str) and not re.fullmatch(r"wiki-[a-z0-9]+(?:-[a-z0-9]+)*", wiki_id):
            findings.append(
                WikiFinding(
                    severity="error",
                    path=rel,
                    code="invalid_wiki_id",
                    message=f"wiki_id {wiki_id!r} does not match the schema pattern",
                    suggested_action="Rename wiki_id to wiki-<slug>",
                )
            )

    for title_key, paths in titles.items():
        if len(paths) > 1:
            for path in paths:
                findings.append(
                    WikiFinding(
                        severity="warning",
                        path=path,
                        code="near_duplicate_title",
                        message=f"duplicate title among wiki pages: {title_key!r}",
                        suggested_action="Merge pages or disambiguate titles",
                    )
                )

    result.findings = [finding.to_dict() for finding in findings]
    has_errors = any(item["severity"] == "error" for item in result.findings)
    result.valid = report.valid and not has_errors
    result.status = "ok" if result.valid else "findings"
    result.message = (
        f"checked {result.checked_pages} wiki page(s); "
        f"{len(result.findings)} finding(s); vault_valid={report.valid}"
    )
    return result


def _write_wiki_page(
    vault_root: Path,
    *,
    kind: str,
    ref_id: str,
    title: str,
    observations: list[dict[str, Any]],
    body_builder: Any | None = None,
    wiki_id: str | None = None,
    slug: str | None = None,
) -> WikiPageChange:
    slug = slug or _slug_from_ref(ref_id, kind)
    wiki_id = wiki_id or f"wiki-{kind}-{slug}"
    directory = vault_root / "wiki" / KIND_DIRS.get(kind, "syntheses")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.md"
    relative = path.relative_to(vault_root).as_posix()
    now = utc_now()

    observation_ids = sorted(
        {
            str(obs["observation_id"])
            for obs in observations
            if isinstance(obs.get("observation_id"), str)
        }
    )
    evidence = _collect_evidence(observations)
    freshness = _aggregate_freshness(observations)
    as_of = _as_of_from_observations(observations)

    prior_ids: list[str] = []
    created_at = now
    status = "created"
    if path.is_file():
        try:
            existing_meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if isinstance(existing_meta.get("created_at"), str):
                created_at = existing_meta["created_at"]
            # Preserve any extra observation ids already on the page (do not erase reviewed links).
            prior_raw = existing_meta.get("observation_ids") if isinstance(existing_meta.get("observation_ids"), list) else []
            prior_ids = [i for i in prior_raw if isinstance(i, str)]
            observation_ids = sorted(set(observation_ids) | set(prior_ids))
            status = "updated"
        except (OSError, ValueError, UnicodeDecodeError):
            status = "created"

    if body_builder is None:
        body = _render_living_body(title, observations, prior_observation_ids=prior_ids, as_of=as_of)
    else:
        body = body_builder(title, observations, prior_ids, as_of)

    extensions: dict[str, Any] = {}
    if as_of:
        extensions["knowledge.desk.wiki"] = {"as_of": as_of}

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "wiki_id": wiki_id,
        "kind": kind,
        "title": title,
        "created_at": created_at,
        "updated_at": now,
        "observation_ids": observation_ids,
        "evidence": evidence,
        "freshness": freshness,
        "extensions": extensions,
    }
    document = render_frontmatter(metadata) + "\n" + body
    if path.is_file() and path.read_text(encoding="utf-8") == document:
        return WikiPageChange(
            status="unchanged",
            path=relative,
            wiki_id=wiki_id,
            kind=kind,
            title=title,
            observation_ids=observation_ids,
            message="page already current",
        )
    replace_text_synced(path, document)
    return WikiPageChange(
        status=status,
        path=relative,
        wiki_id=wiki_id,
        kind=kind,
        title=title,
        observation_ids=observation_ids,
        message=f"wiki page {status} from {len(observation_ids)} observation(s)",
    )


def _write_source_summaries(vault_root: Path, observations: list[dict[str, Any]]) -> list[WikiPageChange]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str):
                by_source.setdefault(locator["source_id"], []).append(obs)
    changes: list[WikiPageChange] = []
    for source_id, group in sorted(by_source.items()):
        # Dedupe observations that cite the same source multiple times.
        unique = _unique_observations(group)
        short = source_id.removeprefix("src-")[:12]
        title = f"Source summary: {source_id}"
        slug = f"source-{short}"
        changes.append(
            _write_wiki_page(
                vault_root,
                kind="synthesis",
                ref_id=source_id,
                title=title,
                observations=unique,
                wiki_id=f"wiki-source-summary-{short}",
                slug=slug,
                body_builder=_render_source_summary_body,
            )
        )
    return changes


def _write_topic_syntheses(vault_root: Path, observations: list[dict[str, Any]]) -> list[WikiPageChange]:
    """Cross-source topic syntheses when a topic has evidence from 2+ sources."""
    by_topic: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for obs in observations:
        for ref in obs.get("topics", []) if isinstance(obs.get("topics"), list) else []:
            if isinstance(ref, dict) and ref.get("kind") == "topic" and isinstance(ref.get("ref_id"), str):
                label = str(ref.get("label") or ref["ref_id"])
                by_topic.setdefault((ref["ref_id"], label), []).append(obs)
    changes: list[WikiPageChange] = []
    for (ref_id, label), group in sorted(by_topic.items()):
        unique = _unique_observations(group)
        sources = _source_ids(unique)
        if len(sources) < 2:
            continue
        slug = f"synthesis-{_slug_from_ref(ref_id, 'topic')}"
        changes.append(
            _write_wiki_page(
                vault_root,
                kind="synthesis",
                ref_id=ref_id,
                title=f"Cross-source: {label}",
                observations=unique,
                wiki_id=f"wiki-synthesis-{_slug_from_ref(ref_id, 'topic')}",
                slug=slug,
                body_builder=_render_cross_source_body,
            )
        )
    return changes


def _write_comparisons(vault_root: Path, observations: list[dict[str, Any]]) -> list[WikiPageChange]:
    """Comparison pages when two+ entities share a topic with differing orientations."""
    # topic_ref -> entity_ref -> list of obs
    matrix: dict[tuple[str, str], dict[tuple[str, str], list[dict[str, Any]]]] = {}
    for obs in observations:
        subjects = [
            ref
            for ref in (obs.get("subjects") if isinstance(obs.get("subjects"), list) else [])
            if isinstance(ref, dict) and ref.get("kind") == "entity" and isinstance(ref.get("ref_id"), str)
        ]
        topics = [
            ref
            for ref in (obs.get("topics") if isinstance(obs.get("topics"), list) else [])
            if isinstance(ref, dict) and ref.get("kind") == "topic" and isinstance(ref.get("ref_id"), str)
        ]
        for topic in topics:
            tkey = (topic["ref_id"], str(topic.get("label") or topic["ref_id"]))
            for subject in subjects:
                skey = (subject["ref_id"], str(subject.get("label") or subject["ref_id"]))
                matrix.setdefault(tkey, {}).setdefault(skey, []).append(obs)

    changes: list[WikiPageChange] = []
    for (topic_id, topic_label), entities in sorted(matrix.items()):
        if len(entities) < 2:
            continue
        # Differing orientations across entities?
        entity_orientations: dict[str, set[str]] = {}
        all_obs: list[dict[str, Any]] = []
        for (entity_id, _label), group in entities.items():
            orients = {
                str(obs.get("orientation"))
                for obs in group
                if isinstance(obs.get("orientation"), str)
            }
            entity_orientations[entity_id] = orients
            all_obs.extend(group)
        flat_orients = {o for values in entity_orientations.values() for o in values}
        if len(flat_orients) < 2 and not _has_contradict_relation(all_obs):
            continue
        unique = _unique_observations(all_obs)
        slug = f"compare-{_slug_from_ref(topic_id, 'topic')}"
        entity_labels = sorted({label for (_, label) in entities.keys()})
        title = f"Comparison: {topic_label} ({', '.join(entity_labels[:4])})"
        changes.append(
            _write_wiki_page(
                vault_root,
                kind="comparison",
                ref_id=topic_id,
                title=title,
                observations=unique,
                wiki_id=f"wiki-comparison-{_slug_from_ref(topic_id, 'topic')}",
                slug=slug,
                body_builder=lambda title, obs, prior, as_of, ents=entities: _render_comparison_body(
                    title, obs, prior, as_of, entities=ents
                ),
            )
        )
    return changes


def _write_event_pages(vault_root: Path, observations: list[dict[str, Any]]) -> list[WikiPageChange]:
    """Event pages group observations that share the same valid_at calendar day."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        day = _calendar_day(obs)
        if day:
            by_day.setdefault(day, []).append(obs)
    changes: list[WikiPageChange] = []
    for day, group in sorted(by_day.items()):
        unique = _unique_observations(group)
        if len(unique) < 2:
            continue
        subjects = sorted(
            {
                str(ref.get("label") or ref.get("ref_id"))
                for obs in unique
                for ref in (obs.get("subjects") if isinstance(obs.get("subjects"), list) else [])
                if isinstance(ref, dict) and ref.get("kind") == "entity"
            }
        )
        title = f"Event cluster {day}" + (f": {', '.join(subjects[:3])}" if subjects else "")
        slug = f"event-{day}"
        changes.append(
            _write_wiki_page(
                vault_root,
                kind="event",
                ref_id=f"event-{day}",
                title=title,
                observations=unique,
                wiki_id=f"wiki-event-{day}",
                slug=slug,
                body_builder=_render_event_body,
            )
        )
    return changes


def _collect_evidence(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obs in observations:
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if not isinstance(locator, dict):
                continue
            # Drop optional quote for wiki embedding stability if present is fine to keep.
            key = json.dumps(locator, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            cleaned = {
                field: locator[field]
                for field in ("source_id", "source_hash", "normalized_path", "locator_kind", "selector")
                if field in locator
            }
            if "quote_sha256" in locator:
                cleaned["quote_sha256"] = locator["quote_sha256"]
            if len(cleaned) >= 5:
                collected.append(cleaned)
    return collected


def _aggregate_freshness(observations: list[dict[str, Any]]) -> str:
    statuses: list[str] = []
    for obs in observations:
        freshness = obs.get("freshness")
        if isinstance(freshness, dict) and isinstance(freshness.get("status"), str):
            statuses.append(freshness["status"])
    if not statuses:
        return "unknown"
    if any(status == "current" for status in statuses):
        return "current"
    if all(status == "historical" for status in statuses):
        return "historical"
    if any(status == "stale" for status in statuses):
        return "stale"
    return "unknown"


def _render_living_body(
    title: str,
    observations: list[dict[str, Any]],
    *,
    prior_observation_ids: list[str] | None = None,
    as_of: str | None = None,
) -> str:
    prior = set(prior_observation_ids or [])
    ordered = _ordered_observations(observations)
    lines = [
        f"# {title}",
        "",
        f"_As of {as_of or 'unknown'}. Reversible synthesis from linked observations "
        f"(`knowledge-desk wiki evolve`). Not primary evidence._",
        "",
        "## Source-specific positions",
        "",
        "Positions are kept per source; they are not averaged into a false consensus.",
        "",
    ]
    by_source = _group_by_source(ordered)
    if not by_source:
        lines.append("_No source locators on linked observations._")
        lines.append("")
    for source_id, group in sorted(by_source.items()):
        lines.append(f"### `{source_id}`")
        lines.append("")
        for obs in group:
            lines.extend(_observation_bullet(obs))
        lines.append("")

    consensus, disagreement, uncertainty = _partition_epistemic(ordered)
    lines.extend(["## Consensus", ""])
    if consensus:
        for obs in consensus:
            lines.append(f"- **{obs.get('observation_id')}**: {obs.get('assertion')}")
    else:
        lines.append("No multi-observation consensus detected (need confirming relations or matching orientations).")
    lines.extend(["", "## Disagreement", ""])
    if disagreement:
        for obs in disagreement:
            lines.append(
                f"- **{obs.get('observation_id')}** ({obs.get('orientation')}): {obs.get('assertion')}"
            )
    else:
        lines.append("No explicit disagreement among linked observations.")
    lines.extend(["", "## Uncertainty and open questions", ""])
    if uncertainty:
        for obs in uncertainty:
            lines.append(
                f"- **{obs.get('observation_id')}** ({obs.get('orientation')}, "
                f"{obs.get('epistemic_class')}): {obs.get('assertion')}"
            )
    else:
        lines.append(
            "No uncertain/unknown orientations or agent hypotheses. "
            "Record open questions in memory/ or as observations with orientation `unknown`."
        )

    lines.extend(["", "## Contradictions and supersession", ""])
    relation_lines = _relation_lines(ordered)
    if relation_lines:
        lines.extend(relation_lines)
    else:
        lines.append("No explicit confirms/contradicts/refines/supersedes relations among linked observations.")

    lines.extend(["", "## What changed", ""])
    new_ids = [
        str(obs.get("observation_id"))
        for obs in ordered
        if isinstance(obs.get("observation_id"), str) and obs.get("observation_id") not in prior
    ]
    if prior and new_ids:
        lines.append(f"- Newly linked observations: {', '.join(f'`{i}`' for i in new_ids)}")
        lines.append(f"- Previously linked: {len(prior)} observation(s) retained")
    elif not prior:
        lines.append(f"- Initial compile from {len(ordered)} observation(s)")
    else:
        lines.append("- No new observation links since last evolve (content regenerated for consistency)")
    lines.append("- Regeneration does not drop prior observation_ids without an explicit page edit.")
    lines.append("")
    return "\n".join(lines)


def _render_source_summary_body(
    title: str,
    observations: list[dict[str, Any]],
    prior_observation_ids: list[str],
    as_of: str | None,
) -> str:
    ordered = _ordered_observations(observations)
    lines = [
        f"# {title}",
        "",
        f"_As of {as_of or 'unknown'}. Source-summary synthesis (revisable)._ ",
        "",
        "## Claims attributed to this source",
        "",
    ]
    for obs in ordered:
        lines.extend(_observation_bullet(obs))
    lines.extend(["", "## Linked subjects and topics", ""])
    subjects = sorted(_ref_labels(ordered, "subjects", "entity"))
    topics = sorted(_ref_labels(ordered, "topics", "topic"))
    lines.append(f"- Subjects: {', '.join(subjects) if subjects else 'none'}")
    lines.append(f"- Topics: {', '.join(topics) if topics else 'none'}")
    lines.extend(["", "## What changed", ""])
    lines.append(f"- Observations on this summary: {len(ordered)}")
    if prior_observation_ids:
        lines.append(f"- Prior observation_ids retained: {len(prior_observation_ids)}")
    lines.append("")
    return "\n".join(lines)


def _render_cross_source_body(
    title: str,
    observations: list[dict[str, Any]],
    prior_observation_ids: list[str],
    as_of: str | None,
) -> str:
    ordered = _ordered_observations(observations)
    lines = [
        f"# {title}",
        "",
        f"_As of {as_of or 'unknown'}. Cross-source synthesis; positions stay source-specific._",
        "",
        "## By source",
        "",
    ]
    for source_id, group in sorted(_group_by_source(ordered).items()):
        lines.append(f"### `{source_id}`")
        lines.append("")
        for obs in group:
            lines.extend(_observation_bullet(obs))
        lines.append("")
    consensus, disagreement, uncertainty = _partition_epistemic(ordered)
    lines.extend(["## Consensus", ""])
    lines.extend(
        [f"- **{o.get('observation_id')}**: {o.get('assertion')}" for o in consensus]
        or ["No multi-observation consensus detected."]
    )
    lines.extend(["", "## Disagreement", ""])
    lines.extend(
        [
            f"- **{o.get('observation_id')}** ({o.get('orientation')}): {o.get('assertion')}"
            for o in disagreement
        ]
        or ["No explicit disagreement."]
    )
    lines.extend(["", "## Uncertainty", ""])
    lines.extend(
        [
            f"- **{o.get('observation_id')}**: {o.get('assertion')}"
            for o in uncertainty
        ]
        or ["None flagged."]
    )
    lines.extend(["", "## What changed", ""])
    lines.append(f"- Compiled from {len(ordered)} observation(s) across {len(_source_ids(ordered))} source(s)")
    if prior_observation_ids:
        lines.append(f"- Prior links retained: {len(prior_observation_ids)}")
    lines.append("")
    return "\n".join(lines)


def _render_comparison_body(
    title: str,
    observations: list[dict[str, Any]],
    prior_observation_ids: list[str],
    as_of: str | None,
    *,
    entities: dict[tuple[str, str], list[dict[str, Any]]],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"_As of {as_of or 'unknown'}. Comparison keeps each subject's position distinct._",
        "",
        "## Positions by subject",
        "",
    ]
    for (entity_id, label), group in sorted(entities.items(), key=lambda item: item[0][0]):
        lines.append(f"### {label} (`{entity_id}`)")
        lines.append("")
        for obs in _ordered_observations(_unique_observations(group)):
            lines.extend(_observation_bullet(obs))
        lines.append("")
    lines.extend(["## Relations", ""])
    rels = _relation_lines(_ordered_observations(observations))
    lines.extend(rels or ["No explicit inter-observation relations."])
    lines.extend(["", "## What changed", ""])
    lines.append(f"- Subjects compared: {len(entities)}")
    lines.append(f"- Observations: {len(_unique_observations(observations))}")
    if prior_observation_ids:
        lines.append(f"- Prior links retained: {len(prior_observation_ids)}")
    lines.append("")
    return "\n".join(lines)


def _render_event_body(
    title: str,
    observations: list[dict[str, Any]],
    prior_observation_ids: list[str],
    as_of: str | None,
) -> str:
    ordered = _ordered_observations(observations)
    lines = [
        f"# {title}",
        "",
        f"_As of {as_of or 'unknown'}. Event cluster from shared valid_at day._",
        "",
        "## Timeline notes",
        "",
    ]
    for obs in ordered:
        day = _calendar_day(obs) or "?"
        lines.append(f"- **{day}** `{obs.get('observation_id')}`: {obs.get('assertion')}")
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if isinstance(locator, dict):
                lines.append(f"  - evidence: `{locator.get('source_id')}`")
    lines.extend(["", "## What changed", ""])
    lines.append(f"- Observations in cluster: {len(ordered)}")
    if prior_observation_ids:
        lines.append(f"- Prior links retained: {len(prior_observation_ids)}")
    lines.append("")
    return "\n".join(lines)


def _observation_bullet(obs: dict[str, Any]) -> list[str]:
    lines = [
        f"- **{obs.get('observation_id')}** ({obs.get('orientation')}): {obs.get('assertion')}"
    ]
    for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
        if not isinstance(locator, dict):
            continue
        lines.append(
            f"  - evidence: `{locator.get('source_id')}` "
            f"{locator.get('locator_kind')} {json.dumps(locator.get('selector'), ensure_ascii=False, sort_keys=True)}"
        )
    statement_basis = obs.get("statement_basis")
    if statement_basis:
        lines.append(f"  - statement_basis: {statement_basis}")
    return lines


def _ordered_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        observations,
        key=lambda obs: (
            str(obs.get("valid_at") or obs.get("expressed_at") or obs.get("recorded_at") or ""),
            str(obs.get("observation_id") or ""),
        ),
    )


def _unique_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for obs in observations:
        oid = obs.get("observation_id")
        if not isinstance(oid, str) or oid in seen:
            continue
        seen.add(oid)
        unique.append(obs)
    return unique


def _source_ids(observations: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for obs in observations:
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str):
                ids.add(locator["source_id"])
    return ids


def _group_by_source(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        sources = {
            locator["source_id"]
            for locator in (obs.get("evidence") if isinstance(obs.get("evidence"), list) else [])
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str)
        }
        if not sources:
            grouped.setdefault("(no source)", []).append(obs)
            continue
        for source_id in sorted(sources):
            grouped.setdefault(source_id, []).append(obs)
    return grouped


def _partition_epistemic(
    observations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into consensus-ish, disagreement, and uncertainty buckets (mechanical)."""
    by_id = {
        str(obs["observation_id"]): obs
        for obs in observations
        if isinstance(obs.get("observation_id"), str)
    }
    confirmed: set[str] = set()
    contradicted: set[str] = set()
    for obs in observations:
        oid = obs.get("observation_id")
        if not isinstance(oid, str):
            continue
        for relation in obs.get("relations", []) if isinstance(obs.get("relations"), list) else []:
            if not isinstance(relation, dict):
                continue
            target = relation.get("observation_id")
            rtype = relation.get("type")
            if rtype == "confirms" and isinstance(target, str):
                confirmed.add(oid)
                confirmed.add(target)
            if rtype == "contradicts" and isinstance(target, str):
                contradicted.add(oid)
                contradicted.add(target)

    orientations = {
        str(obs.get("orientation"))
        for obs in observations
        if isinstance(obs.get("orientation"), str)
    }
    multi_orient = len(orientations - {"unknown"}) > 1

    consensus: list[dict[str, Any]] = []
    disagreement: list[dict[str, Any]] = []
    uncertainty: list[dict[str, Any]] = []
    for obs in _ordered_observations(observations):
        oid = obs.get("observation_id")
        if not isinstance(oid, str):
            continue
        orient = obs.get("orientation")
        epistemic = obs.get("epistemic_class")
        if orient == "unknown" or epistemic == "agent_hypothesis":
            uncertainty.append(obs)
        elif oid in contradicted or (multi_orient and oid not in confirmed and orient in {"supportive", "critical", "opposed"}):
            if multi_orient or oid in contradicted:
                disagreement.append(obs)
            else:
                consensus.append(obs)
        elif oid in confirmed or (not multi_orient and orient not in {None, "unknown"}):
            consensus.append(obs)
        else:
            # Single orientation with no relations: treat as provisional consensus list.
            if not multi_orient:
                consensus.append(obs)
            else:
                disagreement.append(obs)
    # Prefer by_id lookup only for safety; lists already hold full obs.
    _ = by_id
    return consensus, disagreement, uncertainty


def _relation_lines(observations: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for obs in observations:
        for relation in obs.get("relations", []) if isinstance(obs.get("relations"), list) else []:
            if not isinstance(relation, dict):
                continue
            lines.append(
                f"- {obs.get('observation_id')} {relation.get('type')} {relation.get('observation_id')}"
            )
    return lines


def _has_contradict_relation(observations: list[dict[str, Any]]) -> bool:
    for obs in observations:
        for relation in obs.get("relations", []) if isinstance(obs.get("relations"), list) else []:
            if isinstance(relation, dict) and relation.get("type") == "contradicts":
                return True
    return False


def _ref_labels(observations: list[dict[str, Any]], field: str, kind: str) -> set[str]:
    labels: set[str] = set()
    for obs in observations:
        for ref in obs.get(field, []) if isinstance(obs.get(field), list) else []:
            if isinstance(ref, dict) and ref.get("kind") == kind:
                labels.add(str(ref.get("label") or ref.get("ref_id") or ""))
    return {label for label in labels if label}


def _as_of_from_observations(observations: list[dict[str, Any]]) -> str | None:
    stamps = []
    for obs in observations:
        for key in ("valid_at", "expressed_at", "recorded_at", "publication_date"):
            value = obs.get(key)
            if isinstance(value, str) and value:
                stamps.append(value)
                break
    if not stamps:
        return None
    return max(stamps)


def _calendar_day(obs: dict[str, Any]) -> str | None:
    for key in ("valid_at", "expressed_at", "publication_date", "recorded_at"):
        value = obs.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _slug_from_ref(ref_id: str, kind: str) -> str:
    prefix = f"{kind}-"
    slug = ref_id[len(prefix) :] if ref_id.startswith(prefix) else ref_id
    slug = re.sub(r"[^a-z0-9]+", "-", slug.casefold()).strip("-")
    return slug or "item"


def _iter_wiki_pages(vault_root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    root = vault_root / "wiki"
    if not root.is_dir():
        return []
    pages: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(root.glob("**/*.md")):
        if path.name == "README.md":
            continue
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        pages.append((path, metadata, body))
    return pages


def _material_body(body: str) -> bool:
    skip_prefixes = (
        "Mechanical synthesis",
        "No LLM",
        "Agent hypotheses",
        "No explicit",
        "Positions are kept",
        "No multi-observation",
        "No uncertain",
        "Record open questions",
        "Regeneration does not",
        "Initial compile",
        "No new observation",
        "Newly linked",
        "Previously linked",
        "Compiled from",
        "Prior ",
        "Observations ",
        "Subjects ",
        "Topics:",
        "Subjects:",
        "None flagged",
        "No source locators",
        "_As of",
        "As of ",
    )
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(stripped.startswith(prefix) for prefix in skip_prefixes):
            continue
        if stripped.startswith("- "):
            lines.append(stripped)
            continue
        lines.append(stripped)
    return bool(lines)


def _path_from_error(error: str) -> str:
    if ":" in error:
        return error.split(":", 1)[0]
    return "wiki/"
