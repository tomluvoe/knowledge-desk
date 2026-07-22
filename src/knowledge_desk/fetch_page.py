"""Fetch a public web page into reviewable Markdown, then optional ingest.

Network boundary only: plain `ingest`/`validate` stay offline. Remote HTML is
untrusted data (never executed). Main content is extracted with trafilatura;
unit tests inject fake fetchers and never hit the network.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

from knowledge_desk.errors import KnowledgeDeskError
from knowledge_desk.util import safe_filename, utc_now, write_text_synced


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_MAX_REDIRECTS = 5
MIN_CONTENT_CHARS = 40
USER_AGENT = "knowledge-desk-fetch-page/0.1 (+https://github.com/tomluvoe/knowledge-desk; local research tool)"


@dataclass(frozen=True)
class HttpResponse:
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    encoding: str | None = None


@dataclass(frozen=True)
class ExtractedPage:
    title: str | None
    markdown: str
    text_length: int
    warnings: tuple[str, ...] = ()


@dataclass
class FetchPageResult:
    operation: str = "fetch-page"
    status: str = "failed"
    url: str = ""
    final_url: str | None = None
    output_path: str | None = None
    title: str | None = None
    content_type: str | None = None
    canonical_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    ingest: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PageFetcher(Protocol):
    def fetch(self, url: str) -> HttpResponse:
        """Retrieve raw HTTP response. Network boundary."""


class HttpxPageFetcher:
    """Production fetcher using httpx with timeouts, size, and redirect caps."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def fetch(self, url: str) -> HttpResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise KnowledgeDeskError("httpx is not installed; run `uv sync --locked`") from exc

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                max_redirects=self.max_redirects,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"},
            ) as client:
                with client.stream("GET", url) as response:
                    content_type = response.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise KnowledgeDeskError(
                                f"response exceeds size limit of {self.max_bytes} bytes"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    # Raise for HTTP errors after reading body for clearer messages.
                    if response.status_code >= 400:
                        raise KnowledgeDeskError(
                            f"HTTP {response.status_code} fetching {response.url}"
                        )
                    encoding = response.encoding
                    return HttpResponse(
                        final_url=str(response.url),
                        status_code=response.status_code,
                        content_type=content_type,
                        body=body,
                        encoding=encoding,
                    )
        except KnowledgeDeskError:
            raise
        except Exception as exc:
            raise KnowledgeDeskError(f"cannot fetch URL: {exc}") from exc


def validate_http_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        raise KnowledgeDeskError("URL is required")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise KnowledgeDeskError(
            f"only http/https URLs are supported (got scheme {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise KnowledgeDeskError("URL must include a host")
    return text


def extract_main_content(
    html: str,
    *,
    url: str,
    content_type: str,
) -> ExtractedPage:
    """Extract readable main content as Markdown. Does not invent text."""
    media = content_type.split(";")[0].strip().lower()
    if media and media not in {
        "text/html",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
        "text/plain",
        "",
    }:
        if not media.startswith("text/"):
            raise KnowledgeDeskError(
                f"unsupported content-type for page extraction: {media!r} "
                "(expected HTML or plain text)"
            )

    if media == "text/plain" or (not media and not _looks_like_html(html)):
        text = html.strip()
        if not text:
            raise KnowledgeDeskError("empty plain-text body")
        return ExtractedPage(title=None, markdown=text + "\n", text_length=len(text))

    try:
        import trafilatura
        from trafilatura.settings import use_config
    except ImportError as exc:  # pragma: no cover
        raise KnowledgeDeskError("trafilatura is not installed; run `uv sync --locked`") from exc

    config = use_config()
    config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = metadata.title if metadata and metadata.title else _fallback_title(html)

    # Prefer markdown; fall back to plain text if markdown is empty.
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_links=True,
        include_images=False,
        favor_recall=False,
        config=config,
    )
    warnings: list[str] = []
    if not markdown or not markdown.strip():
        plain = trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_comments=False,
            favor_recall=True,
            config=config,
        )
        if plain and plain.strip():
            markdown = plain.strip() + "\n"
            warnings.append("markdown extraction empty; fell back to plain text")
        else:
            raise KnowledgeDeskError(
                "could not extract main content (empty body, paywall, CAPTCHA, or non-article page)"
            )

    text = markdown.strip()
    if len(text) < MIN_CONTENT_CHARS:
        raise KnowledgeDeskError(
            f"extracted content too short ({len(text)} chars; need ≥{MIN_CONTENT_CHARS}) — "
            "possible empty body, paywall, CAPTCHA, or non-article page"
        )

    return ExtractedPage(
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        markdown=text + "\n",
        text_length=len(text),
        warnings=tuple(warnings),
    )


