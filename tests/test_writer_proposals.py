from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_desk.explore import explore_ask, explore_gaps
from knowledge_desk.ingest import IngestMetadata, ingest_file, ingest_path
from knowledge_desk.proposals import apply_proposal, list_proposals, reject_proposal
from knowledge_desk.writer import vault_write_lock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class WriterAndProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()
        (self.vault / "system" / "update-queue").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_directory_ingest_skips_dotfiles(self) -> None:
        batch = self.vault / "batch"
        batch.mkdir()
        (batch / ".hidden.txt").write_text("secret", encoding="utf-8")
        (batch / "visible.txt").write_text("visible content here", encoding="utf-8")
        results = ingest_path(self.vault, batch, IngestMetadata())
        self.assertEqual(1, len(results))
        self.assertEqual("created", results[0].status, results[0].message)
        self.assertEqual(1, len(list((self.vault / "sources").glob("src-*"))))

    def test_empty_text_warns(self) -> None:
        empty = self.vault / "empty.txt"
        empty.write_text("", encoding="utf-8")
        result = ingest_file(self.vault, empty, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        self.assertTrue(any("empty" in warning.casefold() for warning in result.warnings))

    def test_write_lock_context_creates_lock_file(self) -> None:
        with vault_write_lock(self.vault) as lock_path:
            self.assertTrue(lock_path.is_file())
            self.assertIn("writer.lock", lock_path.as_posix())

    def test_proposal_reject_and_apply_open_question(self) -> None:
        source = self.vault / "ecology-field-note.txt"
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        self.assertEqual("created", ingest_file(self.vault, source, IngestMetadata()).status)

        gaps = explore_gaps(self.vault, propose=True)
        self.assertIsNotNone(gaps.proposal_path)
        listed = list_proposals(self.vault)
        self.assertGreaterEqual(listed["count"], 1)

        # Reject the gaps proposal (informational).
        rejected = reject_proposal(self.vault, Path(gaps.proposal_path), reason="not now")
        self.assertEqual("rejected", rejected.status, rejected.message)
        self.assertTrue((self.vault / "system" / "update-queue" / "rejected").exists())

        ask = explore_ask(self.vault, "capital of mars colony", propose=True)
        self.assertEqual("insufficient_evidence", ask.status)
        self.assertIsNotNone(ask.proposal_path)
        applied = apply_proposal(self.vault, Path(ask.proposal_path))
        self.assertEqual("applied", applied.status, applied.message)
        memory_files = list((self.vault / "memory").glob("**/*.md"))
        self.assertTrue(any(path.name != "README.md" for path in memory_files))

    def test_proposal_operations_reject_paths_outside_pending_queue(self) -> None:
        external = self.vault.parent / "external-proposal.json"
        external.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "proposed"}),
            encoding="utf-8",
        )
        rejected = reject_proposal(self.vault, external)
        self.assertEqual("failed", rejected.status)
        self.assertIn("direct pending JSON", rejected.message)
        self.assertTrue(external.is_file())

        traversal = Path("system/update-queue/../../../external-proposal.json")
        applied = apply_proposal(self.vault, traversal)
        self.assertEqual("failed", applied.status)
        self.assertIn("direct pending JSON", applied.message)
        self.assertTrue(external.is_file())

    def test_proposal_operations_reject_archives_symlinks_and_nonpending_state(self) -> None:
        queue = self.vault / "system" / "update-queue"
        archived = queue / "applied" / "old.json"
        archived.parent.mkdir(parents=True)
        archived.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "applied"}),
            encoding="utf-8",
        )
        self.assertEqual("failed", reject_proposal(self.vault, archived).status)
        self.assertTrue(archived.is_file())

        nonpending = queue / "nonpending.json"
        nonpending.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "applied"}),
            encoding="utf-8",
        )
        result = reject_proposal(self.vault, nonpending)
        self.assertEqual("failed", result.status)
        self.assertIn("status must be pending or proposed", result.message)
        self.assertTrue(nonpending.is_file())

        target = queue / "target.json"
        target.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "proposed"}),
            encoding="utf-8",
        )
        link = queue / "link.json"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are unavailable on this platform")
        symlink_result = reject_proposal(self.vault, link)
        self.assertEqual("failed", symlink_result.status)
        self.assertIn("must not be a symlink", symlink_result.message)
        self.assertTrue(target.is_file())

    def test_proposal_archive_collision_preserves_both_records(self) -> None:
        queue = self.vault / "system" / "update-queue"
        archive = queue / "rejected"
        archive.mkdir(parents=True)
        existing = archive / "proposal.json"
        existing.write_text("existing audit record\n", encoding="utf-8")
        pending = queue / "proposal.json"
        pending.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "proposed"}),
            encoding="utf-8",
        )

        result = reject_proposal(self.vault, pending, reason="duplicate filename")

        self.assertEqual("rejected", result.status, result.message)
        self.assertEqual("existing audit record\n", existing.read_text(encoding="utf-8"))
        collision_archive = archive / "proposal-2.json"
        self.assertTrue(collision_archive.is_file())
        self.assertFalse(pending.exists())
        archived_payload = json.loads(collision_archive.read_text(encoding="utf-8"))
        self.assertEqual("rejected", archived_payload["status"])

    def test_proposal_remains_pending_when_archive_publication_fails(self) -> None:
        queue = self.vault / "system" / "update-queue"
        pending = queue / "proposal.json"
        pending.write_text(
            json.dumps({"kind": "explore_gaps_proposal", "status": "proposed"}),
            encoding="utf-8",
        )

        with patch("knowledge_desk.proposals.os.replace", side_effect=OSError("simulated failure")):
            result = reject_proposal(self.vault, pending)

        self.assertEqual("failed", result.status)
        self.assertIn("simulated failure", result.message)
        self.assertTrue(pending.is_file())
        self.assertFalse((queue / "rejected" / "proposal.json").exists())


if __name__ == "__main__":
    unittest.main()
