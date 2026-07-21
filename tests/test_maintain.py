from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.layout import init_vault
from knowledge_desk.maintain import parse_steps, run_maintain_cycle, run_maintain_loop, last_run
from knowledge_desk.observe import append_observation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class MaintainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        init_vault(self.vault, write_readmes=False)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parse_steps_rejects_unknown(self) -> None:
        with self.assertRaises(Exception):
            parse_steps("inbox_ingest,not_a_step")

    def test_once_ingests_inbox_and_evolves_wiki(self) -> None:
        inbox_file = self.vault / "inbox" / "note.txt"
        inbox_file.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())

        result = run_maintain_cycle(
            self.vault,
            steps=["inbox_ingest", "wiki_evolve", "lint", "index_rebuild", "explore_gaps"],
            poll_subscriptions=False,
            propose_gaps=True,
        )
        self.assertEqual("ok", result.status, result.message)
        self.assertTrue((self.vault / "system" / "jobs" / "last-run.json").is_file())
        self.assertTrue((self.vault / "system" / "jobs" / "ledger.jsonl").is_file())
        # Source published under sources/
        sources = list((self.vault / "sources").glob("src-*"))
        self.assertEqual(1, len(sources))

        # Append observation and re-run wiki_evolve via maintain.
        source_id = sources[0].name
        manifest = json.loads((sources[0] / "manifest.json").read_text(encoding="utf-8"))
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
                    "source_id": source_id,
                    "source_hash": manifest["content_hash"],
                    "normalized_path": f"sources/{source_id}/normalized.md",
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        self.assertEqual("created", append_observation(self.vault, observation).status)

        again = run_maintain_cycle(
            self.vault,
            steps=["wiki_evolve", "lint"],
            poll_subscriptions=False,
            propose_gaps=False,
        )
        self.assertEqual("ok", again.status, again.message)
        entity = self.vault / "wiki" / "entities" / "example-wetland.md"
        self.assertTrue(entity.is_file())
        text = entity.read_text(encoding="utf-8")
        self.assertIn("Source-specific positions", text)
        self.assertIn("Consensus", text)
        self.assertIn("What changed", text)
        # Source summary page from living-wiki compile
        summaries = list((self.vault / "wiki" / "syntheses").glob("source-*.md"))
        self.assertTrue(summaries, "expected source summary synthesis page")

        status = last_run(self.vault)
        self.assertEqual("ok", status.get("status"))

        # Idempotent second cycle on empty inbox does not duplicate sources.
        third = run_maintain_cycle(
            self.vault,
            steps=["inbox_ingest"],
            poll_subscriptions=False,
            propose_gaps=False,
        )
        self.assertIn(third.status, {"ok", "noop"})
        self.assertEqual(1, len(list((self.vault / "sources").glob("src-*"))))

    def test_loop_respects_max_cycles(self) -> None:
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        result = run_maintain_loop(
            self.vault,
            interval_seconds=1.0,
            steps=["lint"],
            max_cycles=2,
            poll_subscriptions=False,
            propose_gaps=False,
            sleep_fn=fake_sleep,
        )
        self.assertEqual("maintain.loop", result.operation)
        self.assertEqual(1, len(sleeps))  # sleep between cycle 1 and 2 only once? Wait - after cycle1 sleep, cycle2, then stop
        # max_cycles=2: run, sleep, run, stop → 1 sleep
        self.assertEqual([1.0], sleeps)

    def test_direct_ingest_still_works_without_maintainer(self) -> None:
        source = self.vault / "manual.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        result = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", result.status)


if __name__ == "__main__":
    unittest.main()
