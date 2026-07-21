from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from knowledge_desk.adapters.base import ExtractionResult
from knowledge_desk.errors import ExtractionError
from knowledge_desk.util import CONTENT_END, CONTENT_START


class PdfAdapter:
    extensions = frozenset({".pdf"})
    adapter_id = "knowledge-desk.pdf"
    adapter_version = "1"
    minimum_total_text = 80
    minimum_average_text_per_page = 20

    def extract(self, path: Path) -> ExtractionResult:
        try:
            reader = PdfReader(str(path), strict=True)
        except (PdfReadError, OSError, ValueError) as exc:
            raise ExtractionError(f"cannot read PDF {path.name}: {exc}") from exc
        if not reader.pages:
            raise ExtractionError(f"PDF {path.name} has no pages")

        title = None
        creator = None
        if reader.metadata:
            title = _clean_metadata(reader.metadata.title)
            creator = _clean_metadata(reader.metadata.author)

        page_parts: list[str] = []
        extracted: list[str] = []
        warnings: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # pypdf exposes parser-specific exceptions here
                text = ""
                warnings.append(f"Page {page_number} text extraction failed: {type(exc).__name__}")
            text = text.replace("\x00", "").strip()
            extracted.append(text)
            page_parts.append(
                f'<a id="page-{page_number}"></a>\n\n## Page {page_number}\n\n'
                f"<!-- ev-page:{page_number} -->\n\n{text}\n"
            )

        total_text = sum(len(value.strip()) for value in extracted)
        average_text = total_text / len(extracted)
        low_text = total_text < self.minimum_total_text or average_text < self.minimum_average_text_per_page
        if low_text:
            warnings.append(
                "PDF contains too little extractable text for reliable use; OCR is required and was not attempted"
            )
        elif any(not value for value in extracted):
            warnings.append("One or more PDF pages contain no extractable text")

        status = "needs_ocr" if low_text else ("partial" if any(not value for value in extracted) else "complete")
        display_title = title or path.stem
        body = f"# {display_title}\n\n{CONTENT_START}\n" + "\n".join(page_parts) + f"{CONTENT_END}\n"
        return ExtractionResult(
            media_type="application/pdf",
            markdown_body=body,
            extraction_status=status,
            warnings=warnings,
            title=title or path.stem,
            creator=creator,
            page_count=len(extracted),
        )


def _clean_metadata(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None
