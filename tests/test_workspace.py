from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.layout import init_vault
from knowledge_desk.observe import append_observation
from knowledge_desk.wiki import evolve_wiki
from knowledge_desk.workspace import (
    add_page,
    benchtest_workspace,
    get_workspace,
    init_workspace,
    is_workspace_path,
    list_workspaces,
    refine_workspace,
    workspaces_dir,
)
from knowledge_desk.proposals import apply_proposal
from knowledge_desk.util import write_json_synced
from knowledge_desk.validation import validate_vault


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class WorkspaceTests(unittest.TestCase):
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

    def test_init_list_add_refine_and_benchtest(self) -> None:
        created = init_workspace(
            self.vault,
            title="Wetland amphibian thesis",
            kind="thesis",
            workspace_id="ws-thesis-wetland",
            subject_refs=["entity-example-wetland"],
            topic_refs=["topic-amphibian-activity"],
            statement="Frog activity is a useful signal of wetland health.",
            observation_ids=["obs-20260718-frog-calls"],
        )
        self.assertEqual("created", created.status, created.message)
        self.assertTrue((workspaces_dir(self.vault) / "ws-thesis-wetland" / "workspace.md").is_file())

        listed = list_workspaces(self.vault)
        self.assertEqual(1, listed["count"])

        page = add_page(
            self.vault,
            "ws-thesis-wetland",
            title="Sampling reliability",
            page_kind="pillar",
            body="# Sampling reliability\n\nThree sampling points are enough for a local signal.\n",
            observation_ids=["obs-20260718-frog-calls"],
        )
        self.assertEqual("created", page.status, page.message)

        refined = refine_workspace(
            self.vault,
            "ws-thesis-wetland",
            summary="Clarify spine after review",
            body="# Wetland amphibian thesis\n\nFrog activity remains a useful local signal of wetland health.\n",
            observation_ids=["obs-20260718-frog-calls"],
        )
        self.assertEqual("refined", refined.status, refined.message)

        got = get_workspace(self.vault, "ws-thesis-wetland")
        self.assertTrue(got["success"])
        self.assertEqual(1, len(got["pages"]))
        self.assertTrue(got["changelog_tail"])

        report = benchtest_workspace(self.vault, "ws-thesis-wetland")
        self.assertEqual("ok", report["status"], report.get("message"))
        self.assertGreaterEqual(report["claim_count"], 2)
        verdicts = {c["verdict"] for c in report["claims"]}
        self.assertTrue(verdicts & {"supported", "pending", "untested", "challenged", "conflicted"})
        self.assertTrue(report.get("report_path"))

    def test_wiki_evolve_does_not_touch_workspaces(self) -> None:
        init_workspace(
            self.vault,
            title="Protected thesis",
            kind="thesis",
            workspace_id="ws-thesis-protected",
            statement="Do not overwrite me.",
        )
        spine = workspaces_dir(self.vault) / "ws-thesis-protected" / "workspace.md"
        before = spine.read_text(encoding="utf-8")
        evolve_wiki(self.vault)
        after = spine.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertTrue(is_workspace_path(self.vault, spine))

    def test_workspace_validates_with_vault_validate(self) -> None:
        init_workspace(
            self.vault,
            title="Validate me",
            kind="thesis",
            workspace_id="ws-thesis-validate",
            statement="A working stance for validation.",
            observation_ids=["obs-20260718-frog-calls"],
        )
        add_page(
            self.vault,
            "ws-thesis-validate",
            title="Pillar",
            page_kind="pillar",
            observation_ids=["obs-20260718-frog-calls"],
        )
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))

    def test_validate_rejects_misnamed_workspace_spine_and_page(self) -> None:
        init_workspace(
            self.vault,
            title="Canonical workspace",
            workspace_id="ws-canonical",
        )
        page = add_page(
            self.vault,
            "ws-canonical",
            title="Canonical page",
            page_kind="note",
            page_id="wsp-canonical-page",
        )
        self.assertEqual("created", page.status, page.message)
        root = workspaces_dir(self.vault) / "ws-canonical"
        (root / "workspace.md").replace(root / "misnamed-spine.md")
        (root / "pages" / "canonical-page.md").replace(root / "pages" / "wrong-page.md")

        report = validate_vault(self.vault)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "must be stored at memory/workspaces/ws-canonical/workspace.md" in error
                for error in report.errors
            )
        )
        self.assertTrue(
            any(
                "must be stored at memory/workspaces/ws-canonical/pages/canonical-page.md" in error
                for error in report.errors
            )
        )

    def test_workspace_refine_proposal_missing_fails(self) -> None:
        queue = self.vault / "system" / "update-queue"
        queue.mkdir(parents=True, exist_ok=True)
        path = queue / "workspace-refine-missing.json"
        write_json_synced(
            path,
            {
                "kind": "workspace_refine_proposal",
                "status": "pending",
                "workspace_id": "ws-does-not-exist",
                "summary": "nope",
            },
        )
        result = apply_proposal(self.vault, path)
        self.assertEqual("failed", result.status, result.message)
        self.assertTrue(path.is_file(), "failed apply must not archive the proposal")

    def test_workspace_refine_proposal_apply(self) -> None:
        init_workspace(
            self.vault,
            title="Proposal thesis",
            kind="thesis",
            workspace_id="ws-thesis-prop",
            statement="Initial.",
        )
        queue = self.vault / "system" / "update-queue"
        queue.mkdir(parents=True, exist_ok=True)
        proposal = {
            "kind": "workspace_refine_proposal",
            "status": "pending",
            "workspace_id": "ws-thesis-prop",
            "summary": "AI suggested refine",
            "body": "# Proposal thesis\n\nRefined stance with review.\n",
            "reason": "new evidence",
        }
        path = queue / "workspace-refine-test.json"
        write_json_synced(path, proposal)
        result = apply_proposal(self.vault, path)
        self.assertEqual("applied", result.status, result.message)
        text = (workspaces_dir(self.vault) / "ws-thesis-prop" / "workspace.md").read_text(encoding="utf-8")
        self.assertIn("Refined stance", text)


if __name__ == "__main__":
    unittest.main()
