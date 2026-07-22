from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from knowledge_desk.observations import ObservationRecord, load_all_observations
from knowledge_desk.util import (
    confined_file,
    fsync_directory,
    normalization_for_path,
    normalized_content,
    parse_frontmatter,
    sha256_file,
)


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
    subtype: str | None = None
    publication_date: str | None = None
    valid_at: str | None = None
    epistemic_class: str | None = None
    orientation: str | None = None
    subjects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    observation_ids: list[str] = field(default_factory=list)

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


@dataclass
class DocumentLinks:
    subjects: set[str] = field(default_factory=set)
    topics: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    observation_ids: set[str] = field(default_factory=set)

    def merge(self, other: DocumentLinks) -> None:
        self.subjects.update(other.subjects)
        self.topics.update(other.topics)
        self.source_ids.update(other.source_ids)
        self.observation_ids.update(other.observation_ids)


@dataclass
class ObservationAssociations:
    records: list[ObservationRecord] = field(default_factory=list)
    by_observation: dict[str, DocumentLinks] = field(default_factory=dict)
    by_source: dict[str, DocumentLinks] = field(default_factory=dict)


def index_path(vault_root: Path) -> Path:
    override = os.environ.get("KNOWLEDGE_DESK_INDEX_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return vault_root.resolve() / INDEX_RELATIVE_PATH


def rebuild_index(vault_root: Path) -> IndexRebuildResult:
    """Fully rebuild the disposable SQLite FTS index from canonical artifacts."""
    vault_root = vault_root.resolve()
    result = IndexRebuildResult()
    path = index_path(vault_root)
    staged: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = path.parent / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=staging_dir,
        )
        os.close(descriptor)
        staged = Path(staged_name)
        associations = _observation_associations(vault_root)
        connection = sqlite3.connect(staged)
        try:
            _init_schema(connection)
            counts = {
                "source": _index_sources(connection, vault_root, associations),
                "observation": _index_observations(connection, vault_root, associations),
                "wiki": _index_wiki(connection, vault_root, associations),
                "memory": _index_memory(connection, vault_root, associations),
            }
            connection.commit()
            _validate_index(connection, expected_documents=sum(counts.values()))
        finally:
            connection.close()
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staged, path)
        staged = None
        fsync_directory(path.parent)
        result.status = "rebuilt"
        result.indexed = counts
        result.message = "disposable index rebuilt from canonical vault content"
        return result
    except (OSError, sqlite3.Error, ValueError) as exc:
        result.message = f"index rebuild failed: {exc}"
        return result
    finally:
        if staged is not None and staged.exists():
            staged.unlink()


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
        result.message = f"index missing at {INDEX_RELATIVE_PATH}; run `knowledge-desk index rebuild`"
        return result

    limit = max(1, min(limit, 200))
    clauses = ["search_index MATCH ?"]
    params: list[Any] = [query]
    if layer:
        clauses.append("layer = ?")
        params.append(layer)
    if subject:
        clauses.append(
            "EXISTS (SELECT 1 FROM search_facets f "
            "WHERE f.document_rowid = search_index.rowid "
            "AND f.facet_type = 'subject' AND f.facet_value = ?)"
        )
        params.append(subject)
    if topic:
        clauses.append(
            "EXISTS (SELECT 1 FROM search_facets f "
            "WHERE f.document_rowid = search_index.rowid "
            "AND f.facet_type = 'topic' AND f.facet_value = ?)"
        )
        params.append(topic)
    if source_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM search_facets f "
            "WHERE f.document_rowid = search_index.rowid "
            "AND f.facet_type = 'source' AND f.facet_value = ?)"
        )
        params.append(source_id)
    if epistemic_class:
        clauses.append("epistemic_class = ?")
        params.append(epistemic_class)
    if orientation:
        clauses.append("orientation = ?")
        params.append(orientation)

    sql = f"""
        SELECT vault_id, layer, subtype, path, title, body, publication_date, valid_at,
               epistemic_class, orientation, subjects, topics, source_ids, observation_ids,
               bm25(search_index) AS rank
        FROM search_index
        WHERE {' AND '.join(clauses)}
        ORDER BY rank, layer, vault_id
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
                subtype=row["subtype"],
                publication_date=row["publication_date"],
                valid_at=row["valid_at"],
                epistemic_class=row["epistemic_class"],
                orientation=row["orientation"],
                subjects=_split_csv(row["subjects"]),
                topics=_split_csv(row["topics"]),
                source_ids=_split_csv(row["source_ids"]),
                observation_ids=_split_csv(row["observation_ids"]),
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
            subtype UNINDEXED,
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
            observation_ids UNINDEXED,
            tokenize = 'porter unicode61'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE search_facets(
            document_rowid INTEGER NOT NULL,
            facet_type TEXT NOT NULL,
            facet_value TEXT NOT NULL,
            PRIMARY KEY(document_rowid, facet_type, facet_value)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX search_facets_lookup ON search_facets(facet_type, facet_value, document_rowid)"
    )


def _insert(
    connection: sqlite3.Connection,
    *,
    vault_id: str,
    layer: str,
    subtype: str | None,
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
    observation_ids: Iterable[str] = (),
) -> None:
    normalized_subjects = _normalized_values(subjects)
    normalized_topics = _normalized_values(topics)
    normalized_sources = _normalized_values(source_ids)
    normalized_observations = _normalized_values(observation_ids)
    cursor = connection.execute(
        """
        INSERT INTO search_index(
            vault_id, layer, subtype, path, title, body, publication_date, valid_at,
            epistemic_class, orientation, subjects, topics, source_ids, observation_ids
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vault_id,
            layer,
            subtype,
            path,
            title,
            body,
            publication_date,
            valid_at,
            epistemic_class,
            orientation,
            " ".join(normalized_subjects),
            " ".join(normalized_topics),
            " ".join(normalized_sources),
            " ".join(normalized_observations),
        ),
    )
    document_rowid = cursor.lastrowid
    if document_rowid is None:
        raise sqlite3.DatabaseError(f"index insert did not return a rowid for {vault_id}")
    facets = (
        [("subject", value) for value in normalized_subjects]
        + [("topic", value) for value in normalized_topics]
        + [("source", value) for value in normalized_sources]
        + [("observation", value) for value in normalized_observations]
    )
    connection.executemany(
        "INSERT INTO search_facets(document_rowid, facet_type, facet_value) VALUES (?, ?, ?)",
        [(document_rowid, facet_type, facet_value) for facet_type, facet_value in facets],
    )


