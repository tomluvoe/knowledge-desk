from __future__ import annotations

import re
from pathlib import Path

from knowledge_desk.adapters.base import ExtractionResult
from knowledge_desk.adapters.text import decode_utf8
from knowledge_desk.util import CONTENT_END, CONTENT_START


class MarkdownAdapter:
    extensions = frozenset({".md", ".markdown"})
    adapter_id = "knowledge-desk.markdown"
    adapter_version = "1"

    def extract(self, path: Path) -> ExtractionResult:
        text, warnings = decode_utf8(path)
        title = path.stem
        in_fence = False
        fence_marker = ""
        for line in text.splitlines():
            fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
            if fence_match:
                marker = fence_match.group(1)[0]
                if not in_fence:
                    in_fence = True
                    fence_marker = marker
                elif marker == fence_marker:
                    in_fence = False
                continue
            if not in_fence:
                heading = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
                if heading:
                    title = heading.group(1)
                    break
        separator = "" if text.endswith("\n") or not text else "\n"
        body = f"{CONTENT_START}\n{text}{separator}{CONTENT_END}\n"
        return ExtractionResult(
            media_type="text/markdown",
            markdown_body=body,
            warnings=warnings,
            title=title,
        )
