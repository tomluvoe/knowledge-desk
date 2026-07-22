from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.fetch_page import (
    ExtractedPage,
    HttpResponse,
    decode_body,
    extract_main_content,
    fetch_and_ingest_page,
    fetch_page,
    render_page_document,
    validate_http_url,
)
from knowledge_desk.validation import validate_vault


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Wetland Field Notes</title>
  <style>nav { display: none; }</style>
  <script>alert('xss');</script>
</head>
<body>
  <nav>Home | About | Ads</nav>
  <article>
    <h1>Wetland Field Notes</h1>
    <p>Frog calls were recorded at three sampling points near the marsh edge.</p>
    <p>Water temperature remained stable overnight.</p>
    <ul>
      <li>Point A</li>
      <li>Point B</li>
    </ul>
    <p>See also the <a href="https://example.com/related">related report</a>.</p>
  </article>
  <footer>Copyright example.com</footer>
</body>
</html>
"""


class FakeFetcher:
    def __init__(self, response: HttpResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str) -> HttpResponse:
        self.calls.append(url)
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


class FetchPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")
        (self.vault / "system" / "logs").mkdir(parents=True)
        for name in ("sources", "observations", "wiki", "memory", "domains", "inbox"):
            (self.vault / name).mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validate_rejects_non_http_schemes(self) -> None:
        with self.assertRaises(KnowledgeDeskError):
            validate_http_url("file:///etc/passwd")
        with self.assertRaises(KnowledgeDeskError):
            validate_http_url("javascript:alert(1)")
        with self.assertRaises(KnowledgeDeskError):
            validate_http_url("ftp://example.com/a")
        self.assertEqual("https://example.com/a", validate_http_url("https://example.com/a"))

    def test_extract_main_content_from_html_fixture(self) -> None:
        extracted = extract_main_content(
            SAMPLE_HTML,
            url="https://example.com/article",
            content_type="text/html; charset=utf-8",
        )
        self.assertIsInstance(extracted, ExtractedPage)
        self.assertIn("Frog calls", extracted.markdown)
        self.assertNotIn("alert(", extracted.markdown)
        self.assertNotIn("display: none", extracted.markdown)
        self.assertTrue(extracted.text_length > 40)

    def test_empty_html_fails_clearly(self) -> None:
        with self.assertRaises(KnowledgeDeskError):
            extract_main_content(
                "<html><body><nav>only chrome</nav></body></html>",
                url="https://example.com/empty",
                content_type="text/html",
            )

    def test_non_html_content_type_rejected(self) -> None:
        with self.assertRaises(KnowledgeDeskError):
            extract_main_content(
                "%PDF-1.4 binary",
                url="https://example.com/file.pdf",
                content_type="application/pdf",
            )

    def test_decode_body_utf8_and_refuses_garbage(self) -> None:
        text = decode_body(
            "café".encode("utf-8"),
            content_type="text/html; charset=utf-8",
            encoding_hint="utf-8",
        )
        self.assertIn("café", text)
        with self.assertRaises(KnowledgeDeskError):
            decode_body(
                b"\xff\xfe\x00\x01 not text",
                content_type="text/html; charset=utf-8",
                encoding_hint="utf-8",
            )

    def test_fetch_writes_inbox_markdown_without_network(self) -> None:
        fetcher = FakeFetcher(
            HttpResponse(
                final_url="https://example.com/article",
                status_code=200,
                content_type="text/html; charset=utf-8",
                body=SAMPLE_HTML.encode("utf-8"),
                encoding="utf-8",
            )
        )
        result = fetch_page(
            self.vault,
            "https://example.com/article",
            fetcher=fetcher,
        )
        self.assertEqual("created", result.status, result.message)
        self.assertTrue(result.output_path)
        path = Path(result.output_path).resolve()
        self.assertTrue(path.is_file())
        self.assertTrue(path.is_relative_to((self.vault / "inbox").resolve()))
        text = path.read_text(encoding="utf-8")
        self.assertIn("canonical_url:", text)
        self.assertIn("Frog calls", text)
        self.assertIn("## Content", text)
        self.assertEqual(1, len(fetcher.calls))

    def test_fetch_and_ingest_publishes_source(self) -> None:
        fetcher = FakeFetcher(
            HttpResponse(
                final_url="https://example.com/article",
                status_code=200,
                content_type="text/html; charset=utf-8",
                body=SAMPLE_HTML.encode("utf-8"),
                encoding="utf-8",
            )
        )
        result = fetch_and_ingest_page(
            self.vault,
            "https://example.com/article",
            title="Wetland Field Notes",
            subject_refs=["entity-example-wetland"],
            topic_refs=["topic-amphibian-activity"],
            fetcher=fetcher,
        )
        self.assertEqual("created", result.status, result.message)
        self.assertTrue((result.ingest or {}).get("success"), result.ingest)
        sources = list((self.vault / "sources").glob("src-*"))
        self.assertEqual(1, len(sources))
        manifest = json.loads((sources[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("https://example.com/article", manifest.get("canonical_url"))
        self.assertEqual(["entity-example-wetland"], manifest["subject_refs"])
        self.assertEqual(["topic-amphibian-activity"], manifest["topic_refs"])
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))

    def test_render_document_has_metadata_header(self) -> None:
        extracted = ExtractedPage(title="T", markdown="Hello world from page.\n", text_length=22)
        doc = render_page_document(
            url="https://example.com/a",
            final_url="https://example.com/a",
            content_type="text/html",
            extracted=extracted,
            fetched_at="2026-07-21T00:00:00Z",
            title="T",
        )
        self.assertIn("fetched_at: 2026-07-21T00:00:00Z", doc)
        self.assertIn("Hello world from page.", doc)

    def test_fetch_failure_does_not_write_file(self) -> None:
        fetcher = FakeFetcher(error=KnowledgeDeskError("HTTP 404"))
        result = fetch_page(self.vault, "https://example.com/missing", fetcher=fetcher)
        self.assertEqual("failed", result.status)
        self.assertIsNone(result.output_path)
        self.assertEqual([], list((self.vault / "inbox").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
