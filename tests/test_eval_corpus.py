from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.explore import explore_ask
from knowledge_desk.index import rebuild_index, search_index
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.lint import lint_vault
from knowledge_desk.observe import append_observation
from knowledge_desk.perspective import perspective_at
from knowledge_desk.validation import validate_locator, validate_vault
from knowledge_desk.wiki import evolve_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class EvaluationCorpusTests(unittest.TestCase):
    """Offline quality gates for multi-domain fixtures (issue #9)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()
        self._seed_corpus()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_corpus(self) -> None:
        eco = self.vault / "ecology-field-note.txt"
        eco.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        hist = self.vault / "history-letter.md"
        hist.write_bytes((FIXTURES / "history-letter.md").read_bytes())
        self.eco = ingest_file(self.vault, eco, IngestMetadata(language="en"))
        self.hist = ingest_file(self.vault, hist, IngestMetadata(language="en"))
        self.assertEqual("created", self.eco.status, self.eco.message)
        self.assertEqual("created", self.hist.status, self.hist.message)

        eco_obs = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-frog-calls",
            "subjects": [{"kind": "entity", "label": "Wetland", "ref_id": "entity-example-wetland"}],
            "topics": [{"kind": "topic", "label": "Amphibian activity", "ref_id": "topic-amphibian-activity"}],
            "assertion": "Frog calls were recorded at three sampling points.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "supportive",
            "confidence": 0.92,
            "reasoning": "Restates the field note.",
            "mechanisms": ["Acoustic detection"],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": ["Calls do not establish population size"],
            "publication_date": "2026-07-18",
            "expressed_at": "2026-07-18T20:00:00Z",
            "valid_at": "2026-07-18T20:00:00Z",
            "recorded_at": "2026-07-20T10:05:00Z",
            "horizon": None,
            "freshness": {"as_of": "2026-07-18T20:00:00Z", "status": "historical"},
            "evidence": [
                {
                    "source_id": self.eco.source_id,
                    "source_hash": self.eco.content_hash,
                    "normalized_path": self.eco.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {"org.example.ecology": {"sampling_method": "acoustic"}},
        }
        hist_obs = {
            "schema_version": "1.0.0",
            "observation_id": "obs-18940312-bridge-status",
            "subjects": [
                {"kind": "entity", "label": "River Bridge", "ref_id": "entity-example-river-bridge"}
            ],
            "topics": [
                {"kind": "topic", "label": "Infrastructure history", "ref_id": "topic-infrastructure-history"}
            ],
            "assertion": "The letter describes the bridge as unfinished on its stated date.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "neutral",
            "confidence": 0.84,
            "reasoning": "Preserves the document claim without inferring completion.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": None,
            "expressed_at": "1894-03-12T12:00:00Z",
            "valid_at": "1894-03-12T12:00:00Z",
            "recorded_at": "2026-07-20T10:10:00Z",
            "horizon": None,
            "freshness": {"as_of": None, "status": "historical"},
            "evidence": [
                {
                    "source_id": self.hist.source_id,
                    "source_hash": self.hist.content_hash,
                    "normalized_path": self.hist.normalized_path,
                    "locator_kind": "markdown_heading",
                    "selector": {"heading": "Letter transcription", "occurrence": 1},
                }
            ],
            "relations": [],
            "extensions": {"org.example.history": {"document_genre": "letter"}},
        }
        self.assertEqual("created", append_observation(self.vault, eco_obs).status)
        self.assertEqual("created", append_observation(self.vault, hist_obs).status)
        evolve_wiki(self.vault)

    def test_vault_valid_after_dual_domain_corpus(self) -> None:
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertGreaterEqual(report.checked["sources"], 2)
        self.assertGreaterEqual(report.checked["observations"], 2)
        self.assertGreaterEqual(report.checked["wiki_notes"], 2)

    def test_citation_round_trip(self) -> None:
        locator = {
            "source_id": self.eco.source_id,
            "source_hash": self.eco.content_hash,
            "normalized_path": self.eco.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": 3, "end_line": 3},
        }
        self.assertEqual([], validate_locator(self.vault, locator))
        hist_locator = {
            "source_id": self.hist.source_id,
            "source_hash": self.hist.content_hash,
            "normalized_path": self.hist.normalized_path,
            "locator_kind": "markdown_heading",
            "selector": {"heading": "Letter transcription", "occurrence": 1},
        }
        self.assertEqual([], validate_locator(self.vault, hist_locator))

    def test_unknown_not_neutral_for_missing_perspective(self) -> None:
        missing = perspective_at(self.vault, "entity-missing", "topic-missing", "2020-01-01")
        self.assertEqual("unknown", missing.status)
        self.assertEqual("insufficient_evidence", missing.reason)
        self.assertIsNone(missing.orientation)

    def test_explore_ask_and_index_rebuild_determinism(self) -> None:
        ask = explore_ask(self.vault, "frog calls sampling points")
        self.assertEqual("answered", ask.status)
        first = rebuild_index(self.vault)
        second = rebuild_index(self.vault)
        self.assertEqual("rebuilt", first.status)
        self.assertEqual(first.indexed, second.indexed)
        hits = search_index(self.vault, "frog", layer="observation")
        self.assertGreaterEqual(hits.count, 1)

    def test_lint_clean_on_healthy_corpus(self) -> None:
        lint = lint_vault(self.vault)
        self.assertTrue(lint.vault_valid)
        self.assertEqual(0, lint.counts["error"], lint.findings)


if __name__ == "__main__":
    unittest.main()
