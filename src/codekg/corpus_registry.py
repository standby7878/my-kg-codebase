"""Disk-backed native corpus fact extraction and identity validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from codekg.corpus_config import CorpusSnapshotConfig, effective_sql_config
from codekg.native_evidence import (
    parse_markdown_evidence,
    parse_python_sql,
    parse_sql_source_evidence,
)
from codekg.native_ir import NativeFileFacts
from codekg.native_parser import parse_native_source
from codekg.native_sql import parse_pg_proc_catalog, parse_routine_source

_SUFFIXES = {".py", ".sql", ".in", ".c", ".h", ".md", ".control"}
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class SnapshotIdentity:
    git_commit: str | None
    source_digest: str
    fingerprint: str
    revision: str


def git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return (
        value
        if len(value) == 40 and all(ch in "0123456789abcdef" for ch in value.lower())
        else None
    )


def selected_paths(snapshot: CorpusSnapshotConfig, output: Path) -> Iterator[Path]:
    excluded = {".git", ".venv", "build", "vendor", ".codekg-worktrees", ".codekg-corpus"}
    output_resolved = output.resolve()
    source_root = snapshot.path.resolve()
    sql_config = effective_sql_config(snapshot)
    for directory, names, filenames in os.walk(snapshot.path):
        current = Path(directory)
        names[:] = sorted(
            name
            for name in names
            if name not in excluded and not _inside((current / name).resolve(), output_resolved)
        )
        for filename in sorted(filenames):
            path = current / filename
            rel = path.relative_to(snapshot.path).as_posix()
            normalized_name = filename.lower()
            suffix = path.suffix.lower()
            if suffix not in _SUFFIXES and not normalized_name.endswith(
                (".control.in", "pg_proc.dat")
            ):
                continue
            is_control = normalized_name.endswith((".control", ".control.in"))
            if not is_control:
                is_sql = normalized_name.endswith((".sql", ".sql.in"))
                if suffix == ".in" and not normalized_name.endswith(".sql.in"):
                    continue
                if is_sql and (not sql_config.enabled or not sql_config.matches(rel)):
                    continue
            resolved = path.resolve()
            if not _inside(resolved, source_root) or _inside(resolved, output_resolved):
                continue
            yield path


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def source_digest(snapshot: CorpusSnapshotConfig, output: Path) -> str:
    digest = hashlib.sha256()
    for path in selected_paths(snapshot, output):
        rel = path.relative_to(snapshot.path).as_posix()
        encoded = rel.encode("utf-8")
        try:
            stat = path.stat()
            length = stat.st_size
            unreadable_identity = (stat.st_mode, stat.st_ino, stat.st_mtime_ns)
        except OSError as error:
            length = -1
            unreadable_identity = (type(error).__name__, getattr(error, "errno", None))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(length.to_bytes(8, "big", signed=True))
        try:
            with path.open("rb") as source:
                actual = 0
                for chunk in iter(lambda: source.read(_CHUNK), b""):
                    digest.update(chunk)
                    actual += len(chunk)
            digest.update(b"readable\0")
            digest.update(actual.to_bytes(8, "big"))
        except OSError as error:
            # A selected but unreadable file is still part of coverage. Hash a
            # stable metadata/error marker so becoming readable or changing its
            # observable identity during extraction invalidates the snapshot.
            marker = json.dumps(
                ("unreadable", unreadable_identity, type(error).__name__, error.errno),
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(marker)
    return digest.hexdigest()


def snapshot_identity(
    snapshot: CorpusSnapshotConfig,
    output: Path,
    *,
    dependency_identities: tuple[str, ...] = (),
) -> SnapshotIdentity:
    commit = git_commit(snapshot.path)
    digest = source_digest(snapshot, output)
    sql = repr(effective_sql_config(snapshot))
    versions = []
    for package in ("tree-sitter", "tree-sitter-c", "tree-sitter-language-pack", "pglast"):
        try:
            versions.append((package, metadata.version(package)))
        except metadata.PackageNotFoundError:
            versions.append((package, "absent"))
    fields = (
        snapshot.alias,
        snapshot.logical_repo,
        snapshot.version,
        snapshot.role,
        sql,
        ",".join(snapshot.dependencies),
        ",".join(dependency_identities),
        str(snapshot.max_file_bytes),
        "corpus-extract-v6",
        sys.version,
        repr(versions),
    )
    material = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(material).hexdigest()
    revision_material = json.dumps((commit, digest, fingerprint), separators=(",", ":")).encode()
    revision = hashlib.sha256(revision_material).hexdigest()
    return SnapshotIdentity(commit, digest, fingerprint, revision)


def create_native_registry(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA cache_size=-8192")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (
            snapshot_alias TEXT, path TEXT, language TEXT, loc INTEGER,
            PRIMARY KEY(snapshot_alias,path));
        CREATE TABLE IF NOT EXISTS symbols (
            snapshot_alias TEXT, path TEXT, ordinal INTEGER, fact TEXT,
            PRIMARY KEY(snapshot_alias,path,ordinal));
        CREATE TABLE IF NOT EXISTS calls (
            snapshot_alias TEXT, path TEXT, ordinal INTEGER, fact TEXT,
            PRIMARY KEY(snapshot_alias,path,ordinal));
        CREATE TABLE IF NOT EXISTS routines (
            snapshot_alias TEXT, path TEXT, ordinal INTEGER, fact TEXT,
            PRIMARY KEY(snapshot_alias,path,ordinal));
        CREATE TABLE IF NOT EXISTS evidence (
            snapshot_alias TEXT, path TEXT, ordinal INTEGER, fact TEXT,
            PRIMARY KEY(snapshot_alias,path,ordinal));
        CREATE TABLE IF NOT EXISTS diagnostics (
            snapshot_alias TEXT, path TEXT, ordinal INTEGER, fact TEXT,
            PRIMARY KEY(snapshot_alias,path,ordinal));
        CREATE TABLE IF NOT EXISTS edges (
            source_key TEXT NOT NULL, target_key TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, path TEXT, line INTEGER, column_no INTEGER,
            condition TEXT, ordinal INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source_key,target_key,kind,path,line,column_no,ordinal));
        CREATE TABLE IF NOT EXISTS fact_keys (
            key TEXT PRIMARY KEY, snapshot_alias TEXT NOT NULL, path TEXT NOT NULL,
            table_name TEXT NOT NULL, ordinal INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sqlobjects (
            snapshot_alias TEXT NOT NULL,key TEXT NOT NULL,schema_name TEXT NOT NULL,
            kind TEXT NOT NULL,object_name TEXT NOT NULL,signature TEXT,
            owner_path TEXT NOT NULL,PRIMARY KEY(snapshot_alias,key));
        CREATE TABLE IF NOT EXISTS python_owners (
            snapshot_alias TEXT NOT NULL,key TEXT NOT NULL,path TEXT NOT NULL,
            qname TEXT NOT NULL,start_line INTEGER NOT NULL,
            PRIMARY KEY(snapshot_alias,key));
        CREATE TABLE IF NOT EXISTS control_modules (
            snapshot_alias TEXT NOT NULL,path TEXT NOT NULL,control_name TEXT NOT NULL,
            module_path TEXT NOT NULL,
            PRIMARY KEY(snapshot_alias,path,module_path));
        CREATE INDEX IF NOT EXISTS idx_symbols_name
            ON symbols(snapshot_alias,json_extract(fact,'$.name'));
        CREATE INDEX IF NOT EXISTS idx_calls_name
            ON calls(snapshot_alias,json_extract(fact,'$.callee_name'));
        CREATE INDEX IF NOT EXISTS idx_routines_name
            ON routines(snapshot_alias,json_extract(fact,'$.name'));
        CREATE INDEX IF NOT EXISTS idx_evidence_name ON evidence(snapshot_alias, path);
        CREATE INDEX IF NOT EXISTS idx_evidence_callsite
            ON evidence(snapshot_alias,path,json_extract(fact,'$.origin'),
                        json_extract(fact,'$.start_line'),json_extract(fact,'$.start_column'));
        CREATE INDEX IF NOT EXISTS idx_evidence_owner
            ON evidence(snapshot_alias,path,json_extract(fact,'$.origin'),
                        json_extract(fact,'$.owner_qname'),json_extract(fact,'$.owner_line'));
        CREATE INDEX IF NOT EXISTS idx_symbols_owner
            ON symbols(snapshot_alias,path,json_extract(fact,'$.name'),
                       json_extract(fact,'$.start_line'));
        CREATE INDEX IF NOT EXISTS idx_python_owners_scope
            ON python_owners(snapshot_alias,path,start_line,qname);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_key,kind,status);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_key,kind,status);
        CREATE INDEX IF NOT EXISTS idx_fact_keys_occurrence
            ON fact_keys(snapshot_alias,path,table_name,ordinal);
        CREATE INDEX IF NOT EXISTS idx_routines_scope
            ON routines(snapshot_alias,json_extract(fact,'$.schema_name'),
                        json_extract(fact,'$.name'));
        CREATE INDEX IF NOT EXISTS idx_routines_overload
            ON routines(snapshot_alias,json_extract(fact,'$.schema_name'),
                        json_extract(fact,'$.name'),json_extract(fact,'$.arity'));
        CREATE INDEX IF NOT EXISTS idx_routines_owner
            ON routines(snapshot_alias,path,
                        (json_extract(fact,'$.schema_name') || '.' ||
                         json_extract(fact,'$.name')),
                        json_extract(fact,'$.start_line'));
        CREATE INDEX IF NOT EXISTS idx_symbols_scope
            ON symbols(snapshot_alias,json_extract(fact,'$.name'),json_extract(fact,'$.static'));
        CREATE INDEX IF NOT EXISTS idx_sqlobjects_identity
            ON sqlobjects(snapshot_alias,schema_name,object_name,kind,signature);
    """)
    return connection


