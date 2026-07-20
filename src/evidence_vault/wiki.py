from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evidence_vault.observations import ObservationQuery, list_observations, load_all_observations
from evidence_vault.util import SCHEMA_VERSION, parse_frontmatter, render_frontmatter, utc_now, write_text_synced
from evidence_vault.validation import validate_vault


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
    """Mechanically create/update wiki entity and topic pages from observations.

    LLM-assisted prose is out of scope: pages cite observations and restate assertions.
    """
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

        # Group observations by subject and topic refs.
        pages: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for record in records:
            obs = record.observation
            for ref in obs.get("subjects", []) if isinstance(obs.get("subjects"), list) else []:
                if isinstance(ref, dict) and ref.get("kind") == "entity" and isinstance(ref.get("ref_id"), str):
                    key = ("entity", ref["ref_id"], str(ref.get("label") or ref["ref_id"]))
                    pages.setdefault(key, []).append(obs)
            for ref in obs.get("topics", []) if isinstance(obs.get("topics"), list) else []:
                if isinstance(ref, dict) and ref.get("kind") == "topic" and isinstance(ref.get("ref_id"), str):
                    key = ("topic", ref["ref_id"], str(ref.get("label") or ref["ref_id"]))
                    pages.setdefault(key, []).append(obs)

        changes: list[WikiPageChange] = []
        for (kind, ref_id, label), observations in sorted(pages.items(), key=lambda item: item[0][1]):
            changes.append(_write_wiki_page(vault_root, kind=kind, ref_id=ref_id, title=label, observations=observations))

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
) -> WikiPageChange:
    slug = _slug_from_ref(ref_id, kind)
    wiki_id = f"wiki-{kind}-{slug}"
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
    body = _render_body(title, observations)

    created_at = now
    status = "created"
    if path.is_file():
        try:
            existing_meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if isinstance(existing_meta.get("created_at"), str):
                created_at = existing_meta["created_at"]
            # Preserve any extra observation ids already on the page.
            prior_ids = existing_meta.get("observation_ids") if isinstance(existing_meta.get("observation_ids"), list) else []
            observation_ids = sorted(set(observation_ids) | {i for i in prior_ids if isinstance(i, str)})
            status = "updated"
        except (OSError, ValueError, UnicodeDecodeError):
            status = "created"

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
        "extensions": {},
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
    write_text_synced(path, document)
    return WikiPageChange(
        status=status,
        path=relative,
        wiki_id=wiki_id,
        kind=kind,
        title=title,
        observation_ids=observation_ids,
        message=f"wiki page {status} from {len(observation_ids)} observation(s)",
    )


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


def _render_body(title: str, observations: list[dict[str, Any]]) -> str:
    lines = [
        f"# {title}",
        "",
        "## What the evidence says",
        "",
        "Mechanical synthesis from linked observations (`evidence-vault wiki evolve`). "
        "This page is revisable synthesis, not primary evidence.",
        "",
        "### Observations",
        "",
    ]
    ordered = sorted(
        observations,
        key=lambda obs: (
            str(obs.get("valid_at") or obs.get("expressed_at") or obs.get("recorded_at") or ""),
            str(obs.get("observation_id") or ""),
        ),
    )
    for obs in ordered:
        observation_id = obs.get("observation_id")
        orientation = obs.get("orientation")
        assertion = obs.get("assertion")
        lines.append(f"- **{observation_id}** ({orientation}): {assertion}")
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
    lines.extend(
        [
            "",
            "## Cross-source synthesis",
            "",
            "No LLM synthesis applied. Compare observation orientations and relations manually or via "
            "`evidence-vault perspective`.",
            "",
            "## Hypotheses and uncertainty",
            "",
            "Agent hypotheses are not invented by evolve. Record them as separate observations with "
            "`epistemic_class: agent_hypothesis` before promoting into wiki prose.",
            "",
            "## Contradictions and supersession",
            "",
        ]
    )
    relation_lines: list[str] = []
    for obs in ordered:
        for relation in obs.get("relations", []) if isinstance(obs.get("relations"), list) else []:
            if not isinstance(relation, dict):
                continue
            relation_lines.append(
                f"- {obs.get('observation_id')} {relation.get('type')} {relation.get('observation_id')}"
            )
    if relation_lines:
        lines.extend(relation_lines)
    else:
        lines.append("No explicit confirms/contradicts/refines/supersedes relations among linked observations.")
    lines.append("")
    return "\n".join(lines)


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
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("Mechanical synthesis"):
            continue
        if stripped.startswith("No LLM") or stripped.startswith("Agent hypotheses") or stripped.startswith("No explicit"):
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
