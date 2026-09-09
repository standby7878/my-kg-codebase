"""Immutable, parser-independent semantic IR for SQL source files.

The classes in this module intentionally contain values derived from SQL, not
pglast nodes.  This keeps the public contract stable if the PostgreSQL parser
implementation is replaced later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SqlObjectRole = Literal["define", "read", "write", "call", "alter", "drop"]


@dataclass(frozen=True)
class SqlArtifactIR:
    ordinal: int
    origin: Literal["sql_file"]
    dialect: str
    text: str
    text_hash: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class SqlStatementIR:
    artifact_ordinal: int
    ordinal: int
    kind: str
    parent_ordinal: int | None = None
    control_context: str | None = None
    start_line: int = 1
    start_column: int = 1
    end_line: int = 1
    end_column: int = 1


@dataclass(frozen=True)
class SqlObjectRefIR:
    artifact_ordinal: int
    statement_ordinal: int
    ordinal: int
    role: SqlObjectRole
    raw_name: str
    database_name: str | None
    schema_name: str | None
    object_name: str | None
    object_kind_hint: str | None
    signature_hint: str | None = None
    start_line: int = 1
    start_column: int = 1
    end_line: int = 1
    end_column: int = 1
    dynamic: bool = False
    search_path: tuple[str, ...] = ()
