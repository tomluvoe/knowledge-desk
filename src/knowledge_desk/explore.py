from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.index import index_path, search_index
from knowledge_desk.observations import load_all_observations
from knowledge_desk.util import normalized_content, parse_frontmatter, safe_filename, utc_now, write_json_synced, write_text_synced


@dataclass
class GapEntry:
    source_id: str
    title: str
    path: str
    missing: list[str]  # observation | wiki | both implied via list
    observation_ids: list[str] = field(default_factory=list)
    wiki_ids: list[str] = field(default_factory=list)
    content_preview: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class GapsResult:
    operation: str = "explore.gaps"
    status: str = "failed"
    count: int = 0
    gaps: list[dict[str, object]] = field(default_factory=list)
    covered_sources: int = 0
    total_sources: int = 0
    message: str = ""
    proposal_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class AskCitation:
    layer: str
    vault_id: str
    path: str
    locator_kind: str
    selector: dict[str, Any]
    quote: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class AskResult:
    operation: str = "explore.ask"
    status: str = "unknown"  # answered | insufficient_evidence
    question: str = ""
    answer: str | None = None
    reason: str = ""
    filters: dict[str, str | None] = field(default_factory=dict)
    citations: list[dict[str, object]] = field(default_factory=list)
    layers_consulted: list[str] = field(default_factory=list)
    message: str = ""
    proposal_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def explore_gaps(
    vault_root: Path,
    *,
    source_id: str | None = None,
    topic: str | None = None,
    propose: bool = False,
) -> GapsResult:
    """Report sources with missing observation and/or wiki coverage."""
    vault_root = vault_root.resolve()
    result = GapsResult()
    sources = _load_sources(vault_root)
    if source_id:
        sources = [item for item in sources if item["source_id"] == source_id]
    result.total_sources = len(sources)

    obs_by_source = _observations_by_source(vault_root)
    wiki_by_source = _wiki_by_source(vault_root)

    # Optional topic filter: keep sources that either mention the topic in body
    # or are already linked from observations matching the topic label/ref.
    if topic:
        topic_cf = topic.casefold()
        topic_sources: set[str] = set()
        for sid, records in obs_by_source.items():
            for record in records:
                obs = record["observation"]
                for ref in list(obs.get("topics") or []):
                    if not isinstance(ref, dict):
                        continue
                    if topic_cf in str(ref.get("ref_id", "")).casefold() or topic_cf in str(ref.get("label", "")).casefold():
                        topic_sources.add(sid)
        filtered = []
        for item in sources:
            if item["source_id"] in topic_sources:
                filtered.append(item)
            elif topic_cf in item["body"].casefold() or topic_cf in item["title"].casefold():
                filtered.append(item)
        sources = filtered
        result.total_sources = len(sources)

    gaps: list[GapEntry] = []
    covered = 0
    for item in sources:
        sid = item["source_id"]
        observation_ids = [rec["observation_id"] for rec in obs_by_source.get(sid, [])]
        wiki_ids = [rec["wiki_id"] for rec in wiki_by_source.get(sid, [])]
        missing: list[str] = []
        if not observation_ids:
            missing.append("observation")
        if not wiki_ids:
            missing.append("wiki")
        if not missing:
            covered += 1
            continue
        gaps.append(
            GapEntry(
                source_id=sid,
                title=item["title"],
                path=item["path"],
                missing=missing,
                observation_ids=observation_ids,
                wiki_ids=wiki_ids,
                content_preview=_preview(item["body"]),
            )
        )

    result.gaps = [gap.to_dict() for gap in gaps]
    result.count = len(gaps)
    result.covered_sources = covered
    result.status = "ok"
    if not sources:
        result.message = "no sources to analyze"
    elif not gaps:
        result.message = "all analyzed sources have observation and wiki coverage"
    else:
        result.message = f"{len(gaps)} source gap(s); {covered} fully covered"

    if propose and gaps:
        result.proposal_path = _write_gaps_proposal(vault_root, gaps)
        result.message += f"; proposal written to {result.proposal_path}"
    return result


