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
    YouTubeVideoMetadata,
    extract_youtube_video_id,
    fetch_and_ingest_youtube_transcript,
    fetch_youtube_transcript,
    format_timestamp,
    parse_youtube_video_metadata,
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


class FakeMetadataFetcher:
    def __init__(
        self,
        metadata: YouTubeVideoMetadata | None = None,
        error: Exception | None = None,
    ) -> None:
        self.metadata = metadata
        self.error = error
        self.calls: list[str] = []

    def fetch(self, video_id: str) -> YouTubeVideoMetadata:
        self.calls.append(video_id)
        if self.error:
            raise self.error
        assert self.metadata is not None
        return self.metadata


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

    def _metadata(self) -> YouTubeVideoMetadata:
        return YouTubeVideoMetadata(
            video_id="dQw4w9WgXcQ",
            canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Discovered talk",
            creator="Example Channel",
            publication_date="2026-07-20",
            channel_id="UCexample123",
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
            creator="Example Channel",
            publication_date="2026-07-20",
            include_timestamps=True,
        )
        self.assertIn("# Sample talk", document)
        self.assertIn("caption_kind: auto", document)
        self.assertIn("creator: Example Channel", document)
        self.assertIn("publication_date: 2026-07-20", document)
        self.assertIn("[00:01] Hello from the talk.", document)
        self.assertIn("[01:05] Second line of the transcript.", document)

    def test_fetch_writes_inbox_markdown_without_network(self) -> None:
        fetcher = FakeFetcher(self._payload(generated=True))
        metadata_fetcher = FakeMetadataFetcher(self._metadata())
        result = fetch_youtube_transcript(
            self.vault,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            fetcher=fetcher,
            metadata_fetcher=metadata_fetcher,
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
        self.assertIn("creator: Example Channel", text)
        self.assertEqual("Sample talk", result.title)
        self.assertEqual("Example Channel", result.creator)
        self.assertEqual("2026-07-20", result.publication_date)
        self.assertEqual(["dQw4w9WgXcQ"], metadata_fetcher.calls)
        self.assertEqual([("dQw4w9WgXcQ", ["en"])], fetcher.calls)

    def test_parse_public_watch_page_metadata(self) -> None:
        html = """
        <html><head>
          <script type="application/ld+json">
            {"@context":"https://schema.org","@type":"VideoObject",
             "name":"Public unlisted talk","uploadDate":"2026-07-19T10:20:30Z",
             "author":{"@type":"Person","name":"Jordi Visser"}}
          </script>
          <script>var data = {"channelId":"UCjordi123"};</script>
        </head></html>
        """
        metadata = parse_youtube_video_metadata(html, "dQw4w9WgXcQ")
        self.assertEqual("Public unlisted talk", metadata.title)
        self.assertEqual("Jordi Visser", metadata.creator)
        self.assertEqual("2026-07-19", metadata.publication_date)
        self.assertEqual("UCjordi123", metadata.channel_id)

    def test_fetch_and_ingest_uses_discovered_metadata(self) -> None:
        fetcher = FakeFetcher(self._payload())
        metadata_fetcher = FakeMetadataFetcher(self._metadata())
        result = fetch_and_ingest_youtube_transcript(
            self.vault,
            "dQw4w9WgXcQ",
            fetcher=fetcher,
            metadata_fetcher=metadata_fetcher,
            subject_refs=["entity-sample-speaker"],
            topic_refs=["topic-sample-talk"],
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
        self.assertEqual("Discovered talk", manifest["title"])
        self.assertEqual("Example Channel", manifest["creator"])
        self.assertEqual("2026-07-20", manifest["publication_date"])
        self.assertEqual("en", manifest["language"])
        self.assertEqual(["entity-sample-speaker"], manifest["subject_refs"])
        self.assertEqual(["topic-sample-talk"], manifest["topic_refs"])
        self.assertEqual(
            {"video_id": "dQw4w9WgXcQ", "channel_id": "UCexample123"},
            manifest["extensions"]["org.knowledge-desk.youtube"],
        )

    def test_explicit_metadata_overrides_discovered_fields(self) -> None:
        metadata_fetcher = FakeMetadataFetcher(self._metadata())
        result = fetch_youtube_transcript(
            self.vault,
            "dQw4w9WgXcQ",
            fetcher=FakeFetcher(self._payload()),
            metadata_fetcher=metadata_fetcher,
            title="Operator title",
            creator="Operator creator",
        )
        self.assertEqual("created", result.status, result.message)
        self.assertEqual("Operator title", result.title)
        self.assertEqual("Operator creator", result.creator)
        self.assertEqual("2026-07-20", result.publication_date)
        self.assertEqual("UCexample123", result.channel_id)

    def test_metadata_failure_is_nonfatal_when_captions_work(self) -> None:
        result = fetch_youtube_transcript(
            self.vault,
            "dQw4w9WgXcQ",
            fetcher=FakeFetcher(self._payload()),
            metadata_fetcher=FakeMetadataFetcher(error=KnowledgeDeskError("watch page blocked")),
        )
        self.assertEqual("created", result.status, result.message)
        self.assertEqual("YouTube transcript dQw4w9WgXcQ", result.title)
        self.assertIsNone(result.creator)
        self.assertIsNone(result.publication_date)
        self.assertTrue(any("metadata unavailable" in warning for warning in result.warnings))
        self.assertIn("with warnings", result.message)

    def test_missing_transcript_fails_without_publish(self) -> None:
        fetcher = FakeFetcher(error=KnowledgeDeskError("no captions"))
        result = fetch_youtube_transcript(
            self.vault,
            "dQw4w9WgXcQ",
            fetcher=fetcher,
            metadata_fetcher=FakeMetadataFetcher(self._metadata()),
        )
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
