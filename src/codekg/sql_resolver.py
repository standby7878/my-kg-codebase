"""SQLite-backed global registry and resolver for SQL source facts.

The registry is deliberately built from the normalized spool tables.  SQL
definitions remain source occurrences in ``sqlrefs``; ``sqlobjects`` is only
the global, deduplicated identity index used for graph projection.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codekg.sql_ir import SqlObjectRefIR

SQL_DATABASE_TABLE = "sqldatabases"
SQL_OBJECT_TABLE = "sqlobjects"
SQL_OBJECT_DEFINITION_TABLE = "sqlobject_definitions"
SQL_KNOWN_OBJECT_KINDS = frozenset(
    {
        "table",
        "view",
        "materialized_view",
        "sequence",
        "index",
        "statistics",
        "function",
        "procedure",
        "schema",
        "extension",
        "type",
    }
)
SQL_RELATION_KINDS = frozenset({"table", "view", "materialized_view"})
SQL_ROUTINE_KINDS = frozenset({"function", "procedure"})
SQL_DIRECT_KINDS = frozenset({"sequence", "index", "statistics", "schema", "extension", "type"})
SQL_MAX_CANDIDATES = 32
SQL_CACHE_LIMIT = 4096
_DEFAULT_DATABASE = "<default>"
_SCHEMALESS = "<schema-less>"


@dataclass(frozen=True)
class SqlDatabase:
    key: str
    database_name: str
    owner_path: str
    database_is_default: bool = False


@dataclass(frozen=True)
class SqlObject:
    key: str
    database_name: str
    schema_name: str
    kind: str
    object_name: str
    signature: str | None
    definition_count: int
    owner_path: str
    database_is_default: bool = False


@dataclass(frozen=True)
class SqlResolution:
    """Bounded conclusion for one SQL reference occurrence."""

    ref: SqlObjectRefIR
    status: str
    object_key: str | None = None
    candidate_count: int = 0
    candidate_keys: tuple[str, ...] = ()

    @property
    def target_key(self) -> str | None:
        return self.object_key

    @property
    def resolved_key(self) -> str | None:
        return self.object_key

    @property
    def is_exact(self) -> bool:
        return self.status == "exact" and self.object_key is not None


def _component(value: str | None) -> str:
    """Encode one identity component without delimiter collisions."""

    if value is None:
        return "n"
    return f"v{len(value)}:{value}"


def sql_object_key(
    repo_prefix: str,
    database_name: str | None,
    schema_name: str | None,
    kind: str,
    object_name: str,
    signature: str | None,
) -> str:
    """Return the stable, collision-free logical SQL object identity."""

    return ":".join(
        (
            f"{repo_prefix}:sql",
            _component(database_name),
            _component(schema_name),
            _component(kind),
            _component(object_name),
            _component(signature),
        )
    )


def sql_database_key(
    repo_prefix: str, database_name: str, database_is_default: bool = False
) -> str:
    value = None if database_is_default else database_name
    return f"{repo_prefix}:sql:database:{_component(value)}"


def _create_sql_registry_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {SQL_DATABASE_TABLE} (
            key TEXT PRIMARY KEY,
            database_name TEXT NOT NULL,
            database_is_default INTEGER NOT NULL,
            owner_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {SQL_OBJECT_TABLE} (
            key TEXT PRIMARY KEY,
            database_name TEXT NOT NULL,
            database_is_default INTEGER NOT NULL,
            schema_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_name TEXT NOT NULL,
            signature TEXT,
            definition_count INTEGER NOT NULL,
            owner_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {SQL_OBJECT_DEFINITION_TABLE} (
            path TEXT NOT NULL,
            artifact_ordinal INTEGER NOT NULL,
            statement_ordinal INTEGER NOT NULL,
            ref_ordinal INTEGER NOT NULL,
            object_key TEXT NOT NULL,
            PRIMARY KEY (path, artifact_ordinal, statement_ordinal, ref_ordinal)
        );
        """
    )


