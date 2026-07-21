from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
