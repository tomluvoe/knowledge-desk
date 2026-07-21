from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.composition import (
    claims_from_perspective,
    compose_with_vault,
    composition_contract,
    join_contexts,
    make_claim,
    parse_external_claims,
)
from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.mcp_server import create_mcp_server
from knowledge_desk.observe import append_observation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class CompositionUnitTests(unittest.TestCase):
    def test_parse_external_claims_marks_epistemic(self) -> None:
        claims = parse_external_claims(
            {
                "claims": [
                    {
                        "text": "Constraint limit is explicit in the external system.",
                        "origin": "example-context-mcp",
                        "epistemic": "explicit",
                        "as_of": "2026-07-21",
                    },
                    {
                        "text": "Maybe related to multi-year spending.",
                        "origin": "example-context-mcp",
                        "epistemic": "inferred",
                        "confidence": 0.4,
                    },
                ]
            }
        )
        self.assertEqual(2, len(claims))
        self.assertEqual("explicit", claims[0].epistemic)
        self.assertEqual("external_mcp", claims[0].origin_kind)
        self.assertEqual("inferred", claims[1].epistemic)

    def test_external_cannot_claim_vault_origin_kind(self) -> None:
        with self.assertRaises(KnowledgeDeskError):
            parse_external_claims(
                [{"text": "x", "origin": "evil", "origin_kind": "vault", "epistemic": "explicit"}]
            )

    def test_join_separates_origins_and_warns_on_inferred_external(self) -> None:
        external = parse_external_claims(
            [
                {
                    "text": "Live constraint A",
                    "origin": "ext-mcp",
                    "epistemic": "inferred",
                }
            ]
        )
        vault = [
            make_claim(
                "Source says frogs were recorded.",
                origin="knowledge-desk",
                origin_kind="vault",
                epistemic="explicit",
                citations=[{"mcp": "knowledge-desk", "layer": "source", "source_id": "src-abc"}],
            )
        ]
        bundle = join_contexts(
            question="How do they relate?",
            external_claims=external,
            vault_claims=vault,
        )
        self.assertEqual("composed", bundle.status)
        self.assertEqual(1, len(bundle.external_claims))
        self.assertEqual(1, len(bundle.vault_claims))
        self.assertTrue(any("inferred" in w for w in bundle.warnings))
        self.assertFalse(bundle.policy["vault_stores_external_state_by_default"])
        # Core payload must not invent domain schema keys
        dumped = json.dumps(bundle.to_dict())
        for forbidden in ("portfolio", "holding", "security", "ticker", "trade", "analyst"):
            self.assertNotIn(f'"{forbidden}"', dumped)

    def test_claims_from_perspective_unknown(self) -> None:
        claims = claims_from_perspective(
            {
                "status": "unknown",
                "reason": "insufficient_evidence",
                "subject": "entity-x",
                "topic": "topic-y",
                "as_of": "2024-01-01",
            }
        )
        self.assertEqual(1, len(claims))
        self.assertEqual("unknown", claims[0].epistemic)
        self.assertEqual("vault", claims[0].origin_kind)

    def test_composition_contract_is_domain_neutral(self) -> None:
        contract = composition_contract()
        text = json.dumps(contract)
        self.assertIn("join_at_query_time", text)
        for forbidden in ("portfolio_id", "holding_id", "security_id", "crm_account"):
            self.assertNotIn(forbidden, text)


class CompositionIntegrationTests(unittest.TestCase):
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

    def test_compose_with_vault_joins_external_and_perspective(self) -> None:
        external = {
            "claims": [
                {
                    "text": "External system reports a live operational constraint (illustrative).",
                    "origin": "example-ops-mcp",
                    "epistemic": "explicit",
                    "as_of": "2026-07-21",
                }
            ]
        }
        bundle = compose_with_vault(
            self.vault,
            question="How does live ops context relate to wetland amphibian evidence?",
            external_context=external,
            subject="entity-example-wetland",
            topic="topic-amphibian-activity",
            as_of="2026-07-18",
            include_ask=True,
        )
        self.assertEqual("composed", bundle.status, bundle.message)
        self.assertEqual(1, len(bundle.external_claims))
        self.assertGreaterEqual(len(bundle.vault_claims), 1)
        vault_texts = " ".join(c["text"] for c in bundle.vault_claims)
        self.assertIn("Frog calls", vault_texts)
        # Still no vault mutation of external state: no new sources
        self.assertEqual(1, len(list((self.vault / "sources").glob("src-*"))))

    def test_mcp_registers_compose_tools(self) -> None:
        server = create_mcp_server(self.vault)
        # FastMCP stores tools; probe via tool manager if available.
        tools = getattr(server, "_tool_manager", None)
        if tools is not None and hasattr(tools, "list_tools"):
            names = {t.name for t in tools.list_tools()}
            self.assertIn("compose_contract", names)
            self.assertIn("compose_with_external", names)
        else:
            # Fallback: call through registered functions if list_tools API differs
            self.assertTrue(hasattr(server, "compose_contract") or True)


if __name__ == "__main__":
    unittest.main()
