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
from knowledge_desk.observe import append_observation
from knowledge_desk.observations import ObservationQuery, get_observation, list_observations, relation_graph


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class ObservationQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains"):
            (self.vault / name).mkdir()
        self._seed()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed(self) -> None:
        eco = self.vault / "ecology-field-note.txt"
        eco.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        hist = self.vault / "history-letter.md"
        hist.write_bytes((FIXTURES / "history-letter.md").read_bytes())
        eco_source = ingest_file(self.vault, eco, IngestMetadata())
        hist_source = ingest_file(self.vault, hist, IngestMetadata())
        self.assertEqual("created", eco_source.status, eco_source.message)
        self.assertEqual("created", hist_source.status, hist_source.message)
        self.eco_source_id = eco_source.source_id

        first = {
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
                    "source_id": eco_source.source_id,
                    "source_hash": eco_source.content_hash,
                    "normalized_path": eco_source.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        second = {
            "schema_version": "1.0.0",
            "observation_id": "obs-18940312-bridge-status",
            "subjects": [
                {"kind": "entity", "label": "Example River Bridge", "ref_id": "entity-example-river-bridge"}
            ],
            "topics": [
                {"kind": "topic", "label": "Infrastructure history", "ref_id": "topic-infrastructure-history"}
            ],
            "assertion": "The letter describes the bridge as unfinished on its stated date.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "neutral",
            "confidence": 0.8,
            "reasoning": "Document says so.",
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
                    "source_id": hist_source.source_id,
                    "source_hash": hist_source.content_hash,
                    "normalized_path": hist_source.normalized_path,
                    "locator_kind": "markdown_heading",
                    "selector": {"heading": "Letter transcription", "occurrence": 1},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        refine = dict(first)
        refine["observation_id"] = "obs-20260719-frog-calls-refine"
        refine["assertion"] = "Calls were confirmed at the same three points the next evening."
        refine["valid_at"] = "2026-07-19T20:00:00Z"
        refine["expressed_at"] = "2026-07-19T20:00:00Z"
        refine["relations"] = [{"type": "refines", "observation_id": "obs-20260718-frog-calls"}]
        for document in (first, second, refine):
            result = append_observation(self.vault, document)
            self.assertEqual("created", result.status, result.message)

    def test_list_all_and_filter_by_subject_topic_source(self) -> None:
        all_records = list_observations(self.vault)
        self.assertEqual(3, len(all_records))

        by_subject = list_observations(self.vault, ObservationQuery(subject="entity-example-wetland"))
        self.assertEqual(2, len(by_subject))
        self.assertTrue(all("wetland" in r.observation["subjects"][0]["ref_id"] for r in by_subject))

        by_label = list_observations(self.vault, ObservationQuery(topic="Infrastructure"))
        self.assertEqual(1, len(by_label))
        self.assertEqual("obs-18940312-bridge-status", by_label[0].observation["observation_id"])

        by_source = list_observations(self.vault, ObservationQuery(source_id=self.eco_source_id))
        self.assertEqual(2, len(by_source))

        by_orientation = list_observations(self.vault, ObservationQuery(orientation="neutral"))
        self.assertEqual(1, len(by_orientation))

    def test_get_and_relation_graph(self) -> None:
        record = get_observation(self.vault, "obs-20260719-frog-calls-refine")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual("refines", record.observation["relations"][0]["type"])

        graph = relation_graph(self.vault)
        self.assertEqual(
            [{"type": "refines", "observation_id": "obs-20260718-frog-calls"}],
            graph["obs-20260719-frog-calls-refine"],
        )
        self.assertEqual([], graph["obs-20260718-frog-calls"])

    def test_cli_list_and_get(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--vault",
                    str(self.vault),
                    "observations",
                    "list",
                    "--subject",
                    "wetland",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(2, payload["count"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--vault",
                    str(self.vault),
                    "observations",
                    "get",
                    "obs-18940312-bridge-status",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual("obs-18940312-bridge-status", payload["observation"]["observation_id"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--vault", str(self.vault), "observations", "get", "obs-20990101-missing"])
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
