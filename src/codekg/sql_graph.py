"""Shared SQL graph rows for transactional and sharded projections.

The functions here are intentionally independent of ``bulk_export`` and the
Neo4j loader.  They yield normalized node/relationship rows with the same
semantic facts, leaving each caller to write CSV or Cypher batches.

``iter_sql_global_nodes`` yields ``(label, row)`` pairs.  The file iterators
yield ``(label, row)`` and ``(kind, start, end, properties, identity)`` pairs,
respectively.  Global rows are owner-sharded by default; pass
``all_global=True`` (or use ``iter_all_sql_global_nodes``) for an upfront
legacy projection that must validate all endpoints before file edges.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from codekg.ir import FileIR
from codekg.sql_ir import SqlObjectRefIR
from codekg.sql_resolver import (
    SqlDatabase,
    SqlObject,
    SqlResolution,
    sql_database_key,
)

SQL_NODE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "Database": (
        ("key", "key:ID(CodeKG)"),
        ("database_name", "database_name"),
        ("owner_path", "owner_path"),
    ),
    "SqlObject": (
        ("key", "key:ID(CodeKG)"),
        ("database_name", "database_name"),
        ("schema_name", "schema_name"),
        ("kind", "kind"),
        ("object_name", "object_name"),
        ("signature", "signature"),
        ("definition_count", "definition_count:int"),
        ("owner_path", "owner_path"),
    ),
    "SqlArtifact": (
        ("key", "key:ID(CodeKG)"),
        ("path", "path"),
        ("ordinal", "ordinal:int"),
        ("origin", "origin"),
        ("dialect", "dialect"),
        ("text", "text"),
        ("text_hash", "text_hash"),
        ("start_line", "start_line:int"),
        ("start_column", "start_column:int"),
        ("end_line", "end_line:int"),
        ("end_column", "end_column:int"),
    ),
    "SqlStatement": (
        ("key", "key:ID(CodeKG)"),
        ("path", "path"),
        ("artifact_ordinal", "artifact_ordinal:int"),
        ("ordinal", "ordinal:int"),
        ("kind", "kind"),
        ("parent_key", "parent_key"),
        ("control_context", "control_context"),
        ("start_line", "start_line:int"),
        ("start_column", "start_column:int"),
        ("end_line", "end_line:int"),
        ("end_column", "end_column:int"),
    ),
    "Reference": (
        ("key", "key:ID(CodeKG)"),
        ("path", "path"),
        ("artifact_ordinal", "artifact_ordinal:int"),
        ("statement_ordinal", "statement_ordinal:int"),
        ("ordinal", "ordinal:int"),
        ("raw_name", "raw_name"),
        ("database_name", "database_name"),
        ("schema_name", "schema_name"),
        ("object_name", "object_name"),
        ("object_kind_hint", "object_kind_hint"),
        ("signature_hint", "signature_hint"),
        ("status", "status"),
        ("role", "role"),
        ("dynamic", "dynamic:boolean"),
        ("candidate_count", "candidate_count:int"),
        ("candidate_keys_json", "candidate_keys_json"),
        ("start_line", "start_line:int"),
        ("start_column", "start_column:int"),
        ("end_line", "end_line:int"),
        ("end_column", "end_column:int"),
    ),
}

SQL_REL_PROPS: dict[str, tuple[tuple[str, str], ...]] = {
    "HAS_DATABASE": (),
    "HAS_OBJECT": (),
    "CONTAINS_SQL": (),
    "HAS_REFERENCE": (),
    "REFERS_TO": (("status", "status"),),
    "DEFINES": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
    "READS_FROM": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
    "WRITES_TO": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
    "INVOKES_SQL": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
    "ALTERS": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
    "DROPS": (("role", "role"), ("line", "line:int"), ("column", "column:int")),
}

SQL_REL_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    kind: (
        ("key", "key"),
        ("start", ":START_ID(CodeKG)"),
        ("end", ":END_ID(CodeKG)"),
        *props,
        ("type", ":TYPE"),
    )
    for kind, props in SQL_REL_PROPS.items()
}
# Descriptive alias for callers that prefer the long relationship name.
SQL_RELATIONSHIP_PROPS = SQL_REL_PROPS


class SqlIndex(Protocol):
    def resolve(self, ref: SqlObjectRefIR) -> SqlResolution: ...

    def objects(self, **filters: str) -> Iterator[SqlObject]: ...

    def databases(self, *, owner_path: str | None = None) -> Iterator[SqlDatabase]: ...


@dataclass(frozen=True)
class SqlNodeRow:
    label: str
    row: Mapping[str, Any]


@dataclass(frozen=True)
class SqlRelationshipRow:
    kind: str
    start: str
    end: str
    properties: Mapping[str, Any]
    identity: str


def iter_all_sql_global_nodes(index: SqlIndex) -> Iterator[tuple[str, dict[str, Any]]]:
    yield from iter_sql_global_nodes(index, all_global=True)


def iter_sql_all_global_nodes(index: SqlIndex) -> Iterator[tuple[str, dict[str, Any]]]:
    """Compatibility alias for the explicit all-global iterator."""

    yield from iter_all_sql_global_nodes(index)


def iter_sql_global_nodes(
    index: SqlIndex, *, owner_path: str | None = None, all_global: bool = False
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield Database and SqlObject nodes once, optionally owner-sharded."""

    database_owner = None if all_global else owner_path
    for database in index.databases(owner_path=database_owner):
        yield "Database", _database_row(database)
    object_owner = None if all_global else owner_path
    for obj in index.objects(owner_path=object_owner):
        yield "SqlObject", _object_row(obj)


