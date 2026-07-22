from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.util import safe_filename, write_text_synced


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_EXTENSION_NAMESPACE = "org.knowledge-desk.youtube"
METADATA_TIMEOUT_SECONDS = 15.0
METADATA_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class TranscriptSnippet:
    text: str
    start: float


@dataclass(frozen=True)
class TranscriptPayload:
    video_id: str
    language: str
    language_code: str
    is_generated: bool
    snippets: tuple[TranscriptSnippet, ...]


@dataclass(frozen=True)
class YouTubeVideoMetadata:
    video_id: str
    canonical_url: str
    title: str | None = None
    creator: str | None = None
    publication_date: str | None = None
    channel_id: str | None = None


@dataclass
class FetchTranscriptResult:
    operation: str = "fetch-transcript"
    status: str = "failed"
    url: str = ""
    video_id: str | None = None
    output_path: str | None = None
    language: str | None = None
    language_code: str | None = None
    caption_kind: str | None = None  # human | auto
    title: str | None = None
    creator: str | None = None
    publication_date: str | None = None
    channel_id: str | None = None
    canonical_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    ingest: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TranscriptFetcher(Protocol):
    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        """Retrieve transcript snippets for a video id. Network boundary."""


class VideoMetadataFetcher(Protocol):
    def fetch(self, video_id: str) -> YouTubeVideoMetadata:
        """Retrieve public watch-page metadata for one video id."""


