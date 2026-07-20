from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evidence_vault.ingest import IngestMetadata, ingest_file, ingest_path
from evidence_vault.util import parse_frontmatter, sha256_text
from evidence_vault.validation import load_schema, schema_errors, validate_locator, validate_vault
from pdf_fixture import write_pdf


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        (self.vault / "sources").mkdir()
        (self.vault / "observations").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_markdown_ingestion_preserves_structure_links_and_fences(self) -> None:
        source = self.vault / "research-note.md"
        original = (FIXTURES / "research-note.md").read_bytes()
        source.write_bytes(original)

        result = ingest_file(self.vault, source, IngestMetadata(language="en"))

        self.assertEqual("created", result.status, result.message)
        source_dir = self.vault / "sources" / str(result.source_id)
        manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(original, (self.vault / manifest["original_path"]).read_bytes())
        note = (source_dir / "normalized.md").read_text(encoding="utf-8")
        _, body = parse_frontmatter(note)
        self.assertIn("[protocol link](https://example.invalid/protocol)", body)
        self.assertIn("```python\n# This is code", body)
        self.assertIn("## Results", body)

        locator = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "markdown_heading",
            "selector": {"heading": "Results", "occurrence": 1},
        }
        self.assertEqual([], validate_locator(self.vault, locator))

    def test_duplicate_ingestion_is_noop_and_does_not_append_log(self) -> None:
        source = self.vault / "interview.txt"
        source.write_bytes((FIXTURES / "interview.txt").read_bytes())
        first = ingest_file(self.vault, source, IngestMetadata())
        differently_named = self.vault / "same-content-different-name.txt"
        differently_named.write_bytes(source.read_bytes())
        second = ingest_file(self.vault, differently_named, IngestMetadata())

        self.assertEqual("created", first.status)
        self.assertEqual("noop", second.status)
        entries = (self.vault / "system" / "logs" / "ingest.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(entries))
        self.assertEqual(1, len(list((self.vault / "sources").glob("src-*"))))

    def test_changed_same_named_input_creates_explicit_revision(self) -> None:
        source = self.vault / "note.txt"
        source.write_text("first version\n", encoding="utf-8")
        first = ingest_file(self.vault, source, IngestMetadata())
        source.write_text("second version\n", encoding="utf-8")
        second = ingest_file(self.vault, source, IngestMetadata())

        self.assertEqual("created", first.status)
        self.assertEqual("revision", second.status)
        manifest = json.loads(
            (self.vault / "sources" / str(second.source_id) / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first.source_id, manifest["revision_of"])
        self.assertTrue((self.vault / "sources" / str(first.source_id)).is_dir())

    def test_text_line_and_block_locators_resolve(self) -> None:
        source = self.vault / "interview.txt"
        source.write_bytes((FIXTURES / "interview.txt").read_bytes())
        result = ingest_file(self.vault, source, IngestMetadata())
        base = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
        }
        line_locator = base | {
            "locator_kind": "line_range",
            "selector": {"start_line": 1, "end_line": 2},
        }
        block_locator = base | {"locator_kind": "block", "selector": {"block_id": "block-2"}}
        self.assertEqual([], validate_locator(self.vault, line_locator))
        self.assertEqual([], validate_locator(self.vault, block_locator))

    def test_pdf_ingestion_preserves_pages_and_resolves_page(self) -> None:
        source = self.vault / "paper.pdf"
        original = write_pdf(
            source,
            [
                "Page one reports a controlled observation with enough text for reliable deterministic extraction.",
                "Page two records a distinct result and keeps its own stable page boundary for exact citation.",
            ],
        )
        result = ingest_file(self.vault, source, IngestMetadata(title="Two-page paper"))

        self.assertEqual("created", result.status, result.message)
        self.assertEqual("complete", result.extraction_status)
        source_dir = self.vault / "sources" / str(result.source_id)
        self.assertEqual(original, next((source_dir / "original").iterdir()).read_bytes())
        normalized = (source_dir / "normalized.md").read_text(encoding="utf-8")
        self.assertEqual(1, normalized.count("<!-- ev-page:1 -->"))
        self.assertEqual(1, normalized.count("<!-- ev-page:2 -->"))
        locator = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "pdf_page",
            "selector": {"page": 2},
        }
        self.assertEqual([], validate_locator(self.vault, locator))

    def test_low_text_pdf_requires_ocr_without_hallucinated_content(self) -> None:
        source = self.vault / "scan.pdf"
        write_pdf(source, [""])
        result = ingest_file(self.vault, source, IngestMetadata())

        self.assertEqual("created", result.status, result.message)
        self.assertEqual("needs_ocr", result.extraction_status)
        self.assertTrue(any("OCR" in warning for warning in result.warnings))

    def test_corrupt_and_unsupported_inputs_fail_cleanly(self) -> None:
        corrupt = self.vault / "corrupt.pdf"
        corrupt.write_bytes(b"not a PDF")
        unsupported = self.vault / "archive.zip"
        unsupported.write_bytes(b"not a zip either")

        corrupt_result = ingest_file(self.vault, corrupt, IngestMetadata())
        unsupported_result = ingest_file(self.vault, unsupported, IngestMetadata())

        self.assertEqual("failed", corrupt_result.status)
        self.assertEqual("failed", unsupported_result.status)
        self.assertEqual([], list((self.vault / "sources").glob("src-*")))

    def test_atomic_failure_does_not_publish_or_log(self) -> None:
        source = self.vault / "interview.txt"
        source.write_bytes((FIXTURES / "interview.txt").read_bytes())
        result = ingest_file(self.vault, source, IngestMetadata(publication_date="not-a-date"))

        self.assertEqual("failed", result.status)
        self.assertEqual([], list((self.vault / "sources").glob("src-*")))
        self.assertFalse((self.vault / "system" / "logs" / "ingest.jsonl").exists())
        staging = self.vault / "system" / ".staging"
        self.assertFalse(staging.exists() and any(staging.iterdir()))

    def test_directory_ingest_reports_unsupported_entry(self) -> None:
        directory = self.vault / "batch"
        directory.mkdir()
        (directory / "a.txt").write_text("alpha", encoding="utf-8")
        (directory / "b.bin").write_bytes(b"binary")
        results = ingest_path(self.vault, directory, IngestMetadata())
        self.assertEqual(["created", "failed"], [result.status for result in results])

    def test_non_utf8_text_is_rejected_without_replacement(self) -> None:
        source = self.vault / "legacy.txt"
        source.write_bytes(b"substantial text \xff cannot be decoded")
        result = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("failed", result.status)
        self.assertIn("not valid UTF-8", result.message)

    def test_quote_digest_pins_resolved_text(self) -> None:
        source = self.vault / "short.txt"
        source.write_text("Exact sentence.\n", encoding="utf-8")
        result = ingest_file(self.vault, source, IngestMetadata())
        locator = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": 1, "end_line": 1},
            "quote_sha256": sha256_text("Exact sentence."),
        }
        self.assertEqual([], validate_locator(self.vault, locator))

    def test_validator_detects_immutable_original_tampering(self) -> None:
        source = self.vault / "short.txt"
        source.write_text("Exact sentence.\n", encoding="utf-8")
        result = ingest_file(self.vault, source, IngestMetadata())
        original = next((self.vault / "sources" / str(result.source_id) / "original").iterdir())
        original.write_text("tampered", encoding="utf-8")
        report = validate_vault(self.vault)
        self.assertFalse(report.valid)
        self.assertTrue(any("content hash" in error for error in report.errors))


