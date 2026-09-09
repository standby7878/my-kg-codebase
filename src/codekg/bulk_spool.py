"""Lossless normalized SQLite staging for bounded sharded bulk exports."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
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
from codekg.sql_ir import SqlArtifactIR, SqlObjectRefIR, SqlStatementIR

SPOOL_SCHEMA_VERSION = 3
_LEGACY_SPOOL_SCHEMA_VERSION = 1
_WRITE_BATCH_SIZE = 64
_SQLITE_CACHE_KIB = -8192


def create_spool(path: Path, files: Iterable[FileIR]) -> None:
    """Write a complete, atomically-published normalized extraction spool."""

    partial = path.with_suffix(path.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(partial)
    try:
        _configure(connection)
        _create_normalized_schema(connection, include_symbol_keys=False)
        connection.execute(
            "INSERT INTO metadata VALUES ('schema_version', ?)",
            (str(SPOOL_SCHEMA_VERSION),),
        )
        for ordinal, file in enumerate(files):
            _insert_file(connection, file, ordinal)
            if (ordinal + 1) % _WRITE_BATCH_SIZE == 0:
                connection.commit()
        connection.commit()
    finally:
        connection.close()
    partial.replace(path)


def iter_spool_files(path: Path) -> Iterator[FileIR]:
    """Yield losslessly reconstructed files in source order.

    Version 1 spools are supported as a read-only compatibility path.  New
    spools never contain a serialized ``FileIR`` payload.
    """

    connection = _readonly(path)
    try:
        version = _schema_version(connection)
        if version == _LEGACY_SPOOL_SCHEMA_VERSION:
            yield from _iter_legacy_spool_files(connection)
            return
        if version != SPOOL_SCHEMA_VERSION:
            raise ValueError(f"unsupported spool schema: {version!r}")
        for (path_value,) in connection.execute("SELECT path FROM files ORDER BY ordinal"):
            yield _file_from_normalized(connection, str(path_value))
    finally:
        connection.close()


def build_registry(path: Path, spool_paths: Iterable[Path], *, repo_prefix: str) -> None:
    """Build the global, read-only-after-build resolver registry.

    Each source spool is attached for one bounded SQL copy transaction.  The
    source is detached only after that transaction has committed, so registry
    construction never materializes a ``FileIR`` or crosses spool boundaries
    in Python.
    """

    partial = path.with_suffix(path.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(partial)
    attached = False
    try:
        _configure(connection)
        _create_normalized_schema(connection, include_symbol_keys=True)
        connection.execute(
            "INSERT INTO metadata VALUES ('schema_version', ?)",
            (str(SPOOL_SCHEMA_VERSION),),
        )
        file_ordinal = 0
        for spool_path in spool_paths:
            source_path = Path(spool_path).resolve()
            connection.execute("ATTACH DATABASE ? AS spool", (str(source_path),))
            attached = True
            try:
                version = _schema_version(connection, database="spool")
                if version != SPOOL_SCHEMA_VERSION:
                    raise ValueError(
                        "registry build requires normalized v3 spools, "
                        f"got {version!r} from {source_path}"
                    )
                file_ordinal += _copy_attached_spool(connection, repo_prefix, file_ordinal)
                # SQLite requires a transaction boundary before DETACH.  It
                # also bounds rollback and dirty-page retention per spool.
                connection.commit()
                connection.execute("DETACH DATABASE spool")
                attached = False
            except Exception:
                connection.rollback()
                if attached:
                    connection.execute("DETACH DATABASE spool")
                    attached = False
                raise
        # SQL globals are grouped only after every attached spool has been
        # copied.  The builder operates on normalized SQLite rows and leaves
        # source occurrences in sqlrefs intact for later projection.
        from codekg.sql_resolver import build_sql_registry

        build_sql_registry(connection, repo_prefix)
        _create_registry_indexes(connection)
        connection.commit()
    finally:
        if attached:
            connection.rollback()
            connection.execute("DETACH DATABASE spool")
        connection.close()
    partial.replace(path)


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute(f"PRAGMA cache_size={_SQLITE_CACHE_KIB}")
    connection.execute("PRAGMA foreign_keys=OFF")


def _create_normalized_schema(connection: sqlite3.Connection, *, include_symbol_keys: bool) -> None:
    symbol_key = ", key TEXT PRIMARY KEY" if include_symbol_keys else ""
    connection.executescript(
        f"""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE files (
            ordinal INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            language TEXT NOT NULL,
            loc INTEGER NOT NULL,
            module_qname TEXT NOT NULL,
            parse_status TEXT NOT NULL
        );
        CREATE TABLE moduleinit (
            path TEXT PRIMARY KEY,
            qname TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL
        );
        CREATE TABLE symbols (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qname TEXT NOT NULL,
            signature TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            cyclomatic INTEGER NOT NULL,
            parent_qname TEXT,
            docstring TEXT,
            return_annotation TEXT{symbol_key}
        );
        CREATE TABLE imports (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            alias TEXT
        );
        CREATE TABLE inheritance (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            type_qname TEXT NOT NULL,
            base_name TEXT NOT NULL,
            base_qname TEXT
        );
        CREATE TABLE calls (
            path TEXT NOT NULL,
            row_ordinal INTEGER NOT NULL,
            owner_qname TEXT NOT NULL,
            raw_callee TEXT NOT NULL,
            callee_name TEXT,
            callee_qname_hint TEXT,
            receiver_kind TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            ordinal INTEGER NOT NULL
        );
        CREATE TABLE localbindings (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            owner_qname TEXT NOT NULL,
            target_name TEXT NOT NULL,
            value_kind TEXT NOT NULL,
            value_name TEXT,
            value_qname_hint TEXT,
            annotation TEXT,
            start_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            guarded INTEGER NOT NULL
        );
        CREATE TABLE diagnostics (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            line INTEGER,
            column INTEGER,
            message TEXT NOT NULL
        );
        CREATE TABLE sqlartifacts (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            origin TEXT NOT NULL,
            dialect TEXT NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            PRIMARY KEY (path, ordinal)
        );
        CREATE TABLE sqlstatements (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            artifact_ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL,
            parent_ordinal INTEGER,
            control_context TEXT,
            start_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            PRIMARY KEY (path, ordinal)
        );
        CREATE TABLE sqlrefs (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            artifact_ordinal INTEGER NOT NULL,
            statement_ordinal INTEGER NOT NULL,
            role TEXT NOT NULL,
            raw_name TEXT NOT NULL,
            database_name TEXT,
            schema_name TEXT,
            object_name TEXT,
            object_kind_hint TEXT,
            signature_hint TEXT,
            start_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            dynamic INTEGER NOT NULL,
            PRIMARY KEY (path, ordinal)
        );
        CREATE TABLE sqlref_search_path (
            path TEXT NOT NULL,
            ref_ordinal INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (path, ref_ordinal, ordinal)
        );
        """
    )


def _insert_file(connection: sqlite3.Connection, file: FileIR, ordinal: int) -> None:
    connection.execute(
        "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
        (ordinal, file.path, file.language, file.loc, file.module_qname, file.parse_status),
    )
    if file.module_init is not None:
        connection.execute(
            "INSERT INTO moduleinit VALUES (?, ?, ?, ?)",
            (
                file.path,
                file.module_init.qname,
                file.module_init.start_line,
                file.module_init.end_line,
            ),
        )
    connection.executemany(
        "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                index,
                value.kind,
                value.name,
                value.qname,
                value.signature,
                value.start_line,
                value.end_line,
                value.cyclomatic,
                value.parent_qname,
                value.docstring,
                value.return_annotation,
            )
            for index, value in enumerate(file.symbols)
        ),
    )
    connection.executemany(
        "INSERT INTO imports VALUES (?, ?, ?, ?, ?)",
        (
            (file.path, index, value.module, value.name, value.alias)
            for index, value in enumerate(file.imports)
        ),
    )
    connection.executemany(
        "INSERT INTO inheritance VALUES (?, ?, ?, ?, ?)",
        (
            (file.path, index, value.type_qname, value.base_name, value.base_qname)
            for index, value in enumerate(file.inheritance)
        ),
    )
    connection.executemany(
        "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                index,
                value.owner_qname,
                value.raw_callee,
                value.callee_name,
                value.callee_qname_hint,
                value.receiver_kind,
                value.start_line,
                value.start_column,
                value.end_line,
                value.end_column,
                value.ordinal,
            )
            for index, value in enumerate(file.calls)
        ),
    )
    connection.executemany(
        "INSERT INTO localbindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                index,
                value.owner_qname,
                value.target_name,
                value.value_kind,
                value.value_name,
                value.value_qname_hint,
                value.annotation,
                value.start_line,
                value.start_column,
                int(value.guarded),
            )
            for index, value in enumerate(file.local_bindings)
        ),
    )
    connection.executemany(
        "INSERT INTO diagnostics VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                index,
                value.category,
                value.severity,
                value.line,
                value.column,
                value.message,
            )
            for index, value in enumerate(file.diagnostics)
        ),
    )
    connection.executemany(
        "INSERT INTO sqlartifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                value.ordinal,
                value.origin,
                value.dialect,
                value.text,
                value.text_hash,
                value.start_line,
                value.start_column,
                value.end_line,
                value.end_column,
            )
            for value in file.sql_artifacts
        ),
    )
    connection.executemany(
        "INSERT INTO sqlstatements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                value.ordinal,
                value.artifact_ordinal,
                value.kind,
                value.parent_ordinal,
                value.control_context,
                value.start_line,
                value.start_column,
                value.end_line,
                value.end_column,
            )
            for value in file.sql_statements
        ),
    )
    connection.executemany(
        "INSERT INTO sqlrefs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                file.path,
                value.ordinal,
                value.artifact_ordinal,
                value.statement_ordinal,
                value.role,
                value.raw_name,
                value.database_name,
                value.schema_name,
                value.object_name,
                value.object_kind_hint,
                value.signature_hint,
                value.start_line,
                value.start_column,
                value.end_line,
                value.end_column,
                int(value.dynamic),
            )
            for value in file.sql_object_refs
        ),
    )
    connection.executemany(
        "INSERT INTO sqlref_search_path VALUES (?, ?, ?, ?)",
        (
            (file.path, value.ordinal, index, search_path)
            for value in file.sql_object_refs
            for index, search_path in enumerate(value.search_path)
        ),
    )


def _copy_attached_spool(connection: sqlite3.Connection, repo_prefix: str, file_offset: int) -> int:
    copied = int(connection.execute("SELECT count(*) FROM spool.files").fetchone()[0])
    connection.execute(
        "INSERT INTO files (ordinal, path, language, loc, module_qname, parse_status) "
        "SELECT ordinal + ?, path, language, loc, module_qname, parse_status FROM spool.files "
        "ORDER BY ordinal",
        (file_offset,),
    )
    connection.execute(
        "INSERT INTO moduleinit SELECT path, qname, start_line, end_line FROM spool.moduleinit"
    )
    connection.execute(
        """
        INSERT INTO symbols (
            path, ordinal, kind, name, qname, signature, start_line, end_line,
            cyclomatic, parent_qname, docstring, return_annotation, key
        )
        SELECT path, ordinal, kind, name, qname, signature, start_line, end_line,
               cyclomatic, parent_qname, docstring, return_annotation,
               ? || ':' || path || ':' || qname || ':' || start_line
        FROM spool.symbols
        """,
        (repo_prefix,),
    )
    for table, columns in (
        ("imports", "path, ordinal, module, name, alias"),
        ("inheritance", "path, ordinal, type_qname, base_name, base_qname"),
        (
            "calls",
            "path, row_ordinal, owner_qname, raw_callee, callee_name, "
            "callee_qname_hint, receiver_kind, start_line, start_column, "
            "end_line, end_column, ordinal",
        ),
        (
            "localbindings",
            "path, ordinal, owner_qname, target_name, value_kind, value_name, "
            "value_qname_hint, annotation, start_line, start_column, guarded",
        ),
        ("diagnostics", "path, ordinal, category, severity, line, column, message"),
        (
            "sqlartifacts",
            "path, ordinal, origin, dialect, text, text_hash, start_line, start_column, "
            "end_line, end_column",
        ),
        (
            "sqlstatements",
            "path, ordinal, artifact_ordinal, kind, parent_ordinal, control_context, "
            "start_line, start_column, end_line, end_column",
        ),
        (
            "sqlrefs",
            "path, ordinal, artifact_ordinal, statement_ordinal, role, raw_name, "
            "database_name, schema_name, object_name, object_kind_hint, signature_hint, "
            "start_line, start_column, end_line, end_column, dynamic",
        ),
        ("sqlref_search_path", "path, ref_ordinal, ordinal, value"),
    ):
        connection.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM spool.{table}")
    return copied


def _create_registry_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX symbols_qname_idx ON symbols(qname, key);
        CREATE INDEX symbols_method_idx
            ON symbols(parent_qname, qname, key) WHERE kind = 'method';
        CREATE INDEX imports_path_idx ON imports(path, ordinal);
        CREATE INDEX inheritance_type_idx ON inheritance(type_qname, ordinal);
        CREATE INDEX calls_path_idx ON calls(path, row_ordinal);
        CREATE INDEX localbindings_path_idx ON localbindings(path, ordinal);
        CREATE INDEX diagnostics_path_idx ON diagnostics(path, ordinal);
        CREATE INDEX sqlrefs_lookup_idx
            ON sqlrefs(database_name, schema_name, object_name, object_kind_hint, role);
        CREATE INDEX sqlstatements_artifact_idx
            ON sqlstatements(path, artifact_ordinal, ordinal);
        CREATE INDEX sqlref_search_path_idx
            ON sqlref_search_path(path, ref_ordinal, ordinal);
        """
    )


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    connection.execute(f"PRAGMA cache_size={_SQLITE_CACHE_KIB}")
    return connection


