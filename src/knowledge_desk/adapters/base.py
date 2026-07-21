from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExtractionResult:
    media_type: str
    markdown_body: str
    extraction_status: str = "complete"
    warnings: list[str] = field(default_factory=list)
    title: str | None = None
    creator: str | None = None
    page_count: int | None = None


class IngestionAdapter(Protocol):
    extensions: frozenset[str]
    adapter_id: str
    adapter_version: str

    def extract(self, path: Path) -> ExtractionResult:
        """Normalize an untrusted local source without executing its content."""
