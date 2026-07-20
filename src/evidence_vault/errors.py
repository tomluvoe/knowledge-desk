class EvidenceVaultError(Exception):
    """Base error for deterministic, user-facing failures."""


class UnsupportedFormatError(EvidenceVaultError):
    """Raised when no ingestion adapter supports an input."""


class ExtractionError(EvidenceVaultError):
    """Raised when content cannot be safely normalized."""


class ValidationError(EvidenceVaultError):
    """Raised when staged canonical artifacts are invalid."""
