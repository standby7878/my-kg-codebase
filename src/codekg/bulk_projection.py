"""Streaming projection of extraction spools into Neo4j CSV shards.

The projector consumes one ``FileIR`` at a time and uses the existing resolver
algorithm with ``SqliteResolverIndex``.  It intentionally does not call the
whole-repository ``_build_graph`` path.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codekg.bulk_export import (
    _NODE_COLUMNS,
    _REL_COLUMNS,
    BulkExport,
)
from codekg.bulk_spool import iter_spool_files
from codekg.csv_limits import serialized_csv_field_size_bytes, validate_csv_field_size
from codekg.ir import FileIR, RepositoryIR
from codekg.loader import _callsite_key, _candidate_qnames, _import_aliases, _key, _symbol_key
from codekg.resolver import ResolverIndex, SqliteResolverIndex, SymbolRef, _Resolver
from codekg.sql_graph import (
    iter_sql_file_nodes,
    iter_sql_file_relationships,
    iter_sql_global_nodes,
    iter_sql_global_relationships,
)
from codekg.sql_resolver import SqliteSqlResolverIndex


class ProjectionValidator:
    """Disk-backed node and relationship validation for a projection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        # This database is a private, rebuildable validation scratch file, not
        # the durable serving corpus. Avoid one fsync per small batch commit.
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA cache_size=-8192")
        # Keep SQLite's temporary sort structures disk-backed and bounded.
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.executescript(
            """
            CREATE TABLE nodes (key TEXT PRIMARY KEY, label TEXT NOT NULL);
            CREATE TABLE relationships (
                kind TEXT NOT NULL,
                identity TEXT NOT NULL,
                start_key TEXT NOT NULL,
                end_key TEXT NOT NULL,
                properties TEXT NOT NULL,
                PRIMARY KEY (kind, identity)
            );
            """
        )
        self._pending_writes = 0
        self._closed = False
        self.connection.commit()

    def _flush_if_needed(self) -> None:
        self._pending_writes += 1
        if self._pending_writes >= 1_000:
            self.connection.commit()
            self._pending_writes = 0

    def has_node(self, key: str) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM nodes WHERE key = ?", (key,)).fetchone()
            is not None
        )

    def node(self, label: str, key: str) -> bool:
        row = self.connection.execute("SELECT label FROM nodes WHERE key = ?", (key,)).fetchone()
        if row is not None:
            raise ValueError(f"duplicate node key: {key}")
        self.connection.execute("INSERT INTO nodes VALUES (?, ?)", (key, label))
        self._flush_if_needed()
        return True

    def global_node(self, label: str, key: str) -> bool:
        row = self.connection.execute("SELECT label FROM nodes WHERE key = ?", (key,)).fetchone()
        if row is not None:
            if row[0] != label:
                raise ValueError(f"duplicate node key: {key}")
            return False
        self.connection.execute("INSERT INTO nodes VALUES (?, ?)", (key, label))
        self._flush_if_needed()
        return True

    def relationship(
        self,
        kind: str,
        identity: str,
        start: str,
        end: str,
        properties: Mapping[str, Any] | None = None,
    ) -> bool:
        encoded = json.dumps(dict(properties or {}), sort_keys=True, separators=(",", ":"))
        previous = self.connection.execute(
            "SELECT start_key, end_key, properties FROM relationships "
            "WHERE kind = ? AND identity = ?",
            (kind, identity),
        ).fetchone()
        if previous is not None:
            if kind in {"IMPORTS", "INHERITS"} and previous == (start, end, encoded):
                return False
            raise ValueError(f"duplicate relationship key: {kind}:{identity}")
        self.connection.execute(
            "INSERT INTO relationships VALUES (?, ?, ?, ?, ?)",
            (kind, identity, start, end, encoded),
        )
        self._flush_if_needed()
        return True

    def close(self, *, validate_endpoints: bool = True) -> None:
        if self._closed:
            return
        try:
            self.connection.commit()
            self._pending_writes = 0
            if not validate_endpoints:
                return
            missing = self.connection.execute(
                """
                SELECT kind, start_key, end_key FROM relationships
                WHERE start_key NOT IN (SELECT key FROM nodes)
                   OR end_key NOT IN (SELECT key FROM nodes)
                LIMIT 1
                """
            ).fetchone()
            if missing:
                raise ValueError(
                    f"dangling {missing[0]} relationship endpoint: {missing[1]} -> {missing[2]}"
                )
        except Exception:
            self.connection.close()
            _remove_validation_scratch(self.path)
            raise
        finally:
            self.connection.close()
            self._closed = True

    def abort(self) -> None:
        """Close and discard this private validation scratch after a failed build."""
        if not self._closed:
            with suppress(sqlite3.Error):
                self.connection.close()
            self._closed = True
        _remove_validation_scratch(self.path)

    @classmethod
    def merge(cls, destination: Path, sources: Iterable[Path]) -> None:
        """Merge private partition validation DBs without Python key sets."""
        validator = cls(destination)
        try:
            for source_path in sources:
                source = sqlite3.connect(f"file:{Path(source_path).resolve()}?mode=ro", uri=True)
                try:
                    for key, label in source.execute("SELECT key, label FROM nodes ORDER BY key"):
                        validator.node(label, key)
                    for kind, identity, start, end, properties in source.execute(
                        "SELECT kind, identity, start_key, end_key, properties "
                        "FROM relationships ORDER BY kind, identity"
                    ):
                        previous = validator.connection.execute(
                            "SELECT start_key, end_key, properties FROM relationships "
                            "WHERE kind = ? AND identity = ?",
                            (kind, identity),
                        ).fetchone()
                        if previous is not None:
                            if kind in {"IMPORTS", "INHERITS"} and previous == (
                                start,
                                end,
                                properties,
                            ):
                                continue
                            raise ValueError(f"duplicate relationship key: {kind}:{identity}")
                        validator.connection.execute(
                            "INSERT INTO relationships VALUES (?, ?, ?, ?, ?)",
                            (kind, identity, start, end, properties),
                        )
                        validator._flush_if_needed()
                finally:
                    source.close()
                # A source validation DB is one bounded merge phase.
                validator.connection.commit()
                validator._pending_writes = 0
            validator.close()
        except Exception:
            validator.abort()
            raise


