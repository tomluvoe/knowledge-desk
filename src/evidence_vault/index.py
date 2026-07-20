from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from evidence_vault.observations import load_all_observations
from evidence_vault.util import normalized_content, parse_frontmatter


INDEX_RELATIVE_PATH = "system/.index/vault.sqlite"
LAYERS = frozenset({"source", "observation", "wiki", "memory"})


@dataclass
class IndexRebuildResult:
    operation: str = "index.rebuild"
    status: str = "failed"
    path: str = INDEX_RELATIVE_PATH
    indexed: dict[str, int] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SearchHit:
    vault_id: str
    layer: str
    path: str
    title: str
    snippet: str
    rank: float
    publication_date: str | None = None
    valid_at: str | None = None
    epistemic_class: str | None = None
    orientation: str | None = None
    subjects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SearchResult:
    operation: str = "index.search"
    query: str = ""
    count: int = 0
    hits: list[dict[str, object]] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def index_path(vault_root: Path) -> Path:
    return vault_root.resolve() / INDEX_RELATIVE_PATH


def rebuild_index(vault_root: Path) -> IndexRebuildResult:
    """Fully rebuild the disposable SQLite FTS index from canonical artifacts."""
    vault_root = vault_root.resolve()
    result = IndexRebuildResult()
    path = index_path(vault_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        connection = sqlite3.connect(path)
        try:
            _init_schema(connection)
            counts = {
                "source": _index_sources(connection, vault_root),
                "observation": _index_observations(connection, vault_root),
                "wiki": _index_wiki(connection, vault_root),
                "memory": _index_memory(connection, vault_root),
            }
            connection.commit()
        finally:
            connection.close()
        result.status = "rebuilt"
        result.indexed = counts
        result.message = "disposable index rebuilt from canonical vault content"
        return result
    except (OSError, sqlite3.Error) as exc:
        result.message = f"index rebuild failed: {exc}"
        return result


def search_index(
    vault_root: Path,
    query: str,
    *,
    layer: str | None = None,
    subject: str | None = None,
    topic: str | None = None,
    source_id: str | None = None,
    epistemic_class: str | None = None,
    orientation: str | None = None,
    limit: int = 20,
) -> SearchResult:
    vault_root = vault_root.resolve()
    result = SearchResult(query=query)
    if not query.strip():
        result.message = "query must be non-empty"
        return result
    if layer is not None and layer not in LAYERS:
        result.message = f"unknown layer: {layer}"
        return result
    path = index_path(vault_root)
    if not path.is_file():
        result.message = f"index missing at {INDEX_RELATIVE_PATH}; run `evidence-vault index rebuild`"
        return result

    limit = max(1, min(limit, 200))
    clauses = ["search_index MATCH ?"]
    params: list[Any] = [query]
    if layer:
        clauses.append("layer = ?")
        params.append(layer)
    if subject:
        clauses.append("subjects LIKE ?")
        params.append(f"%{subject}%")
    if topic:
        clauses.append("topics LIKE ?")
        params.append(f"%{topic}%")
    if source_id:
        clauses.append("source_ids LIKE ?")
        params.append(f"%{source_id}%")
    if epistemic_class:
        clauses.append("epistemic_class = ?")
        params.append(epistemic_class)
    if orientation:
        clauses.append("orientation = ?")
        params.append(orientation)

    sql = f"""
        SELECT vault_id, layer, path, title, body, publication_date, valid_at,
               epistemic_class, orientation, subjects, topics, source_ids,
               bm25(search_index) AS rank
        FROM search_index
        WHERE {' AND '.join(clauses)}
        ORDER BY rank
        LIMIT ?
    """
    params.append(limit)

    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        result.message = f"search failed: {exc}"
        return result

    hits: list[SearchHit] = []
    for row in rows:
        body = row["body"] or ""
        hits.append(
            SearchHit(
                vault_id=row["vault_id"],
                layer=row["layer"],
                path=row["path"],
                title=row["title"] or "",
                snippet=_snippet(body, query),
                rank=float(row["rank"] or 0.0),
                publication_date=row["publication_date"],
                valid_at=row["valid_at"],
                epistemic_class=row["epistemic_class"],
                orientation=row["orientation"],
                subjects=_split_csv(row["subjects"]),
                topics=_split_csv(row["topics"]),
                source_ids=_split_csv(row["source_ids"]),
            )
        )
    result.count = len(hits)
    result.hits = [hit.to_dict() for hit in hits]
    result.message = "ok"
    return result


def _init_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE VIRTUAL TABLE search_index USING fts5(
            vault_id UNINDEXED,
            layer UNINDEXED,
            path UNINDEXED,
            title,
            body,
            publication_date UNINDEXED,
            valid_at UNINDEXED,
            epistemic_class UNINDEXED,
            orientation UNINDEXED,
            subjects UNINDEXED,
            topics UNINDEXED,
            source_ids UNINDEXED,
            tokenize = 'porter unicode61'
        )
        """
    )


def _insert(
    connection: sqlite3.Connection,
    *,
    vault_id: str,
    layer: str,
    path: str,
    title: str,
    body: str,
    publication_date: str | None = None,
    valid_at: str | None = None,
    epistemic_class: str | None = None,
    orientation: str | None = None,
    subjects: Iterable[str] = (),
    topics: Iterable[str] = (),
    source_ids: Iterable[str] = (),
) -> None:
    connection.execute(
        """
        INSERT INTO search_index(
            vault_id, layer, path, title, body, publication_date, valid_at,
            epistemic_class, orientation, subjects, topics, source_ids
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vault_id,
            layer,
            path,
            title,
            body,
            publication_date,
            valid_at,
            epistemic_class,
            orientation,
            " ".join(subjects),
            " ".join(topics),
            " ".join(source_ids),
        ),
    )


