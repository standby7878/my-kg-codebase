"""Bounds and sizing helpers for generated Neo4j CSV imports."""

MAX_GENERATED_CSV_FIELD_SIZE_BYTES = 128 * 1024 * 1024 + 4 * 1024
NEO4J_DEFAULT_READ_BUFFER_BYTES = 4 * 1024 * 1024
NEO4J_READ_BUFFER_MARGIN_BYTES = 64 * 1024


def serialized_csv_field_size_bytes(value: str) -> int:
    """Return UTF-8 bytes needed for one QUOTE_MINIMAL serialized CSV field."""
    encoded = len(value.encode("utf-8"))
    quotes = value.count('"')
    if quotes or any(char in value for char in ",\r\n"):
        return encoded + quotes + 2
    return encoded


def validate_csv_field_size(value: object, *, name: str = "CSV max field size") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 0 <= value <= MAX_GENERATED_CSV_FIELD_SIZE_BYTES:
        raise ValueError(f"{name} is outside the configured generated CSV bound")
    return value
