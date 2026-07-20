from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evidence_vault.ingest import IngestMetadata, ingest_file
from evidence_vault.mcp_server import create_mcp_server
from evidence_vault.observe import append_observation
from evidence_vault import read_api
from evidence_vault.wiki import evolve_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class ReadOnlyMcpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()
        source = self.vault / "ecology-field-note.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        self.ingested = ingest_file(self.vault, source, IngestMetadata(title="Wetland note"))
        self.assertEqual("created", self.ingested.status, self.ingested.message)
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_read_api_source_observation_evidence_and_perspective(self) -> None:
        source = read_api.get_source(self.vault, str(self.ingested.source_id))
        self.assertTrue(source["success"])
        self.assertIn("Frog calls", source["text"])

        observations = read_api.get_observations(self.vault, subject="entity-example-wetland")
        self.assertEqual(1, observations["count"])
        self.assertEqual("explicit_statement", observations["observations"][0]["statement_basis"])

        locator = {
            "source_id": self.ingested.source_id,
            "source_hash": self.ingested.content_hash,
            "normalized_path": self.ingested.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": 3, "end_line": 3},
        }
        evidence = read_api.get_evidence(self.vault, locator)
        self.assertTrue(evidence["success"], evidence)

        perspective = read_api.get_perspective_at(
            self.vault,
            "entity-example-wetland",
            "topic-amphibian-activity",
            "2026-07-19",
        )
        self.assertEqual("supported", perspective["status"])
        self.assertEqual("explicit_statement", perspective["statement_basis"])

        search = read_api.search(self.vault, "frog", layer="observation")
        self.assertGreaterEqual(search["count"], 1)
        self.assertTrue(any(hit["layer"] == "observation" for hit in search["hits"]))

        entity = read_api.get_entity(self.vault, "entity-example-wetland")
        self.assertTrue(entity["success"])
        self.assertIsNotNone(entity["wiki"])

    def test_mcp_server_registers_expected_tools_without_writes(self) -> None:
        server = create_mcp_server(self.vault)
        # FastMCP stores tools internally; list via private map or list_tools coroutine.
        tool_names = set(server._tool_manager._tools.keys())  # noqa: SLF001 — test surface
        expected = {
            "search",
            "get_source",
            "get_evidence",
            "get_entity",
            "get_topic",
            "get_synthesis",
            "get_observations",
            "get_perspective_at",
            "get_perspective_timeline",
            "compare_perspectives",
            "explore_gaps",
            "explore_ask",
        }
        self.assertTrue(expected.issubset(tool_names), tool_names)
        # Starting server factory must not append observations or queue proposals.
        self.assertEqual(1, len(list((self.vault / "observations").glob("**/*.json"))))
        self.assertFalse(list((self.vault / "system" / "update-queue").glob("*.json")))

    def test_mcp_tools_are_callable_and_round_trip_citations(self) -> None:
        server = create_mcp_server(self.vault)
        tools = server._tool_manager._tools  # noqa: SLF001
        source_json = tools["get_source"].fn(source_id=str(self.ingested.source_id))
        source = json.loads(source_json)
        self.assertTrue(source["success"])
        evidence_json = tools["get_evidence"].fn(
            source_id=str(self.ingested.source_id),
            source_hash=str(self.ingested.content_hash),
            normalized_path=str(self.ingested.normalized_path),
            locator_kind="line_range",
            selector_json=json.dumps({"start_line": 3, "end_line": 3}),
        )
        evidence = json.loads(evidence_json)
        self.assertTrue(evidence["success"], evidence)
        obs_json = tools["get_observations"].fn(observation_id="obs-20260718-frog-calls")
        obs = json.loads(obs_json)
        self.assertTrue(obs["success"])
        self.assertEqual("explicit_statement", obs["observations"][0]["statement_basis"])


if __name__ == "__main__":
    unittest.main()