def _schema_version(connection: sqlite3.Connection, *, database: str = "main") -> int:
    row = connection.execute(
        f"SELECT value FROM {database}.metadata WHERE key = 'schema_version'"
    ).fetchone()
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _iter_legacy_spool_files(connection: sqlite3.Connection) -> Iterator[FileIR]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(files)")}
    if "payload" not in columns:
        raise ValueError("unsupported version 1 spool: files.payload is missing")
    for (payload,) in connection.execute("SELECT payload FROM files ORDER BY ordinal"):
        yield _file_from_payload(json.loads(payload))


def _file_from_normalized(connection: sqlite3.Connection, path: str) -> FileIR:
    row = connection.execute(
        "SELECT path, language, loc, module_qname, parse_status FROM files WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        raise KeyError(f"file not found in normalized database: {path}")
    module_row = connection.execute(
        "SELECT qname, start_line, end_line FROM moduleinit WHERE path = ?", (path,)
    ).fetchone()
    module_init = ModuleInitIR(*module_row) if module_row is not None else None
    diagnostics = tuple(
        ParseDiagnosticIR(category, severity, line, column, message)
        for category, severity, line, column, message in connection.execute(
            "SELECT category, severity, line, column, message FROM diagnostics "
            "WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    imports = tuple(
        ImportIR(module, name, alias)
        for module, name, alias in connection.execute(
            "SELECT module, name, alias FROM imports WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    symbols = tuple(
        SymbolIR(
            kind,
            name,
            qname,
            signature,
            start_line,
            end_line,
            cyclomatic,
            parent_qname,
            docstring,
            return_annotation,
        )
        for (
            kind,
            name,
            qname,
            signature,
            start_line,
            end_line,
            cyclomatic,
            parent_qname,
            docstring,
            return_annotation,
        ) in connection.execute(
            "SELECT kind, name, qname, signature, start_line, end_line, cyclomatic, "
            "parent_qname, docstring, return_annotation FROM symbols "
            "WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    inheritance = tuple(
        InheritanceIR(type_qname, base_name, base_qname)
        for type_qname, base_name, base_qname in connection.execute(
            "SELECT type_qname, base_name, base_qname FROM inheritance "
            "WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    calls = tuple(
        CallIR(
            owner_qname,
            raw_callee,
            callee_name,
            callee_qname_hint,
            receiver_kind,
            start_line,
            start_column,
            end_line,
            end_column,
            ordinal,
        )
        for (
            owner_qname,
            raw_callee,
            callee_name,
            callee_qname_hint,
            receiver_kind,
            start_line,
            start_column,
            end_line,
            end_column,
            ordinal,
        ) in connection.execute(
            "SELECT owner_qname, raw_callee, callee_name, callee_qname_hint, receiver_kind, "
            "start_line, start_column, end_line, end_column, ordinal FROM calls "
            "WHERE path = ? ORDER BY row_ordinal",
            (path,),
        )
    )
    local_bindings = tuple(
        LocalBindingIR(
            owner_qname,
            target_name,
            value_kind,
            value_name,
            value_qname_hint,
            annotation,
            start_line,
            start_column,
            bool(guarded),
        )
        for (
            owner_qname,
            target_name,
            value_kind,
            value_name,
            value_qname_hint,
            annotation,
            start_line,
            start_column,
            guarded,
        ) in connection.execute(
            "SELECT owner_qname, target_name, value_kind, value_name, value_qname_hint, "
            "annotation, start_line, start_column, guarded FROM localbindings "
            "WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    sql_artifacts = tuple(
        SqlArtifactIR(
            ordinal,
            origin,
            dialect,
            text,
            text_hash,
            start_line,
            start_column,
            end_line,
            end_column,
        )
        for (
            ordinal,
            origin,
            dialect,
            text,
            text_hash,
            start_line,
            start_column,
            end_line,
            end_column,
        ) in connection.execute(
            "SELECT ordinal, origin, dialect, text, text_hash, start_line, start_column, "
            "end_line, end_column FROM sqlartifacts WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    sql_statements = tuple(
        SqlStatementIR(
            artifact_ordinal,
            ordinal,
            kind,
            parent_ordinal,
            control_context,
            start_line,
            start_column,
            end_line,
            end_column,
        )
        for (
            ordinal,
            artifact_ordinal,
            kind,
            parent_ordinal,
            control_context,
            start_line,
            start_column,
            end_line,
            end_column,
        ) in connection.execute(
            "SELECT ordinal, artifact_ordinal, kind, parent_ordinal, control_context, "
            "start_line, start_column, end_line, end_column FROM sqlstatements "
            "WHERE path = ? ORDER BY ordinal",
            (path,),
        )
    )
    sql_object_refs = []
    for (
        ordinal,
        artifact_ordinal,
        statement_ordinal,
        role,
        raw_name,
        database_name,
        schema_name,
        object_name,
        object_kind_hint,
        signature_hint,
        start_line,
        start_column,
        end_line,
        end_column,
        dynamic,
    ) in connection.execute(
        "SELECT ordinal, artifact_ordinal, statement_ordinal, role, raw_name, "
        "database_name, schema_name, object_name, object_kind_hint, signature_hint, "
        "start_line, start_column, end_line, end_column, dynamic FROM sqlrefs "
        "WHERE path = ? ORDER BY ordinal",
        (path,),
    ):
        search_path = tuple(
            value
            for (value,) in connection.execute(
                "SELECT value FROM sqlref_search_path WHERE path = ? AND ref_ordinal = ? "
                "ORDER BY ordinal",
                (path, ordinal),
            )
        )
        sql_object_refs.append(
            SqlObjectRefIR(
                artifact_ordinal,
                statement_ordinal,
                ordinal,
                role,
                raw_name,
                database_name,
                schema_name,
                object_name,
                object_kind_hint,
                signature_hint,
                start_line,
                start_column,
                end_line,
                end_column,
                bool(dynamic),
                search_path,
            )
        )
    return FileIR(
        path=row[0],
        language=row[1],
        loc=row[2],
        module_qname=row[3],
        module_init=module_init,
        parse_status=row[4],
        diagnostics=diagnostics,
        imports=imports,
        symbols=symbols,
        inheritance=inheritance,
        calls=calls,
        local_bindings=local_bindings,
        sql_artifacts=sql_artifacts,
        sql_statements=sql_statements,
        sql_object_refs=tuple(sql_object_refs),
    )


def _file_from_payload(data: dict[str, object]) -> FileIR:
    """Read the v1 JSON payload format only for legacy spool compatibility."""

    init = data.get("module_init")
    return FileIR(
        path=str(data["path"]),
        language=str(data["language"]),
        loc=int(data["loc"]),
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