class RepositoryContractTests(unittest.TestCase):
    def test_repository_schemas_and_unrelated_domain_examples_validate(self) -> None:
        report = validate_vault(REPOSITORY_ROOT)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertGreaterEqual(report.checked["schemas"], 9)
        self.assertGreaterEqual(report.checked["examples"], 10)

    def test_core_schema_rejects_unnamespaced_domain_fields(self) -> None:
        example = json.loads(
            (REPOSITORY_ROOT / "system" / "examples" / "observation-ecology.example.json").read_text(
                encoding="utf-8"
            )
        )
        schema = load_schema(REPOSITORY_ROOT, "observation.schema.json")
        example["species_code"] = "invented-core-field"
        self.assertTrue(any("Additional properties" in error for error in schema_errors(example, schema)))
        example.pop("species_code")
        example["extensions"] = {"ecology": {"species_code": "invalid-namespace"}}
        self.assertTrue(schema_errors(example, schema))

    def test_locator_kind_must_match_selector_shape(self) -> None:
        schema = load_schema(REPOSITORY_ROOT, "evidence-locator.schema.json")
        mismatched = {
            "schema_version": "1.0.0",
            "source_id": "src-" + ("a" * 24),
            "source_hash": "sha256:" + ("a" * 64),
            "normalized_path": f"sources/src-{'a' * 24}/normalized.md",
            "locator_kind": "pdf_page",
            "selector": {"block_id": "block-1"},
        }
        self.assertTrue(schema_errors(mismatched, schema))