def build_sql_registry(connection: sqlite3.Connection, repo_prefix: str) -> None:
    """Build SQL globals after all normalized spool rows have been copied.

    Grouping, occurrence retention, and owner selection all happen in SQLite.
    The function intentionally does not load a repository or a list of files
    into Python.  Existing SQL registry tables are replaced so the operation
    is also safe for a retry on a private ``.partial`` registry.
    """

    _create_sql_registry_schema(connection)
    connection.execute(f"DELETE FROM {SQL_OBJECT_DEFINITION_TABLE}")
    connection.execute(f"DELETE FROM {SQL_OBJECT_TABLE}")
    connection.execute(f"DELETE FROM {SQL_DATABASE_TABLE}")

    connection.execute(
        f"""
        INSERT INTO {SQL_DATABASE_TABLE}
            (key, database_name, database_is_default, owner_path)
        SELECT
            ? || ':sql:database:' ||
            CASE WHEN database_is_default THEN 'n'
                 ELSE 'v' || length(database_name) || ':' || database_name END,
            database_name, database_is_default, MIN(path)
        FROM (
            SELECT COALESCE(NULLIF(database_name, ''), ?) AS database_name,
                   database_name IS NULL OR database_name = '' AS database_is_default,
                   path
            FROM sqlrefs
        )
        GROUP BY database_name, database_is_default
        ORDER BY database_name
        """,
        (repo_prefix, _DEFAULT_DATABASE),
    )

    # ``first_search_path`` is the parser's source-time search_path snapshot.
    # A normal definition with no explicit schema and no captured path is not
    # a globally identifiable object and is intentionally omitted.
    known_kinds = ",".join("?" for _ in SQL_KNOWN_OBJECT_KINDS)
    connection.execute(
        f"""
        INSERT INTO {SQL_OBJECT_TABLE} (
            key, database_name, database_is_default, schema_name, kind, object_name,
            signature, definition_count, owner_path
        )
        SELECT
            ? || ':sql:' ||
            CASE WHEN database_is_default THEN 'n'
                 ELSE 'v' || length(database_name) || ':' || database_name END || ':' ||
            CASE WHEN kind IN ('schema', 'extension') THEN 'n'
                 ELSE 'v' || length(schema_name) || ':' || schema_name END || ':' ||
            'v' || length(kind) || ':' || kind || ':' ||
            'v' || length(object_name) || ':' || object_name || ':' ||
            CASE WHEN signature IS NULL THEN 'n'
                 ELSE 'v' || length(signature) || ':' || signature END,
            database_name, database_is_default, schema_name, kind, object_name,
            signature, COUNT(*), MIN(path)
        FROM (
            SELECT
                COALESCE(NULLIF(r.database_name, ''), ?) AS database_name,
                r.database_name IS NULL OR r.database_name = '' AS database_is_default,
                CASE WHEN r.object_kind_hint IN ('schema', 'extension') THEN ?
                     ELSE COALESCE(
                        NULLIF(r.schema_name, ''),
                        (SELECT NULLIF(value, '') FROM sqlref_search_path AS p
                         WHERE p.path = r.path AND p.ref_ordinal = r.ordinal
                         ORDER BY p.ordinal LIMIT 1)
                     )
                END AS schema_name,
                r.object_kind_hint AS kind,
                r.object_name,
                r.signature_hint AS signature,
                r.path
            FROM sqlrefs AS r
            WHERE r.role = 'define'
              AND r.dynamic = 0
              AND r.object_name IS NOT NULL
              AND r.object_kind_hint IN ({known_kinds})
              AND (
                    r.object_kind_hint IN ('schema', 'extension')
                    OR NULLIF(r.schema_name, '') IS NOT NULL
                    OR EXISTS (SELECT 1 FROM sqlref_search_path AS sp
                               WHERE sp.path = r.path AND sp.ref_ordinal = r.ordinal
                                 AND NULLIF(sp.value, '') IS NOT NULL)
                  )
        )
        GROUP BY database_name, database_is_default, schema_name, kind, object_name, signature
        ORDER BY database_name, schema_name, kind, object_name, signature
        """,
        (repo_prefix, _DEFAULT_DATABASE, _SCHEMALESS, *sorted(SQL_KNOWN_OBJECT_KINDS)),
    )
    connection.execute(
        f"""
        INSERT INTO {SQL_OBJECT_DEFINITION_TABLE}
            (path, artifact_ordinal, statement_ordinal, ref_ordinal, object_key)
        SELECT r.path, r.artifact_ordinal, r.statement_ordinal, r.ordinal, o.key
        FROM sqlrefs AS r
        JOIN {SQL_OBJECT_TABLE} AS o
          ON o.database_name = COALESCE(NULLIF(r.database_name, ''), ?)
         AND o.database_is_default = (r.database_name IS NULL OR r.database_name = '')
         AND o.schema_name = CASE
                WHEN r.object_kind_hint IN ('schema', 'extension') THEN ?
                ELSE COALESCE(
                    NULLIF(r.schema_name, ''),
                    (SELECT NULLIF(value, '') FROM sqlref_search_path AS p
                     WHERE p.path = r.path AND p.ref_ordinal = r.ordinal
                     ORDER BY p.ordinal LIMIT 1)
                )
             END
         AND o.kind = r.object_kind_hint
         AND o.object_name = r.object_name
         AND (o.signature = r.signature_hint OR (o.signature IS NULL AND r.signature_hint IS NULL))
        WHERE r.role = 'define' AND r.dynamic = 0
        """,
        (_DEFAULT_DATABASE, _SCHEMALESS),
    )
    connection.executescript(
        f"""
        CREATE INDEX IF NOT EXISTS sqldatabases_owner_idx
            ON {SQL_DATABASE_TABLE}(owner_path, key);
        CREATE INDEX IF NOT EXISTS sqlobjects_lookup_idx
            ON {SQL_OBJECT_TABLE}(database_name, schema_name, kind, object_name, signature, key);
        CREATE INDEX IF NOT EXISTS sqlobjects_owner_idx
            ON {SQL_OBJECT_TABLE}(owner_path, key);
        CREATE INDEX IF NOT EXISTS sqlobject_definitions_object_idx
            ON {SQL_OBJECT_DEFINITION_TABLE}(
                object_key, path, artifact_ordinal, statement_ordinal, ref_ordinal
            );
        """
    )