def _index_sources(
    connection: sqlite3.Connection,
    vault_root: Path,
    associations: ObservationAssociations | None = None,
) -> int:
    associations = associations or _observation_associations(vault_root)
    count = 0
    sources_root = vault_root / "sources"
    for candidate in sorted(sources_root.glob("src-*/manifest.json")):
        manifest_path = confined_file(sources_root, candidate)
        if manifest_path is None:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        source_id = str(manifest.get("source_id") or manifest_path.parent.name)
        if not re.fullmatch(r"src-[0-9a-f]{24}", source_id):
            continue
        normalized_path = manifest.get("normalized_path")
        revision = (
            normalization_for_path(manifest, normalized_path)
            if isinstance(normalized_path, str)
            else None
        )
        normalization = manifest.get("normalization")
        current_revision = normalization.get("current_revision") if isinstance(normalization, dict) else None
        if (
            manifest_path.parent.name != source_id
            or revision is None
            or revision.get("revision_id") != current_revision
            or revision.get("normalized_hash") != manifest.get("normalized_hash")
        ):
            continue
        note_path = confined_file(manifest_path.parent, vault_root / normalized_path)
        body = ""
        if note_path is not None and f"sha256:{sha256_file(note_path)}" == revision.get("normalized_hash"):
            try:
                _, note_body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
                body = normalized_content(note_body)
            except (OSError, UnicodeDecodeError, ValueError):
                body = ""
        source_links = DocumentLinks(
            subjects=set(_string_values(manifest.get("subject_refs"))),
            topics=set(_string_values(manifest.get("topic_refs"))),
        )
        source_links.merge(associations.by_source.get(source_id, DocumentLinks()))
        _insert(
            connection,
            vault_id=source_id,
            layer="source",
            subtype=manifest.get("media_type") if isinstance(manifest.get("media_type"), str) else None,
            path=str(normalized_path),
            title=str(manifest.get("title") or source_id),
            body=body,
            publication_date=manifest.get("publication_date") if isinstance(manifest.get("publication_date"), str) else None,
            subjects=source_links.subjects,
            topics=source_links.topics,
            source_ids=[source_id],
            observation_ids=source_links.observation_ids,
        )
        count += 1
    return count


def _index_observations(
    connection: sqlite3.Connection,
    vault_root: Path,
    associations: ObservationAssociations | None = None,
) -> int:
    associations = associations or _observation_associations(vault_root)
    count = 0
    for record in associations.records:
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
            subtype=(
                obs.get("statement_basis")
                if isinstance(obs.get("statement_basis"), str)
                else None
            ),
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
            observation_ids=associations.by_observation.get(
                observation_id, DocumentLinks()
            ).observation_ids,
        )
        count += 1
    return count


def _index_wiki(
    connection: sqlite3.Connection,
    vault_root: Path,
    associations: ObservationAssociations | None = None,
) -> int:
    return _index_markdown_layer(
        connection,
        vault_root,
        root=vault_root / "wiki",
        layer="wiki",
        id_key="wiki_id",
        associations=associations or _observation_associations(vault_root),
    )


