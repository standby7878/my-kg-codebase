"""Bounded-memory content identity for repository source and Markdown files."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

from codekg.ingest import (
    _iter_source_files,
    _log_scan_skip,
    _sql_config_identity,
    iter_markdown_files,
)

_READ_CHUNK_SIZE = 1024 * 1024


def content_hash(root: Path) -> str:
    """Return the legacy twelve-character content hash for ``root``.

    Paths are staged in a temporary disk-backed SQLite database so the ordered
    file list does not need to remain in Python memory.  The path collation
    delegates ordering to ``Path`` so component boundaries have the same
    semantics as ``sorted(Path(...))``.
    """

    with tempfile.TemporaryDirectory(prefix="codekg-identity-") as directory:
        database_path = Path(directory) / "paths.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            connection.create_collation("PATH_ORDER", _compare_paths)
            connection.execute("CREATE TABLE paths (path TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO paths(path) VALUES (?)",
                ((relative_path,) for relative_path in _relative_paths(root)),
            )
            connection.commit()

            digest = hashlib.sha256()
            digest.update(_sql_config_identity(root))
            for (relative_path,) in connection.execute(
                "SELECT path FROM paths ORDER BY path COLLATE PATH_ORDER"
            ):
                file_path = root / relative_path
                try:
                    digest.update(relative_path.encode())
                    with file_path.open("rb") as file:
                        for chunk in iter(lambda: file.read(_READ_CHUNK_SIZE), b""):
                            digest.update(chunk)
                except OSError as error:
                    _log_scan_skip("content_hash_skip_file", relative_path, error)
            return digest.hexdigest()[:12]
        finally:
            connection.close()


def _relative_paths(root: Path) -> Iterator[str]:
    for path in chain(_iter_source_files(root), iter_markdown_files(root)):
        yield path.relative_to(root).as_posix()


def _compare_paths(left: str, right: str) -> int:
    left_path = Path(left)
    right_path = Path(right)
    return (left_path > right_path) - (left_path < right_path)