def _row_object(row: tuple[Any, ...]) -> SqlObject:
    return SqlObject(
        key=str(row[0]),
        database_name=str(row[1]),
        schema_name=str(row[2]),
        kind=str(row[3]),
        object_name=str(row[4]),
        signature=str(row[5]) if row[5] is not None else None,
        definition_count=int(row[6]),
        owner_path=str(row[7]),
        database_is_default=bool(row[8]),
    )


def _row_database(row: tuple[Any, ...]) -> SqlDatabase:
    return SqlDatabase(str(row[0]), str(row[1]), str(row[3]), bool(row[2]))


class SqliteSqlResolverIndex:
    """Bounded, read-only SQL lookup over a published registry.

    ``source`` can be a registry path or an existing SQLite connection.  A
    connection passed by another index is borrowed and is not closed by this
    wrapper; this makes direct sharded projection tests cheap and avoids
    competing immutable handles.
    """

    def __init__(
        self,
        source: str | Path | sqlite3.Connection | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if source is not None and connection is not None:
            raise TypeError("pass either source or connection, not both")
        source = connection if connection is not None else source
        if source is None:
            raise TypeError("a registry path or sqlite3.Connection is required")
        self._owns_connection = not isinstance(source, sqlite3.Connection)
        self.connection = (
            sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro&immutable=1", uri=True)
            if self._owns_connection
            else source
        )
        self.connection.execute("PRAGMA cache_size=-32768")
        self._resolution_cache: OrderedDict[tuple[Any, ...], SqlResolution] = OrderedDict()

    @classmethod
    def from_backend(cls, backend: object) -> SqliteSqlResolverIndex:
        connection = getattr(backend, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("backend does not expose a sqlite3.Connection")
        return cls(connection)

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def databases(self, *, owner_path: str | None = None) -> Iterator[SqlDatabase]:
        query = (
            f"SELECT key, database_name, database_is_default, owner_path FROM {SQL_DATABASE_TABLE}"
        )
        params: tuple[Any, ...] = ()
        if owner_path is not None:
            query += " WHERE owner_path = ?"
            params = (owner_path,)
        query += " ORDER BY key"
        for row in self.connection.execute(query, params):
            yield _row_database(row)

    def iter_databases(self, *, owner_path: str | None = None) -> Iterator[SqlDatabase]:
        return self.databases(owner_path=owner_path)

    def databases_owned_by(self, path: str) -> Iterator[SqlDatabase]:
        return self.databases(owner_path=path)

    def iter_databases_owned_by(self, path: str) -> Iterator[SqlDatabase]:
        return self.databases_owned_by(path)

    def objects(
        self,
        *,
        database_name: str | None = None,
        schema_name: str | None = None,
        kind: str | None = None,
        object_name: str | None = None,
        owner_path: str | None = None,
    ) -> Iterator[SqlObject]:
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("database_name", database_name),
            ("schema_name", schema_name),
            ("kind", kind),
            ("object_name", object_name),
            ("owner_path", owner_path),
        ):
            if value is not None:
                filters.append(f"{column} = ?")
                params.append(value)
        query = (
            f"SELECT key, database_name, schema_name, kind, object_name, signature, "
            f"definition_count, owner_path, database_is_default FROM {SQL_OBJECT_TABLE}"
        )
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY key"
        for row in self.connection.execute(query, tuple(params)):
            yield _row_object(row)

    def iter_objects(self, **filters: str) -> Iterator[SqlObject]:
        return self.objects(**filters)

    def objects_owned_by(self, path: str) -> Iterator[SqlObject]:
        return self.objects(owner_path=path)

    def iter_objects_owned_by(self, path: str) -> Iterator[SqlObject]:
        return self.objects_owned_by(path)

    def resolve(self, ref: SqlObjectRefIR) -> SqlResolution:
        cache_key = (
            ref.role,
            ref.raw_name,
            ref.database_name,
            ref.schema_name,
            ref.object_name,
            ref.object_kind_hint,
            ref.signature_hint,
            ref.dynamic,
            ref.search_path,
        )
        cached = self._resolution_cache.get(cache_key)
        if cached is not None:
            self._resolution_cache.move_to_end(cache_key)
            return replace(cached, ref=ref)
        result = self._resolve_uncached(ref)
        self._resolution_cache[cache_key] = result
        self._resolution_cache.move_to_end(cache_key)
        if len(self._resolution_cache) > SQL_CACHE_LIMIT:
            self._resolution_cache.popitem(last=False)
        return result

    def _resolve_uncached(self, ref: SqlObjectRefIR) -> SqlResolution:
        if ref.dynamic:
            return SqlResolution(ref, "dynamic")
        if ref.object_name is None or ref.object_kind_hint not in (
            SQL_KNOWN_OBJECT_KINDS | {"routine"}
        ):
            return SqlResolution(ref, "unresolved")
        database = ref.database_name or _DEFAULT_DATABASE
        database_is_default = ref.database_name is None
        kind = ref.object_kind_hint
        if ref.role == "define":
            # PostgreSQL schema and extension names live in the database
            # namespace, not in a schema.  Their parser search path is not a
            # definition namespace.
            schema = (
                _SCHEMALESS
                if kind in {"schema", "extension"}
                else (ref.schema_name or _first_path(ref))
            )
            if schema is None and kind not in {"schema", "extension"}:
                return SqlResolution(ref, "unresolved")
            schema = schema or _SCHEMALESS
            candidates = self._query_candidates(
                database,
                schema,
                kind,
                ref.object_name,
                ref.signature_hint,
                strict_signature=True,
                database_is_default=database_is_default,
            )
            return self._conclude(ref, candidates, definition=True)

        kinds = _compatible_kinds(ref)
        if not kinds:
            return SqlResolution(ref, "unresolved")
        if kinds <= SQL_DIRECT_KINDS:
            schemas = (
                (ref.schema_name,)
                if ref.schema_name
                else (
                    (_SCHEMALESS,)
                    if ref.object_kind_hint in {"schema", "extension"}
                    else ref.search_path
                )
            )
            if not schemas:
                return SqlResolution(ref, "unresolved")
            for schema in schemas:
                candidates = self._query_candidates(
                    database,
                    schema,
                    ref.object_kind_hint,
                    ref.object_name,
                    ref.signature_hint,
                    kinds=kinds,
                    database_is_default=database_is_default,
                )
                if candidates[0] == 0:
                    continue
                return self._conclude(ref, candidates)
            return SqlResolution(ref, "unresolved")
        schemas = (ref.schema_name,) if ref.schema_name else ref.search_path
        if not schemas:
            return SqlResolution(ref, "unresolved")
        if kinds <= SQL_ROUTINE_KINDS:
            safe = ref.signature_hint is not None and _safe_signature(ref.signature_hint)
            if safe:
                for schema in schemas:
                    candidates = self._query_candidates(
                        database,
                        schema,
                        ref.object_kind_hint,
                        ref.object_name,
                        ref.signature_hint,
                        kinds=kinds,
                        database_is_default=database_is_default,
                    )
                    if candidates[0]:
                        return self._conclude(ref, candidates, allow_exact=True)
                return SqlResolution(ref, "unresolved")
            candidates = self._query_candidates_schemas(
                database,
                schemas,
                kinds,
                ref.object_name,
                ref.signature_hint,
                database_is_default=database_is_default,
            )
            return self._conclude(ref, candidates, allow_exact=False)
        for schema in schemas:
            candidates = self._query_candidates(
                database,
                schema,
                kind,
                ref.object_name,
                ref.signature_hint,
                kinds=kinds,
                database_is_default=database_is_default,
            )
            if candidates[0] == 0:
                continue
            # The first visible schema wins, including an ambiguity there;
            # later schemas must not make a shadowed relation look exact.
            return self._conclude(ref, candidates)
        return SqlResolution(ref, "unresolved")

    def _query_candidates(
        self,
        database: str,
        schema: str,
        kind: str,
        object_name: str,
        signature: str | None,
        *,
        kinds: frozenset[str] | None = None,
        strict_signature: bool = False,
        database_is_default: bool = False,
    ) -> tuple[int, tuple[str, ...]]:
        values = sorted(kinds or {kind})
        placeholders = ",".join("?" for _ in values)
        params: list[Any] = [database, int(database_is_default), schema, *values, object_name]
        where = (
            f"database_name = ? AND database_is_default = ? AND schema_name = ? "
            f"AND kind IN ({placeholders}) "
            "AND object_name = ?"
        )
        if strict_signature:
            if signature is None:
                where += " AND signature IS NULL"
            else:
                where += " AND signature = ?"
                params.append(signature)
        # A signature containing unknown types is not safe for exact routine
        # matching.  It is retained in the reference but resolved by bounded
        # name candidates only.  This conservative behavior applies to calls,
        # not to definitions whose stored identity is authoritative.
        elif signature is not None and _safe_signature(signature):
            where += " AND signature = ?"
            params.append(signature)
        count = int(
            self.connection.execute(
                f"SELECT count(*) FROM {SQL_OBJECT_TABLE} WHERE {where}", tuple(params)
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            f"SELECT key FROM {SQL_OBJECT_TABLE} WHERE {where} ORDER BY key LIMIT ?",
            (*params, SQL_MAX_CANDIDATES),
        )
        return count, tuple(str(row[0]) for row in rows)

    def _query_candidates_schemas(
        self,
        database: str,
        schemas: tuple[str, ...],
        kinds: frozenset[str],
        object_name: str,
        signature: str | None,
        *,
        database_is_default: bool = False,
    ) -> tuple[int, tuple[str, ...]]:
        schema_placeholders = ",".join("?" for _ in schemas)
        kind_placeholders = ",".join("?" for _ in kinds)
        params: list[Any] = [
            database,
            int(database_is_default),
            *schemas,
            *sorted(kinds),
            object_name,
        ]
        where = (
            f"database_name = ? AND database_is_default = ? "
            f"AND schema_name IN ({schema_placeholders}) "
            f"AND kind IN ({kind_placeholders}) AND object_name = ?"
        )
        if signature is not None and _safe_signature(signature):
            where += " AND signature = ?"
            params.append(signature)
        count = int(
            self.connection.execute(
                f"SELECT count(*) FROM {SQL_OBJECT_TABLE} WHERE {where}", tuple(params)
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            f"SELECT key FROM {SQL_OBJECT_TABLE} WHERE {where} ORDER BY key LIMIT ?",
            (*params, SQL_MAX_CANDIDATES),
        )
        return count, tuple(str(row[0]) for row in rows)

    @staticmethod
    def _conclude(
        ref: SqlObjectRefIR,
        candidates: tuple[int, tuple[str, ...]],
        *,
        definition: bool = False,
        allow_exact: bool = True,
    ) -> SqlResolution:
        count, keys = candidates
        if count == 1 and allow_exact:
            return SqlResolution(ref, "exact", keys[0], count, keys)
        if count > 1:
            return SqlResolution(ref, "ambiguous", None, count, keys)
        return SqlResolution(ref, "unresolved", candidate_count=count, candidate_keys=keys)


def _first_path(ref: SqlObjectRefIR) -> str | None:
    return ref.search_path[0] if ref.search_path else None


def _compatible_kinds(ref: SqlObjectRefIR) -> frozenset[str]:
    kind = ref.object_kind_hint
    if kind in SQL_RELATION_KINDS:
        return SQL_RELATION_KINDS if kind == "table" else frozenset({kind})
    if kind in SQL_ROUTINE_KINDS:
        return frozenset({kind})
    if kind in SQL_DIRECT_KINDS:
        return frozenset({kind})
    if kind == "routine":
        return SQL_ROUTINE_KINDS
    return frozenset()


def _safe_signature(signature: str) -> bool:
    lowered = signature.lower()
    return "unknown" not in lowered and "?" not in signature
