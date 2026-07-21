from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.lint import lint_vault
from knowledge_desk.observe import append_observation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class LintTests(unittest.TestCase):
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
        self.assertEqual("created", self.ingested.status)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _base_obs(self, observation_id: str, **overrides):
        payload = {
            "schema_version": "1.0.0",
            "observation_id": observation_id,
            "subjects": [{"kind": "entity", "label": "Wetland", "ref_id": "entity-example-wetland"}],
            "topics": [{"kind": "topic", "label": "Amphibian activity", "ref_id": "topic-amphibian-activity"}],
            "assertion": f"Assertion {observation_id}",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "supportive",
            "confidence": 0.5,
            "reasoning": "Enough reasoning text for an explicit statement.",
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
        payload.update(overrides)
        return payload

    def test_lint_flags_unresolved_contradiction_and_thin_inference(self) -> None:
        first = self._base_obs("obs-20260718-a", orientation="supportive")
        second = self._base_obs(
            "obs-20260718-b",
            orientation="critical",
            assertion="Opposite claim",
            relations=[{"type": "contradicts", "observation_id": "obs-20260718-a"}],
        )
        thin = self._base_obs(
            "obs-20260718-infer",
            statement_basis="agent_inference",
            epistemic_class="agent_hypothesis",
            reasoning="x",
            assertion="Inferred claim",
        )
        self.assertEqual("created", append_observation(self.vault, first).status)
        self.assertEqual("created", append_observation(self.vault, second).status)
        self.assertEqual("created", append_observation(self.vault, thin).status)

        report = lint_vault(self.vault)
        codes = {item["code"] for item in report.findings}
        self.assertIn("unresolved_contradiction", codes)
        self.assertIn("thin_inference_rationale", codes)

    def test_lint_flags_unsupported_wiki_synthesis(self) -> None:
        bad = self.vault / "wiki" / "topics" / "bare.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            "---\n"
            'schema_version: "1.0.0"\n'
            'wiki_id: "wiki-topic-bare"\n'
            'kind: "topic"\n'
            'title: "Bare"\n'
            'created_at: "2026-07-20T00:00:00Z"\n'
            'updated_at: "2026-07-20T00:00:00Z"\n'
            "observation_ids: []\n"
            "evidence: []\n"
            'freshness: "unknown"\n'
            "extensions: {}\n"
            "---\n\n# Bare\n\nUnsupported claim without citations.\n",
            encoding="utf-8",
        )
        report = lint_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any(item["code"] == "unsupported_synthesis" for item in report.findings))


if __name__ == "__main__":
    unittest.main()