class YouTubeWatchPageMetadataFetcher:
    """Best-effort public watch-page metadata fetch with bounded HTTP reads."""

    def __init__(
        self,
        *,
        timeout_seconds: float = METADATA_TIMEOUT_SECONDS,
        max_bytes: int = METADATA_MAX_BYTES,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def fetch(self, video_id: str) -> YouTubeVideoMetadata:
        from knowledge_desk.fetch_page import HttpxPageFetcher

        canonical_url = canonical_watch_url(video_id)
        response = HttpxPageFetcher(
            timeout_seconds=self.timeout_seconds,
            max_bytes=self.max_bytes,
        ).fetch(canonical_url)
        encoding = response.encoding or "utf-8"
        try:
            html_text = response.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            html_text = response.body.decode("utf-8", errors="replace")
        return parse_youtube_video_metadata(html_text, video_id)


class _YouTubeMetadataHTMLParser(HTMLParser):
    """Collect metadata without treating the remote document as executable."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}
        self.json_ld: list[str] = []
        self._in_json_ld = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() in {"meta", "link"}:
            key = attributes.get("property") or attributes.get("name") or attributes.get("itemprop")
            value = attributes.get("content") or attributes.get("href")
            if key and value:
                self.values.setdefault(key.casefold(), []).append(value)
        if tag.lower() == "script" and "ld+json" in attributes.get("type", "").casefold():
            self._in_json_ld = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._script_parts))
            self._in_json_ld = False
            self._script_parts = []


def parse_youtube_video_metadata(html_text: str, video_id: str) -> YouTubeVideoMetadata:
    """Extract public video metadata from inert watch-page HTML."""
    parser = _YouTubeMetadataHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise KnowledgeDeskError(f"cannot parse YouTube metadata: {exc}") from exc

    title: str | None = None
    creator: str | None = None
    publication_date: str | None = None
    channel_id: str | None = None

    for raw_document in parser.json_ld:
        try:
            document = json.loads(raw_document)
        except (json.JSONDecodeError, TypeError):
            continue
        for video in _video_objects(document):
            title = title or _clean_metadata_text(video.get("name"))
            creator = creator or _creator_name(video.get("author")) or _creator_name(video.get("creator"))
            publication_date = publication_date or _normalize_publication_date(
                video.get("uploadDate") or video.get("datePublished")
            )
            channel_id = channel_id or _clean_metadata_text(video.get("channelId"))

    title = title or _first_html_metadata(parser.values, "og:title", "twitter:title", "title", "name")
    creator = creator or _first_html_metadata(parser.values, "author")
    publication_date = publication_date or _normalize_publication_date(
        _first_html_metadata(parser.values, "datepublished", "uploaddate")
    )

    # YouTube also embeds these JSON properties outside JSON-LD. Decode the
    # JSON string token instead of executing or broadly interpreting the page.
    title = title or _embedded_json_string(html_text, "title")
    creator = creator or _embedded_json_string(html_text, "ownerChannelName")
    publication_date = publication_date or _normalize_publication_date(
        _embedded_json_string(html_text, "publishDate")
        or _embedded_json_string(html_text, "uploadDate")
    )
    channel_id = channel_id or _embedded_json_string(html_text, "channelId")

    if not any((title, creator, publication_date, channel_id)):
        raise KnowledgeDeskError("watch page did not expose usable video metadata")
    return YouTubeVideoMetadata(
        video_id=video_id,
        canonical_url=canonical_watch_url(video_id),
        title=title,
        creator=creator,
        publication_date=publication_date,
        channel_id=channel_id,
    )


def _video_objects(value: Any):
    if isinstance(value, dict):
        object_type = value.get("@type")
        types = object_type if isinstance(object_type, list) else [object_type]
        if "VideoObject" in types:
            yield value
        for nested in value.values():
            yield from _video_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _video_objects(nested)


def _creator_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean_metadata_text(value.get("name"))
    if isinstance(value, list):
        for item in value:
            name = _creator_name(item)
            if name:
                return name
        return None
    return _clean_metadata_text(value)


def _clean_metadata_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned or None


def _normalize_publication_date(value: Any) -> str | None:
    cleaned = _clean_metadata_text(value)
    if not cleaned or len(cleaned) < 10:
        return None
    candidate = cleaned[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _first_html_metadata(values: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        for value in values.get(key.casefold(), []):
            cleaned = _clean_metadata_text(value)
            if cleaned:
                return cleaned
    return None


def _embedded_json_string(html_text: str, property_name: str) -> str | None:
    pattern = re.compile(
        rf'"{re.escape(property_name)}"\s*:\s*("(?:\\.|[^"\\])*")'
    )
    match = pattern.search(html_text)
    if not match:
        return None
    try:
        return _clean_metadata_text(json.loads(match.group(1)))
    except (json.JSONDecodeError, TypeError):
        return None


class YouTubeTranscriptApiFetcher:
    """Production fetcher using youtube-transcript-api (network-enabled)."""

    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException
        except ImportError as exc:  # pragma: no cover - dependency is required at runtime
            raise KnowledgeDeskError(
                "youtube-transcript-api is not installed; run `uv sync --locked`"
            ) from exc

        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id, languages=languages, preserve_formatting=False)
        except YouTubeTranscriptApiException as exc:
            raise KnowledgeDeskError(f"cannot retrieve YouTube transcript for {video_id}: {exc}") from exc
        except Exception as exc:  # network / parser failures from the library
            raise KnowledgeDeskError(f"cannot retrieve YouTube transcript for {video_id}: {exc}") from exc

        snippets = tuple(
            TranscriptSnippet(text=_clean_snippet_text(item.text), start=float(item.start))
            for item in fetched.snippets
            if _clean_snippet_text(item.text)
        )
        if not snippets:
            raise KnowledgeDeskError(f"YouTube transcript for {video_id} is empty")
        return TranscriptPayload(
            video_id=fetched.video_id or video_id,
            language=str(fetched.language or fetched.language_code or ""),
            language_code=str(fetched.language_code or ""),
            is_generated=bool(fetched.is_generated),
            snippets=snippets,
        )


def extract_youtube_video_id(url_or_id: str) -> str:
    text = url_or_id.strip()
    if not text:
        raise KnowledgeDeskError("YouTube URL or video id is required")
    if VIDEO_ID_RE.fullmatch(text):
        return text

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or ""

    if host in {"youtu.be"}:
        candidate = path.strip("/").split("/")[0]
        if VIDEO_ID_RE.fullmatch(candidate):
            return candidate
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch" or path.startswith("/watch"):
            values = parse_qs(parsed.query).get("v", [])
            if values and VIDEO_ID_RE.fullmatch(values[0]):
                return values[0]
        for prefix in ("/embed/", "/shorts/", "/live/", "/v/"):
            if path.startswith(prefix):
                candidate = path[len(prefix) :].split("/")[0]
                if VIDEO_ID_RE.fullmatch(candidate):
                    return candidate

    raise KnowledgeDeskError(f"not a supported YouTube URL or video id: {url_or_id}")


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_transcript_document(
    *,
    video_id: str,
    canonical_url: str,
    payload: TranscriptPayload,
    title: str | None = None,
    creator: str | None = None,
    publication_date: str | None = None,
    include_timestamps: bool = True,
) -> str:
    display_title = title or f"YouTube transcript {video_id}"
    caption_kind = "auto" if payload.is_generated else "human"
    lines = [
        f"# {display_title}",
        "",
        f"- video_id: {video_id}",
        f"- canonical_url: {canonical_url}",
        f"- creator: {creator or 'unknown'}",
        f"- publication_date: {publication_date or 'unknown'}",
        f"- language: {payload.language_code or payload.language or 'unknown'}",
        f"- caption_kind: {caption_kind}",
        "",
        "## Transcript",
        "",
    ]
    for snippet in payload.snippets:
        if include_timestamps:
            lines.append(f"[{format_timestamp(snippet.start)}] {snippet.text}")
        else:
            lines.append(snippet.text)
    lines.append("")
    return "\n".join(lines)


def default_output_path(vault_root: Path, video_id: str, extension: str = ".md") -> Path:
    suffix = extension if extension.startswith(".") else f".{extension}"
    name = safe_filename(f"youtube-{video_id}{suffix}")
    return vault_root / "inbox" / name


def fetch_youtube_transcript(
    vault_root: Path,
    url_or_id: str,
    *,
    output_path: Path | None = None,
    languages: list[str] | None = None,
    include_timestamps: bool = True,
    title: str | None = None,
    creator: str | None = None,
    publication_date: str | None = None,
    fetcher: TranscriptFetcher | None = None,
    metadata_fetcher: VideoMetadataFetcher | None = None,
) -> FetchTranscriptResult:
    """Download a plain transcript file for review; does not publish canonical sources."""
    vault_root = vault_root.resolve()
    result = FetchTranscriptResult(url=url_or_id)
    try:
        video_id = extract_youtube_video_id(url_or_id)
        result.video_id = video_id
        result.canonical_url = canonical_watch_url(video_id)
        discovered: YouTubeVideoMetadata | None = None
        if title is None or creator is None or publication_date is None:
            try:
                discovered = (metadata_fetcher or YouTubeWatchPageMetadataFetcher()).fetch(video_id)
            except Exception as exc:
                result.warnings.append(f"YouTube metadata unavailable: {exc}")
        result.title = title if title is not None else (discovered.title if discovered else None)
        result.creator = creator if creator is not None else (discovered.creator if discovered else None)
        result.publication_date = (
            publication_date
            if publication_date is not None
            else (discovered.publication_date if discovered else None)
        )
        result.channel_id = discovered.channel_id if discovered else None
        result.title = result.title or f"YouTube transcript {video_id}"
        language_priority = languages or ["en"]
        client = fetcher or YouTubeTranscriptApiFetcher()
        payload = client.fetch(video_id, language_priority)
        result.language = payload.language or None
        result.language_code = payload.language_code or None
        result.caption_kind = "auto" if payload.is_generated else "human"
        if payload.is_generated:
            result.warnings.append("only auto-generated captions were available")
        if not include_timestamps:
            result.warnings.append("timestamps omitted by request")

        document = render_transcript_document(
            video_id=video_id,
            canonical_url=result.canonical_url,
            payload=payload,
            title=result.title,
            creator=result.creator,
            publication_date=result.publication_date,
            include_timestamps=include_timestamps,
        )
        out = (output_path or default_output_path(vault_root, video_id)).resolve()
        write_text_synced(out, document)
        result.output_path = str(out)
        result.status = "created"
        result.message = "transcript written for review; run ingest to publish canonical sources"
        if result.warnings:
            result.message += " (with warnings)"
        return result
    except KnowledgeDeskError as exc:
        result.message = str(exc)
        return result
    except OSError as exc:
        result.message = f"failed to write transcript: {exc}"
        return result


def fetch_and_ingest_youtube_transcript(
    vault_root: Path,
    url_or_id: str,
    *,
    output_path: Path | None = None,
    languages: list[str] | None = None,
    include_timestamps: bool = True,
    title: str | None = None,
    creator: str | None = None,
    publication_date: str | None = None,
    language: str | None = None,
    subject_refs: list[str] | None = None,
    topic_refs: list[str] | None = None,
    extensions: dict[str, object] | None = None,
    fetcher: TranscriptFetcher | None = None,
    metadata_fetcher: VideoMetadataFetcher | None = None,
    ingest_fn: Callable[..., list] | None = None,
) -> FetchTranscriptResult:
    from knowledge_desk.ingest import IngestMetadata, ingest_path

    result = fetch_youtube_transcript(
        vault_root,
        url_or_id,
        output_path=output_path,
        languages=languages,
        include_timestamps=include_timestamps,
        title=title,
        creator=creator,
        publication_date=publication_date,
        fetcher=fetcher,
        metadata_fetcher=metadata_fetcher,
    )
    if result.status != "created" or not result.output_path:
        return result

    merged_extensions = dict(extensions or {})
    youtube_extension = merged_extensions.get(YOUTUBE_EXTENSION_NAMESPACE)
    youtube_values = dict(youtube_extension) if isinstance(youtube_extension, dict) else {}
    youtube_values.setdefault("video_id", result.video_id)
    if result.channel_id:
        youtube_values["channel_id"] = result.channel_id
    merged_extensions[YOUTUBE_EXTENSION_NAMESPACE] = youtube_values

    metadata = IngestMetadata(
        title=result.title,
        creator=result.creator,
        publication_date=result.publication_date,
        canonical_url=result.canonical_url,
        language=language or result.language_code,
        subject_refs=list(subject_refs or []),
        topic_refs=list(topic_refs or []),
        extensions=merged_extensions,
    )
    runner = ingest_fn or ingest_path
    ingest_results = runner(vault_root, Path(result.output_path), metadata)
    result.ingest = {
        "success": all(item.status in {"created", "revision", "noop"} for item in ingest_results),
        "results": [item.to_dict() for item in ingest_results],
    }
    if result.ingest["success"]:
        result.message = "transcript written and ingested"
    else:
        result.status = "failed"
        result.message = "transcript written but ingest failed"
    return result


def _clean_snippet_text(text: str) -> str:
    cleaned = " ".join(text.replace("\x00", "").split())
    return cleaned
