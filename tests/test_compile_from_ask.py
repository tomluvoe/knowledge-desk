from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.explore import compile_from_ask
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.layout import init_vault
from knowledge_desk.observe import append_observation
from knowledge_desk.proposals import apply_proposal
from knowledge_desk.wiki import evolve_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class CompileFromAskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        init_vault(self.vault, write_readmes=False)
        source = self.vault / "ecology-field-note.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        self.ingested = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", self.ingested.status, self.ingested.message)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_evidence_missing_wiki_writes_compile_proposal(self) -> None:
        result = compile_from_ask(
            self.vault,
            "What does the field note say about frog calls?",
            subject="entity-example-wetland",
            topic="topic-amphibian-activity",
            propose=True,
        )
        self.assertEqual("proposed", result.status, result.message)
        self.assertEqual("missing", result.wiki_health)
        self.assertTrue(result.proposal_path)
        path = self.vault / result.proposal_path
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("compile_from_ask_proposal", payload["kind"])
        self.assertTrue(payload.get("proposed_observations"))
        # Stubs should use real subject/topic from filters (not todo)
        obs0 = payload["proposed_observations"][0]
        self.assertEqual("entity-example-wetland", obs0["subjects"][0]["ref_id"])
        self.assertEqual("topic-amphibian-activity", obs0["topics"][0]["ref_id"])

    def test_healthy_wiki_is_noop(self) -> None:
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
        evolve_wiki(self.vault)

        result = compile_from_ask(
            self.vault,
            "What about frog calls at sampling points?",
            subject="entity-example-wetland",
            topic="topic-amphibian-activity",
            propose=True,
        )
        self.assertEqual("noop", result.status, result.message)
        self.assertEqual("healthy", result.wiki_health)
        self.assertIsNone(result.proposal_path)

    def test_no_evidence_open_question_proposal(self) -> None:
        result = compile_from_ask(
            self.vault,
            "What does the corpus say about quantum teleportation frogs?",
            propose=True,
        )
        self.assertEqual("insufficient_evidence", result.status)
        self.assertTrue(result.proposal_path)
        payload = json.loads((self.vault / result.proposal_path).read_text(encoding="utf-8"))
        self.assertEqual("explore_ask_proposal", payload["kind"])

    def test_apply_compile_proposal_observes_and_evolves(self) -> None:
        result = compile_from_ask(
            self.vault,
            "What does the note say about frog calls?",
            subject="entity-example-wetland",
            topic="topic-amphibian-activity",
            propose=True,
        )
        self.assertEqual("proposed", result.status, result.message)
        applied = apply_proposal(self.vault, self.vault / result.proposal_path)
        self.assertEqual("applied", applied.status, applied.message)
        # Observation published
        self.assertTrue(list((self.vault / "observations").glob("obs-*.json")))
        # Wiki pages from evolve
        self.assertTrue((self.vault / "wiki" / "entities" / "example-wetland.md").is_file()
                        or list((self.vault / "wiki" / "entities").glob("*.md")))
        self.assertTrue(list((self.vault / "wiki" / "topics").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
