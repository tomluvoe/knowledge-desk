from __future__ import annotations

import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.backup import MANIFEST_NAME, backup_vault, restore_vault
from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.layout import init_vault
from knowledge_desk.observe import append_observation
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


if __name__ == "__main__":
    unittest.main()
