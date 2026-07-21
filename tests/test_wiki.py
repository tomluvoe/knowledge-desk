from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.observe import append_observation
from knowledge_desk.validation import validate_vault
from knowledge_desk.wiki import evolve_wiki, refine_validate_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class WikiEvolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains"):
            (self.vault / name).mkdir()
        source = self.vault / "ecology-field-note.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        self.ingested = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", self.ingested.status, self.ingested.message)
        observation = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-frog-calls",
            "subjects": [{"kind": "entity", "label": "Example wetland", "ref_id": "entity-example-wetland"}],
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
                    "source_id": self.ingested.source_id,
                    "source_hash": self.ingested.content_hash,
                    "normalized_path": self.ingested.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        self.assertEqual("created", append_observation(self.vault, observation).status)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_evolve_creates_entity_and_topic_pages(self) -> None:
        result = evolve_wiki(self.vault)
        self.assertEqual("evolved", result.status, result.message)
        # Entity + topic + source summary (single-source topic synthesis/comparison/event may skip)
        kinds = {page["kind"] for page in result.pages}
        self.assertIn("entity", kinds)
        self.assertIn("topic", kinds)
        self.assertIn("synthesis", kinds)
        entity = self.vault / "wiki" / "entities" / "example-wetland.md"
        topic = self.vault / "wiki" / "topics" / "amphibian-activity.md"
        self.assertTrue(entity.is_file())
        self.assertTrue(topic.is_file())
        entity_text = entity.read_text(encoding="utf-8")
        self.assertIn("obs-20260718-frog-calls", entity_text)
        self.assertIn("Frog calls were recorded", entity_text)
        self.assertIn(self.ingested.source_id, entity_text)
        self.assertIn("Source-specific positions", entity_text)
        self.assertIn("Consensus", entity_text)
        self.assertIn("What changed", entity_text)
        self.assertIn("knowledge.desk.wiki", entity_text)
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))

        again = evolve_wiki(self.vault)
        self.assertIn(again.status, {"evolved", "noop"})
        # Idempotent content when observations unchanged.
        statuses = {page["status"] for page in again.pages}
        self.assertTrue(statuses <= {"unchanged", "updated", "created"})

    def test_living_wiki_comparison_and_event_pages(self) -> None:
        # Second observation: different entity, opposing orientation, same day + topic.
        second = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-dry-bank",
            "subjects": [{"kind": "entity", "label": "North bank", "ref_id": "entity-north-bank"}],
            "topics": [{"kind": "topic", "label": "Amphibian activity", "ref_id": "topic-amphibian-activity"}],
            "assertion": "North bank remained silent; no amphibian activity detected.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "critical",
            "confidence": 0.7,
            "reasoning": "Direct count.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": "2026-07-18",
            "expressed_at": "2026-07-18T21:00:00Z",
            "valid_at": "2026-07-18T21:00:00Z",
            "recorded_at": "2026-07-20T10:06:00Z",
            "horizon": None,
            "freshness": {"as_of": "2026-07-18T21:00:00Z", "status": "historical"},
            "evidence": [
                {
                    "source_id": self.ingested.source_id,
                    "source_hash": self.ingested.content_hash,
                    "normalized_path": self.ingested.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 1, "end_line": 1},
                }
            ],
            "relations": [
                {"type": "contradicts", "observation_id": "obs-20260718-frog-calls"},
            ],
            "extensions": {},
        }
        self.assertEqual("created", append_observation(self.vault, second).status)
        result = evolve_wiki(self.vault)
        self.assertEqual("evolved", result.status, result.message)
        kinds = {page["kind"] for page in result.pages}
        self.assertIn("comparison", kinds)
        self.assertIn("event", kinds)
        compare = list((self.vault / "wiki" / "comparisons").glob("compare-*.md"))
        self.assertTrue(compare)
        event = list((self.vault / "wiki" / "events").glob("event-*.md"))
        self.assertTrue(event)
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))

    def test_refine_validate_flags_unsupported_and_orphan_pages(self) -> None:
        evolve_wiki(self.vault)
        ok = refine_validate_wiki(self.vault)
        self.assertTrue(ok.vault_valid)
        # Evolved pages should not produce unsupported_synthesis errors.
        codes = {finding["code"] for finding in ok.findings if finding["severity"] == "error"}
        self.assertNotIn("unsupported_synthesis", codes)

        bad = self.vault / "wiki" / "topics" / "unsupported.md"
        bad.write_text(
            "---\n"
            'schema_version: "1.0.0"\n'
            'wiki_id: "wiki-topic-unsupported"\n'
            'kind: "topic"\n'
            'title: "Unsupported"\n'
            'created_at: "2026-07-20T00:00:00Z"\n'
            'updated_at: "2026-07-20T00:00:00Z"\n'
            "observation_ids: []\n"
            "evidence: []\n"
            'freshness: "unknown"\n'
            "extensions: {}\n"
            "---\n\n"
            "# Unsupported\n\n"
            "This claim has no evidence chain at all.\n",
            encoding="utf-8",
        )
        refined = refine_validate_wiki(self.vault)
        self.assertFalse(refined.valid)
        self.assertTrue(any(f["code"] == "unsupported_synthesis" for f in refined.findings))
        self.assertTrue(any(f["code"] == "orphan_page" for f in refined.findings))


if __name__ == "__main__":
    unittest.main()
