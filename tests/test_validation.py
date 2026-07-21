from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from knowledge_desk.ingest import IngestMetadata, ingest_file
from knowledge_desk.util import render_frontmatter, sha256_text
from knowledge_desk.validation import validate_locator, validate_vault


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _base_observation(observation_id: str, evidence: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "observation_id": observation_id,
        "subjects": [{"ref_id": "entity-subject", "kind": "entity", "label": "Subject"}],
        "topics": [{"ref_id": "topic-topic", "kind": "topic", "label": "Topic"}],
        "assertion": f"Assertion for {observation_id}",
        "epistemic_class": "source_statement",
        "statement_basis": "explicit_statement",
        "orientation": "neutral",
        "confidence": 0.5,
        "reasoning": "Test observation.",
        "mechanisms": [],
        "conditions": [],
        "implications": [],
        "catalysts": [],
        "risks": [],
        "publication_date": "2024-01-01",
        "expressed_at": "2024-01-01T00:00:00Z",
        "valid_at": "2024-01-01T00:00:00Z",
        "recorded_at": "2024-01-02T00:00:00Z",
        "horizon": None,
        "freshness": {"as_of": "2024-01-02T00:00:00Z", "status": "historical"},
        "evidence": evidence,
        "relations": [],
        "extensions": {},
    }
    payload.update(overrides)
    return payload


def _base_memory(memory_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "memory_id": memory_id,
        "kind": "user_conclusion",
        "title": memory_id,
        "statement": f"Statement for {memory_id}",
        "status": "active",
        "created_at": "2024-01-02T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "observation_ids": [],
        "evidence": [],
        "supersedes": None,
        "extensions": {},
    }
    payload.update(overrides)
    return payload


class ValidationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains"):
            (self.vault / name).mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest_text(self, name: str, body: str):
        path = self.vault / name
        path.write_text(body, encoding="utf-8")
        result = ingest_file(self.vault, path, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        return result

    def _write_observation(self, observation: dict[str, Any]) -> Path:
        path = self.vault / "observations" / f"{observation['observation_id']}.json"
        path.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _write_memory(self, record: dict[str, Any], relative: str | None = None) -> Path:
        name = relative or f"conclusions/{record['memory_id']}.md"
        path = self.vault / "memory" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = render_frontmatter(record) + f"\n# {record['title']}\n\nBody.\n"
        path.write_text(body, encoding="utf-8")
        return path

    def _evidence_for(self, result, start_line: int = 1, end_line: int = 1) -> dict[str, Any]:
        return {
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": start_line, "end_line": end_line},
        }

    def test_incomplete_source_errors_use_relative_labels(self) -> None:
        incomplete = self.vault / "sources" / ("src-" + "b" * 24)
        incomplete.mkdir()
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("manifest.json: missing" in error for error in report.errors))
        self.assertFalse(any("/private/" in error or "/var/" in error for error in report.errors))

    def test_missing_evidence_target_source(self) -> None:
        result = self._ingest_text("note.txt", "hello evidence\n")
        evidence = self._evidence_for(result)
        evidence["source_id"] = "src-" + "c" * 24
        evidence["normalized_path"] = f"sources/src-{'c' * 24}/normalized.md"
        self._write_observation(_base_observation("obs-20240101-missing-source", [evidence]))
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("does not exist" in error for error in report.errors))

    def test_invalid_horizon_end_before_start(self) -> None:
        result = self._ingest_text("note.txt", "horizon fixture\n")
        observation = _base_observation(
            "obs-20240101-horizon",
            [self._evidence_for(result)],
            horizon={"start": "2024-06-01", "end": "2024-01-01", "description": "inverted"},
        )
        self._write_observation(observation)
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("horizon end precedes start" in error for error in report.errors))

    def test_duplicate_observation_ids(self) -> None:
        result = self._ingest_text("note.txt", "duplicate ids\n")
        evidence = [self._evidence_for(result)]
        first = _base_observation("obs-20240101-dup", evidence)
        second = _base_observation("obs-20240101-dup", evidence, assertion="Second copy")
        (self.vault / "observations" / "a.json").write_text(json.dumps(first) + "\n", encoding="utf-8")
        (self.vault / "observations" / "b.json").write_text(json.dumps(second) + "\n", encoding="utf-8")
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("duplicate observation ID" in error for error in report.errors))

    def test_misnamed_observation_reports_canonical_flat_path(self) -> None:
        result = self._ingest_text("note.txt", "misnamed observation\n")
        observation = _base_observation("obs-20240101-canonical", [self._evidence_for(result)])
        nested = self.vault / "observations" / "restored" / "wrong-name.json"
        nested.parent.mkdir()
        nested.write_text(json.dumps(observation) + "\n", encoding="utf-8")

        report = validate_vault(self.vault)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "must be stored at observations/obs-20240101-canonical.json" in error
                for error in report.errors
            )
        )

    def test_vault_rejects_reference_kind_and_prefix_mismatch(self) -> None:
        result = self._ingest_text("note.txt", "bad reference\n")
        observation = _base_observation(
            "obs-20240101-bad-reference",
            [self._evidence_for(result)],
            subjects=[{"kind": "topic", "ref_id": "entity-subject", "label": "Subject"}],
        )
        self._write_observation(observation)

        report = validate_vault(self.vault)

        self.assertFalse(report.valid)
        self.assertTrue(any("kind must be 'entity'" in error for error in report.errors))
        self.assertTrue(any("requires ref_id prefix topic-" in error for error in report.errors))

    def test_dangling_revision_of(self) -> None:
        result = self._ingest_text("note.txt", "revision target missing\n")
        manifest_path = self.vault / "sources" / str(result.source_id) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["revision_of"] = "src-" + "d" * 24
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("revision_of target does not exist" in error for error in report.errors))

    def test_dangling_relation_target(self) -> None:
        result = self._ingest_text("note.txt", "relation target missing\n")
        observation = _base_observation(
            "obs-20240101-rel",
            [self._evidence_for(result)],
            relations=[{"type": "confirms", "observation_id": "obs-20240101-absent"}],
        )
        self._write_observation(observation)
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("relation target does not exist" in error for error in report.errors))

    def test_self_relation_rejected(self) -> None:
        result = self._ingest_text("note.txt", "self relation\n")
        observation = _base_observation(
            "obs-20240101-self",
            [self._evidence_for(result)],
            relations=[{"type": "refines", "observation_id": "obs-20240101-self"}],
        )
        self._write_observation(observation)
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("relation cannot target the same observation" in error for error in report.errors))

    def test_observation_relation_cycle_detected(self) -> None:
        result = self._ingest_text("note.txt", "cycle fixture\n")
        evidence = [self._evidence_for(result)]
        self._write_observation(
            _base_observation(
                "obs-20240101-a",
                evidence,
                relations=[{"type": "supersedes", "observation_id": "obs-20240101-b"}],
            )
        )
        self._write_observation(
            _base_observation(
                "obs-20240101-b",
                evidence,
                relations=[{"type": "supersedes", "observation_id": "obs-20240101-a"}],
            )
        )
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("observation relation cycle" in error for error in report.errors))

    def test_memory_supersession_cycle_detected(self) -> None:
        self._write_memory(_base_memory("mem-20240101-a", supersedes="mem-20240101-b"))
        self._write_memory(_base_memory("mem-20240101-b", supersedes="mem-20240101-a"))
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("memory supersession cycle" in error for error in report.errors))

    def test_memory_self_supersedes_rejected(self) -> None:
        self._write_memory(_base_memory("mem-20240101-self", supersedes="mem-20240101-self"))
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("supersedes cannot target the same memory record" in error for error in report.errors))

    def test_dangling_memory_supersedes(self) -> None:
        self._write_memory(_base_memory("mem-20240101-only", supersedes="mem-20240101-missing"))
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("supersedes target does not exist" in error for error in report.errors))

    def test_pdf_page_locator_on_text_source_rejected(self) -> None:
        result = self._ingest_text("note.txt", "not a pdf\n")
        locator = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "pdf_page",
            "selector": {"page": 1},
        }
        errors = validate_locator(self.vault, locator)
        self.assertTrue(any("incompatible with media_type" in error for error in errors))

    def test_markdown_heading_on_text_source_rejected(self) -> None:
        result = self._ingest_text("note.txt", "still not markdown media type\n")
        locator = {
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "markdown_heading",
            "selector": {"heading": "Title", "occurrence": 1},
        }
        errors = validate_locator(self.vault, locator)
        self.assertTrue(any("incompatible with media_type" in error for error in errors))

    def test_block_locator_on_markdown_source_rejected(self) -> None:
        path = self.vault / "note.md"
        path.write_text("# Title\n\nBody paragraph.\n", encoding="utf-8")
        result = ingest_file(self.vault, path, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        locator = {
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "block",
            "selector": {"block_id": "block-1"},
        }
        errors = validate_locator(self.vault, locator)
        self.assertTrue(any("incompatible with media_type" in error for error in errors))

    def test_quote_mismatch_detected(self) -> None:
        result = self._ingest_text("note.txt", "Exact sentence.\n")
        locator = {
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": 1, "end_line": 1},
            "quote_sha256": sha256_text("wrong quote"),
        }
        errors = validate_locator(self.vault, locator)
        self.assertTrue(any("quote_sha256" in error for error in errors))

    def test_valid_chain_still_passes(self) -> None:
        result = self._ingest_text("note.txt", "Healthy chain text.\n")
        observation = _base_observation("obs-20240101-ok", [self._evidence_for(result)])
        self._write_observation(observation)
        self._write_memory(
            _base_memory(
                "mem-20240101-ok",
                observation_ids=["obs-20240101-ok"],
                evidence=[self._evidence_for(result)],
            )
        )
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))


if __name__ == "__main__":
    unittest.main()
