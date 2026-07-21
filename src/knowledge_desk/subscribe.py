from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.layout import init_vault
from knowledge_desk.perspective import perspective_at, perspective_timeline
from knowledge_desk.util import (
    SCHEMA_VERSION,
    render_frontmatter,
    replace_json_synced,
    replace_text_synced,
    safe_filename,
    utc_now,
)
from knowledge_desk.writer import vault_write_lock
from knowledge_desk.youtube_transcript import (
    TranscriptFetcher,
    canonical_watch_url,
    fetch_and_ingest_youtube_transcript,
)


SUBSCRIPTIONS_DIR = "system/subscriptions"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}


@dataclass
class VideoItem:
    video_id: str
    title: str
    published: str  # ISO date or datetime
    url: str


@dataclass
class Subscription:
    subscription_id: str
    kind: str  # youtube_channel | youtube_playlist
    url: str
    since: str  # YYYY-MM-DD
    label: str
    subject_ref: str | None = None
    topic_ref: str | None = None
    resolved_id: str | None = None  # channel_id or playlist_id
    status: str = "active"
    language: str = "en"
    processed_video_ids: list[str] = field(default_factory=list)
    last_polled_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Subscription:
        return cls(
            subscription_id=str(data["subscription_id"]),
            kind=str(data["kind"]),
            url=str(data["url"]),
            since=str(data["since"]),
            label=str(data.get("label") or data["subscription_id"]),
            subject_ref=data.get("subject_ref"),
            topic_ref=data.get("topic_ref"),
            resolved_id=data.get("resolved_id"),
            status=str(data.get("status") or "active"),
            language=str(data.get("language") or "en"),
            processed_video_ids=list(data.get("processed_video_ids") or []),
            last_polled_at=data.get("last_polled_at"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class FeedDiscoverer(Protocol):
    def discover(self, subscription: Subscription) -> list[VideoItem]:
        """Return videos for the subscription (network boundary)."""


class YoutubeAtomDiscoverer:
    """Resolve channel/playlist ids and list recent videos via YouTube Atom feeds."""

    def __init__(self, opener: Any | None = None) -> None:
        self._opener = opener or urllib.request.urlopen

    def discover(self, subscription: Subscription) -> list[VideoItem]:
        kind, resolved = resolve_youtube_feed_target(subscription.url, fetch_text=self._fetch_text)
        feed_url = (
            f"https://www.youtube.com/feeds/videos.xml?channel_id={resolved}"
            if kind == "youtube_channel"
            else f"https://www.youtube.com/feeds/videos.xml?playlist_id={resolved}"
        )
        xml_text = self._fetch_text(feed_url)
        return parse_youtube_atom_feed(xml_text)

    def _fetch_text(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "knowledge-desk/0.1"})
        try:
            with self._opener(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise KnowledgeDeskError(f"failed to fetch {url}: {exc}") from exc


def resolve_youtube_feed_target(url: str, *, fetch_text) -> tuple[str, str]:
    """Return (kind, channel_id|playlist_id)."""
    text = url.strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or ""
    query = parse_qs(parsed.query)

    if "list" in query and query["list"]:
        return "youtube_playlist", query["list"][0]

    if host in {"youtube.com", "m.youtube.com"}:
        if path.startswith("/playlist"):
            if "list" in query and query["list"]:
                return "youtube_playlist", query["list"][0]
        if path.startswith("/channel/"):
            channel_id = path.split("/")[2]
            if channel_id.startswith("UC"):
                return "youtube_channel", channel_id
        if path.startswith("/@"):
            handle_url = f"https://www.youtube.com{path.split('/videos')[0]}"
            html = fetch_text(handle_url)
            match = re.search(r'"channelId":"(UC[\w-]+)"', html)
            if not match:
                match = re.search(r"channel_id=(UC[\w-]+)", html)
            if match:
                return "youtube_channel", match.group(1)
            raise KnowledgeDeskError(f"could not resolve channel id for {url}")
        if path.startswith("/c/") or path.startswith("/user/"):
            html = fetch_text(f"https://www.youtube.com{path}")
            match = re.search(r'"channelId":"(UC[\w-]+)"', html)
            if match:
                return "youtube_channel", match.group(1)

    raise KnowledgeDeskError(f"not a supported YouTube channel or playlist URL: {url}")


def parse_youtube_atom_feed(xml_text: str) -> list[VideoItem]:
    root = ET.fromstring(xml_text)
    items: list[VideoItem] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id_el = entry.find("yt:videoId", ATOM_NS)
        title_el = entry.find("atom:title", ATOM_NS)
        published_el = entry.find("atom:published", ATOM_NS)
        link_el = entry.find("atom:link[@rel='alternate']", ATOM_NS)
        if video_id_el is None or not video_id_el.text:
            continue
        video_id = video_id_el.text.strip()
        title = (title_el.text or video_id).strip() if title_el is not None else video_id
        published = (published_el.text or "").strip() if published_el is not None else ""
        href = link_el.get("href") if link_el is not None else canonical_watch_url(video_id)
        items.append(VideoItem(video_id=video_id, title=title, published=published, url=href or canonical_watch_url(video_id)))
    return items


def subscriptions_dir(vault_root: Path) -> Path:
    return vault_root.resolve() / SUBSCRIPTIONS_DIR


def add_subscription(
    vault_root: Path,
    url: str,
    *,
    since: str,
    label: str | None = None,
    subject_ref: str | None = None,
    topic_ref: str | None = None,
    language: str = "en",
    discoverer: FeedDiscoverer | None = None,
) -> dict[str, object]:
    vault_root = vault_root.resolve()
    _validate_since(since)
    init_vault(vault_root, write_readmes=False)
    discoverer = discoverer or YoutubeAtomDiscoverer()
    # Resolve id once (network) by probing with a temporary subscription shell.
    kind, resolved = resolve_youtube_feed_target(
        url,
        fetch_text=getattr(discoverer, "_fetch_text", None)
        or YoutubeAtomDiscoverer()._fetch_text,  # type: ignore[attr-defined]
    )
    now = utc_now()
    slug = safe_filename((label or resolved or "youtube").casefold().replace(" ", "-")[:40])
    subscription_id = f"sub-{slug}-{resolved[-6:] if resolved else 'feed'}"
    subscription = Subscription(
        subscription_id=subscription_id,
        kind=kind,
        url=url,
        since=since,
        label=label or resolved or subscription_id,
        subject_ref=subject_ref,
        topic_ref=topic_ref,
        resolved_id=resolved,
        language=language,
        created_at=now,
        updated_at=now,
    )
    # Validate feed is reachable when using real discoverer (skip if mock without resolve side effects)
    try:
        discoverer.discover(subscription)
    except KnowledgeDeskError:
        # Still save if resolution worked; discovery can fail transiently — re-raise only if unresolved
        if not resolved:
            raise
    with vault_write_lock(vault_root):
        path = subscriptions_dir(vault_root)
        path.mkdir(parents=True, exist_ok=True)
        dest = path / f"{subscription_id}.json"
        if dest.exists():
            raise KnowledgeDeskError(f"subscription already exists: {subscription_id}")
        replace_json_synced(dest, subscription.to_dict())
    return {
        "operation": "subscribe.add",
        "status": "created",
        "subscription": subscription.to_dict(),
        "path": dest.relative_to(vault_root).as_posix(),
        "message": f"subscription {subscription_id} created (since {since})",
    }


def list_subscriptions(vault_root: Path) -> dict[str, object]:
    vault_root = vault_root.resolve()
    items: list[dict[str, object]] = []
    root = subscriptions_dir(vault_root)
    if root.is_dir():
        for path in sorted(root.glob("sub-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                items.append(data)
    return {"operation": "subscribe.list", "count": len(items), "subscriptions": items}


def load_subscription(vault_root: Path, subscription_id: str) -> Subscription:
    path = subscriptions_dir(vault_root) / f"{subscription_id}.json"
    if not path.is_file():
        # allow path without prefix match
        matches = list(subscriptions_dir(vault_root).glob(f"{subscription_id}*.json")) if subscriptions_dir(vault_root).is_dir() else []
        if len(matches) == 1:
            path = matches[0]
        else:
            raise KnowledgeDeskError(f"subscription not found: {subscription_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Subscription.from_dict(data)


def save_subscription(vault_root: Path, subscription: Subscription) -> Path:
    with vault_write_lock(vault_root):
        return _save_subscription_unlocked(vault_root, subscription)


def _save_subscription_unlocked(vault_root: Path, subscription: Subscription) -> Path:
    path = subscriptions_dir(vault_root) / f"{subscription.subscription_id}.json"
    subscription.updated_at = utc_now()
    replace_json_synced(path, subscription.to_dict())
    return path


def poll_subscriptions(
    vault_root: Path,
    *,
    subscription_id: str | None = None,
    max_videos: int = 10,
    discoverer: FeedDiscoverer | None = None,
    transcript_fetcher: TranscriptFetcher | None = None,
) -> dict[str, object]:
    """Poll one or all active subscriptions; fetch/ingest new videos and write delta briefings."""
    vault_root = vault_root.resolve()
    discoverer = discoverer or YoutubeAtomDiscoverer()
    init_vault(vault_root, write_readmes=False)

    if subscription_id:
        subs = [load_subscription(vault_root, subscription_id)]
    else:
        listed = list_subscriptions(vault_root)["subscriptions"]
        subs = [Subscription.from_dict(item) for item in listed if item.get("status") == "active"]

    batch_results: list[dict[str, object]] = []
    for sub in subs:
        batch_results.append(
            _poll_one(
                vault_root,
                sub,
                max_videos=max_videos,
                discoverer=discoverer,
                transcript_fetcher=transcript_fetcher,
            )
        )
    return {
        "operation": "subscribe.poll",
        "status": "ok",
        "count": len(batch_results),
        "results": batch_results,
        "message": f"polled {len(batch_results)} subscription(s)",
    }


def _poll_one(
    vault_root: Path,
    subscription: Subscription,
    *,
    max_videos: int,
    discoverer: FeedDiscoverer,
    transcript_fetcher: TranscriptFetcher | None,
) -> dict[str, object]:
    if subscription.status != "active":
        return {
            "subscription_id": subscription.subscription_id,
            "status": "skipped",
            "message": f"status is {subscription.status}",
        }
    videos = discoverer.discover(subscription)
    since = date.fromisoformat(subscription.since)
    processed = set(subscription.processed_video_ids)
    candidates: list[VideoItem] = []
    for video in videos:
        if video.video_id in processed:
            continue
        if not _published_on_or_after(video.published, since):
            continue
        candidates.append(video)
    # Prefer oldest first for chronological integration
    candidates.sort(key=lambda item: item.published or "")
    candidates = candidates[: max(1, max_videos)]

    integrated: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for video in candidates:
        try:
            with vault_write_lock(vault_root):
                current = load_subscription(vault_root, subscription.subscription_id)
                if video.video_id in current.processed_video_ids:
                    subscription = current
                    continue
                item_result = _integrate_video(
                    vault_root,
                    current,
                    video,
                    transcript_fetcher=transcript_fetcher,
                )
                current.processed_video_ids.append(video.video_id)
                _save_subscription_unlocked(vault_root, current)
                subscription = current
                integrated.append(item_result)
        except (KnowledgeDeskError, OSError, ValueError) as exc:
            errors.append({"video_id": video.video_id, "title": video.title, "error": str(exc)})

    with vault_write_lock(vault_root):
        current = load_subscription(vault_root, subscription.subscription_id)
        current.last_polled_at = utc_now()
        _save_subscription_unlocked(vault_root, current)
    return {
        "subscription_id": subscription.subscription_id,
        "status": "ok",
        "discovered": len(videos),
        "candidates": len(candidates),
        "integrated": integrated,
        "errors": errors,
        "message": f"{len(integrated)} integrated, {len(errors)} error(s)",
    }


def _integrate_video(
    vault_root: Path,
    subscription: Subscription,
    video: VideoItem,
    *,
    transcript_fetcher: TranscriptFetcher | None,
) -> dict[str, object]:
    from knowledge_desk.youtube_transcript import YouTubeTranscriptApiFetcher

    fetcher = transcript_fetcher or YouTubeTranscriptApiFetcher()
    out = vault_root / "inbox" / safe_filename(f"youtube-{video.video_id}.md")
    ingest_result = fetch_and_ingest_youtube_transcript(
        vault_root,
        video.video_id,
        output_path=out,
        languages=[subscription.language],
        title=video.title,
        creator=subscription.label,
        language=subscription.language,
        fetcher=fetcher,
    )
    if ingest_result.status != "created" and not (ingest_result.ingest or {}).get("success"):
        # fetch may create file but ingest fails
        if ingest_result.status != "created":
            raise KnowledgeDeskError(ingest_result.message)

    source_id = None
    if ingest_result.ingest and isinstance(ingest_result.ingest.get("results"), list):
        for item in ingest_result.ingest["results"]:
            if isinstance(item, dict) and item.get("source_id"):
                source_id = item["source_id"]
                break
    if source_id is None and ingest_result.output_path:
        # re-read from ingest noop path via hash already in vault
        pass

    briefing_path = _write_delta_briefing(
        vault_root,
        subscription=subscription,
        video=video,
        source_id=source_id,
        ingest=ingest_result.to_dict(),
    )
    return {
        "video_id": video.video_id,
        "title": video.title,
        "published": video.published,
        "source_id": source_id,
        "briefing_path": briefing_path,
        "ingest": ingest_result.to_dict(),
    }


def _write_delta_briefing(
    vault_root: Path,
    *,
    subscription: Subscription,
    video: VideoItem,
    source_id: str | None,
    ingest: dict[str, object],
) -> str:
    """Write a cited briefing note: new video + delta vs prior corpus for the subscription subject."""
    identity_slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        f"{subscription.subscription_id}-{video.video_id[:8]}".casefold(),
    ).strip("-")
    wiki_id = f"wiki-synthesis-{identity_slug}"
    path = vault_root / "wiki" / "syntheses" / f"synthesis-{identity_slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    prior_ids = [vid for vid in subscription.processed_video_ids if vid != video.video_id]
    delta_lines = _delta_lines(vault_root, subscription, video)

    now = utc_now()
    evidence: list[dict[str, object]] = []
    if source_id:
        manifest_path = vault_root / "sources" / source_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                evidence.append(
                    {
                        "source_id": source_id,
                        "source_hash": manifest.get("content_hash"),
                        "normalized_path": manifest.get("normalized_path"),
                        "locator_kind": "line_range",
                        "selector": {"start_line": 1, "end_line": 1},
                    }
                )
        except (OSError, json.JSONDecodeError):
            pass
    # Drop incomplete evidence entries
    evidence = [
        item
        for item in evidence
        if item.get("source_id") and item.get("source_hash") and item.get("normalized_path")
    ]

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "wiki_id": wiki_id,
        "kind": "synthesis",
        "title": f"Subscription briefing: {video.title}",
        "created_at": now,
        "updated_at": now,
        "observation_ids": [],
        "evidence": evidence,
        "freshness": "historical",
        "extensions": {
            "org.knowledge-desk.youtube": {
                "subscription_id": subscription.subscription_id,
                "video_id": video.video_id,
                "published": video.published,
            }
        },
    }
    # Wiki schema may reject unknown extension namespaces that don't match pattern - org.knowledge-desk.youtube needs a letter after dots
    # pattern: ^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$
    # "org.knowledge-desk.youtube" - knowledge-desk has hyphen OK, youtube OK

    body = "\n".join(
        [
            f"# Subscription briefing: {video.title}",
            "",
            f"- subscription: `{subscription.subscription_id}` ({subscription.label})",
            f"- video: [{video.title}]({video.url})",
            f"- published: {video.published or 'unknown'}",
            f"- source_id: `{source_id or 'unknown'}`",
            f"- prior videos in subscription (count): {len(prior_ids)}",
            "",
            "## This video",
            "",
            f"Transcript ingested from YouTube for `{video.video_id}`. "
            "Treat claims as source material; extract observations explicitly before treating them as corpus assertions.",
            "",
            "## Delta vs prior corpus",
            "",
            *delta_lines,
            "",
            "## Integration notes",
            "",
            "- Append observations with evidence locators for material claims.",
            "- Use `confirms` / `contradicts` / `refines` / `supersedes` against earlier observations for the same subject/topic.",
            "- Run `knowledge-desk wiki evolve` after new observations are recorded.",
            "",
        ]
    )
    # If evidence empty, still write a page that refine-validate may warn about — better attach nothing than fake hashes
    if not evidence:
        metadata["evidence"] = []
    replace_text_synced(path, render_frontmatter(metadata) + "\n" + body)
    return path.relative_to(vault_root).as_posix()


def _delta_lines(vault_root: Path, subscription: Subscription, video: VideoItem) -> list[str]:
    lines: list[str] = []
    subject = subscription.subject_ref
    topic = subscription.topic_ref or "topic-general"
    if not subject:
        lines.append(
            "No `subject_ref` on this subscription; cannot compute perspective delta automatically. "
            "Bind a subject when subscribing (e.g. `--subject-ref entity-jordi-visser`)."
        )
        lines.append(
            f"Prior processed video ids: {', '.join(subscription.processed_video_ids[-5:]) or '(none)'}."
        )
        return lines

    as_of = (video.published or utc_now())[:10]
    try:
        current = perspective_at(vault_root, subject, topic, as_of)
        timeline = perspective_timeline(vault_root, subject, topic)
    except Exception as exc:  # pragma: no cover
        lines.append(f"Perspective lookup failed: {exc}")
        return lines

    lines.append(f"Subject `{subject}` topic `{topic}` as of {as_of}: **{current.status}**")
    if current.status == "supported" or current.status == "conflicted":
        lines.append(
            f"- Current orientation: `{current.orientation}` — {current.assertion or '(no assertion)'}"
        )
        if current.observation_id:
            lines.append(f"- Observation: `{current.observation_id}`")
        if current.conflicting_observation_ids:
            lines.append(f"- Conflicts: {', '.join(current.conflicting_observation_ids)}")
    else:
        lines.append(
            "- No supported perspective yet in-corpus for this subject/topic "
            "(new source is raw transcript only until observations are extracted)."
        )

    if timeline.events:
        lines.append("")
        lines.append("Recent timeline events (existing observations):")
        for event in timeline.events[-5:]:
            lines.append(
                f"- {event.get('at')}: {event.get('change')} "
                f"`{event.get('observation_id')}` ({event.get('orientation')}) — {event.get('assertion')}"
            )
        # Heuristic callout: last two orientations differ
        orients = [e.get("orientation") for e in timeline.events if e.get("orientation")]
        if len(orients) >= 2 and orients[-1] != orients[-2]:
            lines.append("")
            lines.append(
                f"**Possible position shift:** orientation moved from `{orients[-2]}` to `{orients[-1]}` "
                "on stored observations (verify against new transcript before treating as settled)."
            )
    else:
        lines.append("- No prior observation timeline for this subject/topic.")
    return lines


def _published_on_or_after(published: str, since: date) -> bool:
    if not published:
        return True
    try:
        if "T" in published:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            return dt.date() >= since
        return date.fromisoformat(published[:10]) >= since
    except ValueError:
        return True


def _validate_since(since: str) -> None:
    try:
        date.fromisoformat(since)
    except ValueError as exc:
        raise KnowledgeDeskError(f"since must be YYYY-MM-DD: {since}") from exc
