"""Deterministic Neo4j-admin CSV exports for CodeKG snapshots."""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codekg.csv_limits import serialized_csv_field_size_bytes, validate_csv_field_size
from codekg.ir import RepositoryIR
from codekg.loader import (
    _callsite_rows,
    _construction_rows,
    _diagnostic_rows,
    _file_row,
    _has_method_rows,
    _inheritance_rows,
    _key,
    _module_init_row,
    _resolved_call_rows,
    _symbol_key,
)
from codekg.logging_config import debug_event
from codekg.sql_config import SqlConfig
from codekg.sql_graph import (
    SQL_NODE_COLUMNS,
    SQL_REL_COLUMNS,
    iter_all_sql_global_nodes,
    iter_sql_file_nodes,
    iter_sql_file_relationships,
    iter_sql_global_relationships,
)

logger = logging.getLogger(__name__)


def _extract_spool(
    root_value: str,
    paths: tuple[str, ...],
    spool_value: str,
    sql_config: SqlConfig | None = None,
) -> str:
    """Worker entry point: parse a bounded batch and publish one spool."""
    from codekg.bulk_spool import create_spool
    from codekg.ingest import try_scan_file

    root = Path(root_value)
    spool = Path(spool_value)

    def scanned_files():
        for path in paths:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = root / resolved
            file = (
                try_scan_file(root, resolved)
                if sql_config is None
                else try_scan_file(root, resolved, sql_config=sql_config)
            )
            if file is not None:
                yield file

    create_spool(spool, scanned_files())
    return str(spool)