def _index_memory(
    connection: sqlite3.Connection,
    vault_root: Path,
    associations: ObservationAssociations | None = None,
) -> int:
    return _index_markdown_layer(
        connection,
        vault_root,
        root=vault_root / "memory",
        layer="memory",
        id_key="memory_id",
        associations=associations or _observation_associations(vault_root),
    )


def _index_markdown_layer(
    connection: sqlite3.Connection,
    vault_root: Path,
    *,
    root: Path,
    layer: str,
    id_key: str,
    associations: ObservationAssociations,
) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for candidate in sorted(root.glob("**/*.md")):
        path = confined_file(root, candidate)
        if path is None:
            continue
        if path.name == "README.md":
            continue
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        vault_id = metadata.get(id_key)
        if not isinstance(vault_id, str):
            vault_id = path.stem
        links = DocumentLinks(
            subjects=set(_string_values(metadata.get("subject_refs"))),
            topics=set(_string_values(metadata.get("topic_refs"))),
            observation_ids=set(_string_values(metadata.get("observation_ids"))),
        )
        for locator in metadata.get("evidence", []) if isinstance(metadata.get("evidence"), list) else []:
            if isinstance(locator, dict) and isinstance(locator.get("source_id"), str):
                links.source_ids.add(locator["source_id"])
        for observation_id in tuple(links.observation_ids):
            observation_links = associations.by_observation.get(observation_id)
            if observation_links is not None:
                links.merge(observation_links)
        if layer == "wiki":
            _add_wiki_identity_link(links, root, path, metadata)
        subtype = metadata.get("page_kind") or metadata.get("kind")
        _insert(
            connection,
            vault_id=vault_id,
            layer=layer,
            subtype=str(subtype) if isinstance(subtype, str) else None,
            path=path.relative_to(vault_root).as_posix(),
            title=str(metadata.get("title") or path.stem),
            body=body,
            publication_date=None,
            valid_at=metadata.get("updated_at") if isinstance(metadata.get("updated_at"), str) else None,
            epistemic_class=None,
            orientation=None,
            subjects=links.subjects,
            topics=links.topics,
            source_ids=links.source_ids,
            observation_ids=links.observation_ids,
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


def _observation_associations(vault_root: Path) -> ObservationAssociations:
    associations = ObservationAssociations(records=load_all_observations(vault_root))
    for record in associations.records:
        observation = record.observation
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str):
            continue
        links = DocumentLinks(
            subjects=set(_ref_ids(observation.get("subjects"))),
            topics=set(_ref_ids(observation.get("topics"))),
            source_ids={
                locator["source_id"]
                for locator in observation.get("evidence", [])
                if isinstance(locator, dict) and isinstance(locator.get("source_id"), str)
            },
            observation_ids={observation_id},
        )
        links.observation_ids.update(
            relation["observation_id"]
            for relation in observation.get("relations", [])
            if isinstance(relation, dict)
            and isinstance(relation.get("observation_id"), str)
        )
        associations.by_observation[observation_id] = links
        for source_id in links.source_ids:
            source_links = associations.by_source.setdefault(
                source_id,
                DocumentLinks(source_ids={source_id}),
            )
            source_links.subjects.update(links.subjects)
            source_links.topics.update(links.topics)
            source_links.observation_ids.add(observation_id)
    return associations


def _add_wiki_identity_link(
    links: DocumentLinks,
    root: Path,
    path: Path,
    metadata: dict[str, Any],
) -> None:
    kind = metadata.get("kind")
    if kind not in {"entity", "topic"}:
        return
    wiki_id = metadata.get("wiki_id")
    prefix = f"wiki-{kind}-"
    slug: str | None = None
    if isinstance(wiki_id, str) and wiki_id.startswith(prefix):
        slug = wiki_id.removeprefix(prefix)
    if not slug:
        relative = path.relative_to(root)
        expected_dir = "entities" if kind == "entity" else "topics"
        if len(relative.parts) == 2 and relative.parts[0] == expected_dir:
            slug = path.stem
    if not slug or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        return
    if kind == "entity":
        links.subjects.add(f"entity-{slug}")
    else:
        links.topics.add(f"topic-{slug}")


def _validate_index(connection: sqlite3.Connection, *, expected_documents: int) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise sqlite3.DatabaseError(f"staged index integrity check failed: {integrity!r}")
    actual_documents = int(connection.execute("SELECT count(*) FROM search_index").fetchone()[0])
    if actual_documents != expected_documents:
        raise sqlite3.DatabaseError(
            f"staged index document count mismatch: expected {expected_documents}, got {actual_documents}"
        )
    orphan_facets = int(
        connection.execute(
            "SELECT count(*) FROM search_facets "
            "WHERE document_rowid NOT IN (SELECT rowid FROM search_index)"
        ).fetchone()[0]
    )
    if orphan_facets:
        raise sqlite3.DatabaseError(f"staged index has {orphan_facets} orphan facet row(s)")


def _normalized_values(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if isinstance(value, str) and value})


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


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
