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
from knowledge_desk.observations import ObservationQuery, list_observations
from knowledge_desk.perspective import compare_perspectives, perspective_at, perspective_timeline


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PerspectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains"):
            (self.vault / name).mkdir()
        source = self.vault / "note.txt"
        source.write_text(
            "Alpha view one.\nAlpha view two supersedes.\nBeta contradiction.\nHorizon-bound claim.\n",
            encoding="utf-8",
        )
        ingested = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", ingested.status, ingested.message)
        self.source = ingested
        self.subject = "entity-alpha"
        self.topic = "topic-outlook"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _obs(
        self,
        observation_id: str,
        assertion: str,
        *,
        valid_at: str,
        orientation: str = "supportive",
        horizon: dict | None = None,
        relations: list | None = None,
        line: int = 1,
        subject: str | None = None,
        topic: str | None = None,
    ) -> dict:
        return {
            "schema_version": "1.0.0",
            "observation_id": observation_id,
            "subjects": [
                {
                    "kind": "entity",
                    "label": "Alpha",
                    "ref_id": subject or self.subject,
                }
            ],
            "topics": [
                {
                    "kind": "topic",
                    "label": "Outlook",
                    "ref_id": topic or self.topic,
                }
            ],
            "assertion": assertion,
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": orientation,
            "confidence": 0.8,
            "reasoning": "Test.",
            "mechanisms": [],
            "conditions": [],
            "implications": [],
            "catalysts": [],
            "risks": [],
            "publication_date": valid_at[:10],
            "expressed_at": valid_at,
            "valid_at": valid_at,
            "recorded_at": "2026-07-20T12:00:00Z",
            "horizon": horizon,
            "freshness": {"as_of": valid_at, "status": "historical"},
            "evidence": [
                {
                    "source_id": self.source.source_id,
                    "source_hash": self.source.content_hash,
                    "normalized_path": self.source.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": line, "end_line": line},
                }
            ],
            "relations": relations or [],
            "extensions": {},
        }

    def test_missing_evidence_is_unknown_not_neutral(self) -> None:
        result = perspective_at(self.vault, self.subject, self.topic, "2020-01-01")
        self.assertEqual("unknown", result.status)
        self.assertEqual("insufficient_evidence", result.reason)
        self.assertIsNone(result.orientation)
        self.assertIsNone(result.observation_id)

    def test_perspective_at_selects_latest_applying_observation(self) -> None:
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs("obs-20240101-alpha-one", "First view", valid_at="2024-01-01T00:00:00Z", line=1),
            ).status,
        )
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs(
                    "obs-20240601-alpha-two",
                    "Second view",
                    valid_at="2024-06-01T00:00:00Z",
                    orientation="critical",
                    relations=[{"type": "supersedes", "observation_id": "obs-20240101-alpha-one"}],
                    line=2,
                ),
            ).status,
        )

        early = perspective_at(self.vault, self.subject, self.topic, "2024-03-01")
        self.assertEqual("supported", early.status)
        self.assertEqual("obs-20240101-alpha-one", early.observation_id)
        self.assertEqual("supportive", early.orientation)

        late = perspective_at(self.vault, self.subject, self.topic, "2024-07-01")
        self.assertEqual("supported", late.status)
        self.assertEqual("obs-20240601-alpha-two", late.observation_id)
        self.assertEqual("critical", late.orientation)
        self.assertIn("obs-20240101-alpha-one", late.superseded_observation_ids)

        before = perspective_at(self.vault, self.subject, self.topic, "2023-12-31")
        self.assertEqual("unknown", before.status)

    def test_horizon_excludes_observation_outside_window(self) -> None:
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs(
                    "obs-20240101-horizon",
                    "Only for Q1",
                    valid_at="2024-01-15T00:00:00Z",
                    horizon={"start": "2024-01-01", "end": "2024-03-31", "description": "Q1"},
                    line=4,
                ),
            ).status,
        )
        inside = perspective_at(self.vault, self.subject, self.topic, "2024-02-01")
        self.assertEqual("supported", inside.status)
        outside = perspective_at(self.vault, self.subject, self.topic, "2024-05-01")
        self.assertEqual("unknown", outside.status)
        self.assertEqual("insufficient_evidence", outside.reason)

    def test_explicit_unknown_orientation_is_supported(self) -> None:
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs(
                    "obs-20240101-unknown-orient",
                    "Speaker declined to take a position",
                    valid_at="2024-01-01T00:00:00Z",
                    orientation="unknown",
                    line=1,
                ),
            ).status,
        )
        result = perspective_at(self.vault, self.subject, self.topic, "2024-02-01")
        self.assertEqual("supported", result.status)
        self.assertEqual("unknown", result.orientation)

    def test_conflicted_active_orientations(self) -> None:
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs(
                    "obs-20240101-support",
                    "Supportive reading",
                    valid_at="2024-01-01T00:00:00Z",
                    orientation="supportive",
                    line=1,
                ),
            ).status,
        )
        self.assertEqual(
            "created",
            append_observation(
                self.vault,
                self._obs(
                    "obs-20240102-critical",
                    "Critical reading",
                    valid_at="2024-01-02T00:00:00Z",
                    orientation="critical",
                    relations=[{"type": "contradicts", "observation_id": "obs-20240101-support"}],
                    line=3,
                ),
            ).status,
        )
        result = perspective_at(self.vault, self.subject, self.topic, "2024-02-01")
        self.assertEqual("conflicted", result.status)
        self.assertEqual("obs-20240102-critical", result.observation_id)
        self.assertIn("obs-20240101-support", result.conflicting_observation_ids)

    def test_timeline_lists_changes(self) -> None:
        append_observation(
            self.vault,
            self._obs("obs-20240101-alpha-one", "First", valid_at="2024-01-01T00:00:00Z", line=1),
        )
        append_observation(
            self.vault,
            self._obs(
                "obs-20240601-alpha-two",
                "Second",
                valid_at="2024-06-01T00:00:00Z",
                relations=[{"type": "supersedes", "observation_id": "obs-20240101-alpha-one"}],
                line=2,
            ),
        )
        timeline = perspective_timeline(self.vault, self.subject, self.topic)
        self.assertEqual("supported", timeline.status)
        self.assertEqual(2, len(timeline.events))
        self.assertEqual("introduced", timeline.events[0]["change"])
        self.assertEqual("supersedes", timeline.events[1]["change"])

        clipped = perspective_timeline(
            self.vault, self.subject, self.topic, start="2024-05-01", end="2024-12-31"
        )
        self.assertEqual(1, len(clipped.events))
        self.assertEqual("obs-20240601-alpha-two", clipped.events[0]["observation_id"])

    def test_date_only_bounds_include_entire_day_and_exclude_next_midnight(self) -> None:
        observations = [
            self._obs(
                "obs-20231231-before",
                "Before range",
                valid_at="2023-12-31T23:59:59.999999Z",
                line=1,
            ),
            self._obs(
                "obs-20240101-early",
                "Early fractional",
                valid_at="2024-01-01T00:00:00.000001Z",
                line=1,
            ),
            self._obs(
                "obs-20240101-late",
                "Final fractional second",
                valid_at="2024-01-01T23:59:59.500Z",
                line=2,
            ),
            self._obs(
                "obs-20240102-next",
                "Next midnight",
                valid_at="2024-01-02T00:00:00Z",
                line=2,
            ),
        ]
        for observation in observations:
            result = append_observation(self.vault, observation)
            self.assertEqual("created", result.status, result.message)

        at = perspective_at(self.vault, self.subject, self.topic, "2024-01-01")
        self.assertEqual("supported", at.status)
        self.assertEqual("obs-20240101-late", at.observation_id)

        timeline = perspective_timeline(
            self.vault,
            self.subject,
            self.topic,
            start="2024-01-01",
            end="2024-01-01",
        )
        self.assertEqual(
            ["obs-20240101-early", "obs-20240101-late"],
            [event["observation_id"] for event in timeline.events],
        )

    def test_observation_order_normalizes_offsets_and_breaks_instant_ties_by_id(self) -> None:
        observations = [
            self._obs(
                "obs-20240101-offset-a",
                "Eight UTC",
                valid_at="2024-01-01T10:00:00+02:00",
                line=1,
            ),
            self._obs(
                "obs-20240101-offset-b",
                "Eight thirty UTC",
                valid_at="2024-01-01T08:30:00Z",
                line=2,
            ),
            self._obs(
                "obs-20240101-offset-c",
                "Equivalent eight thirty UTC",
                valid_at="2024-01-01T09:30:00+01:00",
                line=3,
            ),
        ]
        for observation in observations:
            result = append_observation(self.vault, observation)
            self.assertEqual("created", result.status, result.message)

        records = list_observations(
            self.vault,
            ObservationQuery(subject=self.subject, topic=self.topic),
        )
        self.assertEqual(
            [
                "obs-20240101-offset-a",
                "obs-20240101-offset-b",
                "obs-20240101-offset-c",
            ],
            [record.observation["observation_id"] for record in records],
        )

    def test_timeline_preserves_every_material_relation(self) -> None:
        first = self._obs(
            "obs-20240101-relation-first",
            "First relation target",
            valid_at="2024-01-01T00:00:00Z",
            line=1,
        )
        second = self._obs(
            "obs-20240102-relation-second",
            "Second relation target",
            valid_at="2024-01-02T00:00:00Z",
            line=2,
        )
        combined = self._obs(
            "obs-20240103-relation-combined",
            "Supports and refines distinct prior records",
            valid_at="2024-01-03T00:00:00Z",
            relations=[
                {"type": "confirms", "observation_id": first["observation_id"]},
                {"type": "refines", "observation_id": second["observation_id"]},
            ],
            line=3,
        )
        for observation in (first, second, combined):
            result = append_observation(self.vault, observation)
            self.assertEqual("created", result.status, result.message)

        timeline = perspective_timeline(self.vault, self.subject, self.topic)
        event = next(
            item
            for item in timeline.events
            if item["observation_id"] == combined["observation_id"]
        )
        self.assertEqual("confirms", event["change"])
        self.assertEqual(first["observation_id"], event["related_observation_id"])
        self.assertEqual(
            [
                {"type": "confirms", "observation_id": first["observation_id"]},
                {"type": "refines", "observation_id": second["observation_id"]},
            ],
            event["relations"],
        )

    def test_compare_subjects_dimensional(self) -> None:
        other = "entity-beta"
        append_observation(
            self.vault,
            self._obs(
                "obs-20240101-alpha",
                "Alpha is supportive",
                valid_at="2024-01-01T00:00:00Z",
                orientation="supportive",
                line=1,
                subject=self.subject,
            ),
        )
        append_observation(
            self.vault,
            self._obs(
                "obs-20240101-beta",
                "Beta is critical",
                valid_at="2024-01-01T00:00:00Z",
                orientation="critical",
                line=3,
                subject=other,
            ),
        )
        compared = compare_perspectives(
            self.vault,
            [self.subject, other],
            self.topic,
            "2024-06-01",
        )
        self.assertEqual("compared", compared.status)
        orientation_dim = next(
            item for item in compared.dimensions if item["dimension"] == "orientation"
        )
        self.assertEqual("disagree", orientation_dim["agreement"])
        self.assertIn(f"{self.topic}:orientation", compared.disagreements)

        sparse = compare_perspectives(
            self.vault,
            [self.subject, "entity-missing"],
            self.topic,
            "2024-06-01",
        )
        self.assertEqual("partial", sparse.status)
        self.assertIn("entity-missing", sparse.insufficient)
        # Must not invent neutral orientation for the missing subject.
        missing_row = next(row for row in sparse.subjects if row["subject"] == "entity-missing")
        self.assertEqual("unknown", missing_row["status"])
        self.assertIsNone(missing_row["orientation"])

    def test_cli_perspective_at(self) -> None:
        append_observation(
            self.vault,
            self._obs("obs-20240101-alpha-one", "First", valid_at="2024-01-01T00:00:00Z", line=1),
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--vault",
                    str(self.vault),
                    "perspective",
                    "at",
                    "--subject",
                    self.subject,
                    "--topic",
                    self.topic,
                    "--as-of",
                    "2024-02-01",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual("supported", payload["status"])
        self.assertEqual("obs-20240101-alpha-one", payload["observation_id"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--vault",
                    str(self.vault),
                    "perspective",
                    "at",
                    "--subject",
                    self.subject,
                    "--topic",
                    self.topic,
                    "--as-of",
                    "2020-01-01",
                ]
            )
        self.assertEqual(2, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual("unknown", payload["status"])


if __name__ == "__main__":
    unittest.main()