def resolve_corpus_facts(connection: sqlite3.Connection, corpus) -> None:
    """Resolve only explicit dependency contexts; uncertain links remain candidates."""
    from codekg.corpus_config import CorpusConfig

    if not isinstance(corpus, CorpusConfig):
        raise TypeError("corpus must be a CorpusConfig")
    revisions = {
        row[0]: row[1]
        for row in connection.execute("SELECT key,value FROM metadata WHERE key LIKE '%.revision'")
    }
    for table in ("symbols", "calls", "routines", "evidence", "diagnostics"):
        for alias, path, ordinal in connection.execute(
            f"SELECT snapshot_alias,path,ordinal FROM {table} ORDER BY snapshot_alias,path,ordinal"
        ):
            key = _occurrence_key(
                alias, revisions.get(f"{alias}.revision", "unindexed"), path, table, ordinal
            )
            connection.execute(
                "INSERT OR REPLACE INTO fact_keys VALUES (?,?,?,?,?)",
                (key, alias, path, table, ordinal),
            )
    for snapshot in corpus.snapshots:
        visible = (snapshot.alias, *corpus.dependency_closure(snapshot.alias))
        placeholders = ",".join("?" for _ in visible)
        pg_binding_aliases = tuple(
            item.alias
            for item in corpus.snapshots
            if item.role == "postgres" and item.alias in visible
        )

        def node_key(alias: str, path: str, table: str, ordinal: int) -> str:
            row = connection.execute(
                "SELECT key FROM fact_keys WHERE snapshot_alias=? AND path=? "
                "AND table_name=? AND ordinal=?",
                (alias, path, table, ordinal),
            ).fetchone()
            if row is not None:
                return row[0]
            return _occurrence_key(
                alias, revisions.get(f"{alias}.revision", "unindexed"), path, table, ordinal
            )

        def add_diagnostic(alias: str, path: str, ordinal: int, diagnostic: dict) -> None:
            raw = json.dumps(diagnostic, sort_keys=True)
            connection.execute(
                "INSERT OR IGNORE INTO diagnostics VALUES (?,?,?,?)",
                (alias, path, ordinal, raw),
            )
            row = connection.execute(
                "SELECT ordinal,fact FROM diagnostics "
                "WHERE snapshot_alias=? AND path=? AND ordinal=?",
                (alias, path, ordinal),
            ).fetchone()
            if row is not None and row[1] == raw:
                key = _occurrence_key(
                    alias,
                    revisions.get(f"{alias}.revision", "unindexed"),
                    path,
                    "diagnostics",
                    row[0],
                )
                connection.execute(
                    "INSERT OR IGNORE INTO fact_keys VALUES (?,?,?,?,?)",
                    (key, alias, path, "diagnostics", row[0]),
                )

        for alias, path, ordinal, raw_fact in connection.execute(
            "SELECT snapshot_alias,path,ordinal,fact FROM evidence "
            "WHERE snapshot_alias=? ORDER BY path,ordinal",
            (snapshot.alias,),
        ):
            fact = json.loads(raw_fact)
            name = fact.get("object_name")
            origin = fact.get("origin")
            if (
                origin
                not in {
                    "python_execute",
                    "routine_body",
                    "sql_source",
                    "markdown_sql",
                    "markdown_mention",
                }
                or not name
                or fact.get("dynamic")
            ):
                continue
            schemas = (
                (fact["schema_name"],)
                if fact.get("schema_name")
                else effective_sql_config(snapshot).search_path
            )
            candidates = []
            for schema in schemas:
                arity_filter = ""
                params: list[object] = [*visible, name, schema]
                if fact.get("arity") is not None:
                    arity_filter = (
                        " AND (coalesce(json_extract(fact,'$.arity'),-1) - "
                        "coalesce(json_extract(fact,'$.default_arg_count'),0) - "
                        "coalesce(json_extract(fact,'$.variadic_arg_count'),0)) <= ? "
                        "AND (coalesce(json_extract(fact,'$.variadic_arg_count'),0)>0 "
                        "OR ? <= coalesce(json_extract(fact,'$.arity'),-1))"
                    )
                    params.extend((fact["arity"], fact["arity"]))
                params.append(snapshot.alias)
                scoped = connection.execute(
                    f"WITH occurrences AS (SELECT snapshot_alias,path,ordinal,fact,"
                    "ROW_NUMBER() OVER (PARTITION BY snapshot_alias,"
                    "json_extract(fact,'$.schema_name'),json_extract(fact,'$.name'),"
                    "json_extract(fact,'$.signature'),json_extract(fact,'$.kind'),"
                    "json_extract(fact,'$.language'),json_extract(fact,'$.return_type'),"
                    "json_extract(fact,'$.library'),json_extract(fact,'$.entrypoint'),"
                    "json_extract(fact,'$.default_arg_count'),"
                    "json_extract(fact,'$.variadic_arg_count'),"
                    "json_extract(fact,'$.body_hash'),"
                    "json_extract(fact,'$.definition_hash'),json_extract(fact,'$.condition') "
                    "ORDER BY path,ordinal) AS occurrence_rank FROM routines "
                    f"WHERE snapshot_alias IN ({placeholders}) "
                    "AND json_extract(fact,'$.name')=? "
                    "AND json_extract(fact,'$.schema_name')=? "
                    f"{arity_filter} "
                    "), unique_candidates AS (SELECT snapshot_alias,path,ordinal,fact "
                    "FROM occurrences WHERE occurrence_rank=1), preferred AS ("
                    "SELECT *,CASE WHEN snapshot_alias=? THEN 0 ELSE 1 END AS priority "
                    "FROM unique_candidates WHERE snapshot_alias=? OR NOT EXISTS ("
                    "SELECT 1 FROM unique_candidates WHERE snapshot_alias=?)) "
                    "SELECT snapshot_alias,path,ordinal,fact FROM preferred "
                    "ORDER BY priority,path,ordinal LIMIT 33",
                    [*params[:-1], snapshot.alias, snapshot.alias, snapshot.alias],
                ).fetchall()
                if scoped:
                    candidates = scoped
                    break
            source = node_key(alias, path, "evidence", ordinal)
            edge_kind = (
                "DOCUMENTS_ROUTINE"
                if origin in {"markdown_mention", "markdown_sql"}
                else "INVOKES_ROUTINE"
            )
            exact = len(candidates) == 1 and len(candidates) <= 32
            targets = candidates[:32]
            if exact:
                candidate = targets[0]
                routine = json.loads(candidate[3])
                target = node_key(candidate[0], candidate[1], "routines", candidate[2])
                guards = tuple(
                    dict.fromkeys(
                        value
                        for value in (fact.get("condition"), routine.get("condition"))
                        if value
                    )
                )
                status = "exact" if not guards else "conditional"
                _add_edge(
                    connection,
                    source,
                    target,
                    edge_kind,
                    status,
                    path,
                    fact.get("start_line"),
                    fact.get("start_column"),
                    " AND ".join(guards) if guards else None,
                )
            else:
                edge_kind = "ROUTINE_CANDIDATE"
                for candidate in targets:
                    routine = json.loads(candidate[3])
                    target = node_key(candidate[0], candidate[1], "routines", candidate[2])
                    _add_edge(
                        connection,
                        source,
                        target,
                        edge_kind,
                        "ambiguous" if targets else "unresolved",
                        path,
                        fact.get("start_line"),
                        fact.get("start_column"),
                        " AND ".join(
                            dict.fromkeys(
                                value
                                for value in (fact.get("condition"), routine.get("condition"))
                                if value
                            )
                        )
                        or None,
                    )
        for alias, path, routine_ordinal, raw_fact in connection.execute(
            "SELECT snapshot_alias,path,ordinal,fact FROM routines "
            "WHERE snapshot_alias=? ORDER BY path,ordinal",
            (snapshot.alias,),
        ):
            routine = json.loads(raw_fact)
            exact_objects = connection.execute(
                "SELECT key,signature FROM sqlobjects INDEXED BY idx_sqlobjects_identity "
                "WHERE snapshot_alias=? "
                "AND schema_name=? AND object_name=? AND kind=? AND signature=? "
                "ORDER BY key LIMIT 2",
                (
                    alias,
                    routine.get("schema_name"),
                    routine.get("name"),
                    routine.get("kind", "function"),
                    routine.get("signature"),
                ),
            ).fetchall()
            routine_key = node_key(alias, path, "routines", routine_ordinal)
            if len(exact_objects) == 1:
                _add_edge(
                    connection,
                    routine_key,
                    exact_objects[0][0],
                    "DESCRIBES_SQL_OBJECT",
                    "exact",
                    path,
                    routine.get("start_line"),
                    routine.get("start_column"),
                    None,
                )
            # SQL object identity is exact only after kind and signature filtering;
            # unrelated overloads must not consume a candidate cap.
            if routine.get("language") not in {"c", "internal"} or not routine.get("entrypoint"):
                continue
            if routine.get("language") == "internal":
                binding_aliases = pg_binding_aliases
                binding_allowed = bool(binding_aliases)
            else:
                control_name = _routine_control_name(path)
                modules = (
                    connection.execute(
                        "SELECT DISTINCT module_path FROM control_modules "
                        "WHERE snapshot_alias=? AND control_name=? ORDER BY module_path LIMIT 3",
                        (alias, control_name),
                    ).fetchall()
                    if control_name
                    else []
                )
                library = routine.get("library")
                if library == "MODULE_PATHNAME":
                    binding_allowed = len(modules) == 1 and not _is_module_placeholder(
                        modules[0][0]
                    )
                else:
                    binding_allowed = bool(library) and any(
                        module[0] == library and not _is_module_placeholder(module[0])
                        for module in modules
                    )
                unresolved_module = len(modules) != 1 or (
                    modules and _is_module_placeholder(modules[0][0])
                )
                if unresolved_module and library == "MODULE_PATHNAME":
                    diagnostic = {
                        "category": "unresolved_module_pathname",
                        "severity": "warning",
                        "line": routine.get("start_line"),
                        "column": routine.get("start_column"),
                        "message": (
                            "module_pathname could not be bound to a concrete library for "
                            f"control identity {control_name or '<unknown>'}"
                        ),
                    }
                    add_diagnostic(
                        alias,
                        path,
                        100000 + int(routine.get("start_line") or 0),
                        diagnostic,
                    )
                binding_aliases = (alias,)
            if binding_aliases:
                binding_placeholders = ",".join("?" for _ in binding_aliases)
                rows = connection.execute(
                    "SELECT snapshot_alias,path,ordinal,fact FROM symbols "
                    "INDEXED BY idx_symbols_scope "
                    f"WHERE snapshot_alias IN ({binding_placeholders}) "
                    "AND json_extract(fact,'$.name')=? AND json_extract(fact,'$.kind')='function' "
                    "AND json_extract(fact,'$.declaration')=0 "
                    "AND json_extract(fact,'$.static')=0 "
                    "ORDER BY path,ordinal LIMIT 33",
                    (*binding_aliases, routine["entrypoint"]),
                ).fetchall()
            else:
                rows = []
            if routine.get("language") == "internal" and not rows:
                diagnostic = {
                    "category": "internal_binding_unresolved",
                    "severity": "warning",
                    "line": routine.get("start_line"),
                    "column": routine.get("start_column"),
                    "message": (
                        "internal entrypoint could not be resolved in an explicit selected "
                        "PostgreSQL dependency"
                    ),
                }
                add_diagnostic(
                    alias, path, 110000 + int(routine.get("start_line") or 0), diagnostic
                )
            if len(rows) == 1 and binding_allowed:
                target_fact = json.loads(rows[0][3])
                source = node_key(alias, path, "routines", routine_ordinal)
                target = node_key(rows[0][0], rows[0][1], "symbols", rows[0][2])
                status = (
                    "exact"
                    if not routine.get("condition") and not target_fact.get("condition")
                    else "conditional"
                )
                guards = tuple(
                    dict.fromkeys(
                        value
                        for value in (routine.get("condition"), target_fact.get("condition"))
                        if value
                    )
                )
                _add_edge(
                    connection,
                    source,
                    target,
                    "BINDS_TO_NATIVE",
                    status,
                    path,
                    routine.get("start_line"),
                    routine.get("start_column"),
                    " AND ".join(guards) if guards else None,
                )
            elif rows:
                for row in rows[:32]:
                    target_fact = json.loads(row[3])
                    source = node_key(alias, path, "routines", routine_ordinal)
                    target = node_key(row[0], row[1], "symbols", row[2])
                    _add_edge(
                        connection,
                        source,
                        target,
                        "NATIVE_CANDIDATE",
                        "ambiguous" if len(rows) > 1 else "unresolved",
                        path,
                        routine.get("start_line"),
                        routine.get("start_column"),
                        " AND ".join(
                            dict.fromkeys(
                                value
                                for value in (
                                    routine.get("condition"),
                                    target_fact.get("condition"),
                                )
                                if value
                            )
                        )
                        or None,
                    )
        for _alias, path, _call_ordinal, raw_fact in connection.execute(
            "SELECT snapshot_alias,path,ordinal,fact FROM calls "
            "WHERE snapshot_alias=? ORDER BY path,ordinal",
            (snapshot.alias,),
        ):
            call = json.loads(raw_fact)
            callee = call.get("callee_name")
            if callee == "PG_FUNCTION_INFO_V1":
                continue
            owners = connection.execute(
                "SELECT ordinal FROM symbols WHERE snapshot_alias=? AND path=? "
                "AND json_extract(fact,'$.name')=? "
                "AND json_extract(fact,'$.start_line')=? "
                "AND json_extract(fact,'$.declaration')=0 LIMIT 2",
                (snapshot.alias, path, call.get("owner_name"), call.get("owner_start_line")),
            ).fetchall()
            if len(owners) != 1:
                diagnostic = {
                    "category": "native_call_owner_unresolved",
                    "severity": "warning",
                    "line": call.get("start_line"),
                    "column": call.get("start_column"),
                    "message": "native call owner is missing or ambiguous",
                }
                ordinal = connection.execute(
                    "SELECT coalesce(max(ordinal),-1)+1 FROM diagnostics "
                    "WHERE snapshot_alias=? AND path=?",
                    (snapshot.alias, path),
                ).fetchone()[0]
                add_diagnostic(
                    snapshot.alias,
                    path,
                    ordinal,
                    diagnostic,
                )
                continue
            source = node_key(snapshot.alias, path, "symbols", owners[0][0])
            # _insert_facts appends native-call evidence after source evidence,
            # preserving a direct occurrence identity even for repeated calls
            # at the same source coordinate.
            evidence_ordinal = call.get("evidence_ordinal")
            evidence_source = (
                node_key(snapshot.alias, path, "evidence", evidence_ordinal)
                if isinstance(evidence_ordinal, int)
                else None
            )
            if evidence_source is None:
                # Compatibility for older/manual registry fixtures without the
                # carried ordinal. This is bounded by the composite callsite index.
                legacy = connection.execute(
                    "SELECT ordinal FROM evidence INDEXED BY idx_evidence_callsite "
                    "WHERE snapshot_alias=? AND path=? "
                    "AND json_extract(fact,'$.origin')='native_call' "
                    "AND json_extract(fact,'$.start_line')=? "
                    "AND json_extract(fact,'$.start_column')=? ORDER BY ordinal LIMIT 2",
                    (snapshot.alias, path, call.get("start_line"), call.get("start_column")),
                ).fetchall()
                if len(legacy) == 1:
                    evidence_source = node_key(snapshot.alias, path, "evidence", legacy[0][0])
            if not callee or call.get("dynamic"):
                # The dynamic call is retained as a SourceEvidence node, but
                # has no asserted target endpoint.
                continue
            rows = connection.execute(
                f"WITH matches AS (SELECT snapshot_alias,path,ordinal,fact,"
                "CASE WHEN snapshot_alias=? AND path=? "
                "AND coalesce(json_extract(fact,'$.static'),0)=1 THEN 0 "
                "WHEN snapshot_alias=? AND coalesce(json_extract(fact,'$.static'),0)=0 THEN 1 "
                "WHEN snapshot_alias<>? AND coalesce(json_extract(fact,'$.static'),0)=0 THEN 2 "
                "ELSE 9 END priority, "
                "ROW_NUMBER() OVER (PARTITION BY snapshot_alias,"
                "json_extract(fact,'$.name'),json_extract(fact,'$.kind'),"
                "json_extract(fact,'$.signature'),json_extract(fact,'$.condition'),"
                "json_extract(fact,'$.body_hash'),json_extract(fact,'$.static') "
                "ORDER BY path,ordinal) occurrence_rank FROM symbols "
                f"WHERE snapshot_alias IN ({placeholders}) "
                "AND json_extract(fact,'$.name')=? "
                "AND json_extract(fact,'$.kind')='function' "
                "AND json_extract(fact,'$.declaration')=0 "
                "AND (coalesce(json_extract(fact,'$.static'),0)=0 "
                "OR (snapshot_alias=? AND path=?)) "
                "), grouped AS (SELECT * FROM matches WHERE occurrence_rank=1), "
                "preferred AS (SELECT * FROM grouped WHERE priority=("
                "SELECT min(priority) FROM grouped)) "
                "SELECT snapshot_alias,path,ordinal,fact FROM preferred "
                "ORDER BY path,ordinal LIMIT 33",
                (
                    snapshot.alias,
                    path,
                    snapshot.alias,
                    snapshot.alias,
                    *visible,
                    callee,
                    snapshot.alias,
                    path,
                ),
            ).fetchall()
            macros = connection.execute(
                f"SELECT snapshot_alias,path,ordinal,fact FROM symbols "
                f"WHERE snapshot_alias IN ({placeholders}) "
                "AND json_extract(fact,'$.name')=? AND json_extract(fact,'$.kind')='macro' "
                "ORDER BY CASE WHEN snapshot_alias=? AND path=? THEN 0 "
                "WHEN snapshot_alias=? THEN 1 ELSE 2 END,path,ordinal LIMIT 33",
                (*visible, callee, snapshot.alias, path, snapshot.alias),
            ).fetchall()
            for row in rows[:32]:
                target_fact = json.loads(row[3])
                target = node_key(row[0], row[1], "symbols", row[2])
                exact = (
                    not macros
                    and len(rows) == 1
                    and not call.get("condition")
                    and not target_fact.get("condition")
                )
                guards = tuple(
                    dict.fromkeys(
                        value
                        for value in (call.get("condition"), target_fact.get("condition"))
                        if value
                    )
                )
                status = (
                    "exact"
                    if exact
                    else ("conditional" if len(rows) == 1 and guards else "ambiguous")
                )
                _add_edge(
                    connection,
                    source,
                    target,
                    "CALLS_NATIVE" if exact else "NATIVE_CANDIDATE",
                    status,
                    path,
                    call.get("start_line"),
                    call.get("start_column"),
                    " AND ".join(guards) if guards else None,
                )
                if evidence_source is not None:
                    _add_edge(
                        connection,
                        evidence_source,
                        target,
                        "CALLS_NATIVE" if exact else "NATIVE_CANDIDATE",
                        status,
                        path,
                        call.get("start_line"),
                        call.get("start_column"),
                        " AND ".join(guards) if guards else None,
                    )
            if macros and not rows:
                add_diagnostic(
                    snapshot.alias,
                    path,
                    200000
                    + int(call.get("start_line") or 0) * 1000
                    + int(call.get("start_column") or 0),
                    {
                        "category": "native_macro_resolution_uncertain",
                        "severity": "warning",
                        "line": call.get("start_line"),
                        "column": call.get("start_column"),
                        "message": (
                            f"call to {callee} may be affected by a visible macro; "
                            "no executable function target was found"
                        ),
                    },
                )
    connection.commit()


