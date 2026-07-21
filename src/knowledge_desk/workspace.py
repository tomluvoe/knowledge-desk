"""User-owned memory workspaces under memory/workspaces/.

Protected from wiki evolve, ingest, and maintainer auto-writes. Refined only via
explicit workspace CLI/APIs (human + AI). Benchtest maps claims against corpus
evidence without mutating the workspace.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.util import (
    SCHEMA_VERSION,
    parse_frontmatter,
    render_frontmatter,
    safe_filename,
    utc_now,
    write_json_synced,
    write_text_synced,
)


WORKSPACES_ROOT = "memory/workspaces"
WORKSPACE_KINDS = frozenset(
    {"thesis", "framework", "prediction_set", "research_program", "process", "other"}
)
PAGE_KINDS = frozenset({"spine", "pillar", "prediction", "framework", "note", "invalidation", "other"})
WORKSPACE_STATUSES = frozenset({"active", "draft", "superseded", "archived"})


@dataclass
class WorkspaceResult:
    operation: str = "workspace"
    status: str = "failed"
    workspace_id: str | None = None
    path: str | None = None
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def workspaces_dir(vault_root: Path) -> Path:
    return vault_root.resolve() / WORKSPACES_ROOT


def workspace_path(vault_root: Path, workspace_id: str) -> Path:
    return workspaces_dir(vault_root) / _slug_id(workspace_id)


def is_workspace_path(vault_root: Path, path: Path) -> bool:
    """True if path is under memory/workspaces/ (protected from mechanical writers)."""
    try:
        rel = path.resolve().relative_to(vault_root.resolve())
    except ValueError:
        return False
    return rel.as_posix().startswith(f"{WORKSPACES_ROOT}/") or rel.as_posix() == WORKSPACES_ROOT


def init_workspace(
    vault_root: Path,
    *,
    title: str,
    kind: str = "thesis",
    workspace_id: str | None = None,
    status: str = "active",
    as_of: str | None = None,
    subject_refs: list[str] | None = None,
    topic_refs: list[str] | None = None,
    statement: str | None = None,
    observation_ids: list[str] | None = None,
) -> WorkspaceResult:
    """Create a new multi-page workspace under memory/workspaces/."""
    from knowledge_desk.writer import vault_write_lock

    vault_root = vault_root.resolve()
    result = WorkspaceResult(operation="workspace.init")
    if kind not in WORKSPACE_KINDS:
        result.message = f"kind must be one of {sorted(WORKSPACE_KINDS)}"
        return result
    if status not in WORKSPACE_STATUSES:
        result.message = f"status must be one of {sorted(WORKSPACE_STATUSES)}"
        return result
    if not (title or "").strip():
        result.message = "title is required"
        return result

    ws_id = workspace_id or _default_workspace_id(title, kind)
    if not re.fullmatch(r"ws-[a-z0-9]+(?:-[a-z0-9]+)*", ws_id):
        result.message = f"workspace_id must match ws-<slug>; got {ws_id!r}"
        return result

    try:
        with vault_write_lock(vault_root):
            root = workspace_path(vault_root, ws_id)
            if root.exists():
                result.status = "failed"
                result.workspace_id = ws_id
                result.message = f"workspace already exists: {root.relative_to(vault_root)}"
                return result
            now = utc_now()
            for sub in ("pages", "log", "benchtests"):
                (root / sub).mkdir(parents=True, exist_ok=True)

            body = _spine_body(title, kind, statement)
            meta = {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": ws_id,
                "page_id": None,
                "page_kind": "spine",
                "kind": kind,
                "title": title.strip(),
                "status": status,
                "as_of": as_of,
                "created_at": now,
                "updated_at": now,
                "observation_ids": sorted(set(observation_ids or [])),
                "subject_refs": sorted(set(subject_refs or [])),
                "topic_refs": sorted(set(topic_refs or [])),
                "prior": False,
                "horizon": None,
                "supersedes_page": None,
                "extensions": {"knowledge.desk.workspace": {"protected_from_auto_evolve": True}},
            }
            spine = root / "workspace.md"
            write_text_synced(spine, render_frontmatter(meta) + "\n" + body)
            _append_changelog(
                root,
                event="created",
                summary=f"workspace {ws_id} created ({kind})",
                observation_ids=list(observation_ids or []),
            )
            result.status = "created"
            result.workspace_id = ws_id
            result.path = spine.relative_to(vault_root).as_posix()
            result.message = f"workspace created at {root.relative_to(vault_root).as_posix()}"
            result.details = {"kind": kind, "title": title.strip()}
            return result
    except (OSError, KnowledgeDeskError) as exc:
        result.message = str(exc)
        return result


def list_workspaces(vault_root: Path) -> dict[str, object]:
    vault_root = vault_root.resolve()
    root = workspaces_dir(vault_root)
    items: list[dict[str, object]] = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            spine = path / "workspace.md"
            if not spine.is_file():
                continue
            try:
                meta, _ = parse_frontmatter(spine.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                meta = {}
            items.append(
                {
                    "workspace_id": meta.get("workspace_id") or path.name,
                    "kind": meta.get("kind"),
                    "title": meta.get("title") or path.name,
                    "status": meta.get("status"),
                    "as_of": meta.get("as_of"),
                    "path": spine.relative_to(vault_root).as_posix(),
                    "page_count": len(list((path / "pages").glob("*.md"))) if (path / "pages").is_dir() else 0,
                }
            )
    return {
        "operation": "workspace.list",
        "count": len(items),
        "workspaces": items,
        "message": "user-owned workspaces under memory/workspaces/ (not auto-evolved)",
    }


def get_workspace(vault_root: Path, workspace_id: str) -> dict[str, object]:
    vault_root = vault_root.resolve()
    root = workspace_path(vault_root, workspace_id)
    spine = root / "workspace.md"
    if not spine.is_file():
        return {
            "operation": "workspace.get",
            "success": False,
            "workspace_id": workspace_id,
            "message": "workspace not found",
        }
    meta, body = parse_frontmatter(spine.read_text(encoding="utf-8"))
    pages: list[dict[str, object]] = []
    pages_dir = root / "pages"
    if pages_dir.is_dir():
        for path in sorted(pages_dir.glob("*.md")):
            try:
                pmeta, pbody = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            pages.append(
                {
                    "path": path.relative_to(vault_root).as_posix(),
                    "page_id": pmeta.get("page_id"),
                    "page_kind": pmeta.get("page_kind"),
                    "title": pmeta.get("title"),
                    "status": pmeta.get("status"),
                    "observation_ids": pmeta.get("observation_ids") or [],
                    "prior": pmeta.get("prior"),
                    "body_preview": pbody.strip()[:400],
                }
            )
    changelog = _read_changelog(root)
    return {
        "operation": "workspace.get",
        "success": True,
        "workspace_id": meta.get("workspace_id") or workspace_id,
        "path": spine.relative_to(vault_root).as_posix(),
        "metadata": meta,
        "body": body,
        "pages": pages,
        "changelog_tail": changelog[-20:],
        "message": "ok",
    }


def add_page(
    vault_root: Path,
    workspace_id: str,
    *,
    title: str,
    page_kind: str = "pillar",
    body: str | None = None,
    page_id: str | None = None,
    observation_ids: list[str] | None = None,
    prior: bool = False,
    horizon: dict[str, Any] | None = None,
) -> WorkspaceResult:
    from knowledge_desk.writer import vault_write_lock

    vault_root = vault_root.resolve()
    result = WorkspaceResult(operation="workspace.add-page", workspace_id=workspace_id)
    if page_kind not in PAGE_KINDS or page_kind == "spine":
        result.message = f"page_kind must be one of {sorted(PAGE_KINDS - {'spine'})}"
        return result
    if not (title or "").strip():
        result.message = "title is required"
        return result

    try:
        with vault_write_lock(vault_root):
            root = workspace_path(vault_root, workspace_id)
            spine = root / "workspace.md"
            if not spine.is_file():
                result.message = f"workspace not found: {workspace_id}"
                return result
            spine_meta, _ = parse_frontmatter(spine.read_text(encoding="utf-8"))
            pid = page_id or _default_page_id(title, page_kind)
            if not re.fullmatch(r"wsp-[a-z0-9]+(?:-[a-z0-9]+)*", pid):
                result.message = f"page_id must match wsp-<slug>; got {pid!r}"
                return result
            pages_dir = root / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            path = pages_dir / f"{safe_filename(pid.removeprefix('wsp-'))}.md"
            if path.exists():
                result.message = f"page already exists: {path.relative_to(vault_root)}"
                return result
            now = utc_now()
            meta = {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": spine_meta.get("workspace_id") or workspace_id,
                "page_id": pid,
                "page_kind": page_kind,
                "kind": spine_meta.get("kind") or "other",
                "title": title.strip(),
                "status": "active",
                "as_of": spine_meta.get("as_of"),
                "created_at": now,
                "updated_at": now,
                "observation_ids": sorted(set(observation_ids or [])),
                "subject_refs": list(spine_meta.get("subject_refs") or []),
                "topic_refs": list(spine_meta.get("topic_refs") or []),
                "prior": bool(prior),
                "horizon": horizon,
                "supersedes_page": None,
                "extensions": {},
            }
            content = (body or f"# {title.strip()}\n\n_User-owned workspace page. Not source evidence._\n").strip() + "\n"
            write_text_synced(path, render_frontmatter(meta) + "\n" + content)
            _append_changelog(
                root,
                event="page_added",
                summary=f"added {page_kind} page {pid}: {title.strip()}",
                observation_ids=list(observation_ids or []),
                page_id=pid,
            )
            result.status = "created"
            result.path = path.relative_to(vault_root).as_posix()
            result.message = f"page created at {result.path}"
            result.details = {"page_id": pid, "page_kind": page_kind}
            return result
    except (OSError, ValueError, KnowledgeDeskError) as exc:
        result.message = str(exc)
        return result


def refine_workspace(
    vault_root: Path,
    workspace_id: str,
    *,
    summary: str,
    page_id: str | None = None,
    body: str | None = None,
    title: str | None = None,
    observation_ids: list[str] | None = None,
    status: str | None = None,
    as_of: str | None = None,
    reason: str | None = None,
) -> WorkspaceResult:
    """Explicit refine: update spine or a page and append changelog. Never called by wiki evolve."""
    from knowledge_desk.writer import vault_write_lock

    vault_root = vault_root.resolve()
    result = WorkspaceResult(operation="workspace.refine", workspace_id=workspace_id)
    if not (summary or "").strip():
        result.message = "refine summary is required (recorded in changelog)"
        return result

    try:
        with vault_write_lock(vault_root):
            root = workspace_path(vault_root, workspace_id)
            if page_id:
                path = _find_page_path(root, page_id)
                if path is None:
                    result.message = f"page not found: {page_id}"
                    return result
            else:
                path = root / "workspace.md"
                if not path.is_file():
                    result.message = f"workspace not found: {workspace_id}"
                    return result

            meta, old_body = parse_frontmatter(path.read_text(encoding="utf-8"))
            now = utc_now()
            if title:
                meta["title"] = title.strip()
            if status:
                if status not in WORKSPACE_STATUSES:
                    result.message = f"status must be one of {sorted(WORKSPACE_STATUSES)}"
                    return result
                meta["status"] = status
            if as_of is not None:
                meta["as_of"] = as_of
            if observation_ids:
                prior = meta.get("observation_ids") if isinstance(meta.get("observation_ids"), list) else []
                meta["observation_ids"] = sorted(
                    {i for i in prior if isinstance(i, str)} | set(observation_ids)
                )
            meta["updated_at"] = now
            new_body = body if body is not None else old_body
            write_text_synced(path, render_frontmatter(meta) + "\n" + new_body.lstrip("\n"))
            _append_changelog(
                root,
                event="refined",
                summary=summary.strip(),
                reason=reason,
                observation_ids=list(observation_ids or []),
                page_id=page_id or "spine",
            )
            result.status = "refined"
            result.path = path.relative_to(vault_root).as_posix()
            result.message = f"refined {result.path}: {summary.strip()}"
            result.details = {"page_id": page_id or "spine", "summary": summary.strip()}
            return result
    except (OSError, ValueError, KnowledgeDeskError) as exc:
        result.message = str(exc)
        return result


def benchtest_workspace(
    vault_root: Path,
    workspace_id: str,
    *,
    since: str | None = None,
    source_id: str | None = None,
    persist: bool = True,
) -> dict[str, object]:
    """Stress-test workspace claims against corpus. Does not mutate pages (optional report file)."""
    from knowledge_desk.observations import ObservationQuery, list_observations
    from knowledge_desk.perspective import perspective_at

    vault_root = vault_root.resolve()
    root = workspace_path(vault_root, workspace_id)
    spine = root / "workspace.md"
    if not spine.is_file():
        return {
            "operation": "workspace.benchtest",
            "status": "failed",
            "workspace_id": workspace_id,
            "message": "workspace not found",
        }

    spine_meta, spine_body = parse_frontmatter(spine.read_text(encoding="utf-8"))
    claims = _collect_claims(root, spine_meta, spine_body)
    subjects = [str(s) for s in (spine_meta.get("subject_refs") or []) if isinstance(s, str)]
    topics = [str(t) for t in (spine_meta.get("topic_refs") or []) if isinstance(t, str)]

    # Observations in scope (optional since / source filter).
    obs_records = list_observations(
        vault_root,
        ObservationQuery(
            subject=subjects[0] if len(subjects) == 1 else None,
            topic=topics[0] if len(topics) == 1 else None,
            source_id=source_id,
        ),
    )
    if since:
        obs_records = [
            r
            for r in obs_records
            if _obs_time(r.observation) is not None and str(_obs_time(r.observation)) >= since
        ]

    results: list[dict[str, object]] = []
    for claim in claims:
        verdict = _classify_claim(vault_root, claim, obs_records, subjects, topics)
        results.append(verdict)

    report = {
        "operation": "workspace.benchtest",
        "status": "ok",
        "workspace_id": workspace_id,
        "ran_at": utc_now(),
        "filters": {"since": since, "source_id": source_id},
        "claim_count": len(results),
        "claims": results,
        "summary": _summarize_verdicts(results),
        "message": "benchtest complete; workspace pages not modified",
        "policy": {
            "auto_mutate_workspace": False,
            "protected_from_wiki_evolve": True,
            "epistemic": "user-owned workspace claims are not source_statement",
        },
    }

    if persist:
        bench_dir = root / "benchtests"
        bench_dir.mkdir(parents=True, exist_ok=True)
        name = f"bench-{utc_now().replace(':', '').replace('-', '')}.json"
        out = bench_dir / name
        write_json_synced(out, report)
        report["report_path"] = out.relative_to(vault_root).as_posix()
        _append_changelog(
            root,
            event="benchtest",
            summary=f"benchtest {report['summary']}",
            observation_ids=[],
        )

    return report


def apply_workspace_refine_proposal(vault_root: Path, payload: dict[str, Any]) -> dict[str, object]:
    """Apply a workspace_refine_proposal (called under writer lock from proposals)."""
    workspace_id = payload.get("workspace_id")
    if not isinstance(workspace_id, str):
        raise KnowledgeDeskError("workspace_refine_proposal missing workspace_id")
    summary = payload.get("summary") or payload.get("reason") or "applied refine proposal"
    if not isinstance(summary, str):
        summary = "applied refine proposal"
    page_id = payload.get("page_id") if isinstance(payload.get("page_id"), str) else None
    body = payload.get("body") if isinstance(payload.get("body"), str) else None
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    obs_ids = payload.get("observation_ids") if isinstance(payload.get("observation_ids"), list) else None
    status = payload.get("status") if isinstance(payload.get("status"), str) else None
    as_of = payload.get("as_of") if isinstance(payload.get("as_of"), str) else None

    # refine_workspace takes its own lock — avoid nested lock: use unlocked path.
    return _refine_unlocked(
        vault_root,
        workspace_id,
        summary=summary,
        page_id=page_id,
        body=body,
        title=title,
        observation_ids=[i for i in (obs_ids or []) if isinstance(i, str)] or None,
        status=status,
        as_of=as_of,
        reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
    ).to_dict()


def _refine_unlocked(
    vault_root: Path,
    workspace_id: str,
    *,
    summary: str,
    page_id: str | None = None,
    body: str | None = None,
    title: str | None = None,
    observation_ids: list[str] | None = None,
    status: str | None = None,
    as_of: str | None = None,
    reason: str | None = None,
) -> WorkspaceResult:
    """Refine without acquiring writer lock (caller holds lock)."""
    result = WorkspaceResult(operation="workspace.refine", workspace_id=workspace_id)
    root = workspace_path(vault_root, workspace_id)
    if page_id:
        path = _find_page_path(root, page_id)
        if path is None:
            result.message = f"page not found: {page_id}"
            return result
    else:
        path = root / "workspace.md"
        if not path.is_file():
            result.message = f"workspace not found: {workspace_id}"
            return result
    meta, old_body = parse_frontmatter(path.read_text(encoding="utf-8"))
    now = utc_now()
    if title:
        meta["title"] = title.strip()
    if status:
        meta["status"] = status
    if as_of is not None:
        meta["as_of"] = as_of
    if observation_ids:
        prior = meta.get("observation_ids") if isinstance(meta.get("observation_ids"), list) else []
        meta["observation_ids"] = sorted({i for i in prior if isinstance(i, str)} | set(observation_ids))
    meta["updated_at"] = now
    new_body = body if body is not None else old_body
    write_text_synced(path, render_frontmatter(meta) + "\n" + new_body.lstrip("\n"))
    _append_changelog(
        root,
        event="refined",
        summary=summary.strip(),
        reason=reason,
        observation_ids=list(observation_ids or []),
        page_id=page_id or "spine",
    )
    result.status = "refined"
    result.path = path.relative_to(vault_root).as_posix()
    result.message = f"refined {result.path}: {summary.strip()}"
    return result


def _collect_claims(root: Path, spine_meta: dict[str, Any], spine_body: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    claims.append(
        {
            "page_id": "spine",
            "page_kind": "spine",
            "title": spine_meta.get("title"),
            "text": _first_claim_line(spine_body) or str(spine_meta.get("title") or ""),
            "observation_ids": list(spine_meta.get("observation_ids") or []),
            "prior": bool(spine_meta.get("prior")),
            "subject_refs": list(spine_meta.get("subject_refs") or []),
            "topic_refs": list(spine_meta.get("topic_refs") or []),
        }
    )
    pages_dir = root / "pages"
    if pages_dir.is_dir():
        for path in sorted(pages_dir.glob("*.md")):
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            claims.append(
                {
                    "page_id": meta.get("page_id") or path.stem,
                    "page_kind": meta.get("page_kind") or "note",
                    "title": meta.get("title"),
                    "text": _first_claim_line(body) or str(meta.get("title") or path.stem),
                    "observation_ids": list(meta.get("observation_ids") or []),
                    "prior": bool(meta.get("prior")),
                    "subject_refs": list(meta.get("subject_refs") or spine_meta.get("subject_refs") or []),
                    "topic_refs": list(meta.get("topic_refs") or spine_meta.get("topic_refs") or []),
                }
            )
    return claims


def _classify_claim(
    vault_root: Path,
    claim: dict[str, Any],
    obs_records: list[Any],
    default_subjects: list[str],
    default_topics: list[str],
) -> dict[str, object]:
    from knowledge_desk.perspective import perspective_at

    linked = [i for i in (claim.get("observation_ids") or []) if isinstance(i, str)]
    text = str(claim.get("text") or "").casefold()
    terms = [t for t in re.findall(r"[a-z0-9]{3,}", text) if t not in {"the", "and", "for", "with"}]

    matching_obs: list[str] = []
    opposing_obs: list[str] = []
    for record in obs_records:
        obs = record.observation
        oid = obs.get("observation_id")
        if not isinstance(oid, str):
            continue
        if oid in linked:
            matching_obs.append(oid)
            continue
        assertion = str(obs.get("assertion") or "").casefold()
        if terms and any(term in assertion for term in terms[:8]):
            orient = obs.get("orientation")
            if orient in {"critical", "opposed"}:
                opposing_obs.append(oid)
            else:
                matching_obs.append(oid)

    perspective_notes: list[dict[str, object]] = []
    subjects = [s for s in (claim.get("subject_refs") or default_subjects) if isinstance(s, str)]
    topics = [t for t in (claim.get("topic_refs") or default_topics) if isinstance(t, str)]
    if subjects and topics:
        try:
            persp = perspective_at(vault_root, subjects[0], topics[0], claim.get("as_of") or utc_now()[:10])
            perspective_notes.append(
                {
                    "subject": subjects[0],
                    "topic": topics[0],
                    "status": persp.status,
                    "orientation": persp.orientation,
                    "observation_id": persp.observation_id,
                }
            )
            if persp.status == "conflicted":
                opposing_obs.extend(persp.conflicting_observation_ids or [])
            elif persp.status == "supported" and persp.observation_id:
                matching_obs.append(persp.observation_id)
        except Exception:  # noqa: BLE001 — benchtest must not fail the whole run
            pass

    matching_obs = sorted(set(matching_obs))
    opposing_obs = sorted(set(opposing_obs) - set(matching_obs))

    if claim.get("prior") and not matching_obs and not opposing_obs:
        verdict = "untested"
        reason = "labeled prior with no corpus hits in scope"
    elif opposing_obs and matching_obs:
        verdict = "conflicted"
        reason = "both supporting and challenging observations"
    elif opposing_obs:
        verdict = "challenged"
        reason = "challenging observations or conflicted perspective"
    elif matching_obs or linked:
        verdict = "supported"
        reason = "linked or matching observations in scope"
    else:
        verdict = "untested"
        reason = "insufficient_evidence in filtered corpus scope"

    # Predictions: map untested with horizon end in past → pending review note
    page_kind = claim.get("page_kind")
    if page_kind == "prediction" and verdict == "untested":
        verdict = "pending"
        reason = "prediction without decisive support/challenge in scope"

    return {
        "page_id": claim.get("page_id"),
        "page_kind": page_kind,
        "title": claim.get("title"),
        "claim": claim.get("text"),
        "verdict": verdict,
        "reason": reason,
        "matching_observation_ids": matching_obs,
        "challenging_observation_ids": opposing_obs,
        "linked_observation_ids": linked,
        "perspective": perspective_notes,
        "prior": bool(claim.get("prior")),
    }


def _summarize_verdicts(results: list[dict[str, object]]) -> str:
    counts: dict[str, int] = {}
    for item in results:
        v = str(item.get("verdict") or "unknown")
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))


def _spine_body(title: str, kind: str, statement: str | None) -> str:
    lines = [
        f"# {title}",
        "",
        f"_User-owned memory workspace (`{kind}`). Not source evidence. "
        "Protected from wiki evolve / maintainer auto-writes. Refine explicitly._",
        "",
        "## Stance",
        "",
        statement.strip() if statement else "_Add the working stance or thesis spine here._",
        "",
        "## Pillars and pages",
        "",
        "Add pages with `knowledge-desk workspace add-page`.",
        "",
        "## Benchtesting",
        "",
        "Run `knowledge-desk workspace benchtest --id <workspace_id>` when new sources arrive.",
        "",
    ]
    return "\n".join(lines)


def _append_changelog(
    root: Path,
    *,
    event: str,
    summary: str,
    observation_ids: list[str],
    page_id: str | None = None,
    reason: str | None = None,
) -> None:
    log_dir = root / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "changelog.jsonl"
    entry = {
        "at": utc_now(),
        "event": event,
        "summary": summary,
        "page_id": page_id,
        "reason": reason,
        "observation_ids": observation_ids,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _read_changelog(root: Path) -> list[dict[str, Any]]:
    path = root / "log" / "changelog.jsonl"
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _find_page_path(root: Path, page_id: str) -> Path | None:
    pages = root / "pages"
    if not pages.is_dir():
        return None
    for path in pages.glob("*.md"):
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if meta.get("page_id") == page_id or path.stem == page_id.removeprefix("wsp-"):
            return path
    return None


def _default_workspace_id(title: str, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "workspace"
    slug = slug[:40]
    return f"ws-{kind}-{slug}" if not slug.startswith(kind) else f"ws-{slug}"


def _default_page_id(title: str, page_kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "page"
    slug = slug[:40]
    return f"wsp-{page_kind}-{slug}"


def _slug_id(workspace_id: str) -> str:
    return safe_filename(workspace_id)


def _first_claim_line(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("_"):
            continue
        if stripped.startswith("Add pages") or stripped.startswith("Run `"):
            continue
        return stripped[:500]
    return None


def _obs_time(obs: dict[str, Any]) -> str | None:
    for key in ("valid_at", "expressed_at", "recorded_at", "publication_date"):
        value = obs.get(key)
        if isinstance(value, str) and value:
            return value
    return None
