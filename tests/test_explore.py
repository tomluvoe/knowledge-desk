from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.explore import explore_ask, explore_gaps
from knowledge_desk.index import rebuild_index
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.observe import append_observation
from knowledge_desk.wiki import evolve_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class ExploreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()
        (self.vault / "system" / "update-queue").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest_fixture(self, name: str = "ecology-field-note.txt"):
        path = self.vault / name
        path.write_bytes((FIXTURES / name).read_bytes())
        result = ingest_file(self.vault, path, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        return result

    def _observe(self, ingested) -> None:
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
            "reasoning": "Direct.",
            "mechanisms": [],
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

    def test_empty_vault_gaps_and_ask(self) -> None:
        gaps = explore_gaps(self.vault)
        self.assertEqual("ok", gaps.status)
        self.assertEqual(0, gaps.total_sources)
        ask = explore_ask(self.vault, "Where were frog calls recorded?")
        self.assertEqual("insufficient_evidence", ask.status)
        self.assertIsNone(ask.answer)

    def test_source_only_is_gap_missing_observation_and_wiki(self) -> None:
        ingested = self._ingest_fixture()
        gaps = explore_gaps(self.vault)
        self.assertEqual(1, gaps.count)
        entry = gaps.gaps[0]
        self.assertEqual(ingested.source_id, entry["source_id"])
        self.assertEqual(["observation", "wiki"], entry["missing"])

    def test_partial_coverage_observation_without_wiki(self) -> None:
        ingested = self._ingest_fixture()
        self._observe(ingested)
        gaps = explore_gaps(self.vault)
        self.assertEqual(1, gaps.count)
        self.assertEqual(["wiki"], gaps.gaps[0]["missing"])
        self.assertIn("obs-20260718-frog-calls", gaps.gaps[0]["observation_ids"])

    def test_fully_covered_source_not_listed(self) -> None:
        ingested = self._ingest_fixture()
        self._observe(ingested)
        evolve_wiki(self.vault)
        gaps = explore_gaps(self.vault)
        self.assertEqual(0, gaps.count)
        self.assertEqual(1, gaps.covered_sources)

    def test_ask_answers_from_source_with_citations(self) -> None:
        self._ingest_fixture()
        ask = explore_ask(self.vault, "Where were frog calls recorded?")
        self.assertEqual("answered", ask.status, ask.message)
        self.assertIsNotNone(ask.answer)
        self.assertGreaterEqual(len(ask.citations), 1)
        self.assertTrue(any(c["layer"] == "source" for c in ask.citations))
        self.assertIn("frog", ask.citations[0]["quote"].casefold())

    def test_ask_with_index_and_propose_writes_queue(self) -> None:
        self._ingest_fixture()
        rebuild_index(self.vault)
        ask = explore_ask(self.vault, "frog sampling points", propose=True)
        self.assertEqual("answered", ask.status, ask.message)
        self.assertIsNotNone(ask.proposal_path)
        proposal = self.vault / ask.proposal_path
        self.assertTrue(proposal.is_file())
        payload = json.loads(proposal.read_text(encoding="utf-8"))
        self.assertEqual("proposed", payload["status"])
        self.assertEqual("explore_ask_proposal", payload["kind"])
        # Canonical wiki remains untouched by propose.
        self.assertFalse(list((self.vault / "wiki").glob("**/*.md")))

    def test_gaps_propose_does_not_mutate_canonical_layers(self) -> None:
        self._ingest_fixture()
        gaps = explore_gaps(self.vault, propose=True)
        self.assertEqual("ok", gaps.status)
        self.assertIsNotNone(gaps.proposal_path)
        self.assertTrue((self.vault / gaps.proposal_path).is_file())
        self.assertEqual([], list((self.vault / "observations").glob("**/*.json")))
        self.assertEqual([], [p for p in (self.vault / "wiki").glob("**/*.md") if p.name != "README.md"])


if __name__ == "__main__":
    unittest.main()