def iter_sql_global_relationships(
    repo_prefix: str,
    repo_key: str,
    index: SqlIndex,
    *,
    owner_path: str | None = None,
    all_global: bool = False,
) -> Iterator[tuple[str, str, str, dict[str, Any], str]]:
    """Yield repository-to-database and database-to-object global edges.

    ``owner_path`` uses the same deterministic minimum-owner sharding as
    :func:`iter_sql_global_nodes`; ``all_global`` is for legacy upfront graph
    construction where every global endpoint is emitted before file edges.
    """

    selected_owner = None if all_global else owner_path
    for database in index.databases(owner_path=selected_owner):
        yield _relationship(
            "HAS_DATABASE",
            repo_key,
            database.key,
            {},
            f"{repo_key}:has-database:{database.key}",
        )
    for obj in index.objects(owner_path=selected_owner):
        database_key = sql_database_key(repo_prefix, obj.database_name, obj.database_is_default)
        yield _relationship(
            "HAS_OBJECT",
            database_key,
            obj.key,
            {},
            f"{database_key}:has-object:{obj.key}",
        )


def iter_sql_file_nodes(
    repo_prefix: str, file: FileIR, index: SqlIndex
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield all SQL source nodes for one file in source ordinal order."""

    for artifact in sorted(file.sql_artifacts, key=lambda value: value.ordinal):
        yield (
            "SqlArtifact",
            {
                "key": _artifact_key(repo_prefix, file.path, artifact.ordinal),
                "path": file.path,
                "ordinal": artifact.ordinal,
                "origin": artifact.origin,
                "dialect": artifact.dialect,
                "text": artifact.text,
                "text_hash": artifact.text_hash,
                "start_line": artifact.start_line,
                "start_column": artifact.start_column,
                "end_line": artifact.end_line,
                "end_column": artifact.end_column,
            },
        )
    for statement in sorted(file.sql_statements, key=lambda value: value.ordinal):
        key = _statement_key(repo_prefix, file.path, statement.artifact_ordinal, statement.ordinal)
        parent_key = (
            _statement_key(
                repo_prefix,
                file.path,
                statement.artifact_ordinal,
                statement.parent_ordinal,
            )
            if statement.parent_ordinal is not None
            else None
        )
        yield (
            "SqlStatement",
            {
                "key": key,
                "path": file.path,
                "artifact_ordinal": statement.artifact_ordinal,
                "ordinal": statement.ordinal,
                "kind": statement.kind,
                "parent_key": parent_key,
                "control_context": statement.control_context,
                "start_line": statement.start_line,
                "start_column": statement.start_column,
                "end_line": statement.end_line,
                "end_column": statement.end_column,
            },
        )
    for ref in sorted(file.sql_object_refs, key=lambda value: value.ordinal):
        resolution = index.resolve(ref)
        yield "Reference", _reference_row(repo_prefix, file.path, ref, resolution)


def iter_sql_file_relationships(
    repo_prefix: str, file: FileIR, index: SqlIndex
) -> Iterator[tuple[str, str, str, dict[str, Any], str]]:
    """Yield SQL relationships for one file.

    Every source occurrence gets a distinct Reference identity.  Derived
    statement-to-object edges are deduplicated by statement/role/target within
    this file, preserving stable graph semantics for repeated AST visits.
    """

    file_key = _file_key(repo_prefix, file.path)
    statements = {value.ordinal: value for value in file.sql_statements}
    for artifact in sorted(file.sql_artifacts, key=lambda value: value.ordinal):
        artifact_key = _artifact_key(repo_prefix, file.path, artifact.ordinal)
        yield _relationship("CONTAINS_SQL", file_key, artifact_key, {}, f"{artifact_key}:contains")
    for statement in sorted(file.sql_statements, key=lambda value: value.ordinal):
        statement_key = _statement_key(
            repo_prefix, file.path, statement.artifact_ordinal, statement.ordinal
        )
        artifact_key = _artifact_key(repo_prefix, file.path, statement.artifact_ordinal)
        yield _relationship(
            "CONTAINS_SQL", artifact_key, statement_key, {}, f"{statement_key}:artifact"
        )
        if statement.parent_ordinal is not None:
            parent_key = _statement_key(
                repo_prefix, file.path, statement.artifact_ordinal, statement.parent_ordinal
            )
            yield _relationship(
                "CONTAINS_SQL", parent_key, statement_key, {}, f"{statement_key}:parent"
            )

    derived: set[tuple[int, str, str]] = set()
    for ref in sorted(file.sql_object_refs, key=lambda value: value.ordinal):
        resolution = index.resolve(ref)
        ref_key = _reference_key(repo_prefix, file.path, ref)
        statement = statements.get(ref.statement_ordinal)
        if statement is None:
            continue
        statement_key = _statement_key(
            repo_prefix, file.path, statement.artifact_ordinal, statement.ordinal
        )
        yield _relationship("HAS_REFERENCE", statement_key, ref_key, {}, f"{ref_key}:statement")
        if not resolution.is_exact:
            continue
        assert resolution.object_key is not None
        yield _relationship(
            "REFERS_TO",
            ref_key,
            resolution.object_key,
            {"status": resolution.status},
            f"{ref_key}:refers",
        )
        derived_kind = {
            "define": "DEFINES",
            "read": "READS_FROM",
            "write": "WRITES_TO",
            "call": "INVOKES_SQL",
            "alter": "ALTERS",
            "drop": "DROPS",
        }.get(ref.role)
        if derived_kind is None:
            continue
        dedup_key = (statement.ordinal, ref.role, resolution.object_key)
        if dedup_key in derived:
            continue
        derived.add(dedup_key)
        yield _relationship(
            derived_kind,
            statement_key,
            resolution.object_key,
            {"role": ref.role, "line": ref.start_line, "column": ref.start_column},
            f"{statement_key}:{derived_kind}:{resolution.object_key}",
        )


def _database_row(database: SqlDatabase) -> dict[str, Any]:
    return {
        "key": database.key,
        "database_name": database.database_name,
        "owner_path": database.owner_path,
    }


def _object_row(obj: SqlObject) -> dict[str, Any]:
    return {
        "key": obj.key,
        "database_name": obj.database_name,
        "schema_name": obj.schema_name,
        "kind": obj.kind,
        "object_name": obj.object_name,
        "signature": obj.signature,
        "definition_count": obj.definition_count,
        "owner_path": obj.owner_path,
    }


def _reference_row(
    repo_prefix: str, path: str, ref: SqlObjectRefIR, resolution: SqlResolution
) -> dict[str, Any]:
    return {
        "key": _reference_key(repo_prefix, path, ref),
        "path": path,
        "artifact_ordinal": ref.artifact_ordinal,
        "statement_ordinal": ref.statement_ordinal,
        "ordinal": ref.ordinal,
        "raw_name": ref.raw_name,
        "database_name": ref.database_name,
        "schema_name": ref.schema_name,
        "object_name": ref.object_name,
        "object_kind_hint": ref.object_kind_hint,
        "signature_hint": ref.signature_hint,
        "status": resolution.status,
        "role": ref.role,
        "dynamic": ref.dynamic,
        "candidate_count": resolution.candidate_count,
        "candidate_keys_json": json.dumps(
            resolution.candidate_keys, ensure_ascii=False, separators=(",", ":")
        ),
        "start_line": ref.start_line,
        "start_column": ref.start_column,
        "end_line": ref.end_line,
        "end_column": ref.end_column,
    }


def _relationship(
    kind: str, start: str, end: str, properties: dict[str, Any], identity: str
) -> tuple[str, str, str, dict[str, Any], str]:
    return kind, start, end, properties, identity


def _file_key(repo_prefix: str, path: str) -> str:
    return f"{repo_prefix}:{path}"


def _artifact_key(repo_prefix: str, path: str, ordinal: int) -> str:
    return f"{_file_key(repo_prefix, path)}:sql-artifact:{ordinal}"


def _statement_key(repo_prefix: str, path: str, artifact: int, ordinal: int) -> str:
    return f"{_file_key(repo_prefix, path)}:sql-statement:{artifact}:{ordinal}"


def _reference_key(repo_prefix: str, path: str, ref: SqlObjectRefIR) -> str:
    return (
        f"{_file_key(repo_prefix, path)}:sql-reference:"
        f"{ref.artifact_ordinal}:{ref.statement_ordinal}:{ref.ordinal}"
    )
