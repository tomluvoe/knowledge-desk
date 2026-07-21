from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.observations import load_all_observations
from knowledge_desk.perspective import applies_at, parse_as_of
from knowledge_desk.util import utc_now
from knowledge_desk.validation import validate_vault
from knowledge_desk.wiki import refine_validate_wiki


@dataclass
class LintFinding:
    severity: str  # error | warning | info
    path: str
    code: str
    message: str
    suggested_action: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class LintReport:
    operation: str = "lint"
    valid: bool = False
    vault_valid: bool = False
    findings: list[dict[str, object]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def lint_vault(vault_root: Path) -> LintReport:
    """Semantic/structural lint. Findings are review suggestions; nothing is auto-fixed."""
    vault_root = vault_root.resolve()
    report = LintReport()
    findings: list[LintFinding] = []

    validation = validate_vault(vault_root)
    report.vault_valid = validation.valid
    for error in validation.errors:
        findings.append(
            LintFinding(
                severity="error",
                path=_path_hint(error),
                code="validate_error",
                message=error,
                suggested_action="Repair the canonical artifact so `knowledge-desk validate` passes",
                evidence=[error],
            )
        )

    wiki = refine_validate_wiki(vault_root)
    for item in wiki.findings:
        # Avoid duplicating pure validate errors already listed.
        if item.get("code") == "vault_validate":
            continue
        findings.append(
            LintFinding(
                severity=str(item.get("severity") or "warning"),
                path=str(item.get("path") or "wiki/"),
                code=str(item.get("code") or "wiki"),
                message=str(item.get("message") or ""),
                suggested_action=str(item.get("suggested_action") or "Review the wiki page"),
            )
        )

    findings.extend(_observation_lint_findings(vault_root))

    # De-dupe by code+path+message
    unique: list[LintFinding] = []
    seen: set[str] = set()
    for finding in findings:
        key = f"{finding.code}|{finding.path}|{finding.message}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)

    report.findings = [f.to_dict() for f in unique]
    report.counts = {
        "error": sum(1 for f in unique if f.severity == "error"),
        "warning": sum(1 for f in unique if f.severity == "warning"),
        "info": sum(1 for f in unique if f.severity == "info"),
        "total": len(unique),
    }
    report.valid = report.vault_valid and report.counts["error"] == 0
    report.message = (
        f"lint complete: {report.counts['total']} finding(s) "
        f"({report.counts['error']} error, {report.counts['warning']} warning, {report.counts['info']} info); "
        f"vault_valid={report.vault_valid}"
    )
    return report


def _observation_lint_findings(vault_root: Path) -> list[LintFinding]:
    findings: list[LintFinding] = []
    records = load_all_observations(vault_root)
    by_id = {
        str(r.observation.get("observation_id")): r
        for r in records
        if isinstance(r.observation.get("observation_id"), str)
    }

    # Explicit contradiction pairs still both present without supersession.
    for record in records:
        obs = record.observation
        obs_id = obs.get("observation_id")
        if not isinstance(obs_id, str):
            continue
        for relation in obs.get("relations", []) if isinstance(obs.get("relations"), list) else []:
            if not isinstance(relation, dict) or relation.get("type") != "contradicts":
                continue
            target = relation.get("observation_id")
            if not isinstance(target, str) or target not in by_id:
                continue
            # If neither supersedes the other, flag potential unresolved contradiction.
            if not _has_supersession(obs, by_id[target].observation) and not _has_supersession(
                by_id[target].observation, obs
            ):
                findings.append(
                    LintFinding(
                        severity="info",
                        path=record.path,
                        code="unresolved_contradiction",
                        message=f"{obs_id} contradicts {target} without supersession",
                        suggested_action="Leave as history, or append a superseding observation after review",
                        evidence=[obs_id, target],
                    )
                )

    # Stale/current mismatch hints: freshness.current but horizon ended.
    try:
        now = parse_as_of(utc_now()[:10])
    except Exception:
        now = None
    if now is not None:
        for record in records:
            obs = record.observation
            freshness = obs.get("freshness")
            if not isinstance(freshness, dict) or freshness.get("status") != "current":
                continue
            horizon = obs.get("horizon")
            if isinstance(horizon, dict) and isinstance(horizon.get("end"), str):
                try:
                    end = parse_as_of(horizon["end"])
                except Exception:
                    continue
                if end < now and applies_at(obs, end):
                    findings.append(
                        LintFinding(
                            severity="warning",
                            path=record.path,
                            code="stale_current_claim",
                            message=(
                                f"{obs.get('observation_id')} marked freshness.current but horizon ended {horizon['end']}"
                            ),
                            suggested_action="Set freshness to historical/stale or append an updated observation",
                            evidence=[str(obs.get("observation_id"))],
                        )
                    )

    # Inferred statement without reasoning is weak provenance.
    for record in records:
        obs = record.observation
        if obs.get("statement_basis") == "agent_inference":
            reasoning = str(obs.get("reasoning") or "").strip()
            if len(reasoning) < 8:
                findings.append(
                    LintFinding(
                        severity="warning",
                        path=record.path,
                        code="thin_inference_rationale",
                        message=f"{obs.get('observation_id')} is agent_inference with thin reasoning",
                        suggested_action="Document the inference rationale and evidence basis",
                        evidence=[str(obs.get("observation_id"))],
                    )
                )

    return findings


def _has_supersession(left: dict[str, Any], right: dict[str, Any]) -> bool:
    right_id = right.get("observation_id")
    for relation in left.get("relations", []) if isinstance(left.get("relations"), list) else []:
        if (
            isinstance(relation, dict)
            and relation.get("type") == "supersedes"
            and relation.get("observation_id") == right_id
        ):
            return True
    return False


def _path_hint(error: str) -> str:
    if ":" in error:
        return error.split(":", 1)[0]
    return "<vault>"
