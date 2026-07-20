from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from evidence_vault.errors import EvidenceVaultError
from evidence_vault.util import safe_filename, write_text_synced


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


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
    canonical_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    ingest: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TranscriptFetcher(Protocol):
    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        """Retrieve transcript snippets for a video id. Network boundary."""


class YouTubeTranscriptApiFetcher:
    """Production fetcher using youtube-transcript-api (network-enabled)."""

    def fetch(self, video_id: str, languages: list[str]) -> TranscriptPayload:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException
        except ImportError as exc:  # pragma: no cover - dependency is required at runtime
            raise EvidenceVaultError(
                "youtube-transcript-api is not installed; run `uv sync --locked`"
            ) from exc

        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id, languages=languages, preserve_formatting=False)
        except YouTubeTranscriptApiException as exc:
            raise EvidenceVaultError(f"cannot retrieve YouTube transcript for {video_id}: {exc}") from exc
        except Exception as exc:  # network / parser failures from the library
            raise EvidenceVaultError(f"cannot retrieve YouTube transcript for {video_id}: {exc}") from exc

        snippets = tuple(
            TranscriptSnippet(text=_clean_snippet_text(item.text), start=float(item.start))
            for item in fetched.snippets
            if _clean_snippet_text(item.text)
        )
        if not snippets:
            raise EvidenceVaultError(f"YouTube transcript for {video_id} is empty")
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
        raise EvidenceVaultError("YouTube URL or video id is required")
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

    raise EvidenceVaultError(f"not a supported YouTube URL or video id: {url_or_id}")


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
    include_timestamps: bool = True,
) -> str:
    display_title = title or f"YouTube transcript {video_id}"
    caption_kind = "auto" if payload.is_generated else "human"
    lines = [
        f"# {display_title}",
        "",
        f"- video_id: {video_id}",
        f"- canonical_url: {canonical_url}",
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
    fetcher: TranscriptFetcher | None = None,
) -> FetchTranscriptResult:
    """Download a plain transcript file for review; does not publish canonical sources."""
    vault_root = vault_root.resolve()
    result = FetchTranscriptResult(url=url_or_id)
    try:
        video_id = extract_youtube_video_id(url_or_id)
        result.video_id = video_id
        result.canonical_url = canonical_watch_url(video_id)
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
            title=title,
            include_timestamps=include_timestamps,
        )
        out = (output_path or default_output_path(vault_root, video_id)).resolve()
        write_text_synced(out, document)
        result.output_path = str(out)
        result.title = title or f"YouTube transcript {video_id}"
        result.status = "created"
        result.message = "transcript written for review; run ingest to publish canonical sources"
        return result
    except EvidenceVaultError as exc:
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
    language: str | None = None,
    fetcher: TranscriptFetcher | None = None,
    ingest_fn: Callable[..., list] | None = None,
) -> FetchTranscriptResult:
    from evidence_vault.ingest import IngestMetadata, ingest_path

    result = fetch_youtube_transcript(
        vault_root,
        url_or_id,
        output_path=output_path,
        languages=languages,
        include_timestamps=include_timestamps,
        title=title,
        fetcher=fetcher,
    )
    if result.status != "created" or not result.output_path:
        return result

    metadata = IngestMetadata(
        title=title or result.title,
        creator=creator,
        canonical_url=result.canonical_url,
        language=language or result.language_code,
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