class ShardWriter:
    """Headerless CSV files for one deterministic projection partition."""

    def __init__(self, root: Path, partition: int, *, wide_defines: bool = False) -> None:
        self.root = Path(root)
        self.partition = partition
        self.wide_defines = wide_defines
        self._handles: dict[tuple[bool, str], Any] = {}
        self._writers: dict[tuple[bool, str], csv.writer] = {}
        self.paths: dict[tuple[bool, str], Path] = {}
        self.max_csv_field_size_bytes = 0

    def row(self, kind: str, values: Mapping[str, Any], *, node: bool) -> Path:
        key = (node, kind)
        directory = self.root / ("nodes" if node else "relationships") / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"part-{self.partition:06d}.csv"
        if key not in self._writers:
            handle = path.open("w", encoding="utf-8", newline="")
            self._handles[key] = handle
            self._writers[key] = csv.writer(handle, lineterminator="\n")
            self.paths[key] = path
        columns = _NODE_COLUMNS[kind] if node else _REL_COLUMNS[kind]
        if not node and kind == "DEFINES" and not self.wide_defines:
            from codekg.bulk_export import _PYTHON_DEFINES_COLUMNS

            columns = _PYTHON_DEFINES_COLUMNS
        values_out = [_csv_value(values.get(name)) for name, _ in columns]
        if node:
            values_out.append(kind)
        self.max_csv_field_size_bytes = max(
            self.max_csv_field_size_bytes,
            *(serialized_csv_field_size_bytes(value) for value in values_out),
        )
        validate_csv_field_size(self.max_csv_field_size_bytes)
        self._writers[key].writerow(values_out)
        return path

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()


@dataclass(frozen=True)
class ProjectionResult:
    """Result of a streaming projection, compatible with ``BulkExport`` use."""

    manifest_path: Path
    output_dir: Path
    node_files: Mapping[str, Path]
    relationship_files: Mapping[str, Path]
    counts: Mapping[str, int]


