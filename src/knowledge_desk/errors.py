class KnowledgeDeskError(Exception):
    """Base error for deterministic, user-facing failures."""


class UnsupportedFormatError(KnowledgeDeskError):
    """Raised when no ingestion adapter supports an input."""


class ExtractionError(KnowledgeDeskError):
    """Raised when content cannot be safely normalized."""


class ValidationError(KnowledgeDeskError):
    """Raised when staged canonical artifacts are invalid."""