def _index_sources(connection: sqlite3.Connection, vault_root: Path) -> int:
    count = 0
    for manifest_path in sorted((vault_root / "sources").glob("src-*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        source_id = str(manifest.get("source_id") or manifest_path.parent.name)
        note_path = vault_root / str(manifest.get("normalized_path") or f"sources/{source_id}/normalized.md")
        body = ""
        try:
            _, note_body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
            body = normalized_content(note_body)
        except (OSError, UnicodeDecodeError, ValueError):
            body = ""
        _insert(
            connection,
            vault_id=source_id,
            layer="source",
            path=f"sources/{source_id}/normalized.md",
            title=str(manifest.get("title") or source_id),
            body=body,
            publication_date=manifest.get("publication_date") if isinstance(manifest.get("publication_date"), str) else None,
            source_ids=[source_id],
        )
        count += 1
    return count


def _index_observations(connection: sqlite3.Connection, vault_root: Path) -> int:
    count = 0
    for record in load_all_observations(vault_root):
        obs = record.observation
        observation_id = obs.get("observation_id")
        if not isinstance(observation_id, str):
            continue
        subjects = _ref_ids(obs.get("subjects"))
        topics = _ref_ids(obs.get("topics"))
        source_ids = [
            locator.get("source_id")
            for locator in obs.get("evidence", [])
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str)
        ]
        body_parts = [
            str(obs.get("assertion") or ""),
            str(obs.get("reasoning") or ""),
            " ".join(obs.get("mechanisms") or []),
            " ".join(obs.get("risks") or []),
            " ".join(obs.get("conditions") or []),
            " ".join(obs.get("implications") or []),
        ]
        _insert(
            connection,
            vault_id=observation_id,
            layer="observation",
            path=record.path,
            title=str(obs.get("assertion") or observation_id)[:200],
            body="\n".join(part for part in body_parts if part),
            publication_date=obs.get("publication_date") if isinstance(obs.get("publication_date"), str) else None,
            valid_at=obs.get("valid_at") if isinstance(obs.get("valid_at"), str) else None,
            epistemic_class=obs.get("epistemic_class") if isinstance(obs.get("epistemic_class"), str) else None,
            orientation=obs.get("orientation") if isinstance(obs.get("orientation"), str) else None,
            subjects=subjects,
            topics=topics,
            source_ids=[sid for sid in source_ids if isinstance(sid, str)],
        )
        count += 1
    return count


def _index_wiki(connection: sqlite3.Connection, vault_root: Path) -> int:
    return _index_markdown_layer(
        connection,
        vault_root,
        root=vault_root / "wiki",
        layer="wiki",
        id_key="wiki_id",
    )


def _index_memory(connection: sqlite3.Connection, vault_root: Path) -> int:
    return _index_markdown_layer(
        connection,
        vault_root,
        root=vault_root / "memory",
        layer="memory",
        id_key="memory_id",
    )


def _index_markdown_layer(
    connection: sqlite3.Connection,
    vault_root: Path,
    *,
    root: Path,
    layer: str,
    id_key: str,
) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.glob("**/*.md")):
        if path.name == "README.md":
            continue
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        vault_id = metadata.get(id_key)
        if not isinstance(vault_id, str):
            vault_id = path.stem
        subjects: list[str] = []
        topics: list[str] = []
        source_ids: list[str] = []
        for locator in metadata.get("evidence", []) if isinstance(metadata.get("evidence"), list) else []:
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str):
                source_ids.append(locator["source_id"])
        for observation_id in metadata.get("observation_ids", []) if isinstance(metadata.get("observation_ids"), list) else []:
            if isinstance(observation_id, str):
                subjects.append(observation_id)  # searchable association; not a subject ref
        _insert(
            connection,
            vault_id=vault_id,
            layer=layer,
            path=path.relative_to(vault_root).as_posix(),
            title=str(metadata.get("title") or path.stem),
            body=body,
            publication_date=None,
            valid_at=metadata.get("updated_at") if isinstance(metadata.get("updated_at"), str) else None,
            epistemic_class=None,
            orientation=None,
            subjects=subjects,
            topics=topics,
            source_ids=source_ids,
        )
        count += 1
    return count


def _ref_ids(refs: object) -> list[str]:
    if not isinstance(refs, list):
        return []
    values: list[str] = []
    for ref in refs:
        if isinstance(ref, dict) and isinstance(ref.get("ref_id"), str):
            values.append(ref["ref_id"])
    return values


def _split_csv(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return value.split()


def _snippet(body: str, query: str, radius: int = 80) -> str:
    text = " ".join(body.split())
    if not text:
        return ""
    terms = [term.strip('"') for term in query.split() if term.strip('"')]
    lower = text.casefold()
    for term in terms:
        index = lower.find(term.casefold())
        if index >= 0:
            start = max(0, index - radius)
            end = min(len(text), index + len(term) + radius)
            prefix = "…" if start else ""
            suffix = "…" if end < len(text) else ""
            return prefix + text[start:end] + suffix
    return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