def explore_ask(
    vault_root: Path,
    question: str,
    *,
    limit: int = 5,
    propose: bool = False,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
) -> AskResult:
    """Answer from sources (and observations) with citations, or explicit insufficiency.

    Optional subject/topic/source_id filters AND with the query. Out-of-scope hits are
    never used; empty scope yields insufficient_evidence with reason no_matches_in_filter.
    Does not invent wiki consensus. Prefer exact source passages; observations are secondary.
    """
    vault_root = vault_root.resolve()
    result = AskResult(
        question=question,
        filters={"subject": subject, "topic": topic, "source_id": source_id},
    )
    cleaned = " ".join(question.split())
    if not cleaned:
        result.status = "insufficient_evidence"
        result.reason = "empty_question"
        result.message = "question must be non-empty"
        return result

    terms = _query_terms(cleaned)
    scope = _resolve_ask_scope(vault_root, subject=subject, topic=topic, source_id=source_id)
    has_filter = any(v is not None and str(v).strip() for v in (subject, topic, source_id))
    citations: list[AskCitation] = []
    layers: list[str] = []
    query_text = " OR ".join(terms) if terms else cleaned

    # Prefer disposable index when present; always fall back to direct scan.
    if index_path(vault_root).is_file():
        for layer in ("source", "observation"):
            search = search_index(
                vault_root,
                query_text,
                layer=layer,
                subject=subject,
                topic=topic,
                source_id=source_id,
                limit=limit * 3,
            )
            if search.message == "ok" and search.hits:
                for hit in search.hits:
                    if not _hit_in_scope(hit, scope, layer=layer, has_filter=has_filter):
                        continue
                    citation = _citation_from_hit(vault_root, hit)
                    if citation:
                        citations.append(citation)
                        if layer not in layers:
                            layers.append(layer)
                    if len(citations) >= limit:
                        break
            if len(citations) >= limit:
                break

    if len(citations) < limit:
        allowed_sources = scope.get("source_ids")
        scanned = _scan_sources_for_terms(
            vault_root,
            terms,
            limit=limit,
            allowed_source_ids=allowed_sources if has_filter else None,
        )
        if scanned:
            if "source" not in layers:
                layers.append("source")
            citations.extend(scanned)

    # Observation-first scoped path when filters name subject/topic but FTS missed assertions.
    if len(citations) < limit and has_filter and (subject or topic):
        from knowledge_desk.observations import ObservationQuery, list_observations

        for record in list_observations(
            vault_root,
            ObservationQuery(subject=subject, topic=topic, source_id=source_id),
        ):
            obs = record.observation
            assertion = str(obs.get("assertion") or "")
            lower = assertion.casefold()
            if terms and not any(term.casefold() in lower for term in terms):
                # Still allow if filters alone define scope and assertion is non-empty.
                if not (subject or topic):
                    continue
            citation = AskCitation(
                layer="observation",
                vault_id=str(obs.get("observation_id") or ""),
                path=record.path,
                locator_kind="observation",
                selector={"observation_id": str(obs.get("observation_id") or "")},
                quote=assertion[:500],
            )
            if citation.vault_id and citation.quote:
                citations.append(citation)
                if "observation" not in layers:
                    layers.append("observation")
            if len(citations) >= limit:
                break

    # Deduplicate by vault_id+quote
    unique: list[AskCitation] = []
    seen: set[str] = set()
    for citation in citations:
        key = f"{citation.vault_id}:{citation.quote}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    citations = unique[:limit]
    result.citations = [item.to_dict() for item in citations]
    result.layers_consulted = sorted(set(layers))

    if not citations:
        result.status = "insufficient_evidence"
        result.reason = "no_matches_in_filter" if has_filter else "no_matching_source_passages"
        result.answer = None
        if has_filter:
            result.message = (
                "no source or observation passages matched the question within the given "
                f"filters (subject={subject!r}, topic={topic!r}, source_id={source_id!r})"
            )
        else:
            result.message = "no source or observation passages matched the question terms"
        if propose:
            result.proposal_path = _write_open_question_proposal(
                vault_root,
                question=cleaned,
                citations=[],
                status="insufficient_evidence",
            )
            result.message += f"; open-question proposal at {result.proposal_path}"
        return result

    # Mechanical answer: quote the strongest source passages; do not synthesize consensus.
    source_citations = [c for c in citations if c.layer == "source"]
    primary = source_citations or citations
    quoted = " ".join(c.quote for c in primary[:3])
    result.status = "answered"
    result.reason = "source_passages_matched"
    scope_note = ""
    if has_filter:
        bits = [f"{k}={v}" for k, v in result.filters.items() if v]
        scope_note = f" (scoped to {', '.join(bits)})"
    result.answer = "Evidence-first excerpts (not wiki consensus)" + scope_note + ": " + quoted
    result.message = f"{len(citations)} citation(s) from {', '.join(result.layers_consulted)}"
    if propose:
        result.proposal_path = _write_observation_stub_proposal(
            vault_root,
            question=cleaned,
            citations=primary,
        )
        result.message += f"; proposal at {result.proposal_path}"
    return result