class _ModuleInitIndex(ResolverIndex):
    """Add the current file's module initializer to the SQLite index."""

    def __init__(self, backend: ResolverIndex, repo: RepositoryIR) -> None:
        self.backend = backend
        self.repo = repo
        self.current: FileIR | None = None

    def set_current(self, file: FileIR) -> None:
        self.current = file

    def owners(self, path: str, qname: str) -> tuple[SymbolRef, ...]:
        values = list(self.backend.owners(path, qname))
        file = self.current
        if (
            file is not None
            and file.path == path
            and file.module_init is not None
            and file.module_init.qname == qname
        ):
            values.append(
                SymbolRef(
                    key=_key(self.repo, f"{path}:module-init"),
                    qname=qname,
                    path=path,
                    kind="module_init",
                )
            )
        return tuple(sorted(values, key=lambda value: value.key))

    def callables(self, qname: str) -> tuple[SymbolRef, ...]:
        return self.backend.callables(qname)

    def types(self, qname: str) -> tuple[SymbolRef, ...]:
        return self.backend.types(qname)

    def file(self, path: str) -> FileIR | None:
        return self.backend.file(path)

    def files(self) -> Iterable[FileIR]:
        return self.backend.files()

    def base_state(self, type_qname: str):
        return self.backend.base_state(type_qname)

    def module_owner(self, language: str, module_qname: str) -> str | None:
        return self.backend.module_owner(language, module_qname)


