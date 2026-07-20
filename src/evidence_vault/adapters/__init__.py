from evidence_vault.adapters.base import IngestionAdapter
from evidence_vault.adapters.markdown import MarkdownAdapter
from evidence_vault.adapters.pdf import PdfAdapter
from evidence_vault.adapters.text import TextAdapter
from evidence_vault.errors import UnsupportedFormatError


ADAPTERS: tuple[IngestionAdapter, ...] = (PdfAdapter(), MarkdownAdapter(), TextAdapter())


def adapter_for_suffix(suffix: str) -> IngestionAdapter:
    lowered = suffix.lower()
    for adapter in ADAPTERS:
        if lowered in adapter.extensions:
            return adapter
    raise UnsupportedFormatError(f"unsupported source format: {suffix or '(no extension)'}")


def supported_suffixes() -> frozenset[str]:
    return frozenset(extension for adapter in ADAPTERS for extension in adapter.extensions)