@dataclass
class CompileFromAskResult:
    operation: str = "explore.compile-from-ask"
    status: str = "failed"  # proposed | noop | insufficient_evidence | failed
    question: str = ""
    ask_status: str = ""
    wiki_health: str = ""  # healthy | thin | missing | unknown
    proposal_path: str | None = None
    proposal_kind: str | None = None
    citations: list[dict[str, object]] = field(default_factory=list)
    filters: dict[str, str | None] = field(default_factory=dict)
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compile_from_ask(
    vault_root: Path,
    question: str,
    *,
    limit: int = 5,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
    propose: bool = True,
) -> CompileFromAskResult:
    """Demand-driven compile: ask → detect thin/missing wiki → proposal (no silent MCP write).

    MCP explore_ask stays read-only. This CLI path turns question traffic into a
    reviewable compile_from_ask_proposal or open-question proposal.
    """
    vault_root = vault_root.resolve()
    result = CompileFromAskResult(
        question=question,
        filters={"subject": subject, "topic": topic, "source_id": source_id},
    )
    ask = explore_ask(
        vault_root,
        question,
        limit=limit,
        propose=False,
        subject=subject,
        topic=topic,
        source_id=source_id,
    )
    # Subject/topic filters only bind sources already linked by observations.
    # Compile-from-ask often runs *before* observations exist — fall back to
    # unfiltered (or source_id-only) retrieval for evidence discovery while still
    # using filters for wiki-health and proposed observation refs.
    used_filter_fallback = False
    if (ask.status != "answered" or not ask.citations) and (subject or topic):
        fallback = explore_ask(
            vault_root,
            question,
            limit=limit,
            propose=False,
            subject=None,
            topic=None,
            source_id=source_id,
        )
        if fallback.status == "answered" and fallback.citations:
            ask = fallback
            used_filter_fallback = True

    result.ask_status = ask.status
    result.citations = list(ask.citations)
    result.details["ask"] = {
        "status": ask.status,
        "reason": ask.reason,
        "message": ask.message,
        "layers_consulted": ask.layers_consulted,
        "filter_fallback": used_filter_fallback,
    }

    if ask.status != "answered" or not ask.citations:
        result.wiki_health = "unknown"
        result.status = "insufficient_evidence"
        result.message = ask.message or "no evidence to compile from"
        if propose:
            # Reuse open-question proposal path.
            citations = [
                AskCitation(
                    layer=str(c.get("layer") or ""),
                    vault_id=str(c.get("vault_id") or ""),
                    path=str(c.get("path") or ""),
                    locator_kind=str(c.get("locator_kind") or ""),
                    selector=c.get("selector") if isinstance(c.get("selector"), dict) else {},
                    quote=str(c.get("quote") or ""),
                )
                for c in ask.citations
                if isinstance(c, dict)
            ]
            result.proposal_path = _write_open_question_proposal(
                vault_root,
                question=" ".join(question.split()),
                citations=citations,
                status="insufficient_evidence",
            )
            result.proposal_kind = "explore_ask_proposal"
            result.message += f"; open-question proposal at {result.proposal_path}"
        return result

    health = _assess_wiki_health(vault_root, subject=subject, topic=topic, citations=ask.citations)
    result.wiki_health = health["status"]
    result.details["wiki_health"] = health

    if health["status"] == "healthy":
        result.status = "noop"
        result.message = (
            f"evidence found ({len(ask.citations)} citation(s)) and wiki coverage is healthy; "
            "no compile proposal written"
        )
        return result

    # thin or missing → structured compile proposal
    if not propose:
        result.status = "noop"
        result.message = (
            f"wiki is {health['status']}; pass propose=True / CLI default to queue a compile proposal"
        )
        return result

    proposal_path = _write_compile_from_ask_proposal(
        vault_root,
        question=" ".join(question.split()),
        ask=ask,
        subject=subject,
        topic=topic,
        wiki_health=health,
    )
    result.proposal_path = proposal_path
    result.proposal_kind = "compile_from_ask_proposal"
    result.status = "proposed"
    result.message = (
        f"wiki {health['status']}; compile_from_ask proposal at {proposal_path} "
        "(review, then proposal apply → observations + wiki evolve)"
    )
    return result