def decode_body(body: bytes, *, content_type: str, encoding_hint: str | None) -> str:
    """Decode response bytes; refuse undecodable garbage (no silent mojibake)."""
    candidates: list[str] = []
    charset = _charset_from_content_type(content_type)
    if charset:
        candidates.append(charset)
    if encoding_hint and encoding_hint not in candidates:
        candidates.append(encoding_hint)
    # Common fallbacks only after declared encodings fail.
    for name in ("utf-8", "utf-8-sig"):
        if name not in candidates:
            candidates.append(name)

    last_error: Exception | None = None
    for name in candidates:
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError) as exc:
            last_error = exc
            continue

    # Strict last resort: utf-8 with errors=strict already failed; do not use replace.
    raise KnowledgeDeskError(
        f"cannot decode response body with charset candidates {candidates}: {last_error}"
    )


def render_page_document(
    *,
    url: str,
    final_url: str,
    content_type: str,
    extracted: ExtractedPage,
    fetched_at: str,
    title: str | None = None,
) -> str:
    display_title = title or extracted.title or f"Web page {urlparse(final_url).netloc}"
    lines = [
        f"# {display_title}",
        "",
        f"- canonical_url: {url}",
        f"- final_url: {final_url}",
        f"- fetched_at: {fetched_at}",
        f"- content_type: {content_type or 'unknown'}",
        f"- extraction: trafilatura (main content; not full DOM)",
        "",
        "## Content",
        "",
        extracted.markdown.rstrip(),
        "",
    ]
    return "\n".join(lines)


def default_output_path(vault_root: Path, url: str) -> Path:
    host = urlparse(url).netloc.lower().removeprefix("www.") or "page"
    host_slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "page"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    name = safe_filename(f"web-{host_slug}-{digest}.md")
    return vault_root / "inbox" / name


def fetch_page(
    vault_root: Path,
    url: str,
    *,
    output_path: Path | None = None,
    title: str | None = None,
    fetcher: PageFetcher | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchPageResult:
    """Download and extract a page to inbox Markdown; does not publish sources."""
    vault_root = vault_root.resolve()
    result = FetchPageResult(url=url)
    try:
        clean_url = validate_http_url(url)
        result.url = clean_url
        result.canonical_url = clean_url
        client = fetcher or HttpxPageFetcher(timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        response = client.fetch(clean_url)
        result.final_url = response.final_url
        result.content_type = response.content_type
        if not response.body:
            raise KnowledgeDeskError("empty HTTP response body")

        html = decode_body(
            response.body,
            content_type=response.content_type,
            encoding_hint=response.encoding,
        )
        extracted = extract_main_content(
            html,
            url=response.final_url or clean_url,
            content_type=response.content_type,
        )
        result.warnings.extend(extracted.warnings)
        result.title = title or extracted.title

        document = render_page_document(
            url=clean_url,
            final_url=response.final_url,
            content_type=response.content_type,
            extracted=extracted,
            fetched_at=utc_now(),
            title=result.title,
        )
        out = (output_path or default_output_path(vault_root, clean_url)).resolve()
        write_text_synced(out, document)
        result.output_path = str(out)
        result.status = "created"
        if result.warnings:
            result.message = (
                "page written for review with warnings; run ingest to publish canonical sources"
            )
        else:
            result.message = "page written for review; run ingest to publish canonical sources"
        return result
    except KnowledgeDeskError as exc:
        result.message = str(exc)
        return result
    except OSError as exc:
        result.message = f"failed to write page: {exc}"
        return result


def fetch_and_ingest_page(
    vault_root: Path,
    url: str,
    *,
    output_path: Path | None = None,
    title: str | None = None,
    creator: str | None = None,
    language: str | None = None,
    subject_refs: list[str] | None = None,
    topic_refs: list[str] | None = None,
    fetcher: PageFetcher | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    ingest_fn: Callable[..., list] | None = None,
) -> FetchPageResult:
    from knowledge_desk.ingest import IngestMetadata, ingest_path

    result = fetch_page(
        vault_root,
        url,
        output_path=output_path,
        title=title,
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )
    if result.status != "created" or not result.output_path:
        return result

    metadata = IngestMetadata(
        title=title or result.title,
        creator=creator,
        canonical_url=result.canonical_url or result.final_url or url,
        language=language,
        subject_refs=list(subject_refs or []),
        topic_refs=list(topic_refs or []),
    )
    runner = ingest_fn or ingest_path
    ingest_results = runner(vault_root, Path(result.output_path), metadata)
    result.ingest = {
        "success": all(item.status in {"created", "revision", "noop"} for item in ingest_results),
        "results": [item.to_dict() for item in ingest_results],
    }
    if result.ingest["success"]:
        result.message = "page written and ingested"
    else:
        result.status = "failed"
        result.message = "page written but ingest failed"
    return result


def _charset_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset\s*=\s*([^\s;]+)", content_type or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip("\"'").lower()


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip()[:500].lower()
    return "<html" in sample or "<!doctype html" in sample or "<body" in sample or "<article" in sample


def _fallback_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    raw = re.sub(r"\s+", " ", match.group(1)).strip()
    return raw or None