def export_repository_path(
    root: Path,
    output_dir: Path,
    *,
    workers: int = 1,
    repo_name: str | None = None,
    commit_override: str | None = None,
    sql_config: SqlConfig | None = None,
) -> BulkExport:
    """Export one logical repository through durable extraction staging.

    The public single-root entry point intentionally owns repository identity
    once.  Spooling is also useful for recovery and makes subsequent pipeline
    stages independent of the source tree.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    from codekg.bulk_identity import content_hash
    from codekg.bulk_spool import build_registry
    from codekg.ingest import _git_commit

    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {root}")
    if repo_name is not None and (not isinstance(repo_name, str) or not repo_name.strip()):
        raise ValueError("repo_name must be a non-empty snapshot alias")
    if commit_override is not None and (
        not isinstance(commit_override, str) or not commit_override.strip()
    ):
        raise ValueError("commit_override must be a non-empty revision")
    snapshot_name = repo_name if repo_name is not None else root.name
    output_dir = Path(output_dir)
    generation = output_dir / "generations" / f"{int(time.time_ns())}"
    spool_dir = generation / ".building" / "spools"
    generation.mkdir(parents=True, exist_ok=True)
    spool_dir.mkdir(parents=True, exist_ok=True)
    catalog = sqlite3.connect(generation / ".building" / "schedule.sqlite")
    catalog.execute("CREATE TABLE spools (ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL)")
    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            pending = set()
            batches = (
                _source_batches(root)
                if sql_config is None
                else _source_batches(root, sql_config=sql_config)
            )
            for ordinal, paths in enumerate(batches):
                spool = spool_dir / f"extract-{ordinal:06d}.sqlite"
                catalog.execute("INSERT INTO spools VALUES (?, ?)", (ordinal, str(spool)))
                while len(pending) >= workers * 2:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for completed in done:
                        completed.result()
                pending.add(
                    executor.submit(
                        _extract_spool,
                        str(root),
                        tuple(str(path) for path in paths),
                        str(spool),
                        sql_config,
                    )
                )
            for completed in pending:
                completed.result()
        catalog.commit()
        commit = commit_override or _git_commit(root) or content_hash(root, sql_config=sql_config)
        build_registry(
            generation / ".building" / "resolver.sqlite",
            (Path(row[0]) for row in catalog.execute("SELECT path FROM spools ORDER BY ordinal")),
            repo_prefix=f"{snapshot_name}@{commit}",
        )
        from codekg.bulk_projection import project_repository

        repo = RepositoryIR(repo_name=snapshot_name, commit=commit, root_path=str(root))
        from codekg.bulk_search import create_search_stage_from_registry

        search_documents = create_search_stage_from_registry(
            generation / "search.sqlite",
            generation / ".building" / "resolver.sqlite",
            root,
            repo,
        )
        projection_spools = (
            Path(row[0]) for row in catalog.execute("SELECT path FROM spools ORDER BY ordinal")
        )
        result = project_repository(
            repo,
            projection_spools,
            generation / ".building" / "resolver.sqlite",
            generation,
            workers=workers,
        )
    finally:
        catalog.close()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "mode": "sharded-monorepo",
            "workers": workers,
            "repository": {
                "repo_name": repo.repo_name,
                "commit": repo.commit,
                "root_path": repo.root_path,
            },
            "search_stage": {
                "version": 1,
                "file": "search.sqlite",
                "documents": search_documents,
            },
        }
    )
    result.manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    published = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    generation_name = result.manifest_path.parent.relative_to(output_dir)
    published["version"] = 2
    published["output_dir"] = "."
    for section in ("nodes", "relationships"):
        for entry in published[section].values():
            if "files" in entry:
                entry["files"] = [str(generation_name / value) for value in entry["files"]]
            else:
                entry["file"] = str(generation_name / entry["file"])
    published["search_stage"]["file"] = str(generation_name / published["search_stage"]["file"])
    top_manifest = output_dir / "manifest.json"
    temporary = output_dir / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(published, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, top_manifest)
    # Public callers and the Docker importer must consume the generation
    # pointer that was atomically published, never the private build manifest.
    return load_bulk_export(top_manifest)


def stage_corpus_source_file(
    root: Path,
    source: Path,
    raw: bytes | None,
    status: str | None,
    language: str,
    spool_batcher,
    catalog: sqlite3.Connection,
    ordinal: int,
    *,
    sql_config: SqlConfig,
    max_file_bytes: int,
    markdown_dir: Path,
) -> None:
    """Stage one already-bounded corpus file without reopening its source."""
    from codekg.ingest import _module_qname, scan_file_bytes
    from codekg.ir import FileIR, ParseDiagnosticIR

    relative = source.relative_to(root).as_posix()
    ordinary_source = source.suffix.lower() in {".py", ".sql"} or source.name.lower().endswith(
        ".sql.in"
    )
    if ordinary_source and raw is None:
        diagnostic = ParseDiagnosticIR(
            category="file_too_large" if status == "file_too_large" else "unreadable_file",
            severity="warning",
            line=None,
            column=None,
            message=f"source exceeds {max_file_bytes} byte corpus limit"
            if status == "file_too_large"
            else "source could not be read within corpus limits",
        )
        file = FileIR(
            path=relative,
            language=language,
            loc=0,
            module_qname=(
                f"sql:{relative}"
                if language == "sql"
                else _module_qname(relative, repository_name=root.name)
            ),
            parse_status="error",
            diagnostics=(diagnostic,),
        )
    elif ordinary_source:
        file = scan_file_bytes(root, source, raw, sql_config=sql_config)
    if ordinary_source:
        spool_batcher.write(file, len(raw) if raw is not None else 0)
    if source.suffix.lower() == ".md" and raw is not None:
        staged = markdown_dir / f"{ordinal:08d}.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(raw)
        catalog.execute("INSERT INTO markdown_files VALUES (?, ?)", (relative, str(staged)))


class CorpusSpoolBatcher:
    """Roll incremental source spools at bounded file and byte limits."""

    MAX_FILES = 128
    MAX_SOURCE_BYTES = 32 * 1024 * 1024

    def __init__(self, spool_dir: Path, catalog: sqlite3.Connection) -> None:
        from codekg.bulk_spool import SpoolWriter

        self._writer_type = SpoolWriter
        self.spool_dir = spool_dir
        self.catalog = catalog
        self.ordinal = 0
        self.writer = None
        self.source_bytes = 0

    def write(self, file, source_bytes: int) -> None:
        if self.writer is not None and (
            self.writer.count >= self.MAX_FILES
            or self.source_bytes + source_bytes > self.MAX_SOURCE_BYTES
        ):
            self._finish_spool()
        if self.writer is None:
            path = self.spool_dir / f"extract-{self.ordinal:06d}.sqlite"
            self.writer = self._writer_type(path)
            self.current_path = path
            self.source_bytes = 0
        self.writer.write(file)
        self.source_bytes += source_bytes

    def finish(self) -> None:
        self._finish_spool()

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.close(publish=False)
            self.writer = None

    def _finish_spool(self) -> None:
        if self.writer is None:
            return
        self.writer.close()
        self.catalog.execute(
            "INSERT INTO spools VALUES (?, ?)", (self.ordinal, str(self.current_path))
        )
        self.ordinal += 1
        self.writer = None
        self.source_bytes = 0


def finalize_corpus_snapshot(
    root: Path,
    stage_dir: Path,
    alias: str,
    revision: str,
    sql_config: SqlConfig,
    catalog: sqlite3.Connection,
    markdown_dir: Path,
    *,
    workers: int,
) -> tuple[BulkExport, int, int]:
    """Build resolver/search/projection stages from corpus extraction spools."""
    from codekg.bulk_projection import project_repository
    from codekg.bulk_search import create_search_stage_from_registry
    from codekg.bulk_spool import build_registry

    resolver = stage_dir / ".building" / "resolver.sqlite"

    def spool_paths():
        for (value,) in catalog.execute("SELECT path FROM spools ORDER BY ordinal"):
            yield Path(value)

    spool_count = int(catalog.execute("SELECT count(*) FROM spools").fetchone()[0])
    projection_workers = min(workers, max(1, spool_count))
    build_registry(resolver, spool_paths(), repo_prefix=f"{alias}@{revision}")
    repo = RepositoryIR(repo_name=alias, commit=revision, root_path=str(root))
    search_path = stage_dir / "search.sqlite"
    search_documents = create_search_stage_from_registry(
        search_path,
        resolver,
        root,
        repo,
        markdown_paths=(
            Path(row[0])
            for row in catalog.execute(
                "SELECT staged_path FROM markdown_files ORDER BY source_path"
            )
        ),
    )
    result = project_repository(
        repo,
        spool_paths(),
        resolver,
        stage_dir,
        workers=projection_workers,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "mode": "sharded-monorepo",
            "workers": projection_workers,
            "repository": {
                "repo_name": repo.repo_name,
                "commit": repo.commit,
                "root_path": repo.root_path,
            },
            "search_stage": {"version": 1, "file": "search.sqlite", "documents": search_documents},
        }
    )
    result.manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return result, search_documents, projection_workers


def _source_batches(
    root: Path, *, sql_config: SqlConfig | None = None
) -> Iterable[tuple[Path, ...]]:
    """Yield deterministic, bounded extraction work without retaining it all."""
    from codekg.ingest import _iter_source_files

    batch: list[Path] = []
    bytes_used = 0
    paths = (
        _iter_source_files(root)
        if sql_config is None
        else _iter_source_files(root, sql_config=sql_config)
    )
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError as error:
            from codekg.ingest import _log_scan_skip

            _log_scan_skip("scan_skip_file", path.relative_to(root).as_posix(), error)
            continue
        if batch and (len(batch) >= 128 or bytes_used + size > 32 * 1024 * 1024):
            yield tuple(batch)
            batch, bytes_used = [], 0
        batch.append(path)
        bytes_used += size
    if batch:
        yield tuple(batch)


@dataclass(frozen=True)
class BulkExport:
    """The files and counts published by :func:`export_repositories`."""

    manifest_path: Path
    output_dir: Path
    node_files: Mapping[str, Path]
    relationship_files: Mapping[str, Path]
    counts: Mapping[str, int]
    node_groups: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    relationship_groups: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)
    search_stage: Path | None = None
    max_csv_field_size_bytes: int = 0


_NODE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "Repository": (
        ("key", "key:ID(CodeKG)"),
        ("repo_name", "repo_name"),
        ("commit", "commit"),
        ("root_path", "root_path"),
    ),
    "File": (
        ("key", "key:ID(CodeKG)"),
        ("path", "path"),
        ("language", "language"),
        ("loc", "loc:int"),
        ("parse_status", "parse_status"),
        ("diagnostic_count", "diagnostic_count:int"),
    ),
    "Module": (
        ("key", "key:ID(CodeKG)"),
        ("name", "name"),
        ("qname", "qname"),
        ("language", "language"),
    ),
    "ParseDiagnostic": (
        ("key", "key:ID(CodeKG)"),
        ("category", "category"),
        ("severity", "severity"),
        ("line", "line:int"),
        ("column", "column:int"),
        ("message", "message"),
    ),
    "ModuleInit": (
        ("key", "key:ID(CodeKG)"),
        ("qname", "qname"),
        ("name", "name"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
    ),
    "Type": (
        ("key", "key:ID(CodeKG)"),
        ("name", "name"),
        ("qname", "qname"),
        ("signature", "signature"),
        ("kind", "kind"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
        ("cyclomatic", "cyclomatic:int"),
    ),
    "Function": (
        ("key", "key:ID(CodeKG)"),
        ("name", "name"),
        ("qname", "qname"),
        ("signature", "signature"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
        ("cyclomatic", "cyclomatic:int"),
    ),
    "Method": (
        ("key", "key:ID(CodeKG)"),
        ("name", "name"),
        ("qname", "qname"),
        ("signature", "signature"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
        ("cyclomatic", "cyclomatic:int"),
    ),
    "CallSite": (
        ("key", "key:ID(CodeKG)"),
        ("path", "path"),
        ("owner_key", "owner_key"),
        ("owner_qname", "owner_qname"),
        ("raw_callee", "raw_callee"),
        ("callee_name", "callee_name"),
        ("callee_qname_hint", "callee_qname_hint"),
        ("receiver_kind", "receiver_kind"),
        ("start_line", "start_line:int"),
        ("start_column", "start_column:int"),
        ("end_line", "end_line:int"),
        ("end_column", "end_column:int"),
        ("ordinal", "ordinal:int"),
        ("status", "status"),
        ("resolution_strategy", "resolution_strategy"),
        ("candidate_count", "candidate_count:int"),
        ("candidate_keys", "candidate_keys:string[]"),
        ("initializer_candidate_count", "initializer_candidate_count:int"),
        ("initializer_candidate_keys", "initializer_candidate_keys:string[]"),
    ),
}

_KEYED_RELATIONSHIPS = {"IMPORTS", "CALLS", "EXACT_CALLS", "RESOLVES_TO", "CONSTRUCTS"}

_REL_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    name: ((("key", "key"),) if name in _KEYED_RELATIONSHIPS else ())
    + (
        ("start", ":START_ID(CodeKG)"),
        ("end", ":END_ID(CodeKG)"),
        *props,
        ("type", ":TYPE"),
    )
    for name, props in {
        "CONTAINS": (),
        "DEFINES": (),
        "HAS_DIAGNOSTIC": (),
        "INITIALIZES": (),
        "HAS_METHOD": (),
        "INHERITS": (),
        "HAS_CALLSITE": (),
        "IMPORTS": (("name", "name"), ("alias", "alias")),
        "CALLS": (("resolution", "resolution"), ("line", "line:int"), ("column", "column:int")),
        "EXACT_CALLS": (
            ("resolution", "resolution"),
            ("line", "line:int"),
            ("column", "column:int"),
        ),
        "RESOLVES_TO": (("strategy", "strategy"), ("confidence", "confidence")),
        "CONSTRUCTS": (
            ("resolution", "resolution"),
            ("line", "line:int"),
            ("column", "column:int"),
        ),
    }.items()
}
_PYTHON_DEFINES_COLUMNS = _REL_COLUMNS["DEFINES"]
_REL_COLUMNS["DEFINES"] = SQL_REL_COLUMNS["DEFINES"]
_NODE_COLUMNS.update(SQL_NODE_COLUMNS)
# SQL and Python share the DEFINES relationship kind.  The CSV writer selects
# the narrow legacy shape for Python-only exports and the augmented shape when
# SQL definitions are present.
for _kind, _columns in SQL_REL_COLUMNS.items():
    if _kind != "DEFINES":
        _REL_COLUMNS[_kind] = _columns


def export_repositories(repositories: Iterable[RepositoryIR], output_dir: Path) -> BulkExport:
    """Export snapshots and publish a JSON manifest after validation."""
    repository_snapshots = tuple(repositories)
    started = time.perf_counter()
    debug_event(
        logger,
        "bulk_export_started",
        repositories=len(repository_snapshots),
    )
    try:
        graph = _build_graph(repository_snapshots)
    except Exception as exc:
        debug_event(
            logger,
            "bulk_export_graph_failed",
            repositories=len(repository_snapshots),
            error_type=type(exc).__name__,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise
    debug_event(
        logger,
        "bulk_export_graph_built",
        repositories=graph["counts"].get("repositories", 0),
        nodes=graph["counts"].get("nodes", 0),
        relationships=graph["counts"].get("relationships", 0),
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    from codekg.bulk_search import create_search_stage_from_repositories

    search_documents = create_search_stage_from_repositories(
        output_dir / "search.sqlite", repository_snapshots
    )
    node_files = {label: output_dir / f"nodes_{label}.csv" for label in sorted(graph["nodes"])}
    relationship_files = {
        kind: output_dir / f"relationships_{kind}.csv" for kind in sorted(graph["relationships"])
    }
    for label, rows in graph["nodes"].items():
        debug_event(logger, "bulk_export_node_file", label=label, rows=len(rows))
        _write_csv(node_files[label], _NODE_COLUMNS[label], rows, label)
    for kind, rows in graph["relationships"].items():
        debug_event(logger, "bulk_export_relationship_file", kind=kind, rows=len(rows))
        _write_csv(
            relationship_files[kind],
            _REL_COLUMNS[kind],
            rows,
            kind,
            wide_defines=bool(graph["nodes"].get("SqlObject")),
        )
    manifest = {
        "version": 1,
        "output_dir": str(output_dir),
        "nodes": {
            label: {"file": path.name, "count": len(graph["nodes"][label])}
            for label, path in node_files.items()
        },
        "relationships": {
            kind: {"file": path.name, "count": len(graph["relationships"][kind])}
            for kind, path in relationship_files.items()
        },
        "counts": graph["counts"],
        "max_csv_field_size_bytes": _graph_max_csv_field_size(graph),
        "search_stage": {
            "version": 1,
            "file": "search.sqlite",
            "documents": search_documents,
        },
    }
    manifest_path = output_dir / "manifest.json"
    fd, temporary = tempfile.mkstemp(prefix=".manifest.", suffix=".json", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, manifest_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    result = BulkExport(
        manifest_path,
        output_dir,
        node_files,
        relationship_files,
        graph["counts"],
        {label: (path,) for label, path in node_files.items()},
        {kind: (path,) for kind, path in relationship_files.items()},
        output_dir / "search.sqlite",
        manifest["max_csv_field_size_bytes"],
    )
    debug_event(
        logger,
        "bulk_export_completed",
        nodes=graph["counts"].get("nodes", 0),
        relationships=graph["counts"].get("relationships", 0),
    )
    return result


def load_bulk_export(manifest_path: Path) -> BulkExport:
    """Load a previously published export manifest."""
    manifest_path = Path(manifest_path)
    debug_event(logger, "bulk_export_manifest_load_started")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    max_csv_field_size = validate_csv_field_size(data.get("max_csv_field_size_bytes", 0))
    output_dir = Path(data["output_dir"])
    if not output_dir.is_absolute():
        output_dir = manifest_path.parent / output_dir
    node_groups = {
        label: tuple(output_dir / value for value in (entry.get("files") or [entry["file"]]))
        for label, entry in data["nodes"].items()
    }
    relationship_groups = {
        kind: tuple(output_dir / value for value in (entry.get("files") or [entry["file"]]))
        for kind, entry in data["relationships"].items()
    }
    node_files = {label: paths[0] for label, paths in node_groups.items()}
    relationship_files = {kind: paths[0] for kind, paths in relationship_groups.items()}
    search_entry = data.get("search_stage")
    search_stage = (
        output_dir / str(search_entry["file"])
        if isinstance(search_entry, dict) and isinstance(search_entry.get("file"), str)
        else None
    )
    result = BulkExport(
        manifest_path,
        output_dir,
        node_files,
        relationship_files,
        data["counts"],
        node_groups,
        relationship_groups,
        search_stage,
        max_csv_field_size,
    )
    debug_event(
        logger,
        "bulk_export_manifest_load_completed",
        node_files=len(node_files),
        relationship_files=len(relationship_files),
    )
    return result


def _build_graph(repositories: tuple[RepositoryIR, ...]) -> dict[str, Any]:
    nodes: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    relationships: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    node_keys: set[str] = set()
    rel_seen: dict[tuple[str, str], tuple[str, str, dict[str, Any]]] = {}

    def node(label: str, row: dict[str, Any]) -> None:
        key = str(row["key"])
        if key in node_keys:
            raise ValueError(f"duplicate node key: {key}")
        node_keys.add(key)
        nodes[label].append(row)

    def rel(
        kind: str,
        start: str,
        end: str,
        props: Mapping[str, Any],
        identity: str,
        relationship_key: str | None = None,
    ) -> None:
        if start not in node_keys or end not in node_keys:
            raise ValueError(f"dangling {kind} relationship endpoint: {start} -> {end}")
        key = identity
        semantic = (start, end, dict(props))
        previous = rel_seen.get((kind, key))
        if previous is not None:
            if kind == "IMPORTS" and previous == semantic:
                return
            raise ValueError(f"duplicate relationship key: {kind}:{key}")
        rel_seen[(kind, key)] = semantic
        relationships[kind].append(
            {
                "key": (
                    None
                    if kind == "DEFINES" and not props
                    else relationship_key
                    if relationship_key is not None
                    else key
                ),
                "_identity": key,
                "start": start,
                "end": end,
                **props,
                "type": kind,
            }
        )

    for repo in repositories:
        repo_key = repo.repo_name
        node(
            "Repository",
            {
                "key": repo_key,
                "repo_name": repo.repo_name,
                "commit": repo.commit,
                "root_path": repo.root_path,
            },
        )
        module_owners: dict[tuple[str, str], str] = {}
        for source_file in sorted(repo.files, key=lambda item: item.path):
            module_owners.setdefault(
                (source_file.language, source_file.module_qname), source_file.path
            )
        for language, module_qname in sorted(module_owners):
            node(
                "Module",
                {
                    "key": _key(repo, f"module:{module_qname}"),
                    "name": module_qname.rsplit(".", maxsplit=1)[-1],
                    "qname": module_qname,
                    "language": language,
                },
            )
        type_rows: list[dict[str, Any]] = []
        callable_rows: list[dict[str, Any]] = []
        owner_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        diagnostics_by_file: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for diagnostic in _diagnostic_rows(repo):
            diagnostics_by_file[str(diagnostic["file_key"])].append(diagnostic)
        for file in repo.files:
            f = _file_row(repo, file)
            node("File", f)
            rel("CONTAINS", repo_key, f["key"], {}, f"{repo_key}:contains:{f['key']}")
            m = {
                "key": f["module_key"],
                "name": f["module_name"],
                "qname": f["module_qname"],
                "language": f["language"],
            }
            rel(
                "DEFINES",
                f["key"],
                m["key"],
                {},
                f"{f['key']}:{m['key']}:defines",
            )
            for diagnostic in diagnostics_by_file[f["key"]]:
                node("ParseDiagnostic", diagnostic)
                rel(
                    "HAS_DIAGNOSTIC",
                    f["key"],
                    diagnostic["key"],
                    {},
                    f"{diagnostic['key']}:has",
                )
            if file.module_init:
                init = _module_init_row(repo, file)
                node("ModuleInit", init)
                rel("CONTAINS", f["key"], init["key"], {}, f"{init['key']}:contains")
                rel("INITIALIZES", m["key"], init["key"], {}, f"{init['key']}:initializes")
                # _callsite_rows treats an owner without a label as a module
                # initializer; retain that loader convention exactly.
                owner_rows[(file.path, init["qname"])].append(dict(init))
            for symbol in file.symbols:
                row = {
                    "key": _symbol_key(repo, file.path, symbol.qname, symbol.start_line),
                    "name": symbol.name,
                    "qname": symbol.qname,
                    "signature": symbol.signature,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "cyclomatic": symbol.cyclomatic,
                    "return_annotation": symbol.return_annotation,
                }
                if symbol.kind == "type":
                    row["kind"] = "class"
                    node("Type", row)
                    type_rows.append({**row, "path": file.path})
                else:
                    label = "Method" if symbol.kind == "method" else "Function"
                    node(label, row)
                    callable_rows.append(
                        {
                            **row,
                            "label": label,
                            "path": file.path,
                            "parent_qname": symbol.parent_qname,
                        }
                    )
                rel("CONTAINS", f["key"], row["key"], {}, f"{row['key']}:contains")
                if symbol.kind != "type":
                    owner_rows[(file.path, symbol.qname)].append(
                        {
                            **row,
                            "label": label,
                            "path": file.path,
                            "parent_qname": symbol.parent_qname,
                        }
                    )
        type_by_qname: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in type_rows:
            type_by_qname[str(row["qname"])].append(row)
        for method_row in _has_method_rows(type_by_qname, callable_rows):
            rel(
                "HAS_METHOD",
                method_row["type_key"],
                method_row["method_key"],
                {},
                f"{method_row['type_key']}:{method_row['method_key']}:has-method",
            )
        for file in repo.files:
            for imp in file.imports:
                key = _key(
                    repo,
                    ":".join([file.path, "import", imp.module, imp.name, imp.alias or "<none>"]),
                )
                target = _key(repo, f"external-module:{imp.module}")
                if target not in node_keys:
                    node(
                        "Module",
                        {
                            "key": target,
                            "name": imp.module,
                            "qname": imp.module,
                            "language": file.language,
                        },
                    )
                rel(
                    "IMPORTS",
                    _key(repo, file.path),
                    target,
                    {"name": imp.name, "alias": imp.alias},
                    key,
                )
        for edge in _inheritance_rows(repo, type_by_qname):
            rel(
                "INHERITS",
                edge["child_key"],
                edge["parent_key"],
                {},
                f"{edge['child_key']}:{edge['parent_key']}",
            )
        calls, resolutions = _callsite_rows(repo, owner_rows, callable_rows, type_rows)
        for call in calls:
            node("CallSite", call)
            if call["owner_key"] is not None:
                rel("HAS_CALLSITE", call["owner_key"], call["key"], {}, f"{call['key']}:owner")
        for row in _resolved_call_rows(calls, resolutions):
            props = {k: row[k] for k in ("resolution", "line", "column")}
            for kind in ("CALLS", "EXACT_CALLS"):
                rel(
                    kind,
                    row["caller_key"],
                    row["callee_key"],
                    props,
                    f"{row['callsite_key']}:{kind}",
                    relationship_key=str(row["callsite_key"]),
                )
            rel(
                "RESOLVES_TO",
                row["callsite_key"],
                row["callee_key"],
                {"strategy": row["resolution"], "confidence": "exact"},
                f"{row['callsite_key']}:resolve",
                relationship_key=str(row["callsite_key"]),
            )
        for row in _construction_rows(calls, resolutions):
            props = {k: row[k] for k in ("resolution", "line", "column")}
            rel(
                "CONSTRUCTS",
                row["callsite_key"],
                row["type_key"],
                props,
                f"{row['callsite_key']}:site-constructs",
                relationship_key=str(row["callsite_key"]),
            )
            rel(
                "CONSTRUCTS",
                row["owner_key"],
                row["type_key"],
                props,
                f"{row['callsite_key']}:owner-constructs",
                relationship_key=str(row["callsite_key"]),
            )
        if any(
            file.sql_artifacts or file.sql_statements or file.sql_object_refs for file in repo.files
        ):
            with _sql_index_for_files(repo) as sql_index:
                repo_prefix = f"{repo.repo_name}@{repo.commit}"
                for label, row in iter_all_sql_global_nodes(sql_index):
                    node(label, row)
                for kind, start, end, props, identity in iter_sql_global_relationships(
                    repo_prefix, repo.repo_name, sql_index, all_global=True
                ):
                    rel(kind, start, end, props, identity)
                for file in repo.files:
                    for label, row in iter_sql_file_nodes(repo_prefix, file, sql_index):
                        node(label, row)
                    for kind, start, end, props, identity in iter_sql_file_relationships(
                        repo_prefix, file, sql_index
                    ):
                        rel(kind, start, end, props, identity)
    for rows in nodes.values():
        rows.sort(key=lambda row: str(row["key"]))
    for rows in relationships.values():
        rows.sort(key=lambda row: str(row["_identity"]))
    counts = {
        "nodes": sum(map(len, nodes.values())),
        "relationships": sum(map(len, relationships.values())),
    }
    counts.update({f"nodes_{label}": len(rows) for label, rows in nodes.items()})
    counts.update({f"relationships_{kind}": len(rows) for kind, rows in relationships.items()})
    return {"nodes": nodes, "relationships": relationships, "counts": counts}


def _write_csv(
    path: Path,
    columns: tuple[tuple[str, str], ...],
    rows: list[dict[str, Any]],
    kind: str,
    *,
    wide_defines: bool = False,
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if kind == "DEFINES" and not wide_defines:
            columns = _PYTHON_DEFINES_COLUMNS
        headers = [header for _, header in columns]
        if kind in _NODE_COLUMNS:
            headers.append(":LABEL")
        writer.writerow(headers)
        for row in rows:
            values = [_csv_value(row.get(name)) for name, _ in columns]
            if kind in _NODE_COLUMNS:
                values.append(kind)
            writer.writerow(values)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _graph_max_csv_field_size(graph: Mapping[str, Any]) -> int:
    """Measure the largest serialized data field before writing graph CSVs."""
    maximum = 0
    for label, rows in graph["nodes"].items():
        columns = _NODE_COLUMNS[label]
        for row in rows:
            values = [_csv_value(row.get(name)) for name, _ in columns] + [label]
            maximum = max(maximum, *(serialized_csv_field_size_bytes(value) for value in values))
    wide_defines = bool(graph["nodes"].get("SqlObject"))
    for kind, rows in graph["relationships"].items():
        columns = (
            _PYTHON_DEFINES_COLUMNS
            if kind == "DEFINES" and not wide_defines
            else _REL_COLUMNS[kind]
        )
        for row in rows:
            values = [_csv_value(row.get(name)) for name, _ in columns]
            maximum = max(maximum, *(serialized_csv_field_size_bytes(value) for value in values))
    return validate_csv_field_size(maximum)


@contextmanager
def _sql_index_for_files(repo: RepositoryIR):
    """Build a bounded SQL registry for a legacy in-memory repository graph."""
    from codekg.bulk_spool import build_registry, create_spool
    from codekg.sql_resolver import SqliteSqlResolverIndex

    with tempfile.TemporaryDirectory(prefix="codekg-sql-") as directory:
        root = Path(directory)
        spool = root / "source.sqlite"
        registry = root / "registry.sqlite"
        create_spool(spool, repo.files)
        build_registry(registry, [spool], repo_prefix=f"{repo.repo_name}@{repo.commit}")
        index = SqliteSqlResolverIndex(registry)
        try:
            yield index
        finally:
            index.close()