def _assess_wiki_health(
    vault_root: Path,
    *,
    subject: str | None,
    topic: str | None,
    citations: list[dict[str, object]],
) -> dict[str, object]:
    """Mechanical thin/missing check for subject/topic pages and citation overlap."""
    pages = list(_iter_entity_topic_wiki(vault_root))
    subject_cf = subject.casefold() if subject else None
    topic_cf = topic.casefold() if topic else None

    matched: list[dict[str, Any]] = []
    for path, meta, _body in pages:
        kind = meta.get("kind")
        wiki_id = str(meta.get("wiki_id") or path.stem)
        title = str(meta.get("title") or "")
        obs_ids = meta.get("observation_ids") if isinstance(meta.get("observation_ids"), list) else []
        hay = f"{wiki_id} {title} {path.stem}".casefold()
        if subject_cf and kind == "entity" and subject_cf in hay:
            matched.append({"path": path.as_posix(), "wiki_id": wiki_id, "kind": kind, "observation_ids": obs_ids})
        if topic_cf and kind == "topic" and topic_cf in hay:
            matched.append({"path": path.as_posix(), "wiki_id": wiki_id, "kind": kind, "observation_ids": obs_ids})

    # If filters name subject/topic but no page matched → missing.
    if (subject or topic) and not matched:
        return {"status": "missing", "matched_pages": [], "reason": "no entity/topic wiki page matched filters"}

    # No filters: consider wiki thin if zero entity/topic pages overall that cite any hit source.
    citation_obs = {
        str(c.get("vault_id"))
        for c in citations
        if isinstance(c, dict) and c.get("layer") == "observation" and c.get("vault_id")
    }
    citation_sources = {
        str(c.get("vault_id"))
        for c in citations
        if isinstance(c, dict) and c.get("layer") == "source" and c.get("vault_id")
    }

    if not subject and not topic:
        if not pages:
            return {"status": "missing", "matched_pages": [], "reason": "no entity/topic wiki pages in vault"}
        # Any page overlapping observation ids or source evidence?
        overlapping = []
        wiki_by_source = _wiki_by_source(vault_root)
        for sid in citation_sources:
            overlapping.extend(wiki_by_source.get(sid) or [])
        if overlapping or any(
            any(oid in citation_obs for oid in (p[1].get("observation_ids") or []) if isinstance(oid, str))
            for p in pages
        ):
            return {
                "status": "healthy",
                "matched_pages": [{"wiki_id": p[1].get("wiki_id"), "path": p[0].as_posix()} for p in pages[:5]],
                "reason": "wiki pages already linked to citation sources/observations",
            }
        return {
            "status": "thin",
            "matched_pages": [],
            "reason": "wiki pages exist but none overlap ask citations; compile may still help",
        }

    # With matched pages: thin if few/no observation_ids overlapping citations.
    total_obs_links = 0
    overlap = 0
    for page in matched:
        ids = [i for i in page.get("observation_ids") or [] if isinstance(i, str)]
        total_obs_links += len(ids)
        overlap += sum(1 for i in ids if i in citation_obs)
    if total_obs_links == 0:
        return {
            "status": "thin",
            "matched_pages": matched,
            "reason": "matched wiki page(s) have no observation_ids",
        }
    if citation_obs and overlap == 0:
        # Page has obs but not from this ask — thin for this question scope.
        return {
            "status": "thin",
            "matched_pages": matched,
            "reason": "matched wiki page(s) do not link observations from this ask",
        }
    # Source-only ask: require wiki evidence locators (or source summaries) to
    # mention citation sources; otherwise treat as thin even if unrelated obs exist.
    if citation_sources and not citation_obs:
        wiki_by_source = _wiki_by_source(vault_root)
        source_overlap = any(wiki_by_source.get(sid) for sid in citation_sources)
        if not source_overlap:
            return {
                "status": "thin",
                "matched_pages": matched,
                "reason": "matched wiki page(s) do not cite sources from this ask",
            }
    return {
        "status": "healthy",
        "matched_pages": matched,
        "reason": "matched wiki page(s) have observation coverage",
        "observation_link_count": total_obs_links,
        "citation_overlap": overlap,
    }