def project_partition(
    repo: RepositoryIR,
    spool_paths: Iterable[Path] | None,
    registry: Path | SqliteResolverIndex,
    generation_dir: Path,
    *,
    partition: int = 0,
    validator: ProjectionValidator | None = None,
) -> dict[str, Any]:
    """Project assigned spools into headerless CSV shards.

    ``repo.files`` must be empty.  ``spool_paths`` is consumed lazily in
    deterministic path order; pass ``None`` to read files from ``registry``.
    A caller projecting several partitions can share one validator and close
    it after the final partition, allowing endpoint validation across shards.
    """

    if repo.files:
        raise ValueError("streaming projection requires an identity-only RepositoryIR")
    if partition < 0:
        raise ValueError("partition must be non-negative")
    output_dir = Path(generation_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    owns_registry = isinstance(registry, (str, Path))
    backend = SqliteResolverIndex(str(registry)) if owns_registry else registry
    owns_validator = validator is None
    validator = validator or ProjectionValidator(output_dir / ".projection-validation.sqlite")
    index = _ModuleInitIndex(backend, repo)
    resolver = _Resolver(index)
    sql_index = _sql_index_from_backend(backend)
    wide_defines = bool(
        sql_index is not None
        and sql_index.connection.execute("SELECT 1 FROM sqlobject_definitions LIMIT 1").fetchone()
    )
    writer = ShardWriter(output_dir, partition, wide_defines=wide_defines)
    counts: dict[str, int] = {}
    try:
        if partition == 0 and not validator.has_node(repo.repo_name):
            repository_row = {
                "key": repo.repo_name,
                "repo_name": repo.repo_name,
                "commit": repo.commit,
                "root_path": repo.root_path,
            }
            validator.node("Repository", repo.repo_name)
            writer.row("Repository", repository_row, node=True)
            _increment(counts, "nodes_Repository")
        for file in _iter_project_files(spool_paths, backend):
            index.set_current(file)
            _project_file(repo, file, resolver, writer, validator, counts, sql_index=sql_index)
        writer.close()
        validator.connection.commit()
    except Exception:
        writer.close()
        if owns_validator:
            validator.abort()
        raise
    finally:
        if owns_registry:
            backend.close()  # type: ignore[union-attr]
        if owns_validator:
            validator.close()
    return {
        "partition": partition,
        "files": dict(writer.paths),
        "counts": counts,
        "max_csv_field_size_bytes": writer.max_csv_field_size_bytes,
    }


def project_repository(
    repo: RepositoryIR,
    spool_paths: Iterable[Path] | None,
    registry: Path | SqliteResolverIndex,
    output_dir: Path,
    *,
    workers: int = 1,
) -> BulkExport:
    """Project disk-catalogued spools in bounded, deterministic partitions."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    output_dir = Path(output_dir)
    catalog_path = output_dir / ".building" / "projection-catalog.sqlite"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = sqlite3.connect(catalog_path)
    validation_paths: list[Path] = []
    try:
        catalog.execute(
            "CREATE TABLE spools (ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL, part INTEGER)"
        )
        for ordinal, path in enumerate(spool_paths or ()):
            catalog.execute(
                "INSERT INTO spools (ordinal, path) VALUES (?, ?)", (ordinal, str(path))
            )
        batch_count = int(catalog.execute("SELECT count(*) FROM spools").fetchone()[0])
        # The repository node is a valid export even when there are no source
        # files (for example, a Markdown-only repository).
        partition_count = max(1, min(workers * 2, batch_count))
        catalog.execute("UPDATE spools SET part = ordinal % ?", (partition_count,))
        catalog.commit()
        catalog.close()
        validation_paths = [
            output_dir / ".building" / f"projection-validation-{partition:06d}.sqlite"
            for partition in range(partition_count)
        ]

        pending = set()
        results: dict[int, dict[str, Any]] = {}
        with ProcessPoolExecutor(max_workers=min(workers, partition_count)) as executor:
            for partition in range(partition_count):
                while len(pending) >= workers * 2:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for completed in done:
                        result = completed.result()
                        results[result["partition"]] = result
                pending.add(
                    executor.submit(
                        _project_catalog_partition,
                        repo,
                        str(catalog_path),
                        str(registry),
                        str(output_dir),
                        partition,
                    )
                )
            for completed in pending:
                result = completed.result()
                results[result["partition"]] = result

        ProjectionValidator.merge(output_dir / ".projection-validation.sqlite", validation_paths)
        counts: dict[str, int] = {}
        max_csv_field_size = 0
        paths: dict[tuple[bool, str, int], Path] = {}
        for partition in range(partition_count):
            result = results[partition]
            for key, value in result["counts"].items():
                counts[key] = counts.get(key, 0) + value
            max_csv_field_size = max(max_csv_field_size, result["max_csv_field_size_bytes"])
            paths.update(
                {(node, kind, partition): path for (node, kind), path in result["files"].items()}
            )
    except Exception:
        for path in validation_paths:
            _remove_validation_scratch(path)
        _remove_validation_scratch(output_dir / ".projection-validation.sqlite")
        raise
    finally:
        with suppress(sqlite3.ProgrammingError):
            catalog.close()
    return _publish_projection(
        repo,
        output_dir,
        counts,
        paths,
        workers,
        partitions=partition_count,
        max_csv_field_size_bytes=max_csv_field_size,
    )


def _project_catalog_partition(
    repo: RepositoryIR,
    catalog_path: str,
    registry_path: str,
    output_dir: str,
    partition: int,
) -> dict[str, Any]:
    """Process entry point; reads its spool assignment from a disk catalog."""
    catalog = sqlite3.connect(f"file:{Path(catalog_path).resolve()}?mode=ro&immutable=1", uri=True)

    def assigned_spools() -> Iterator[Path]:
        try:
            for (path,) in catalog.execute(
                "SELECT path FROM spools WHERE part = ? ORDER BY ordinal", (partition,)
            ):
                yield Path(path)
        finally:
            catalog.close()

    validator = ProjectionValidator(
        Path(output_dir) / ".building" / f"projection-validation-{partition:06d}.sqlite"
    )
    try:
        return project_partition(
            repo,
            assigned_spools(),
            Path(registry_path),
            Path(output_dir),
            partition=partition,
            validator=validator,
        )
    finally:
        validator.close(validate_endpoints=False)


def _iter_project_files(
    spool_paths: Iterable[Path] | None,
    registry: ResolverIndex,
) -> Iterator[FileIR]:
    if spool_paths is None:
        yield from registry.files()
        return
    for value in spool_paths:
        yield from iter_spool_files(Path(value))


def _project_file(
    repo: RepositoryIR,
    file: FileIR,
    resolver: _Resolver,
    writer: ShardWriter,
    validator: ProjectionValidator,
    counts: dict[str, int],
    *,
    sql_index: SqliteSqlResolverIndex | None = None,
) -> None:
    def node(label: str, row: Mapping[str, Any], *, global_node: bool = False) -> None:
        key = str(row["key"])
        inserted = validator.global_node(label, key) if global_node else validator.node(label, key)
        if inserted:
            writer.row(label, row, node=True)
            _increment(counts, f"nodes_{label}")

    def relationship(
        kind: str,
        start: str,
        end: str,
        properties: Mapping[str, Any],
        identity: str,
        relationship_key: str | None = None,
    ) -> None:
        if validator.relationship(kind, identity, start, end, properties):
            row = {
                "key": (
                    None
                    if kind == "DEFINES" and not properties
                    else relationship_key
                    if relationship_key is not None
                    else identity
                ),
                "_identity": identity,
                "start": start,
                "end": end,
                **properties,
                "type": kind,
            }
            writer.row(kind, row, node=False)
            _increment(counts, f"relationships_{kind}")

    file_row = {
        "key": _key(repo, file.path),
        "path": file.path,
        "language": file.language,
        "loc": file.loc,
        "parse_status": file.parse_status,
        "diagnostic_count": len(file.diagnostics),
    }
    node("File", file_row)
    relationship(
        "CONTAINS",
        repo.repo_name,
        str(file_row["key"]),
        {},
        f"{repo.repo_name}:contains:{file_row['key']}",
    )
    module_row = {
        "key": _key(repo, f"module:{file.module_qname}"),
        "name": file.module_qname.rsplit(".", maxsplit=1)[-1],
        "qname": file.module_qname,
        "language": file.language,
    }
    if resolver.index.module_owner(file.language, file.module_qname) == file.path:
        node("Module", module_row)
    relationship(
        "DEFINES",
        str(file_row["key"]),
        str(module_row["key"]),
        {},
        f"{file_row['key']}:{module_row['key']}:defines",
    )

    for ordinal, diagnostic in enumerate(file.diagnostics, start=1):
        diagnostic_row = {
            "key": _key(repo, f"{file.path}:diagnostic:{ordinal}"),
            "category": diagnostic.category,
            "severity": diagnostic.severity,
            "line": diagnostic.line,
            "column": diagnostic.column,
            "message": diagnostic.message,
        }
        node("ParseDiagnostic", diagnostic_row)
        relationship(
            "HAS_DIAGNOSTIC",
            str(file_row["key"]),
            str(diagnostic_row["key"]),
            {},
            f"{diagnostic_row['key']}:has",
        )

    if file.module_init is not None:
        init = file.module_init
        init_row = {
            "key": _key(repo, f"{file.path}:module-init"),
            "qname": init.qname,
            # The legacy bulk row has no ``name`` field; retain its empty CSV
            # value rather than inventing a display name in sharded mode.
            "name": "",
            "start_line": init.start_line,
            "end_line": init.end_line,
        }
        node("ModuleInit", init_row)
        relationship(
            "CONTAINS",
            str(file_row["key"]),
            str(init_row["key"]),
            {},
            f"{init_row['key']}:contains",
        )
        relationship(
            "INITIALIZES",
            str(module_row["key"]),
            str(init_row["key"]),
            {},
            f"{init_row['key']}:initializes",
        )

    callable_rows: list[dict[str, Any]] = []
    for symbol in file.symbols:
        row = {
            "key": _symbol_key(repo, file.path, symbol.qname, symbol.start_line),
            "name": symbol.name,
            "qname": symbol.qname,
            "signature": symbol.signature,
            "start_line": symbol.start_line,
            "end_line": symbol.end_line,
            "cyclomatic": symbol.cyclomatic,
        }
        if symbol.kind == "type":
            row["kind"] = "class"
            node("Type", row)
        else:
            label = "Method" if symbol.kind == "method" else "Function"
            node(label, row)
            callable_rows.append(
                {**row, "label": label, "path": file.path, "parent_qname": symbol.parent_qname}
            )
        relationship(
            "CONTAINS",
            str(file_row["key"]),
            str(row["key"]),
            {},
            f"{row['key']}:contains",
        )
        if symbol.kind == "method" and symbol.parent_qname:
            parents = resolver.index.types(symbol.parent_qname)
            if len(parents) == 1:
                relationship(
                    "HAS_METHOD",
                    parents[0].key,
                    str(row["key"]),
                    {},
                    f"{parents[0].key}:{row['key']}:has-method",
                )

    for import_ir in file.imports:
        module_key = _key(repo, f"external-module:{import_ir.module}")
        backend = resolver.index.backend
        if isinstance(backend, SqliteResolverIndex) and (
            backend.external_module_owner(import_ir.module) == file.path
        ):
            node(
                "Module",
                {
                    "key": module_key,
                    "name": import_ir.module,
                    "qname": import_ir.module,
                    "language": file.language,
                },
                global_node=True,
            )
        import_key = _key(
            repo,
            ":".join(
                [file.path, "import", import_ir.module, import_ir.name, import_ir.alias or "<none>"]
            ),
        )
        relationship(
            "IMPORTS",
            str(file_row["key"]),
            module_key,
            {"name": import_ir.name, "alias": import_ir.alias},
            import_key,
        )

    for inheritance in file.inheritance:
        children = resolver.index.types(inheritance.type_qname)
        if len(children) != 1:
            continue
        for candidate in _candidate_qnames(
            inheritance.base_name,
            inheritance.base_qname,
            _import_aliases(file),
        ):
            parents = resolver.index.types(candidate)
            if len(parents) != 1 or children[0].key == parents[0].key:
                continue
            relationship(
                "INHERITS",
                children[0].key,
                parents[0].key,
                {},
                f"{children[0].key}:{parents[0].key}",
            )
            break

    for call in file.calls:
        resolution = resolver.resolve(file, call)
        callsite_key = _callsite_key(repo, file.path, resolution.owner_key, call)
        callsite = {
            "key": callsite_key,
            "path": file.path,
            "owner_key": resolution.owner_key,
            "owner_qname": call.owner_qname,
            "raw_callee": call.raw_callee,
            "callee_name": call.callee_name,
            "callee_qname_hint": call.callee_qname_hint,
            "receiver_kind": call.receiver_kind,
            "start_line": call.start_line,
            "start_column": call.start_column,
            "end_line": call.end_line,
            "end_column": call.end_column,
            "ordinal": call.ordinal,
            "status": resolution.status,
            "resolution_strategy": resolution.status,
            "candidate_count": len(resolution.candidate_keys),
            "candidate_keys": list(resolution.candidate_keys),
            "initializer_candidate_count": len(resolution.initializer_candidate_keys),
            "initializer_candidate_keys": list(resolution.initializer_candidate_keys),
        }
        node("CallSite", callsite)
        if resolution.owner_key is not None:
            relationship(
                "HAS_CALLSITE",
                resolution.owner_key,
                callsite_key,
                {},
                f"{callsite_key}:owner",
            )

        target_key = resolution.target_key
        resolution_status = resolution.status
        if resolution.is_constructor:
            target_key = resolution.initializer_target_key
            resolution_status = resolution.initializer_status
        if target_key is not None and resolution.owner_key is not None:
            properties = {
                "resolution": resolution_status,
                "line": call.start_line,
                "column": call.start_column,
            }
            for kind in ("CALLS", "EXACT_CALLS"):
                relationship(
                    kind,
                    resolution.owner_key,
                    target_key,
                    properties,
                    f"{callsite_key}:{kind}",
                    relationship_key=callsite_key,
                )
            relationship(
                "RESOLVES_TO",
                callsite_key,
                target_key,
                {"strategy": resolution_status, "confidence": "exact"},
                f"{callsite_key}:resolve",
                relationship_key=callsite_key,
            )
        if (
            resolution.is_constructor
            and resolution.owner_key is not None
            and resolution.construction_target_key is not None
        ):
            properties = {
                "resolution": resolution.status,
                "line": call.start_line,
                "column": call.start_column,
            }
            relationship(
                "CONSTRUCTS",
                callsite_key,
                resolution.construction_target_key,
                properties,
                f"{callsite_key}:site-constructs",
                relationship_key=callsite_key,
            )
            relationship(
                "CONSTRUCTS",
                resolution.owner_key,
                resolution.construction_target_key,
                properties,
                f"{callsite_key}:owner-constructs",
                relationship_key=callsite_key,
            )

    if sql_index is not None and (
        file.sql_artifacts or file.sql_statements or file.sql_object_refs
    ):
        repo_prefix = f"{repo.repo_name}@{repo.commit}"

        def sql_node(label: str, row: Mapping[str, Any], *, global_node: bool = False) -> None:
            node(label, row, global_node=global_node)

        # Global SQL identities are owner-sharded, so each is emitted once by
        # the partition containing its source owner.  Their relationships use
        # the same owner rule and may target nodes in another partition; the
        # merged validator checks those endpoints after all partitions finish.
        for label, row in iter_sql_global_nodes(sql_index, owner_path=file.path):
            sql_node(label, row, global_node=True)
        for kind, start, end, properties, identity in iter_sql_global_relationships(
            repo_prefix, repo.repo_name, sql_index, owner_path=file.path
        ):
            relationship(kind, start, end, properties, identity)
        for label, row in iter_sql_file_nodes(repo_prefix, file, sql_index):
            sql_node(label, row)
        for kind, start, end, properties, identity in iter_sql_file_relationships(
            repo_prefix, file, sql_index
        ):
            relationship(kind, start, end, properties, identity)


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
    total = "nodes" if key.startswith("nodes_") else "relationships"
    counts[total] = counts.get(total, 0) + 1


def _remove_validation_scratch(path: Path) -> None:
    """Remove a failed disposable validator and any SQLite sidecar files."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _publish_projection(
    repo: RepositoryIR,
    output_dir: Path,
    counts: Mapping[str, int],
    paths: Mapping[tuple[bool, str, int], Path],
    workers: int,
    *,
    partitions: int,
    max_csv_field_size_bytes: int = 0,
) -> BulkExport:
    headers_dir = output_dir / "headers"
    headers_dir.mkdir(parents=True, exist_ok=True)
    node_files: dict[str, Path] = {}
    relationship_files: dict[str, Path] = {}
    nodes: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}
    for node, kind in sorted({(node, kind) for node, kind, _ in paths}, key=lambda item: item):
        header = headers_dir / (
            f"nodes_{kind}.header.csv" if node else f"relationships_{kind}.header.csv"
        )
        columns = _NODE_COLUMNS[kind] if node else _REL_COLUMNS[kind]
        if not node and kind == "DEFINES" and counts.get("nodes_SqlObject", 0) == 0:
            from codekg.bulk_export import _PYTHON_DEFINES_COLUMNS

            columns = _PYTHON_DEFINES_COLUMNS
        with header.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerow(
                [value for _, value in columns] + ([":LABEL"] if node else [])
            )
        group = "nodes" if node else "relationships"
        count_key = f"{group}_{kind}"
        shards = [
            value
            for (is_node, value_kind, _), value in sorted(paths.items())
            if is_node == node and value_kind == kind
        ]
        entry = {
            "count": counts.get(count_key, 0),
            "files": [str(header.relative_to(output_dir))]
            + [str(shard.relative_to(output_dir)) for shard in shards],
        }
        (nodes if node else relationships)[kind] = entry
        (node_files if node else relationship_files)[kind] = header
    manifest = {
        "version": 2,
        "mode": "sharded-monorepo",
        "output_dir": ".",
        "repository": {
            "repo_name": repo.repo_name,
            "commit": repo.commit,
            "root_path": repo.root_path,
        },
        "workers": workers,
        "partitions": partitions,
        "nodes": dict(sorted(nodes.items())),
        "relationships": dict(sorted(relationships.items())),
        "counts": dict(counts),
        "max_csv_field_size_bytes": validate_csv_field_size(max_csv_field_size_bytes),
    }
    manifest_path = output_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return BulkExport(
        manifest_path,
        output_dir,
        node_files,
        relationship_files,
        dict(counts),
        max_csv_field_size_bytes=max_csv_field_size_bytes,
    )


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sql_index_from_backend(backend: ResolverIndex) -> SqliteSqlResolverIndex | None:
    connection = getattr(backend, "connection", None)
    if not isinstance(connection, sqlite3.Connection):
        return None
    has_sql = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlobjects'"
    ).fetchone()
    if has_sql is None:
        return None
    return SqliteSqlResolverIndex.from_backend(backend)
