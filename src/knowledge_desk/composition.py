"""Cross-MCP composition: join external context with vault evidence without coupling.

Knowledge Desk does not store private external state (portfolio, CRM, calendar, …)
by default. Consuming agents call external MCPs and this desk separately, then join
results at query time. This module provides a domain-neutral claim envelope and
join helpers so citations identify which system supplied each fact and whether
the fact was explicit or inferred.

No core schema names domain-specific objects (holdings, securities, tickets, …).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.util import utc_now


VAULT_ORIGIN = "knowledge-desk"
ALLOWED_EPISTEMIC = frozenset({"explicit", "inferred", "unknown"})
ALLOWED_ORIGIN_KIND = frozenset({"vault", "external_mcp", "agent_reasoning"})


@dataclass
class ContextClaim:
    """One fact or assertion in a multi-MCP composition bundle."""

    claim_id: str
    text: str
    origin: str  # MCP / system name, e.g. knowledge-desk | example-portfolio-mcp
    origin_kind: str  # vault | external_mcp | agent_reasoning
    epistemic: str  # explicit | inferred | unknown
    confidence: float | None = None
    as_of: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    subject_refs: list[str] = field(default_factory=list)
    topic_refs: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompositionBundle:
    """Joined view for an agent reasoning layer. Never written to the vault by default."""

    operation: str = "compose.join"
    status: str = "failed"
    question: str = ""
    composed_at: str = ""
    vault_origin: str = VAULT_ORIGIN
    external_claims: list[dict[str, Any]] = field(default_factory=list)
    vault_claims: list[dict[str, Any]] = field(default_factory=list)
    agent_notes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_claim_id(prefix: str = "claim") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def make_claim(
    text: str,
    *,
    origin: str,
    origin_kind: str,
    epistemic: str,
    claim_id: str | None = None,
    confidence: float | None = None,
    as_of: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    subject_refs: list[str] | None = None,
    topic_refs: list[str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> ContextClaim:
    if not (text or "").strip():
        raise KnowledgeDeskError("claim text is required")
    if origin_kind not in ALLOWED_ORIGIN_KIND:
        raise KnowledgeDeskError(
            f"origin_kind must be one of {sorted(ALLOWED_ORIGIN_KIND)}; got {origin_kind!r}"
        )
    if epistemic not in ALLOWED_EPISTEMIC:
        raise KnowledgeDeskError(
            f"epistemic must be one of {sorted(ALLOWED_EPISTEMIC)}; got {epistemic!r}"
        )
    if not (origin or "").strip():
        raise KnowledgeDeskError("origin (MCP/system name) is required")
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise KnowledgeDeskError("confidence must be between 0 and 1 inclusive")
    return ContextClaim(
        claim_id=claim_id or new_claim_id(),
        text=text.strip(),
        origin=origin.strip(),
        origin_kind=origin_kind,
        epistemic=epistemic,
        confidence=None if confidence is None else float(confidence),
        as_of=as_of,
        citations=list(citations or []),
        subject_refs=list(subject_refs or []),
        topic_refs=list(topic_refs or []),
        extensions=dict(extensions or {}),
    )


def parse_external_claims(payload: Any) -> list[ContextClaim]:
    """Parse client-supplied external MCP facts. Never persisted by this helper."""
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KnowledgeDeskError(f"external_context_json is not valid JSON: {exc}") from exc

    items: list[Any]
    if isinstance(payload, dict):
        if isinstance(payload.get("claims"), list):
            items = payload["claims"]
        elif isinstance(payload.get("external_claims"), list):
            items = payload["external_claims"]
        else:
            # Single claim object
            items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        raise KnowledgeDeskError("external context must be a JSON object or array of claims")

    claims: list[ContextClaim] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise KnowledgeDeskError(f"external claim at index {index} must be an object")
        text = item.get("text") or item.get("assertion") or item.get("statement")
        if not isinstance(text, str) or not text.strip():
            raise KnowledgeDeskError(f"external claim at index {index} needs text/assertion")
        origin = str(item.get("origin") or item.get("mcp") or item.get("source_system") or "external")
        origin_kind = str(item.get("origin_kind") or "external_mcp")
        if origin_kind == "vault":
            raise KnowledgeDeskError(
                "external claims must not use origin_kind=vault; use vault tools for desk facts"
            )
        if origin_kind not in ALLOWED_ORIGIN_KIND:
            origin_kind = "external_mcp"
        epistemic = str(item.get("epistemic") or item.get("statement_basis_class") or "unknown")
        # Map common loose values
        if epistemic in {"explicit_statement", "disclosed", "observed"}:
            epistemic = "explicit"
        elif epistemic in {"inferred", "agent_inference", "hypothesis"}:
            epistemic = "inferred"
        elif epistemic not in ALLOWED_EPISTEMIC:
            epistemic = "unknown"
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise KnowledgeDeskError(f"external claim {index}: invalid confidence") from exc
        citations = item.get("citations") if isinstance(item.get("citations"), list) else []
        # Keep opaque external refs only as citation objects with system tags.
        normalized_citations: list[dict[str, Any]] = []
        for cite in citations:
            if isinstance(cite, dict):
                normalized_citations.append(dict(cite))
            elif isinstance(cite, str):
                normalized_citations.append({"ref": cite, "origin": origin})
        claims.append(
            make_claim(
                text,
                claim_id=str(item["claim_id"]) if isinstance(item.get("claim_id"), str) else None,
                origin=origin,
                origin_kind=origin_kind if origin_kind != "vault" else "external_mcp",
                epistemic=epistemic,
                confidence=confidence,
                as_of=item.get("as_of") if isinstance(item.get("as_of"), str) else None,
                citations=normalized_citations,
                subject_refs=_string_list(item.get("subject_refs") or item.get("subjects")),
                topic_refs=_string_list(item.get("topic_refs") or item.get("topics")),
                extensions=item.get("extensions") if isinstance(item.get("extensions"), dict) else {},
            )
        )
    return claims


def claims_from_perspective(payload: dict[str, Any], *, origin: str = VAULT_ORIGIN) -> list[ContextClaim]:
    """Project a perspective_at (or similar) result into stamped vault claims."""
    claims: list[ContextClaim] = []
    status = payload.get("status")
    subject = payload.get("subject") or payload.get("subject_query")
    topic = payload.get("topic") or payload.get("topic_query")
    subject_refs = [str(subject)] if subject else []
    topic_refs = [str(topic)] if topic else []
    as_of = payload.get("as_of") if isinstance(payload.get("as_of"), str) else None

    if status in {None, "unknown"} or payload.get("reason") == "insufficient_evidence":
        claims.append(
            make_claim(
                str(
                    payload.get("message")
                    or payload.get("reason")
                    or "insufficient vault evidence for this subject+topic at as_of"
                ),
                origin=origin,
                origin_kind="vault",
                epistemic="unknown",
                as_of=as_of,
                subject_refs=subject_refs,
                topic_refs=topic_refs,
                citations=[],
                extensions={"perspective_status": status or "unknown"},
            )
        )
        return claims

    primary = payload.get("observation") if isinstance(payload.get("observation"), dict) else None
    assertion = None
    if primary and isinstance(primary.get("assertion"), str):
        assertion = primary["assertion"]
    elif isinstance(payload.get("assertion"), str):
        assertion = payload["assertion"]

    if assertion and assertion.strip():
        epistemic = _epistemic_from_observation(primary or {
            "statement_basis": payload.get("statement_basis"),
            "epistemic_class": payload.get("epistemic_class"),
        })
        citations = _citations_from_observation(primary) if primary else []
        if not citations and isinstance(payload.get("evidence"), list):
            for locator in payload["evidence"]:
                if isinstance(locator, dict):
                    citations.append({"mcp": origin, "layer": "source", **{
                        k: locator[k]
                        for k in (
                            "source_id",
                            "source_hash",
                            "normalized_path",
                            "locator_kind",
                            "selector",
                        )
                        if k in locator
                    }})
        if isinstance(payload.get("observation_id"), str):
            citations.insert(0, {
                "mcp": origin,
                "layer": "observation",
                "observation_id": payload["observation_id"],
            })
        claims.append(
            make_claim(
                assertion,
                origin=origin,
                origin_kind="vault",
                epistemic=epistemic,
                confidence=_as_float(payload.get("confidence") if primary is None else primary.get("confidence")),
                as_of=as_of or (_obs_time(primary) if primary else None),
                citations=citations,
                subject_refs=subject_refs,
                topic_refs=topic_refs,
                extensions={
                    "observation_id": payload.get("observation_id") or (primary or {}).get("observation_id"),
                    "orientation": payload.get("orientation") or (primary or {}).get("orientation"),
                    "perspective_status": status,
                    "statement_basis": payload.get("statement_basis"),
                },
            )
        )

    # Conflicting ids only — stamp an explicit note so agents do not hide disagreement.
    conflict_ids = payload.get("conflicting_observation_ids")
    if isinstance(conflict_ids, list) and conflict_ids:
        claims.append(
            make_claim(
                "Vault perspective is conflicted; additional observation_ids disagree with the primary.",
                origin=origin,
                origin_kind="vault",
                epistemic="explicit",
                as_of=as_of,
                subject_refs=subject_refs,
                topic_refs=topic_refs,
                citations=[
                    {"mcp": origin, "layer": "observation", "observation_id": oid}
                    for oid in conflict_ids
                    if isinstance(oid, str)
                ],
                extensions={"perspective_status": "conflicted", "conflicting_observation_ids": conflict_ids},
            )
        )

    if not claims:
        claims.append(
            make_claim(
                str(payload.get("message") or f"perspective status={status}"),
                origin=origin,
                origin_kind="vault",
                epistemic="unknown",
                as_of=as_of,
                subject_refs=subject_refs,
                topic_refs=topic_refs,
                extensions={"perspective_status": status},
            )
        )
    return claims


def claims_from_explore_ask(payload: dict[str, Any], *, origin: str = VAULT_ORIGIN) -> list[ContextClaim]:
    """Project explore_ask results into vault claims (excerpts, not wiki consensus)."""
    claims: list[ContextClaim] = []
    status = payload.get("status")
    if status != "answered":
        claims.append(
            make_claim(
                str(payload.get("message") or payload.get("reason") or "insufficient vault evidence"),
                origin=origin,
                origin_kind="vault",
                epistemic="unknown",
                extensions={"ask_status": status, "reason": payload.get("reason")},
            )
        )
        return claims

    for citation in payload.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        quote = citation.get("quote") or citation.get("text")
        if not isinstance(quote, str) or not quote.strip():
            continue
        layer = citation.get("layer")
        epistemic = "explicit" if layer == "source" else "unknown"
        if layer == "observation":
            epistemic = "explicit"
        claims.append(
            make_claim(
                quote,
                origin=origin,
                origin_kind="vault",
                epistemic=epistemic,
                citations=[
                    {
                        "mcp": origin,
                        "layer": layer,
                        "vault_id": citation.get("vault_id"),
                        "path": citation.get("path"),
                        "locator_kind": citation.get("locator_kind"),
                        "selector": citation.get("selector"),
                    }
                ],
                extensions={"ask_status": "answered"},
            )
        )
    if not claims and isinstance(payload.get("answer"), str):
        claims.append(
            make_claim(
                payload["answer"],
                origin=origin,
                origin_kind="vault",
                epistemic="unknown",
                extensions={"ask_status": "answered", "note": "answer_without_structured_citations"},
            )
        )
    return claims


def join_contexts(
    *,
    question: str,
    external_claims: list[ContextClaim] | list[dict[str, Any]] | None = None,
    vault_claims: list[ContextClaim] | list[dict[str, Any]] | None = None,
    agent_notes: list[ContextClaim] | list[dict[str, Any]] | None = None,
) -> CompositionBundle:
    """Join external + vault claims into one bundle. Read-only; does not write the vault."""
    bundle = CompositionBundle(
        question=(question or "").strip(),
        composed_at=utc_now(),
        policy={
            "vault_stores_external_state_by_default": False,
            "import_requires_explicit_proposal_or_ingest": True,
            "mcp_read_only": True,
            "origins_must_be_stamped": True,
            "epistemic_values": sorted(ALLOWED_EPISTEMIC),
        },
    )
    if not bundle.question:
        bundle.message = "question is required for composition"
        return bundle

    try:
        ext = _normalize_claim_list(external_claims, default_origin_kind="external_mcp")
        vault = _normalize_claim_list(vault_claims, default_origin_kind="vault")
        notes = _normalize_claim_list(agent_notes, default_origin_kind="agent_reasoning")
    except KnowledgeDeskError as exc:
        bundle.message = str(exc)
        return bundle

    for claim in vault:
        if claim.origin_kind != "vault":
            bundle.warnings.append(
                f"vault claim {claim.claim_id} has origin_kind={claim.origin_kind!r}; expected vault"
            )
        if claim.origin != VAULT_ORIGIN:
            # Allow alternate desk names but warn.
            bundle.warnings.append(
                f"vault claim {claim.claim_id} origin={claim.origin!r} (expected {VAULT_ORIGIN!r})"
            )

    for claim in ext:
        if claim.origin_kind == "vault":
            bundle.warnings.append(
                f"external claim {claim.claim_id} incorrectly marked origin_kind=vault"
            )
        if claim.epistemic == "inferred":
            bundle.warnings.append(
                f"external claim {claim.claim_id} is inferred — do not treat as explicit external fact"
            )

    bundle.external_claims = [c.to_dict() for c in ext]
    bundle.vault_claims = [c.to_dict() for c in vault]
    bundle.agent_notes = [c.to_dict() for c in notes]
    bundle.status = "composed"
    bundle.message = (
        f"joined {len(ext)} external claim(s) with {len(vault)} vault claim(s) "
        f"and {len(notes)} agent note(s); nothing written to the vault"
    )
    return bundle


def compose_with_vault(
    vault_root: Path,
    *,
    question: str,
    external_context: Any = None,
    subject: str | None = None,
    topic: str | None = None,
    as_of: str | None = None,
    include_ask: bool = True,
    ask_limit: int = 5,
) -> CompositionBundle:
    """Fetch vault perspective/ask (read-only) and join with caller-supplied external claims."""
    from knowledge_desk import read_api

    warnings: list[str] = []
    try:
        external = parse_external_claims(external_context)
    except KnowledgeDeskError as exc:
        bundle = CompositionBundle(question=question or "", composed_at=utc_now(), status="failed")
        bundle.message = str(exc)
        return bundle

    vault_claims: list[ContextClaim] = []

    if subject and topic and as_of:
        perspective = read_api.get_perspective_at(vault_root, subject, topic, as_of)
        vault_claims.extend(claims_from_perspective(perspective))
    elif subject and topic and not as_of:
        warnings.append("subject+topic provided without as_of; skipped perspective_at")

    if include_ask and (question or "").strip():
        ask = read_api.explore_ask_api(
            vault_root,
            question,
            limit=ask_limit,
            subject=subject,
            topic=topic,
        )
        vault_claims.extend(claims_from_explore_ask(ask))

    bundle = join_contexts(question=question, external_claims=external, vault_claims=vault_claims)
    bundle.warnings = list(dict.fromkeys([*warnings, *bundle.warnings]))
    if bundle.status == "composed" and not vault_claims and not external:
        bundle.warnings.append("no external or vault claims to join")
    return bundle


def composition_contract() -> dict[str, Any]:
    """Machine-readable contract for agents orchestrating multiple MCPs."""
    return {
        "operation": "compose.contract",
        "api_version": "1.0.0",
        "vault_origin": VAULT_ORIGIN,
        "policy": {
            "vault_stores_external_state_by_default": False,
            "join_at_query_time": True,
            "import_requires_explicit_workflow": True,
            "knowledge_desk_mcp_is_read_only": True,
            "citations_must_identify_origin_mcp": True,
        },
        "claim_fields": {
            "claim_id": "stable id for this composition session",
            "text": "fact or assertion text",
            "origin": "MCP or system name that supplied the fact",
            "origin_kind": sorted(ALLOWED_ORIGIN_KIND),
            "epistemic": sorted(ALLOWED_EPISTEMIC),
            "confidence": "optional 0..1",
            "as_of": "optional RFC3339 or date when the fact was valid",
            "citations": "vault locators or opaque external refs",
            "subject_refs": "optional entity-… style refs when known",
            "topic_refs": "optional topic-… style refs when known",
        },
        "orchestration_recipe": [
            "Call external MCP(s) for private/live context; stamp each fact with origin + epistemic.",
            "Call knowledge-desk MCP (search, perspective, explore_ask) for corpus evidence.",
            "Join with compose_with_external / compose join — do not write external state into the vault.",
            "Reasoning layer compares datasets; mark agent inferences as origin_kind=agent_reasoning.",
            "Only if the user explicitly wants durable import: fetch/ingest or proposal workflow.",
        ],
        "example_domains_without_core_schema_coupling": [
            "portfolio / exposures (external MCP) + people/mechanisms/timelines (vault)",
            "project tickets (external) + design decisions in vault sources",
            "CRM accounts (external) + research notes in vault",
            "calendar events (external) + meeting notes sources in vault",
            "codebase MCP (external) + architecture decision records in vault",
        ],
    }


def _normalize_claim_list(
    items: list[ContextClaim] | list[dict[str, Any]] | None,
    *,
    default_origin_kind: str,
) -> list[ContextClaim]:
    if not items:
        return []
    out: list[ContextClaim] = []
    for item in items:
        if isinstance(item, ContextClaim):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise KnowledgeDeskError("claims must be ContextClaim or objects")
        if default_origin_kind == "external_mcp":
            out.extend(parse_external_claims([item]))
        else:
            text = item.get("text") or item.get("assertion")
            if not isinstance(text, str):
                raise KnowledgeDeskError("claim object needs text")
            out.append(
                make_claim(
                    text,
                    claim_id=item.get("claim_id") if isinstance(item.get("claim_id"), str) else None,
                    origin=str(item.get("origin") or VAULT_ORIGIN),
                    origin_kind=str(item.get("origin_kind") or default_origin_kind),
                    epistemic=str(item.get("epistemic") or "unknown"),
                    confidence=_as_float(item.get("confidence")),
                    as_of=item.get("as_of") if isinstance(item.get("as_of"), str) else None,
                    citations=item.get("citations") if isinstance(item.get("citations"), list) else [],
                    subject_refs=_string_list(item.get("subject_refs")),
                    topic_refs=_string_list(item.get("topic_refs")),
                    extensions=item.get("extensions") if isinstance(item.get("extensions"), dict) else {},
                )
            )
    return out


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                ref = item.get("ref_id") or item.get("label") or item.get("id")
                if isinstance(ref, str) and ref.strip():
                    out.append(ref.strip())
        return out
    return []


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epistemic_from_observation(obs: dict[str, Any]) -> str:
    basis = obs.get("statement_basis")
    epistemic_class = obs.get("epistemic_class")
    if basis == "explicit_statement" or epistemic_class == "source_statement":
        return "explicit"
    if basis == "agent_inference" or epistemic_class == "agent_hypothesis":
        return "inferred"
    if basis in {"disclosed_action", "hypothetical_example"}:
        return "explicit" if basis == "disclosed_action" else "inferred"
    return "unknown"


def _obs_time(obs: dict[str, Any]) -> str | None:
    for key in ("valid_at", "expressed_at", "recorded_at", "publication_date"):
        value = obs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _citations_from_observation(obs: dict[str, Any]) -> list[dict[str, Any]]:
    cites: list[dict[str, Any]] = []
    oid = obs.get("observation_id")
    if isinstance(oid, str):
        cites.append({"mcp": VAULT_ORIGIN, "layer": "observation", "observation_id": oid})
    for locator in obs.get("evidence") or []:
        if isinstance(locator, dict):
            cites.append({"mcp": VAULT_ORIGIN, "layer": "source", **{
                k: locator[k]
                for k in (
                    "source_id",
                    "source_hash",
                    "normalized_path",
                    "locator_kind",
                    "selector",
                    "quote_sha256",
                )
                if k in locator
            }})
    return cites
