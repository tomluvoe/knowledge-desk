from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from knowledge_desk.subscribe import (
    Subscription,
    VideoItem,
    add_subscription,
    list_subscriptions,
    parse_youtube_atom_feed,
    poll_subscriptions,
    resolve_youtube_feed_target,
)
from knowledge_desk.youtube_transcript import TranscriptPayload, TranscriptSnippet
from knowledge_desk.errors import KnowledgeDeskError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:aaaaaaaaaaa</id>
    <yt:videoId>aaaaaaaaaaa</yt:videoId>
    <title>Older video</title>
    <published>2025-12-01T12:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/>
  </entry>
  <entry>
    <id>yt:video:bbbbbbbbbbb</id>
    <yt:videoId>bbbbbbbbbbb</yt:videoId>
    <title>New market update</title>
    <published>2026-03-15T12:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=bbbbbbbbbbb"/>
  </entry>
  <entry>
    <id>yt:video:ccccccccccc</id>
    <yt:videoId>ccccccccccc</yt:videoId>
    <title>Even newer</title>
    <published>2026-06-01T12:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=ccccccccccc"/>
  </entry>
</feed>
"""


class FakeDiscoverer:
    def __init__(self, items: list[VideoItem] | None = None) -> None:
        self.items = items or parse_youtube_atom_feed(SAMPLE_ATOM)

    def discover(self, subscription: Subscription) -> list[VideoItem]:
        return list(self.items)

    def _fetch_text(self, url: str) -> str:
        if "list=" in url or "playlist" in url:
            return ""
        return '"channelId":"UCxxxxxxxxxxxxxxxxxxxxxx"'


class FakeFetcher:
    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        return TranscriptPayload(
            video_id=video_id,
            language="English",
            language_code="en",
            is_generated=False,
            snippets=(
                TranscriptSnippet(text=f"Transcript body for {video_id} with enough words.", start=0.0),
                TranscriptSnippet(text="Second line of spoken content for the video.", start=5.0),
            ),
        )


class SubscribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "system" / "schemas", self.vault / "system" / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "system" / "examples", self.vault / "system" / "examples")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parse_atom_and_resolve_playlist(self) -> None:
        items = parse_youtube_atom_feed(SAMPLE_ATOM)
        self.assertEqual(3, len(items))
        kind, resolved = resolve_youtube_feed_target(
            "https://www.youtube.com/playlist?list=PLtestdata123",
            fetch_text=lambda url: "",
        )
        self.assertEqual("youtube_playlist", kind)
        self.assertEqual("PLtestdata123", resolved)

    def test_add_list_and_poll_respects_since(self) -> None:
        discoverer = FakeDiscoverer()
        added = add_subscription(
            self.vault,
            "https://www.youtube.com/playlist?list=PLtestdata123",
            since="2026-01-01",
            label="Test playlist",
            subject_ref="entity-test-speaker",
            topic_ref="topic-markets",
            discoverer=discoverer,
        )
        self.assertEqual("created", added["status"], added)
        listed = list_subscriptions(self.vault)
        self.assertEqual(1, listed["count"])

        polled = poll_subscriptions(
            self.vault,
            max_videos=5,
            discoverer=discoverer,
            transcript_fetcher=FakeFetcher(),
        )
        self.assertEqual("ok", polled["status"])
        result = polled["results"][0]
        # aaaaaaaaaaa is before since → skipped; two videos on/after 2026-01-01
        self.assertEqual(2, len(result["integrated"]), result)
        self.assertEqual([], result["errors"], result)
        sub = listed["subscriptions"][0]
        # reload after poll
        listed2 = list_subscriptions(self.vault)
        processed = listed2["subscriptions"][0]["processed_video_ids"]
        self.assertIn("bbbbbbbbbbb", processed)
        self.assertIn("ccccccccccc", processed)
        self.assertNotIn("aaaaaaaaaaa", processed)

        briefings = list((self.vault / "wiki" / "syntheses").glob("*.md"))
        self.assertEqual(2, len(briefings))
        text = briefings[0].read_text(encoding="utf-8")
        self.assertIn("Delta vs prior corpus", text)
        self.assertIn("entity-test-speaker", text)

        # Second poll is a no-op integrate
        polled2 = poll_subscriptions(
            self.vault,
            max_videos=5,
            discoverer=discoverer,
            transcript_fetcher=FakeFetcher(),
        )
        self.assertEqual(0, len(polled2["results"][0]["integrated"]))

    def test_resolve_channel_handle(self) -> None:
        kind, channel_id = resolve_youtube_feed_target(
            "https://www.youtube.com/@JordiVisserLabs/videos",
            fetch_text=lambda url: 'meta "channelId":"UCabc123xyzABCDEFGHIjk"',
        )
        self.assertEqual("youtube_channel", kind)
        self.assertEqual("UCabc123xyzABCDEFGHIjk", channel_id)

    def test_invalid_since(self) -> None:
        with self.assertRaises(KnowledgeDeskError):
            add_subscription(
                self.vault,
                "https://www.youtube.com/playlist?list=PLx",
                since="not-a-date",
                discoverer=FakeDiscoverer(),
            )


if __name__ == "__main__":
    unittest.main()