def _add_edge(connection, source, target, kind, status, path, line, column, condition) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
        (source, target, kind, status, path, line, column, condition, 0),
    )


def _occurrence_key(alias: str, revision: str, path: str, table: str, ordinal: int) -> str:
    """Opaque stable ID for one fact occurrence; source text is never embedded."""
    material = json.dumps(
        (alias, revision, path, table, ordinal), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "fact:" + hashlib.sha256(material).hexdigest()


def extract_snapshot_facts(
    connection: sqlite3.Connection,
    snapshot: CorpusSnapshotConfig,
    output: Path,
    *,
    on_source=None,
) -> dict[str, int]:
    """Parse selected files sequentially; retain only normalized facts on disk."""
    counts = {
        "files": 0,
        "symbols": 0,
        "routines": 0,
        "evidence": 0,
        "diagnostics": 0,
        "oversized": 0,
    }
    config = effective_sql_config(snapshot)
    for file_path in selected_paths(snapshot, output):
        relative = file_path.relative_to(snapshot.path).as_posix()
        counts["files"] += 1
        normalized_name = file_path.name.lower()
        suffix = file_path.suffix.lower()
        language = (
            "sql"
            if normalized_name.endswith(".sql.in")
            else {
                ".py": "python",
                ".sql": "sql",
                ".in": "unknown",
                ".c": "c",
                ".h": "c_header",
                ".md": "markdown",
                ".dat": "catalog",
                ".control": "control",
            }.get(
                suffix,
                "control" if normalized_name.endswith((".control", ".control.in")) else "unknown",
            )
        )
        if normalized_name.endswith((".control", ".control.in")):
            language = "control"
        raw = None
        connection.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
            (snapshot.alias, relative, language, 0),
        )
        try:
            if file_path.stat().st_size > snapshot.max_file_bytes:
                counts["oversized"] += 1
                if on_source is not None:
                    on_source(file_path, None, language, "file_too_large")
                _insert_facts(connection, snapshot.alias, relative, NativeFileFacts(diagnostics=()))
                connection.execute(
                    "INSERT INTO diagnostics VALUES (?,?,?,?)",
                    (
                        snapshot.alias,
                        relative,
                        0,
                        json.dumps(
                            {
                                "category": "file_too_large",
                                "severity": "warning",
                                "line": None,
                                "column": None,
                                "message": (
                                    f"source exceeds {snapshot.max_file_bytes} byte corpus limit"
                                ),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                counts["diagnostics"] += 1
                continue
            with file_path.open("rb") as source:
                raw = source.read(snapshot.max_file_bytes + 1)
            if len(raw) > snapshot.max_file_bytes:
                counts["oversized"] += 1
                if on_source is not None:
                    on_source(file_path, None, language, "file_too_large")
                _insert_facts(connection, snapshot.alias, relative, NativeFileFacts(diagnostics=()))
                connection.execute(
                    "INSERT INTO diagnostics VALUES (?,?,?,?)",
                    (
                        snapshot.alias,
                        relative,
                        0,
                        json.dumps(
                            {
                                "category": "file_too_large",
                                "severity": "warning",
                                "line": None,
                                "column": None,
                                "message": (
                                    f"source exceeds {snapshot.max_file_bytes} byte corpus limit"
                                ),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                counts["diagnostics"] += 1
                continue
        except OSError as error:
            connection.execute(
                "INSERT INTO diagnostics VALUES (?,?,?,?)",
                (
                    snapshot.alias,
                    relative,
                    0,
                    json.dumps(
                        {
                            "category": "unreadable_file",
                            "severity": "warning",
                            "line": None,
                            "column": None,
                            "message": str(error),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            counts["diagnostics"] += 1
            if on_source is not None:
                on_source(file_path, None, language, "unreadable_file")
            continue
        if on_source is not None:
            on_source(file_path, raw, language, None)
        connection.execute(
            "UPDATE files SET loc=? WHERE snapshot_alias=? AND path=?",
            (raw.count(b"\n"), snapshot.alias, relative),
        )
        facts = NativeFileFacts()
        suffix = file_path.suffix.lower()
        if suffix in {".c", ".h"}:
            facts = parse_native_source(raw, relative)
        elif file_path.name == "pg_proc.dat":
            facts = parse_pg_proc_catalog(raw, relative)
        elif normalized_name.endswith((".control", ".control.in")):
            module_path = _control_module_path(raw)
            if module_path:
                control_name = _control_name(relative)
                connection.execute(
                    "INSERT OR IGNORE INTO control_modules VALUES (?,?,?,?)",
                    (snapshot.alias, relative, control_name, module_path),
                )
        elif suffix == ".py":
            facts = parse_python_sql(raw, relative)
        elif suffix == ".md":
            facts = parse_markdown_evidence(raw, relative)
        elif suffix == ".sql" or normalized_name.endswith(".sql.in"):
            routines = parse_routine_source(raw, relative, config)
            sql_evidence = parse_sql_source_evidence(raw, relative)
            facts = NativeFileFacts(
                routines=routines.routines,
                evidence=(*routines.evidence, *sql_evidence.evidence),
                diagnostics=(*routines.diagnostics, *sql_evidence.diagnostics),
            )
        _insert_facts(connection, snapshot.alias, relative, facts)
        counts["symbols"] += len(facts.symbols)
        counts["routines"] += len(facts.routines)
        counts["evidence"] += len(facts.evidence)
        counts["evidence"] += len(facts.calls)
        counts["diagnostics"] += len(facts.diagnostics)
        if counts["files"] % 64 == 0:
            connection.commit()
    connection.commit()
    return counts


def _insert_facts(db: sqlite3.Connection, alias: str, path: str, facts: NativeFileFacts) -> None:
    for table, values in (
        ("symbols", facts.symbols),
        ("routines", facts.routines),
        ("evidence", facts.evidence),
        ("diagnostics", facts.diagnostics),
    ):
        db.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?)",
            (
                (alias, path, index, json.dumps(value.__dict__, sort_keys=True))
                for index, value in enumerate(values)
            ),
        )
    db.executemany(
        "INSERT INTO calls VALUES (?,?,?,?)",
        (
            (
                alias,
                path,
                index,
                json.dumps(
                    {**call.__dict__, "evidence_ordinal": len(facts.evidence) + index},
                    sort_keys=True,
                ),
            )
            for index, call in enumerate(facts.calls)
        ),
    )
    db.executemany(
        "INSERT INTO evidence VALUES (?,?,?,?)",
        (
            (
                alias,
                path,
                len(facts.evidence) + index,
                json.dumps(
                    {
                        "origin": "native_call",
                        "schema_name": None,
                        "object_name": call.callee_name or "<dynamic>",
                        "arity": None,
                        "owner_qname": call.owner_name,
                        "owner_line": call.owner_start_line,
                        "start_line": call.start_line,
                        "start_column": call.start_column,
                        "end_line": call.end_line,
                        "end_column": call.end_column,
                        "dynamic": call.dynamic,
                        "condition": call.condition,
                    },
                    sort_keys=True,
                ),
            )
            for index, call in enumerate(facts.calls)
        ),
    )


def _control_module_path(raw: bytes) -> str | None:
    """Read only the quoted `module_pathname` scalar; never evaluate control syntax."""
    source = raw.decode("utf-8", "replace")
    values = set()
    for line in source.splitlines():
        line = line.split("#", 1)[0]
        match = re.match(r"\s*module_pathname\s*=\s*(['\"])(.*?)\1\s*$", line, re.I)
        if match:
            values.add(match.group(2))
    return next(iter(values)) if len(values) == 1 else None


def _control_name(path: str) -> str:
    name = Path(path).name
    for suffix in (".control.in", ".control"):
        if name.endswith(suffix):
            return name[: -len(suffix)].lower()
    return ""


def _routine_control_name(path: str) -> str | None:
    """Infer the control-file identity from the SQL routine script basename."""
    name = Path(path).name.lower()
    for suffix in (".sql.in", ".sql"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        return None
    # Versioned upgrade scripts retain the extension's base identity.
    name = name.split("--", 1)[0]
    if name in {"rtpostgis", "raster", "postgis_raster"}:
        return "postgis_raster"
    if name == "postgis":
        return "postgis"
    return name


def _is_module_placeholder(value: str) -> bool:
    return bool(re.search(r"@[A-Za-z_][A-Za-z0-9_]*@|\$\{[^}]+\}", value))