def _iter_entity_topic_wiki(vault_root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    root = vault_root / "wiki"
    if not root.is_dir():
        return []
    pages: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(root.glob("**/*.md")):
        if path.name == "README.md":
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if meta.get("kind") in {"entity", "topic"}:
            pages.append((path, meta, body))
    return pages


def _write_compile_from_ask_proposal(
    vault_root: Path,
    *,
    question: str,
    ask: AskResult,
    subject: str | None,
    topic: str | None,
    wiki_health: dict[str, Any],
) -> str:
    queue = vault_root / "system" / "update-queue"
    queue.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    slug = safe_filename(re.sub(r"[^a-z0-9]+", "-", question.casefold())[:40].strip("-") or "compile")
    name = safe_filename(f"compile-from-ask-{stamp}-{slug}.json")
    path = queue / name
    day = utc_now()[:10].replace("-", "")

    # Build observation stubs from source citations when subject/topic filters are real (not todo).
    proposed_observations: list[dict[str, Any]] = []
    subject_ref = _ref_from_filter(subject, "entity")
    topic_ref = _ref_from_filter(topic, "topic")
    for index, citation in enumerate(ask.citations):
        if not isinstance(citation, dict) or citation.get("layer") != "source":
            continue
        vault_id = citation.get("vault_id")
        if not isinstance(vault_id, str):
            continue
        manifest_path = vault_root / "sources" / vault_id / "manifest.json"
        source_hash = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                source_hash = manifest.get("content_hash")
        except (OSError, json.JSONDecodeError):
            source_hash = None
        if not isinstance(source_hash, str):
            continue
        quote = str(citation.get("quote") or question)[:500]
        obs_slug = safe_filename(re.sub(r"[^a-z0-9]+", "-", quote.casefold())[:24].strip("-") or f"c{index}")
        stub = {
            "schema_version": "1.0.0",
            "observation_id": f"obs-{day}-compile-{obs_slug}"[:80],
            "subjects": [
                subject_ref
                or {"kind": "entity", "label": "TODO", "ref_id": "entity-todo"}
            ],
            "topics": [
                topic_ref
                or {"kind": "topic", "label": "TODO", "ref_id": "topic-todo"}
            ],
            "assertion": quote,
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "unknown",
            "confidence": 0.5,
            "reasoning": "Drafted by explore compile-from-ask; review before apply.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": None,
            "expressed_at": None,
            "valid_at": None,
            "recorded_at": utc_now(),
            "horizon": None,
            "freshness": {"as_of": None, "status": "unknown"},
            "evidence": [
                {
                    "source_id": vault_id,
                    "source_hash": source_hash,
                    "normalized_path": str(citation.get("path") or f"sources/{vault_id}/normalized.md"),
                    "locator_kind": citation.get("locator_kind") or "line_range",
                    "selector": citation.get("selector")
                    if isinstance(citation.get("selector"), dict)
                    else {"start_line": 1, "end_line": 1},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        proposed_observations.append(stub)
        if len(proposed_observations) >= 3:
            break

    payload = {
        "schema_version": "1.0.0",
        "kind": "compile_from_ask_proposal",
        "created_at": utc_now(),
        "status": "proposed",
        "question": question,
        "ask_status": ask.status,
        "wiki_health": wiki_health,
        "filters": {"subject": subject, "topic": topic},
        "citations": ask.citations,
        "proposed_observations": proposed_observations,
        "wiki_evolve": {
            "subject": subject,
            "topic": topic,
            "observation_ids": [
                c.get("vault_id")
                for c in ask.citations
                if isinstance(c, dict) and c.get("layer") == "observation" and c.get("vault_id")
            ],
        },
        "run_wiki_evolve": True,
        "note": (
            "Review-only. Edit TODO subjects/topics before apply. "
            "proposal apply appends complete observations then runs wiki evolve under the writer lock. "
            "MCP explore_ask never writes this automatically."
        ),
    }
    write_json_synced(path, payload)
    return path.relative_to(vault_root).as_posix()


def _ref_from_filter(value: str | None, kind: str) -> dict[str, str] | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    prefix = f"{kind}-"
    if text.startswith(prefix) and re.fullmatch(rf"{kind}-[a-z0-9]+(?:-[a-z0-9]+)*", text):
        label = text[len(prefix) :].replace("-", " ")
        return {"kind": kind, "ref_id": text, "label": label or text}
    # Non-id filter string → still TODO for apply safety unless it already looks like a ref
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "todo"
    ref_id = f"{kind}-{slug}"
    if re.fullmatch(rf"{kind}-[a-z0-9]+(?:-[a-z0-9]+)*", ref_id):
        return {"kind": kind, "ref_id": ref_id, "label": text}
    return {"kind": kind, "label": "TODO", "ref_id": f"{kind}-todo"}


def _resolve_ask_scope(
    vault_root: Path,
    *,
    subject: str | None,
    topic: str | None,
    source_id: str | None,
) -> dict[str, Any]:
    """Build allowed source_ids / observation_ids when filters are set."""
    from knowledge_desk.observations import ObservationQuery, list_observations

    if source_id and not subject and not topic:
        return {"source_ids": {source_id}, "observation_ids": None}
    if not subject and not topic and not source_id:
        return {"source_ids": None, "observation_ids": None}

    records = list_observations(
        vault_root,
        ObservationQuery(subject=subject, topic=topic, source_id=source_id),
    )
    source_ids: set[str] = set()
    observation_ids: set[str] = set()
    if source_id:
        source_ids.add(source_id)
    for record in records:
        obs = record.observation
        oid = obs.get("observation_id")
        if isinstance(oid, str):
            observation_ids.add(oid)
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str):
                source_ids.add(locator["source_id"])
    return {"source_ids": source_ids, "observation_ids": observation_ids}


def _hit_in_scope(hit: dict[str, Any], scope: dict[str, Any], *, layer: str, has_filter: bool) -> bool:
    if not has_filter:
        return True
    allowed_sources = scope.get("source_ids")
    allowed_obs = scope.get("observation_ids")
    if layer == "source":
        if allowed_sources is None:
            return True
        vault_id = str(hit.get("vault_id") or "")
        source_ids = hit.get("source_ids") or []
        if vault_id in allowed_sources:
            return True
        return any(sid in allowed_sources for sid in source_ids)
    if layer == "observation":
        vault_id = str(hit.get("vault_id") or "")
        if allowed_obs is not None and vault_id and vault_id not in allowed_obs:
            # Still allow if subject/topic FTS fields already constrained the search.
            subjects = " ".join(hit.get("subjects") or [])
            topics = " ".join(hit.get("topics") or [])
            # If observation_ids set is empty, nothing is in scope.
            if not allowed_obs:
                return False
            return vault_id in allowed_obs
        if allowed_sources is not None:
            hit_sources = hit.get("source_ids") or []
            if hit_sources and not any(sid in allowed_sources for sid in hit_sources):
                return False
        return True
    return True


def _load_sources(vault_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for manifest_path in sorted((vault_root / "sources").glob("src-*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        source_id = str(manifest.get("source_id") or manifest_path.parent.name)
        path = str(manifest.get("normalized_path") or f"sources/{source_id}/normalized.md")
        body = ""
        try:
            _, note_body = parse_frontmatter((vault_root / path).read_text(encoding="utf-8"))
            body = normalized_content(note_body)
        except (OSError, UnicodeDecodeError, ValueError):
            body = ""
        items.append(
            {
                "source_id": source_id,
                "title": str(manifest.get("title") or source_id),
                "path": path,
                "content_hash": manifest.get("content_hash"),
                "body": body,
            }
        )
    return items


def _observations_by_source(vault_root: Path) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for record in load_all_observations(vault_root):
        obs = record.observation
        observation_id = obs.get("observation_id")
        if not isinstance(observation_id, str):
            continue
        for locator in obs.get("evidence", []) if isinstance(obs.get("evidence"), list) else []:
            if not isinstance(locator, dict):
                continue
            source_id = locator.get("source_id")
            if isinstance(source_id, str):
                mapping.setdefault(source_id, []).append(
                    {"observation_id": observation_id, "observation": obs, "path": record.path}
                )
    return mapping


def _wiki_by_source(vault_root: Path) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    root = vault_root / "wiki"
    if not root.is_dir():
        return mapping
    for path in sorted(root.glob("**/*.md")):
        if path.name == "README.md":
            continue
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        wiki_id = metadata.get("wiki_id")
        if not isinstance(wiki_id, str):
            wiki_id = path.stem
        for locator in metadata.get("evidence", []) if isinstance(metadata.get("evidence"), list) else []:
            if not isinstance(locator, dict):
                continue
            source_id = locator.get("source_id")
            if isinstance(source_id, str):
                mapping.setdefault(source_id, []).append(
                    {
                        "wiki_id": wiki_id,
                        "path": path.relative_to(vault_root).as_posix(),
                    }
                )
    return mapping


def _query_terms(question: str) -> list[str]:
    stop = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "what",
        "which",
        "who",
        "whom",
        "where",
        "when",
        "why",
        "how",
        "do",
        "does",
        "did",
        "of",
        "in",
        "on",
        "for",
        "to",
        "and",
        "or",
        "about",
        "with",
        "from",
    }
    terms = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", question):
        if token.casefold() in stop:
            continue
        terms.append(token)
    return terms or [question.strip()]


def _scan_sources_for_terms(
    vault_root: Path,
    terms: list[str],
    *,
    limit: int,
    allowed_source_ids: set[str] | None = None,
) -> list[AskCitation]:
    citations: list[AskCitation] = []
    terms_cf = [term.casefold() for term in terms]
    for item in _load_sources(vault_root):
        if allowed_source_ids is not None and item["source_id"] not in allowed_source_ids:
            continue
        body = item["body"]
        lines = [line for line in body.splitlines() if line.strip() and not line.startswith("<!--")]
        for index, line in enumerate(lines, start=1):
            lower = line.casefold()
            if terms_cf and not any(term in lower for term in terms_cf):
                continue
            citations.append(
                AskCitation(
                    layer="source",
                    vault_id=item["source_id"],
                    path=item["path"],
                    locator_kind="line_range",
                    selector={"start_line": index, "end_line": index},
                    quote=line.strip()[:500],
                )
            )
            break
        if len(citations) >= limit:
            break
    return citations


def _citation_from_hit(vault_root: Path, hit: dict[str, Any]) -> AskCitation | None:
    layer = str(hit.get("layer") or "")
    vault_id = str(hit.get("vault_id") or "")
    path = str(hit.get("path") or "")
    snippet = str(hit.get("snippet") or hit.get("title") or "").strip()
    if not vault_id or not path:
        return None
    if layer == "source":
        # Best-effort line locate for the first matching snippet line.
        selector = {"start_line": 1, "end_line": 1}
        try:
            _, body = parse_frontmatter((vault_root / path).read_text(encoding="utf-8"))
            content = normalized_content(body)
            lines = [line for line in content.splitlines() if not line.startswith("<!-- ev-block")]
            needle = snippet.strip("…").strip()[:40].casefold()
            for index, line in enumerate(lines, start=1):
                if needle and needle in line.casefold():
                    selector = {"start_line": index, "end_line": index}
                    snippet = line.strip()
                    break
        except (OSError, UnicodeDecodeError, ValueError):
            pass
        return AskCitation(
            layer="source",
            vault_id=vault_id,
            path=path,
            locator_kind="line_range",
            selector=selector,
            quote=snippet[:500],
        )
    if layer == "observation":
        return AskCitation(
            layer="observation",
            vault_id=vault_id,
            path=path,
            locator_kind="observation",
            selector={"observation_id": vault_id},
            quote=snippet[:500],
        )
    return None


def _preview(body: str, limit: int = 160) -> str:
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _write_gaps_proposal(vault_root: Path, gaps: list[GapEntry]) -> str:
    queue = vault_root / "system" / "update-queue"
    queue.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    name = safe_filename(f"explore-gaps-{stamp}.json")
    path = queue / name
    payload = {
        "schema_version": "1.0.0",
        "kind": "explore_gaps_proposal",
        "created_at": utc_now(),
        "status": "proposed",
        "summary": f"{len(gaps)} source coverage gap(s)",
        "gaps": [gap.to_dict() for gap in gaps],
        "suggested_actions": [
            "Append observations for sources missing observation coverage",
            "Run `knowledge-desk wiki evolve` after observations exist",
            "Do not treat this file as canonical truth until reviewed",
        ],
    }
    write_json_synced(path, payload)
    return path.relative_to(vault_root).as_posix()


def _write_open_question_proposal(
    vault_root: Path,
    *,
    question: str,
    citations: list[AskCitation],
    status: str,
) -> str:
    queue = vault_root / "system" / "update-queue"
    queue.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    slug = safe_filename(re.sub(r"[^a-z0-9]+", "-", question.casefold())[:40].strip("-") or "question")
    name = safe_filename(f"explore-ask-{stamp}-{slug}.json")
    path = queue / name
    day = utc_now()[:10].replace("-", "")
    payload = {
        "schema_version": "1.0.0",
        "kind": "explore_ask_proposal",
        "created_at": utc_now(),
        "status": "proposed",
        "question": question,
        "ask_status": status,
        "citations": [c.to_dict() for c in citations],
        "proposed_memory_open_question": {
            "schema_version": "1.0.0",
            "memory_id": f"mem-{day}-{slug[:24] or 'open-question'}",
            "kind": "open_question",
            "title": question[:120],
            "statement": question,
            "status": "active",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "observation_ids": [],
            "evidence": [],
            "supersedes": None,
            "extensions": {},
        },
        "note": "Review-only proposal. Does not write memory/ or wiki/ until a writer applies it.",
    }
    write_json_synced(path, payload)
    return path.relative_to(vault_root).as_posix()


def _write_observation_stub_proposal(
    vault_root: Path,
    *,
    question: str,
    citations: list[AskCitation],
) -> str:
    queue = vault_root / "system" / "update-queue"
    queue.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    slug = safe_filename(re.sub(r"[^a-z0-9]+", "-", question.casefold())[:40].strip("-") or "claim")
    name = safe_filename(f"explore-ask-{stamp}-{slug}.json")
    path = queue / name
    day = utc_now()[:10].replace("-", "")
    evidence = []
    for citation in citations:
        if citation.layer != "source":
            continue
        # Load hash from manifest when possible.
        manifest_path = vault_root / "sources" / citation.vault_id / "manifest.json"
        source_hash = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                source_hash = manifest.get("content_hash")
        except (OSError, json.JSONDecodeError):
            source_hash = None
        if not isinstance(source_hash, str):
            continue
        evidence.append(
            {
                "source_id": citation.vault_id,
                "source_hash": source_hash,
                "normalized_path": citation.path,
                "locator_kind": citation.locator_kind,
                "selector": citation.selector,
            }
        )
    payload = {
        "schema_version": "1.0.0",
        "kind": "explore_ask_proposal",
        "created_at": utc_now(),
        "status": "proposed",
        "question": question,
        "ask_status": "answered",
        "citations": [c.to_dict() for c in citations],
        "proposed_observation_stub": {
            "schema_version": "1.0.0",
            "observation_id": f"obs-{day}-{slug[:32] or 'stub'}",
            "subjects": [{"kind": "entity", "label": "TODO", "ref_id": "entity-todo"}],
            "topics": [{"kind": "topic", "label": "TODO", "ref_id": "topic-todo"}],
            "assertion": citations[0].quote if citations else question,
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "unknown",
            "confidence": 0.5,
            "reasoning": "Draft from explore ask; review before observe.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": None,
            "expressed_at": None,
            "valid_at": None,
            "recorded_at": utc_now(),
            "horizon": None,
            "freshness": {"as_of": None, "status": "unknown"},
            "evidence": evidence,
            "relations": [],
            "extensions": {},
        },
        "note": "Review-only proposal. Run knowledge-desk observe only after editing subjects/topics/assertion.",
    }
    write_json_synced(path, payload)
    return path.relative_to(vault_root).as_posix()
