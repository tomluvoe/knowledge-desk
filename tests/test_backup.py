from __future__ import annotations

import concurrent.futures
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_desk.backup as backup_module
from knowledge_desk import __version__
from knowledge_desk.backup import MANIFEST_NAME, backup_vault, restore_vault
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.layout import init_vault
from knowledge_desk.observe import append_observation
from knowledge_desk.util import utc_now
from knowledge_desk.validation import validate_vault
from knowledge_desk.wiki import evolve_wiki


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class InitBackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name) / "desk"
        self.vault.mkdir()
        # Product schemas required for ingest/validate.
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialized_vault(self, name: str) -> Path:
        vault = Path(self.temporary.name) / name
        vault.mkdir()
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", vault / "system" / "examples")
        initialized = init_vault(vault)
        self.assertEqual("initialized", initialized.status, initialized.message)
        return vault

    def test_init_is_idempotent(self) -> None:
        first = init_vault(self.vault)
        self.assertEqual("initialized", first.status, first.message)
        self.assertTrue((self.vault / "sources").is_dir())
        self.assertTrue((self.vault / "wiki" / "entities").is_dir())
        second = init_vault(self.vault)
        self.assertEqual("initialized", second.status)
        self.assertTrue(any("sources/" in item for item in second.skipped))

    def test_backup_restore_round_trip(self) -> None:
        init_vault(self.vault)
        source = self.vault / "inbox" / "ecology-field-note.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes((FIXTURES / "ecology-field-note.txt").read_bytes())
        ingested = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", ingested.status, ingested.message)
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
                    "source_id": ingested.source_id,
                    "source_hash": ingested.content_hash,
                    "normalized_path": ingested.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 3, "end_line": 3},
                }
            ],
            "relations": [],
            "extensions": {},
        }
        self.assertEqual("created", append_observation(self.vault, observation).status)
        evolve_wiki(self.vault)

        archive = Path(self.temporary.name) / "desk-backup.tar.gz"
        backed = backup_vault(self.vault, archive)
        self.assertEqual("created", backed.status, backed.message)
        self.assertTrue(archive.is_file())
        with tarfile.open(archive, "r:gz") as handle:
            names = handle.getnames()
        self.assertIn(MANIFEST_NAME, names)
        self.assertTrue(any(name.startswith("sources/") for name in names))
        self.assertTrue(any(name.startswith("observations/") for name in names))
        self.assertTrue(any(name.startswith("wiki/") for name in names))

        target = Path(self.temporary.name) / "restored"
        target.mkdir()
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", target / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", target / "system" / "examples")
        init_vault(target)
        restored = restore_vault(target, archive)
        self.assertEqual("restored", restored.status, restored.message)
        report = validate_vault(target)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertGreaterEqual(report.checked["sources"], 1)
        self.assertGreaterEqual(report.checked["observations"], 1)
        self.assertGreaterEqual(report.checked["wiki_notes"], 1)

        # Without --force, a second restore into a data-filled desk is refused.
        refused = restore_vault(target, archive, force=False)
        self.assertEqual("failed", refused.status)
        self.assertIn("refused", refused.message)

        forced = restore_vault(target, archive, force=True)
        self.assertEqual("restored", forced.status, forced.message)
        self.assertIsNotNone(forced.recovery_archive)
        self.assertTrue(Path(forced.recovery_archive or "").is_file())

    def test_backup_refuses_output_inside_archived_root(self) -> None:
        init_vault(self.vault)
        output = self.vault / "inbox" / "recursive-backup.tar.gz"

        result = backup_vault(self.vault, output)

        self.assertEqual("failed", result.status)
        self.assertIn("outside archived root", result.message)
        self.assertFalse(output.exists())

    def test_concurrent_writer_finishes_before_backup_snapshot(self) -> None:
        init_vault(self.vault)
        first = self.vault / "inbox" / "first.txt"
        second = self.vault / "inbox" / "second.txt"
        first.write_text("before\n", encoding="utf-8")
        second.write_text("before\n", encoding="utf-8")
        archive = Path(self.temporary.name) / "concurrent.tar.gz"
        child_code = """
import sys
from pathlib import Path
from knowledge_desk.util import replace_text_synced
from knowledge_desk.writer import vault_write_lock

vault = Path(sys.argv[1])
with vault_write_lock(vault):
    print("locked", flush=True)
    sys.stdin.readline()
    replace_text_synced(vault / "inbox" / "first.txt", "after\\n")
    replace_text_synced(vault / "inbox" / "second.txt", "after\\n")
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
            future = executor.submit(backup_vault, self.vault, archive)
            with self.assertRaises(concurrent.futures.TimeoutError):
                future.result(timeout=0.1)
            self.assertIsNotNone(child.stdin)
            child.stdin.write("continue\n")
            child.stdin.flush()
            child.stdin.close()
            child_result = child.wait(timeout=10)
            child_error = child.stderr.read() if child.stderr else ""
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
            self.assertEqual(0, child_result, child_error)
            result = future.result(timeout=10)

        self.assertEqual("created", result.status, result.message)
        with tarfile.open(archive, "r:gz") as handle:
            self.assertEqual(b"after\n", handle.extractfile("inbox/first.txt").read())
            self.assertEqual(b"after\n", handle.extractfile("inbox/second.txt").read())

    def test_invalid_archive_leaves_destination_unchanged(self) -> None:
        target = self.initialized_vault("invalid-target")
        marker = target / "inbox" / "keep.txt"
        marker.write_text("keep me\n", encoding="utf-8")
        archive_path = Path(self.temporary.name) / "invalid.tar.gz"
        manifest = {
            "schema_version": "1.0.0",
            "kind": "knowledge_desk_backup",
            "created_at": utc_now(),
            "tool_version": __version__,
            "vault_label": "invalid",
            "paths": ["inbox/"],
            "include_index": False,
        }
        with tarfile.open(archive_path, "w:gz") as archive:
            root = tarfile.TarInfo("inbox")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            unsafe = tarfile.TarInfo("inbox/link")
            unsafe.type = tarfile.SYMTYPE
            unsafe.linkname = "../../outside"
            archive.addfile(unsafe)
            payload = (json.dumps(manifest) + "\n").encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

        result = restore_vault(target, archive_path, force=True)

        self.assertEqual("failed", result.status)
        self.assertIn("unsupported member type", result.message)
        self.assertEqual("keep me\n", marker.read_text(encoding="utf-8"))
        self.assertIsNone(result.recovery_archive)

    def test_staged_validation_failure_leaves_destination_unchanged(self) -> None:
        source = self.initialized_vault("invalid-source")
        (source / "observations" / "broken.json").write_text("{}\n", encoding="utf-8")
        archive = Path(self.temporary.name) / "invalid-content.tar.gz"
        backed = backup_vault(source, archive)
        self.assertEqual("created", backed.status, backed.message)
        target = self.initialized_vault("validation-target")
        marker = target / "inbox" / "keep.txt"
        marker.write_text("keep me\n", encoding="utf-8")

        result = restore_vault(target, archive, force=True)

        self.assertEqual("failed", result.status)
        self.assertIn("staged restore failed vault validation", result.message)
        self.assertEqual("keep me\n", marker.read_text(encoding="utf-8"))
        self.assertIsNone(result.recovery_archive)

    def test_force_restore_replaces_roots_and_creates_recovery_archive(self) -> None:
        source = self.initialized_vault("force-source")
        (source / "inbox" / "archive-only.txt").write_text("archive\n", encoding="utf-8")
        archive = Path(self.temporary.name) / "force.tar.gz"
        backed = backup_vault(source, archive)
        self.assertEqual("created", backed.status, backed.message)
        target = self.initialized_vault("force-target")
        (target / "inbox" / "local-only.txt").write_text("local\n", encoding="utf-8")

        result = restore_vault(target, archive, force=True)

        self.assertEqual("restored", result.status, result.message)
        self.assertTrue((target / "inbox" / "archive-only.txt").is_file())
        self.assertFalse((target / "inbox" / "local-only.txt").exists())
        self.assertIsNotNone(result.recovery_archive)
        recovery_archive = Path(result.recovery_archive or "")
        self.assertTrue(recovery_archive.is_file())

        recovered = self.initialized_vault("recovered-target")
        recovery_result = restore_vault(recovered, recovery_archive)
        self.assertEqual("restored", recovery_result.status, recovery_result.message)
        self.assertEqual(
            "local\n",
            (recovered / "inbox" / "local-only.txt").read_text(encoding="utf-8"),
        )

    def test_publication_failure_rolls_back_all_prior_roots(self) -> None:
        source = self.initialized_vault("rollback-source")
        (source / "inbox" / "archive-only.txt").write_text("archive\n", encoding="utf-8")
        archive = Path(self.temporary.name) / "rollback.tar.gz"
        backed = backup_vault(source, archive)
        self.assertEqual("created", backed.status, backed.message)
        target = self.initialized_vault("rollback-target")
        inbox_readme = target / "inbox" / "README.md"
        sources_readme = target / "sources" / "README.md"
        inbox_readme.write_text("custom inbox\n", encoding="utf-8")
        sources_readme.write_text("custom sources\n", encoding="utf-8")
        actual_move = backup_module._move_path
        candidate_moves = 0

        def fail_second_candidate_move(source_path: Path, destination_path: Path) -> None:
            nonlocal candidate_moves
            if source_path.parent.name == "candidate" or "candidate" in source_path.parts:
                candidate_moves += 1
                if candidate_moves == 2:
                    raise OSError("injected publication failure")
            actual_move(source_path, destination_path)

        with patch("knowledge_desk.backup._move_path", side_effect=fail_second_candidate_move):
            result = restore_vault(target, archive)

        self.assertEqual("failed", result.status)
        self.assertIn("prior roots were restored", result.message)
        self.assertIsNone(result.recovery_path)
        self.assertEqual("custom inbox\n", inbox_readme.read_text(encoding="utf-8"))
        self.assertEqual("custom sources\n", sources_readme.read_text(encoding="utf-8"))
        self.assertFalse((target / "inbox" / "archive-only.txt").exists())


if __name__ == "__main__":
    unittest.main()
