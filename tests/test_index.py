from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evidence_vault.index import index_path, rebuild_index, search_index
from evidence_vault.ingest import IngestMetadata, ingest_file
from evidence_vault.observe import append_observation
from evidence_vault.util import render_frontmatter, utc_now


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
        self.assertGreaterEqual(by_subject.count, 1)
        self.assertTrue(all("entity-example-wetland" in hit["subjects"] for hit in by_subject.hits))

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


if __name__ == "__main__":
    unittest.main()
