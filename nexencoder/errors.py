"""Shared exception types."""


class ParseError(ValueError):
    """Raised when a binary payload is malformed, truncated, or fails a checksum."""


class TranslateError(ValueError):
    """Raised when a message cannot be translated to the target format."""