class ObservationValidationTests(unittest.TestCase):
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

    def _ingest_text(self, name: str, body: str) -> object:
        source = self.vault / name
        source.write_text(body, encoding="utf-8")
        result = ingest_file(self.vault, source, IngestMetadata())
        self.assertEqual("created", result.status, result.message)
        return result

    def test_observation_with_embedded_evidence_passes_vault_validation(self) -> None:
        result = self._ingest_text(
            "field-note.txt",
            "Species X declined by 12 percent in the northern transect.\n",
        )
        quote = "Species X declined by 12 percent in the northern transect."
        observation = {
            "schema_version": "1.0.0",
            "observation_id": "obs-20240101-species-x",
            "subjects": [{"ref_id": "entity-species-x", "kind": "entity", "label": "Species X"}],
            "topics": [{"ref_id": "topic-population", "kind": "topic", "label": "Population"}],
            "assertion": "Species X declined by 12 percent.",
            "epistemic_class": "source_statement",
            "statement_basis": "explicit_statement",
            "orientation": "critical",
            "confidence": 0.9,
            "reasoning": "Direct statement in source.",
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
            "evidence": [
                {
                    "source_id": result.source_id,
                    "source_hash": result.content_hash,
                    "normalized_path": result.normalized_path,
                    "locator_kind": "line_range",
                    "selector": {"start_line": 1, "end_line": 1},
                    "quote_sha256": sha256_text(quote),
                }
            ],
            "relations": [],
            "extensions": {},
        }
        path = self.vault / "observations" / "obs-20240101-species-x.json"
        path.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")

        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertEqual(1, report.checked["observations"])

    def test_embedded_evidence_rejects_schema_version(self) -> None:
        example = json.loads(
            (REPOSITORY_ROOT / "system" / "examples" / "observation-ecology.example.json").read_text(
                encoding="utf-8"
            )
        )
        example["evidence"][0]["schema_version"] = "1.0.0"
        schema = load_schema(REPOSITORY_ROOT, "observation.schema.json")
        self.assertTrue(
            any("schema_version" in error for error in schema_errors(example, schema)),
            "embedded evidence must not carry schema_version",
        )

    def test_kind_selector_mismatch_returns_error_not_exception(self) -> None:
        result = self._ingest_text("note.txt", "plain text line\n")
        locator = {
            "schema_version": "1.0.0",
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "pdf_page",
            "selector": {"block_id": "block-1"},
        }
        errors = validate_locator(self.vault, locator)
        self.assertTrue(errors)
        self.assertFalse(any("KeyError" in error for error in errors))

    def test_embedded_evidence_without_schema_version_resolves(self) -> None:
        result = self._ingest_text("note.txt", "Exact sentence.\n")
        locator = {
            "source_id": result.source_id,
            "source_hash": result.content_hash,
            "normalized_path": result.normalized_path,
            "locator_kind": "line_range",
            "selector": {"start_line": 1, "end_line": 1},
            "quote_sha256": sha256_text("Exact sentence."),
        }
        self.assertEqual([], validate_locator(self.vault, locator))


if __name__ == "__main__":
    unittest.main()
