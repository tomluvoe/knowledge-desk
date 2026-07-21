from knowledge_desk.adapters.base import IngestionAdapter
from knowledge_desk.adapters.markdown import MarkdownAdapter
from knowledge_desk.adapters.pdf import PdfAdapter
from knowledge_desk.adapters.text import TextAdapter
from knowledge_desk.errors import UnsupportedFormatError


ADAPTERS: tuple[IngestionAdapter, ...] = (PdfAdapter(), MarkdownAdapter(), TextAdapter())


def adapter_for_suffix(suffix: str) -> IngestionAdapter:
    lowered = suffix.lower()
    for adapter in ADAPTERS:
        if lowered in adapter.extensions:
            return adapter
    raise UnsupportedFormatError(f"unsupported source format: {suffix or '(no extension)'}")


def supported_suffixes() -> frozenset[str]:
    return frozenset(extension for adapter in ADAPTERS for extension in adapter.extensions)
