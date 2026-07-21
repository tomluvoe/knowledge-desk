from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from knowledge_desk.cli import main
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.observe import append_observation, append_observation_path
from knowledge_desk.validation import validate_vault


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class ObservationWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains"):
            (self.vault / name).mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(self, fixture_name: str):
        source = self.vault / fixture_name
        source.write_bytes((FIXTURES / fixture_name).read_bytes())
        result = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        return result

    def test_ecology_and_history_observations_end_to_end(self) -> None:
        eco_source = self._ingest("ecology-field-note.txt")
        hist_source = self._ingest("history-letter.md")

        ecology = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-frog-calls",
            "subjects": [
                {"kind": "entity", "label": "Example wetland", "ref_id": "entity-example-wetland"}
            ],
            "topics": [
                {"kind": "topic", "label": "Amphibian activity", "ref_id": "topic-amphibian-activity"}
            ],
            "assertion": "Frog calls were recorded at three sampling points.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "supportive",
            "confidence": 0.92,
            "reasoning": "This restates the recorded field observation without inferring abundance.",
            "mechanisms": ["Acoustic detection"],
            "conditions": ["The recording protocol was applied consistently"],
            "implications": ["The sites had detectable frog activity during sampling"],
            "catalysts": ["Additional nocturnal sampling"],
            "risks": ["Calls do not establish population size"],
            "publication_date": "2026-07-18",
            "expressed_at": "2026-07-18T20:00:00Z",
            "valid_at": "2026-07-18T20:00:00Z",
            "recorded_at": "2026-07-20T10:05:00Z",
            "horizon": {"description": "The sampling evening", "end": "2026-07-18", "start": "2026-07-18"},
            "freshness": {"as_of": "2026-07-18T20:00:00Z", "status": "historical"},
            "evidence": [
                {
                    "source_id": eco_source.source_id,
                    "source_hash": eco_source.content_hash,
                    "normalized_path": eco_source.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {"org.example.ecology": {"sampling_method": "acoustic"}},
        }
        history = {
            "schema_version": "1.0.0",
            "observation_id": "obs-18940312-bridge-status",
            "subjects": [
                {
                    "kind": "entity",
                    "label": "Example River Bridge",
                    "ref_id": "entity-example-river-bridge",
                }
            ],
            "topics": [
                {
                    "kind": "topic",
                    "label": "Infrastructure history",
                    "ref_id": "topic-infrastructure-history",
                }
            ],
            "assertion": "The letter describes the bridge as unfinished on its stated date.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "neutral",
            "confidence": 0.84,
            "reasoning": "The observation preserves what the document says and does not infer an exact completion date.",
            "mechanisms": [],
            "conditions": ["The letter date and transcription are accurate"],
            "implications": [
                "Completion occurred after the letter's stated date if the account is reliable"
            ],
            "catalysts": ["Discovery of construction records"],
            "risks": ["The writer may have had incomplete information"],
            "publication_date": None,
            "expressed_at": "1894-03-12T12:00:00Z",
            "valid_at": "1894-03-12T12:00:00Z",
            "recorded_at": "2026-07-20T10:10:00Z",
            "horizon": None,
            "freshness": {"as_of": None, "status": "historical"},
            "evidence": [
                {
                    "source_id": hist_source.source_id,
                    "source_hash": hist_source.content_hash,
                    "normalized_path": hist_source.normalized_path,
                    "locator_kind": "markdown_heading",
                    "selector": {"heading": "Letter transcription", "occurrence": 1},
                }
            ],
            "relations": [],
            "extensions": {"org.example.history": {"document_genre": "letter"}},
        }

        eco_result = append_observation(self.vault, ecology)
        hist_result = append_observation(self.vault, history)
        self.assertEqual("created", eco_result.status, eco_result.message)
        self.assertEqual("created", hist_result.status, hist_result.message)

        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertEqual(2, report.checked["observations"])
        self.assertEqual(2, report.checked["sources"])

        # Identical re-append is a noop; mutation attempt fails without rewrite.
        self.assertEqual("noop", append_observation(self.vault, ecology).status)
        changed = dict(ecology)
        changed["assertion"] = "Mutated assertion must not rewrite history."
        blocked = append_observation(self.vault, changed)
        self.assertEqual("failed", blocked.status)
        self.assertIn("already exists", blocked.message)
        stored = json.loads(
            (self.vault / "observations" / "obs-20260718-frog-calls.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ecology["assertion"], stored["assertion"])

    def test_cli_observe_writes_validated_observation(self) -> None:
        source = self._ingest("ecology-field-note.txt")
        document = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20260718-cli-frog",
            "subjects": [{"kind": "entity", "label": "Wetland", "ref_id": "entity-wetland"}],
            "topics": [{"kind": "topic", "label": "Calls", "ref_id": "topic-calls"}],
            "assertion": "Frog calls were recorded at three sampling points.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "supportive",
            "confidence": 0.9,
            "reasoning": "CLI path.",
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
                    "source_id": source.source_id,
                    "source_hash": source.content_hash,
                    "normalized_path": source.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        path = self.vault / "draft-observation.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--vault", str(self.vault), "observe", str(path)])
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual("created", payload["results"][0]["status"])
        self.assertTrue((self.vault / "observations" / "obs-20260718-cli-frog.json").is_file())

    def test_observe_rejects_unresolvable_evidence(self) -> None:
        document = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20240101-bad-evidence",
            "subjects": [{"kind": "entity", "label": "X", "ref_id": "entity-x"}],
            "topics": [{"kind": "topic", "label": "Y", "ref_id": "topic-y"}],
            "assertion": "Unsupported claim.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "unknown",
            "confidence": 0.1,
            "reasoning": "Missing source.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": None,
            "expressed_at": None,
            "valid_at": None,
            "recorded_at": "2024-01-02T00:00:00Z",
            "horizon": None,
            "freshness": {"as_of": None, "status": "unknown"},
            "evidence": [
                {
                    "source_id": "src-" + "e" * 24,
                    "source_hash": "sha256:" + "e" * 64,
                    "normalized_path": f"sources/src-{'e' * 24}/normalized.md",
                    "locator_kind": "line_range",
                    "selector": {"start_line": 1, "end_line": 1},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        path = self.vault / "bad.json"
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        result = append_observation_path(self.vault, path)
        self.assertEqual("failed", result.status)
        self.assertFalse(list((self.vault / "observations").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
