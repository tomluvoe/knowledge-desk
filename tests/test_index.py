from __future__ import annotations

import concurrent.futures
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_desk.index as index_module
from knowledge_desk.index import index_path, rebuild_index, search_index
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.observe import append_observation
from knowledge_desk.util import render_frontmatter, utc_now
from knowledge_desk.workspace import init_workspace


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()
        self._seed()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed(self) -> None:
        source = self.vault / "ecology-field-note.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        ingested = ingest_file(self.vault, source, IngestMetadata(title="Wetland note"))
        self.assertEqual("created", ingested.status, ingested.message)
        observation = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-frog-calls",
            "subjects": [{"kind": "entity", "label": "Wetland", "ref_id": "entity-example-wetland"}],
            "topics": [{"kind": "topic", "label": "Amphibian activity", "ref_id": "topic-amphibian-activity"}],
            "assertion": "Frog calls were recorded at three sampling points.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "supportive",
            "confidence": 0.9,
            "reasoning": "Direct acoustic detection in the field note.",
            "mechanisms": ["Acoustic detection"],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": "2026-07-18",
            "expressed_at": "2026-07-18T20:00:00Z",
            "valid_at": "2026-07-18T20:00:00Z",
            "recorded_at": "2026-07-20T10:05:00Z",
            "horizon": None,
            "freshness": {"as_of": "2026-07-18T20:00:00Z", "status": "historical"},
            "evidence": [
                {
                    "source_id": ingested.source_id,
                    "source_hash": ingested.content_hash,
                    "normalized_path": ingested.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        self.assertEqual("created", append_observation(self.vault, observation).status)
        now = utc_now()
        wiki_meta = {
            "schema_version": "1.0.0",
            "wiki_id": "wiki-amphibian-activity",
            "kind": "topic",
            "title": "Amphibian activity",
            "created_at": now,
            "updated_at": now,
            "observation_ids": ["obs-20260718-frog-calls"],
            "evidence": [
                {
                    "source_id": ingested.source_id,
                    "source_hash": ingested.content_hash,
                    "normalized_path": ingested.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "freshness": "historical",
            "extensions": {},
        }
        wiki_path = self.vault / "wiki" / "topics" / "amphibian-activity.md"
        wiki_path.parent.mkdir(parents=True, exist_ok=True)
        wiki_path.write_text(
            render_frontmatter(wiki_meta)
            + "\n# Amphibian activity\n\nFrog calls were recorded at three sampling points.\n",
            encoding="utf-8",
        )

    def test_rebuild_and_search_identifies_layers(self) -> None:
        rebuilt = rebuild_index(self.vault)
        self.assertEqual("rebuilt", rebuilt.status, rebuilt.message)
        self.assertGreaterEqual(rebuilt.indexed.get("source", 0), 1)
        self.assertGreaterEqual(rebuilt.indexed.get("observation", 0), 1)
        self.assertGreaterEqual(rebuilt.indexed.get("wiki", 0), 1)
        self.assertTrue(index_path(self.vault).is_file())

        all_hits = search_index(self.vault, "frog")
        self.assertEqual("ok", all_hits.message, all_hits.message)
        self.assertGreaterEqual(all_hits.count, 2)
        layers = {hit["layer"] for hit in all_hits.hits}
        self.assertTrue({"source", "observation"} & layers)

        obs_only = search_index(self.vault, "frog", layer="observation")
        self.assertTrue(all(hit["layer"] == "observation" for hit in obs_only.hits))
        self.assertTrue(any(hit["vault_id"] == "obs-20260718-frog-calls" for hit in obs_only.hits))

        by_subject = search_index(self.vault, "frog", subject="entity-example-wetland")
        self.assertGreaterEqual(by_subject.count, 3)
        self.assertTrue(all("entity-example-wetland" in hit["subjects"] for hit in by_subject.hits))

        for layer in ("source", "observation", "wiki"):
            with self.subTest(layer=layer):
                filtered = search_index(
                    self.vault,
                    "frog",
                    layer=layer,
                    subject="entity-example-wetland",
                    topic="topic-amphibian-activity",
                )
                self.assertGreaterEqual(filtered.count, 1, filtered.message)
                self.assertTrue(
                    all("obs-20260718-frog-calls" in hit["observation_ids"] for hit in filtered.hits)
                )
        wiki_hit = search_index(
            self.vault,
            "frog",
            layer="wiki",
            topic="topic-amphibian-activity",
        ).hits[0]
        self.assertEqual("topic", wiki_hit["subtype"])

    def test_exact_facets_do_not_cross_match_similar_ids(self) -> None:
        original_path = self.vault / "observations" / "obs-20260718-frog-calls.json"
        similar = json.loads(original_path.read_text(encoding="utf-8"))
        similar["observation_id"] = "obs-20260718-frog-calls-extended"
        similar["subjects"] = [
            {
                "kind": "entity",
                "label": "Extended wetland",
                "ref_id": "entity-example-wetland-extended",
            }
        ]
        similar["assertion"] = "Exactfacetuniquemarker appears only on the extended subject."
        result = append_observation(self.vault, similar)
        self.assertEqual("created", result.status, result.message)
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)

        wrong = search_index(
            self.vault,
            "Exactfacetuniquemarker",
            layer="observation",
            subject="entity-example-wetland",
        )
        correct = search_index(
            self.vault,
            "Exactfacetuniquemarker",
            layer="observation",
            subject="entity-example-wetland-extended",
        )

        self.assertEqual(0, wrong.count)
        self.assertEqual(1, correct.count)
        self.assertEqual(similar["observation_id"], correct.hits[0]["vault_id"])

    def test_direct_source_catalog_associations_are_exact_search_facets(self) -> None:
        source = self.vault / "direct-catalogue.txt"
        source.write_text("Directsourcecatalogmarker belongs to a known series.\n", encoding="utf-8")
        ingested = ingest_file(
            self.vault,
            source,
            IngestMetadata(
                subject_refs=["entity-jordi-visser"],
                topic_refs=["topic-macro-nexus-podcast"],
            ),
        )
        self.assertEqual("created", ingested.status, ingested.message)
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)

        result = search_index(
            self.vault,
            "Directsourcecatalogmarker",
            layer="source",
            subject="entity-jordi-visser",
            topic="topic-macro-nexus-podcast",
        )

        self.assertEqual(1, result.count, result.message)
        self.assertEqual(ingested.source_id, result.hits[0]["vault_id"])
        self.assertEqual(["entity-jordi-visser"], result.hits[0]["subjects"])
        self.assertEqual(["topic-macro-nexus-podcast"], result.hits[0]["topics"])
        self.assertEqual([], result.hits[0]["observation_ids"])

    def test_workspace_subject_topic_facets_are_searchable(self) -> None:
        created = init_workspace(
            self.vault,
            title="Wetland monitoring thesis",
            workspace_id="ws-thesis-wetland-monitoring",
            subject_refs=["entity-example-wetland"],
            topic_refs=["topic-amphibian-activity"],
            statement="Workspaceuniquemarker tracks frog evidence.",
            observation_ids=["obs-20260718-frog-calls"],
        )
        self.assertEqual("created", created.status, created.message)
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)

        result = search_index(
            self.vault,
            "Workspaceuniquemarker",
            layer="memory",
            subject="entity-example-wetland",
            topic="topic-amphibian-activity",
        )

        self.assertEqual(1, result.count, result.message)
        self.assertEqual("spine", result.hits[0]["subtype"])
        self.assertIn("obs-20260718-frog-calls", result.hits[0]["observation_ids"])
        self.assertTrue(result.hits[0]["path"].endswith("/workspace.md"))

    def test_entity_and_topic_wiki_identity_create_direct_facets(self) -> None:
        now = utc_now()
        for kind, directory, slug, marker in (
            ("entity", "entities", "standalone-wetland", "Standaloneentitymarker"),
            ("topic", "topics", "standalone-habitat", "Standalonetopicmarker"),
        ):
            metadata = {
                "schema_version": "1.0.0",
                "wiki_id": f"wiki-{kind}-{slug}",
                "kind": kind,
                "title": slug.replace("-", " ").title(),
                "created_at": now,
                "updated_at": now,
                "observation_ids": [],
                "evidence": [],
                "freshness": "unknown",
                "extensions": {},
            }
            path = self.vault / "wiki" / directory / f"{slug}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                render_frontmatter(metadata) + f"\n# {metadata['title']}\n\n{marker}.\n",
                encoding="utf-8",
            )
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)

        entity = search_index(
            self.vault,
            "Standaloneentitymarker",
            layer="wiki",
            subject="entity-standalone-wetland",
        )
        topic = search_index(
            self.vault,
            "Standalonetopicmarker",
            layer="wiki",
            topic="topic-standalone-habitat",
        )

        self.assertEqual(1, entity.count)
        self.assertEqual(["entity-standalone-wetland"], entity.hits[0]["subjects"])
        self.assertEqual(1, topic.count)
        self.assertEqual(["topic-standalone-habitat"], topic.hits[0]["topics"])

    def test_live_index_is_replaced_only_after_staged_validation(self) -> None:
        wiki_path = self.vault / "wiki" / "topics" / "amphibian-activity.md"
        prior = wiki_path.read_text(encoding="utf-8") + "\nAtomicoldmarker.\n"
        wiki_path.write_text(prior, encoding="utf-8")
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)
        wiki_path.write_text(prior.replace("Atomicoldmarker", "Atomicnewmarker"), encoding="utf-8")
        validation_started = threading.Event()
        release_validation = threading.Event()
        actual_validate = index_module._validate_index

        def block_validation(connection: sqlite3.Connection, *, expected_documents: int) -> None:
            actual_validate(connection, expected_documents=expected_documents)
            validation_started.set()
            if not release_validation.wait(timeout=10):
                raise sqlite3.DatabaseError("timed out waiting to release staged validation")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            with patch("knowledge_desk.index._validate_index", side_effect=block_validation):
                future = executor.submit(rebuild_index, self.vault)
                self.assertTrue(validation_started.wait(timeout=10))
                try:
                    old_live = search_index(self.vault, "Atomicoldmarker", layer="wiki")
                    new_not_live = search_index(self.vault, "Atomicnewmarker", layer="wiki")
                    self.assertEqual(1, old_live.count)
                    self.assertEqual(0, new_not_live.count)
                finally:
                    release_validation.set()
                rebuilt = future.result(timeout=10)

        self.assertEqual("rebuilt", rebuilt.status, rebuilt.message)
        self.assertEqual(0, search_index(self.vault, "Atomicoldmarker", layer="wiki").count)
        self.assertEqual(1, search_index(self.vault, "Atomicnewmarker", layer="wiki").count)
        self.assertEqual(
            [],
            list((index_path(self.vault).parent / ".staging").glob("*.tmp")),
        )

    def test_failed_staged_rebuild_preserves_live_index(self) -> None:
        wiki_path = self.vault / "wiki" / "topics" / "amphibian-activity.md"
        wiki_path.write_text(
            wiki_path.read_text(encoding="utf-8") + "\nPreservedlivemarker.\n",
            encoding="utf-8",
        )
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)
        wiki_path.write_text(
            wiki_path.read_text(encoding="utf-8").replace(
                "Preservedlivemarker", "Failedreplacementmarker"
            ),
            encoding="utf-8",
        )

        with patch(
            "knowledge_desk.index._validate_index",
            side_effect=sqlite3.DatabaseError("injected staged validation failure"),
        ):
            failed = rebuild_index(self.vault)

        self.assertEqual("failed", failed.status)
        self.assertIn("injected staged validation failure", failed.message)
        self.assertEqual(1, search_index(self.vault, "Preservedlivemarker", layer="wiki").count)
        self.assertEqual(0, search_index(self.vault, "Failedreplacementmarker", layer="wiki").count)

    def test_missing_index_reports_rebuild_hint(self) -> None:
        result = search_index(self.vault, "frog")
        self.assertNotEqual("ok", result.message)
        self.assertIn("rebuild", result.message)

    def test_delete_index_loses_no_canonical_data(self) -> None:
        rebuild_index(self.vault)
        path = index_path(self.vault)
        self.assertTrue(path.is_file())
        path.unlink()
        self.assertTrue((self.vault / "sources").exists())
        self.assertTrue(list((self.vault / "observations").glob("**/*.json")))
        self.assertEqual("rebuilt", rebuild_index(self.vault).status)

    def test_rebuild_does_not_read_manifest_paths_outside_source_directory(self) -> None:
        manifest_path = next((self.vault / "sources").glob("src-*/manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["normalized_path"] = "../outside.md"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.vault / "outside.md").write_text(
            '---\ntitle: "Outside"\n---\n\noutside-vault-secret-marker\n',
            encoding="utf-8",
        )

        self.assertEqual("rebuilt", rebuild_index(self.vault).status)
        result = search_index(self.vault, "outside-vault-secret-marker")
        self.assertEqual(0, result.count)


if __name__ == "__main__":
    unittest.main()
