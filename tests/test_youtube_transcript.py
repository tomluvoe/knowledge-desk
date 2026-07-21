from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from knowledge_desk.cli import main
from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.validation import validate_vault
from knowledge_desk.youtube_transcript import (
    TranscriptPayload,
    TranscriptSnippet,
    extract_youtube_video_id,
    fetch_and_ingest_youtube_transcript,
    fetch_youtube_transcript,
    format_timestamp,
    render_transcript_document,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class FakeFetcher:
    def __init__(self, payload: TranscriptPayload | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        self.calls.append((video_id, list(languages)))
        if self.error:
            raise self.error
        assert self.payload is not None
        return self.payload


class YouTubeTranscriptTests(unittest.TestCase):
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

    def _payload(self, video_id: str = "dQw4w9WgXcQ", generated: bool = False) -> TranscriptPayload:
        return TranscriptPayload(
            video_id=video_id,
            language="English",
            language_code="en",
            is_generated=generated,
            snippets=(
                TranscriptSnippet(text="Hello from the talk.", start=1.5),
                TranscriptSnippet(text="Second line of the transcript.", start=65.0),
            ),
        )

    def test_extract_video_id_from_common_url_forms(self) -> None:
        video_id = "dQw4w9WgXcQ"
        self.assertEqual(video_id, extract_youtube_video_id(video_id))
        self.assertEqual(video_id, extract_youtube_video_id(f"https://youtu.be/{video_id}"))
        self.assertEqual(
            video_id,
            extract_youtube_video_id(f"https://www.youtube.com/watch?v={video_id}&t=12s"),
        )
        self.assertEqual(
            video_id,
            extract_youtube_video_id(f"https://www.youtube.com/embed/{video_id}"),
        )
        self.assertEqual(
            video_id,
            extract_youtube_video_id(f"https://www.youtube.com/shorts/{video_id}"),
        )
        with self.assertRaises(KnowledgeDeskError):
            extract_youtube_video_id("https://example.com/not-youtube")

    def test_render_and_format_timestamp(self) -> None:
        self.assertEqual("01:05", format_timestamp(65))
        self.assertEqual("01:00:05", format_timestamp(3605))
        document = render_transcript_document(
            video_id="dQw4w9WgXcQ",
            canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            payload=self._payload(generated=True),
            title="Sample talk",
            include_timestamps=True,
        )
        self.assertIn("# Sample talk", document)
        self.assertIn("caption_kind: auto", document)
        self.assertIn("[00:01] Hello from the talk.", document)
        self.assertIn("[01:05] Second line of the transcript.", document)

    def test_fetch_writes_inbox_markdown_without_network(self) -> None:
        fetcher = FakeFetcher(self._payload(generated=True))
        result = fetch_youtube_transcript(
            self.vault,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            fetcher=fetcher,
            title="Sample talk",
        )
        self.assertEqual("created", result.status, result.message)
        self.assertEqual("auto", result.caption_kind)
        self.assertTrue(any("auto-generated" in warning for warning in result.warnings))
        path = Path(result.output_path or "")
        self.assertTrue(path.is_file())
        self.assertEqual((self.vault / "inbox" / "youtube-dQw4w9WgXcQ.md").resolve(), path.resolve())
        text = path.read_text(encoding="utf-8")
        self.assertIn("Hello from the talk.", text)
        self.assertEqual([("dQw4w9WgXcQ", ["en"])], fetcher.calls)

    def test_fetch_and_ingest_publishes_source(self) -> None:
        fetcher = FakeFetcher(self._payload())
        result = fetch_and_ingest_youtube_transcript(
            self.vault,
            "dQw4w9WgXcQ",
            fetcher=fetcher,
            title="Sample talk",
        )
        self.assertEqual("created", result.status, result.message)
        self.assertTrue((result.ingest or {}).get("success"))
        report = validate_vault(self.vault)
        self.assertTrue(report.valid, "\n".join(report.errors))
        self.assertEqual(1, report.checked["sources"])
        sources = list((self.vault / "sources").glob("src-*"))
        self.assertEqual(1, len(sources))
        manifest = json.loads((sources[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("https://www.youtube.com/watch?v=dQw4w9WgXcQ", manifest["canonical_url"])
        self.assertEqual("Sample talk", manifest["title"])
        self.assertEqual("en", manifest["language"])

    def test_missing_transcript_fails_without_publish(self) -> None:
        fetcher = FakeFetcher(error=KnowledgeDeskError("no captions"))
        result = fetch_youtube_transcript(self.vault, "dQw4w9WgXcQ", fetcher=fetcher)
        self.assertEqual("failed", result.status)
        self.assertIn("no captions", result.message)
        self.assertFalse(list((self.vault / "inbox").glob("youtube-*")))
        self.assertFalse(list((self.vault / "sources").glob("src-*")))

    def test_cli_fetch_transcript_with_mock_via_unit_path(self) -> None:
        # CLI uses the real fetcher; exercise CLI wiring with a pre-written file + ingest path
        # and separately ensure CLI rejects bad URLs offline.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--vault",
                    str(self.vault),
                    "fetch-transcript",
                    "https://example.com/not-youtube",
                ]
            )
        self.assertEqual(1, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual("failed", payload["status"])
        self.assertIn("not a supported YouTube", payload["message"])


if __name__ == "__main__":
    unittest.main()
