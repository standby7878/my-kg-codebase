"""Lossless SQLite staging for bounded sharded bulk exports.

These databases are private exporter build artifacts.  They deliberately store
one JSON representation per :class:`FileIR` as well as registry-friendly fact
tables; the JSON record makes the spool round-trip lossless while the facts
avoid rebuilding repository-wide Python indexes during projection.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from pathlib import Path

from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    InheritanceIR,
    LocalBindingIR,
    ModuleInitIR,
    ParseDiagnosticIR,
    SymbolIR,
)

SPOOL_SCHEMA_VERSION = 1
_WRITE_BATCH_SIZE = 64


def create_spool(path: Path, files: Iterable[FileIR]) -> None:
    """Write a complete, atomically-published extraction spool."""

    partial = path.with_suffix(path.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(partial)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("CREATE TABLE files (ordinal INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")  # noqa: E501
        connection.execute("CREATE TABLE symbols (path TEXT NOT NULL, qname TEXT NOT NULL, key_suffix TEXT NOT NULL, kind TEXT NOT NULL, parent_qname TEXT, return_annotation TEXT, ordinal INTEGER NOT NULL)")  # noqa: E501
        connection.execute("CREATE TABLE inheritance (path TEXT NOT NULL, type_qname TEXT NOT NULL, base_name TEXT NOT NULL, base_qname TEXT, ordinal INTEGER NOT NULL)")  # noqa: E501
        connection.execute("CREATE TABLE imports (path TEXT NOT NULL, module TEXT NOT NULL, name TEXT NOT NULL, alias TEXT, ordinal INTEGER NOT NULL)")  # noqa: E501
        connection.execute(
            "INSERT INTO metadata VALUES ('schema_version', ?)", (str(SPOOL_SCHEMA_VERSION),)
        )
        for ordinal, file in enumerate(files):
            payload = json.dumps(asdict(file), sort_keys=True, separators=(",", ":"))
            connection.execute("INSERT INTO files VALUES (?, ?, ?)", (ordinal, file.path, payload))
            for symbol_ordinal, symbol in enumerate(file.symbols):
                connection.execute(
                    "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        file.path,
                        symbol.qname,
                        f"{file.path}:{symbol.qname}:{symbol.start_line}",
                        symbol.kind,
                        symbol.parent_qname,
                        symbol.return_annotation,
                        symbol_ordinal,
                    ),
                )
            connection.executemany(
                "INSERT INTO imports VALUES (?, ?, ?, ?, ?)",
                (
                    (file.path, value.module, value.name, value.alias, index)
                    for index, value in enumerate(file.imports)
                ),
            )
            connection.executemany(
                "INSERT INTO inheritance VALUES (?, ?, ?, ?, ?)",
                (
                    (file.path, value.type_qname, value.base_name, value.base_qname, index)
                    for index, value in enumerate(file.inheritance)
                ),
            )
            if (ordinal + 1) % _WRITE_BATCH_SIZE == 0:
                connection.commit()
        connection.commit()
    finally:
        connection.close()
    partial.replace(path)


def iter_spool_files(path: Path) -> Iterator[FileIR]:
    """Yield losslessly reconstructed files in source order."""

    connection = _readonly(path)
    try:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if version != (str(SPOOL_SCHEMA_VERSION),):
            raise ValueError(f"unsupported spool schema: {version!r}")
        for (payload,) in connection.execute("SELECT payload FROM files ORDER BY ordinal"):
            yield _file_from_payload(json.loads(payload))
    finally:
        connection.close()


def build_registry(path: Path, spool_paths: Iterable[Path], *, repo_prefix: str) -> None:
    """Build the global, read-only-after-build resolver registry."""

    partial = path.with_suffix(path.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(partial)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(
            """
            CREATE TABLE files (
                path TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE symbols (
                key TEXT PRIMARY KEY, path TEXT NOT NULL, qname TEXT NOT NULL,
                kind TEXT NOT NULL, parent_qname TEXT, return_annotation TEXT NOT NULL DEFAULT '');
            CREATE TABLE imports (
                path TEXT NOT NULL, module TEXT NOT NULL, name TEXT NOT NULL,
                alias TEXT, ordinal INTEGER NOT NULL
            );
            CREATE TABLE inheritance (
                path TEXT NOT NULL, type_qname TEXT NOT NULL, base_name TEXT NOT NULL,
                base_qname TEXT, ordinal INTEGER NOT NULL
            );
            """
        )
        file_ordinal = 0
        for spool_path in spool_paths:
            source = _readonly(spool_path)
            try:
                cursor = source.execute("SELECT path, payload FROM files ORDER BY ordinal")
                for path_value, payload in cursor:
                    file = _file_from_payload(json.loads(payload))
                    connection.execute(
                        "INSERT INTO files VALUES (?, ?, ?)",
                        (path_value, file_ordinal, payload),
                    )
                    file_ordinal += 1
                    for symbol in file.symbols:
                        key = f"{repo_prefix}:{file.path}:{symbol.qname}:{symbol.start_line}"
                        connection.execute(
                            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                key, file.path, symbol.qname, symbol.kind,
                                symbol.parent_qname, symbol.return_annotation or "",
                            ),
                        )
                    connection.executemany(
                        "INSERT INTO imports VALUES (?, ?, ?, ?, ?)",
                        (
                            (file.path, item.module, item.name, item.alias, ordinal)
                            for ordinal, item in enumerate(file.imports)
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO inheritance VALUES (?, ?, ?, ?, ?)",
                        (
                            (file.path, item.type_qname, item.base_name, item.base_qname, ordinal)
                            for ordinal, item in enumerate(file.inheritance)
                        ),
                    )
            finally:
                source.close()
            # Each source spool is an independently bounded import phase.
            # Commit before moving to the next one so the write transaction
            # never spans the full repository.
            connection.commit()
        connection.executescript(
            """
            CREATE INDEX symbols_qname_idx ON symbols(qname, key);
            CREATE INDEX symbols_method_idx
                ON symbols(parent_qname, qname, key) WHERE kind = 'method';
            CREATE INDEX imports_path_idx ON imports(path, ordinal);
            CREATE INDEX inheritance_type_idx ON inheritance(type_qname, ordinal);
            """
        )
        connection.commit()
    finally:
        connection.close()
    partial.replace(path)


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)


def _file_from_payload(data: dict[str, object]) -> FileIR:
    init = data.get("module_init")
    return FileIR(
        path=str(data["path"]), language=str(data["language"]), loc=int(data["loc"]),
        module_qname=str(data["module_qname"]),
        module_init=ModuleInitIR(**init) if isinstance(init, dict) else None,
        parse_status=str(data["parse_status"]),
        diagnostics=tuple(ParseDiagnosticIR(**item) for item in data["diagnostics"]),
        imports=tuple(ImportIR(**item) for item in data["imports"]),
        symbols=tuple(SymbolIR(**item) for item in data["symbols"]),
        inheritance=tuple(InheritanceIR(**item) for item in data["inheritance"]),
        calls=tuple(CallIR(**item) for item in data["calls"]),
        local_bindings=tuple(LocalBindingIR(**item) for item in data["local_bindings"]),
    )
