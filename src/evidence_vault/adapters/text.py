from __future__ import annotations

from pathlib import Path

from evidence_vault.adapters.base import ExtractionResult
from evidence_vault.errors import ExtractionError
from evidence_vault.util import CONTENT_END, CONTENT_START


def decode_utf8(path: Path) -> tuple[str, list[str]]:
    data = path.read_bytes()
    warnings: list[str] = []
    encoding = "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
        warnings.append("UTF-8 byte-order mark removed during normalization")
    try:
        return data.decode(encoding, errors="strict"), warnings
    except UnicodeDecodeError as exc:
        raise ExtractionError(
            f"{path.name} is not valid UTF-8; normalization refused instead of replacing undecodable bytes"
        ) from exc


class TextAdapter:
    extensions = frozenset({".txt"})

    def extract(self, path: Path) -> ExtractionResult:
        text, warnings = decode_utf8(path)
        if not text.strip():
            warnings.append("plain text source is empty; normalized content will be empty")
        lines = text.splitlines()
        blocks: list[str] = []
        in_block = False
        block_number = 0
        for line_number, line in enumerate(lines, start=1):
            if line.strip() and not in_block:
                block_number += 1
                blocks.append(f"<!-- ev-block:block-{block_number} start-line:{line_number} -->")
                in_block = True
            if not line.strip() and in_block:
                blocks.append(f"<!-- ev-block-end:block-{block_number} end-line:{line_number - 1} -->")
                in_block = False
            blocks.append(line)
        if in_block:
            blocks.append(f"<!-- ev-block-end:block-{block_number} end-line:{len(lines)} -->")
        rendered = "\n".join(blocks)
        if text.endswith(("\n", "\r")):
            rendered += "\n"
        separator = "" if rendered.endswith("\n") or not rendered else "\n"
        body = f"# {path.stem}\n\n{CONTENT_START}\n{rendered}{separator}{CONTENT_END}\n"
        return ExtractionResult(
            media_type="text/plain",
            markdown_body=body,
            warnings=warnings,
            title=path.stem,
        )
