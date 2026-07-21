from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from knowledge_desk.util import (
    append_jsonl_synced,
    replace_json_synced,
    write_json_synced,
)
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

    def test_refine_failure_preserves_prior_workspace_page(self) -> None:
        init_workspace(
            self.vault,
            title="Crash-safe workspace",
            workspace_id="ws-crash-safe",
            statement="Prior complete stance.",
        )
        spine = workspaces_dir(self.vault) / "ws-crash-safe" / "workspace.md"
        before = spine.read_bytes()

        with patch("knowledge_desk.util.os.replace", side_effect=OSError("simulated crash")):
            result = refine_workspace(
                self.vault,
                "ws-crash-safe",
                summary="Attempt replacement",
                body="# Changed\n\nReplacement that must not truncate the prior page.\n",
            )

        self.assertEqual("failed", result.status)
        self.assertIn("simulated crash", result.message)
        self.assertEqual(before, spine.read_bytes())

    def test_persisted_benchtest_uses_synced_report_and_changelog_helpers(self) -> None:
        init_workspace(
            self.vault,
            title="Durable workspace",
            workspace_id="ws-durable",
            statement="A durable stance.",
        )

        with patch(
            "knowledge_desk.workspace.replace_json_synced",
            wraps=replace_json_synced,
        ) as report_write, patch(
            "knowledge_desk.workspace.append_jsonl_synced",
            wraps=append_jsonl_synced,
        ) as changelog_append:
            report = benchtest_workspace(self.vault, "ws-durable", persist=True)

        self.assertEqual("ok", report["status"], report.get("message"))
        report_write.assert_called_once()
        changelog_append.assert_called_once()
        self.assertTrue((self.vault / str(report["report_path"])).is_file())

    def test_synced_jsonl_append_fsyncs_file_and_directory(self) -> None:
        path = self.vault / "memory" / "append-sync-test.jsonl"
        with patch("knowledge_desk.util.os.fsync", wraps=os.fsync) as fsync:
            append_jsonl_synced(path, {"event": "test"})

        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertEqual({"event": "test"}, json.loads(path.read_text(encoding="utf-8")))

    def test_read_only_benchtest_does_not_take_writer_lock_or_mutate_workspace(self) -> None:
        init_workspace(
            self.vault,
            title="Read-only benchtest",
            workspace_id="ws-read-only-benchtest",
            statement="Do not persist this run.",
        )
        root = workspaces_dir(self.vault) / "ws-read-only-benchtest"
        changelog = root / "log" / "changelog.jsonl"
        before = changelog.read_bytes()

        with patch(
            "knowledge_desk.writer.vault_write_lock",
            side_effect=AssertionError("read-only benchtest must not acquire writer lock"),
        ):
            report = benchtest_workspace(
                self.vault,
                "ws-read-only-benchtest",
                persist=False,
            )

        self.assertEqual("ok", report["status"], report.get("message"))
        self.assertEqual(before, changelog.read_bytes())
        self.assertEqual([], list((root / "benchtests").glob("*.json")))

    def test_persisted_benchtest_serializes_after_concurrent_refine(self) -> None:
        init_workspace(
            self.vault,
            title="Serialized workspace",
            workspace_id="ws-serialized",
            statement="Initial stance.",
        )
        child_code = """
import sys
from pathlib import Path
from knowledge_desk.workspace import refine_workspace
from knowledge_desk.writer import vault_write_lock

vault = Path(sys.argv[1])
with vault_write_lock(vault):
    print("locked", flush=True)
    sys.stdin.readline()
    result = refine_workspace(vault, "ws-serialized", summary="child refine")
    if result.status != "refined":
        raise RuntimeError(result.message)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.vault)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        self.assertEqual("locked", child.stdout.readline().strip() if child.stdout else "")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                benchtest_workspace,
                self.vault,
                "ws-serialized",
                persist=True,
            )
            with self.assertRaises(concurrent.futures.TimeoutError):
                future.result(timeout=0.1)
            self.assertIsNotNone(child.stdin)
            child.stdin.write("refine\n")
            child.stdin.flush()
            child.stdin.close()
            child_result = child.wait(timeout=10)
            child_error = child.stderr.read() if child.stderr else ""
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
            self.assertEqual(0, child_result, child_error)
            report = future.result(timeout=10)

        self.assertEqual("ok", report["status"], report.get("message"))
        events = [
            item["event"]
            for item in get_workspace(self.vault, "ws-serialized")["changelog_tail"]
        ]
        self.assertEqual(["created", "refined", "benchtest"], events)

    def test_persisted_benchtests_never_overwrite_same_second_report(self) -> None:
        init_workspace(
            self.vault,
            title="Report identity",
            workspace_id="ws-report-identity",
        )
        with patch("knowledge_desk.workspace.utc_now", return_value="2026-07-21T12:00:00Z"):
            first = benchtest_workspace(self.vault, "ws-report-identity", persist=True)
            second = benchtest_workspace(self.vault, "ws-report-identity", persist=True)

        self.assertEqual("ok", first["status"])
        self.assertEqual("ok", second["status"])
        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertTrue((self.vault / str(first["report_path"])).is_file())
        self.assertTrue((self.vault / str(second["report_path"])).is_file())

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
