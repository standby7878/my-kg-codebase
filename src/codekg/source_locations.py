"""Compact file-local UTF-8 position indexes (never retain a global source cache)."""

from __future__ import annotations

import re
from array import array
from bisect import bisect_right


class ByteLocations:
    """Index newlines once; return one-based lines and zero-based byte columns."""

    def __init__(self, raw: bytes):
        self.size = len(raw)
        self.starts = array("Q", [0])
        self.starts.extend(match.end() for match in re.finditer(b"\n", raw))

    def position(self, offset: int) -> tuple[int, int]:
        offset = min(max(offset, 0), self.size)
        index = bisect_right(self.starts, offset) - 1
        return index + 1, offset - self.starts[index]

    def line_start(self, line: int) -> int:
        return self.starts[min(max(line - 1, 0), len(self.starts) - 1)]


class Utf8Offsets:
    """Sparse character-to-byte index; ASCII files require no per-character table."""

    def __init__(self, source: str):
        self.size = len(source)
        self.positions = array("Q")
        self.extra = array("Q")
        extra = 0
        for match in re.finditer(r"[^\x00-\x7f]", source):
            extra += len(match.group().encode("utf-8")) - 1
            self.positions.append(match.end())
            self.extra.append(extra)

    def byte_offset(self, char_offset: int) -> int:
        char_offset = min(max(char_offset, 0), self.size)
        index = bisect_right(self.positions, char_offset) - 1
        return char_offset + (self.extra[index] if index >= 0 else 0)
