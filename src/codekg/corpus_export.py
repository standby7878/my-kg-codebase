"""Build and atomically publish a disk-backed, explicitly versioned corpus."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import resource
import shutil
import sqlite3
import time
from itertools import chain
from pathlib import Path
from typing import Any

from codekg.corpus_config import CorpusConfig, effective_sql_config, load_corpus_config
from codekg.corpus_registry import (
    create_native_registry,
    extract_snapshot_facts,
    resolve_corpus_facts,
    snapshot_identity,
)
from codekg.csv_limits import serialized_csv_field_size_bytes, validate_csv_field_size

MAX_CORPUS_CSV_FIELD_SIZE = 128 * 1024 * 1024 + 4 * 1024


def _bounded_csv_reader(source, *, field_size_limit: int = MAX_CORPUS_CSV_FIELD_SIZE):
    """Read trusted generated CSV with a bounded limit derived from source caps.

    ``csv.field_size_limit`` is process-global; composition and validation are
    serial, and each reader resets the same production bound rather than
    temporarily mutating/restoring it around concurrent work.
    """

    if isinstance(field_size_limit, bool) or not 1 <= field_size_limit <= MAX_CORPUS_CSV_FIELD_SIZE:
        raise ValueError("CSV field size limit must be within the configured corpus bound")
    csv.field_size_limit(field_size_limit)
    return csv.reader(source)


def export_corpus(
    config: CorpusConfig | str | Path, output: str | Path, *, workers: int = 1
) -> dict:
    """Build per-snapshot ordinary exports plus a durable native-facts registry.

    Standard and native parsers share one bounded, file-at-a-time source read.
    Projection honors `workers`; extraction is serial and corpus resolution,
    graph composition, and validation remain disk-backed.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    overall_started = time.perf_counter()
    corpus = load_corpus_config(config) if isinstance(config, (str, Path)) else config
    output = Path(output).resolve()
    for snapshot in corpus.snapshots:
        if _overlap(output, snapshot.path):
            raise ValueError(f"output directory overlaps source root for {snapshot.alias}")
    output.mkdir(parents=True, exist_ok=True)
    identity_started = time.perf_counter()
    before_identities = _corpus_identities(corpus, output)
    identity_seconds = time.perf_counter() - identity_started
    generation = output / "generations" / str(time.time_ns())
    generation.mkdir(parents=True)
    registry_path = generation / "corpus.sqlite"
    database = create_native_registry(registry_path)
    records: list[dict] = []
    native_seconds = 0.0
    projection_seconds = 0.0
    projection_workers = 1
    catalog = None
    spool_batcher = None
    try:
        for snapshot in corpus.snapshots:
            identity_before = before_identities[snapshot.alias]
            native_started = time.perf_counter()
            from codekg.bulk_export import (
                CorpusSpoolBatcher,
                finalize_corpus_snapshot,
                stage_corpus_source_file,
            )

            snapshot_output = generation / "snapshots" / snapshot.alias
            spool_dir = snapshot_output / ".building" / "spools"
            spool_dir.mkdir(parents=True, exist_ok=True)
            catalog = sqlite3.connect(snapshot_output / ".building" / "schedule.sqlite")
            catalog.execute("CREATE TABLE spools (ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL)")
            catalog.execute(
                "CREATE TABLE markdown_files ("
                "source_path TEXT PRIMARY KEY, staged_path TEXT NOT NULL)"
            )
            markdown_dir = snapshot_output / ".building" / "markdown"
            spool_batcher = CorpusSpoolBatcher(spool_dir, catalog)
            staged_ordinal = 0

            def stage(
                path,
                raw,
                language,
                status,
                _snapshot=snapshot,
                _spool_batcher=spool_batcher,
                _catalog=catalog,
                _markdown_dir=markdown_dir,
            ):
                nonlocal staged_ordinal
                stage_corpus_source_file(
                    _snapshot.path,
                    path,
                    raw,
                    status,
                    language,
                    _spool_batcher,
                    _catalog,
                    staged_ordinal,
                    sql_config=effective_sql_config(_snapshot),
                    max_file_bytes=_snapshot.max_file_bytes,
                    markdown_dir=_markdown_dir,
                )
                staged_ordinal += 1

            native_counts = extract_snapshot_facts(database, snapshot, output, on_source=stage)
            spool_batcher.finish()
            spool_batcher = None
            catalog.commit()
            native_seconds += time.perf_counter() - native_started
            identity_started = time.perf_counter()
            identity_after = snapshot_identity(
                snapshot,
                output,
                dependency_identities=tuple(
                    before_identities[dep].revision for dep in snapshot.dependencies
                ),
            )
            identity_seconds += time.perf_counter() - identity_started
            if identity_before != identity_after:
                raise RuntimeError(f"source identity changed during extraction: {snapshot.alias}")
            database.executemany(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                (
                    (f"{snapshot.alias}.git_commit", identity_after.git_commit or ""),
                    (f"{snapshot.alias}.source_digest", identity_after.source_digest),
                    (f"{snapshot.alias}.fingerprint", identity_after.fingerprint),
                    (f"{snapshot.alias}.revision", identity_after.revision),
                ),
            )
            bulk_started = time.perf_counter()
            result, _, snapshot_projection_workers = finalize_corpus_snapshot(
                snapshot.path,
                snapshot_output,
                snapshot.alias,
                identity_after.revision,
                effective_sql_config(snapshot),
                catalog,
                markdown_dir,
                workers=workers,
            )
            catalog.close()
            catalog = None
            projection_seconds += time.perf_counter() - bulk_started
            projection_workers = max(projection_workers, snapshot_projection_workers)
            _import_sql_objects(database, snapshot.alias, snapshot_output)
            _import_python_owners(database, snapshot.alias, snapshot_output / "search.sqlite")
            records.append(
                {
                    "alias": snapshot.alias,
                    "logical_repo": snapshot.logical_repo,
                    "version": snapshot.version,
                    "role": snapshot.role,
                    "root_path": str(snapshot.path),
                    "git_commit": identity_after.git_commit,
                    "revision": identity_after.revision,
                    "source_digest": identity_after.source_digest,
                    "fingerprint": identity_after.fingerprint,
                    "dependencies": list(snapshot.dependencies),
                    "native_counts": native_counts,
                    "bulk_manifest": str(result.manifest_path.relative_to(output)),
                }
            )
        resolution_started = time.perf_counter()
        resolve_corpus_facts(database, corpus)
        resolution_seconds = time.perf_counter() - resolution_started
        supplemental_started = time.perf_counter()
        supplemental = _write_supplemental_graph(database, records, generation)
        supplemental_seconds = time.perf_counter() - supplemental_started
        database.commit()
    except BaseException:
        if spool_batcher is not None:
            spool_batcher.abort()
        if catalog is not None:
            catalog.close()
        database.close()
        shutil.rmtree(generation, ignore_errors=True)
        raise
    database.close()
    try:
        return _compose_and_publish(
            corpus,
            output,
            generation,
            registry_path,
            records,
            supplemental,
            native_seconds,
            projection_seconds,
            projection_workers,
            workers,
            overall_started,
            identity_seconds,
            resolution_seconds,
            supplemental_seconds,
        )
    except BaseException:
        (output / f".manifest.{generation.name}.json.tmp").unlink(missing_ok=True)
        shutil.rmtree(generation, ignore_errors=True)
        raise


def _compose_and_publish(
    corpus,
    output,
    generation,
    registry_path,
    records,
    supplemental,
    native_seconds,
    projection_seconds,
    projection_workers,
    workers,
    overall_started,
    identity_seconds,
    resolution_seconds,
    supplemental_seconds,
):
    identity_started = time.perf_counter()
    after_identities = _corpus_identities(corpus, output)
    identity_seconds += time.perf_counter() - identity_started
    for record in records:
        after = after_identities[record["alias"]]
        if after.revision != record["revision"]:
            raise RuntimeError(f"source identity changed during export: {record['alias']}")
    compose_started = time.perf_counter()
    graph = _compose_graph(records, supplemental, generation)
    compose_seconds = time.perf_counter() - compose_started
    generation_prefix = generation.relative_to(output)
    for section in ("nodes", "relationships"):
        for entry in graph[section].values():
            entry["file"] = str(generation_prefix / entry["file"])
    graph["output_dir"] = "."
    graph["search_stage"]["file"] = str(generation_prefix / graph["search_stage"]["file"])
    manifest = {
        "version": 1,
        "kind": "codekg-corpus",
        "output_dir": ".",
        "registry": str(registry_path.relative_to(output)),
        "snapshots": records,
        "metrics": {
            "native_extraction": "serial_file_at_a_time",
            "native_fact_workers": 1,
            "bulk_extract_workers": 1,
            "projection_workers": projection_workers,
            "native_seconds": round(native_seconds, 3),
            "bulk_export_seconds": round(projection_seconds, 3),
            "composition_seconds": round(compose_seconds, 3),
            "identity_seconds": round(identity_seconds, 3),
            "resolution_seconds": round(resolution_seconds, 3),
            "supplemental_projection_seconds": round(supplemental_seconds, 3),
            "elapsed_seconds": round(time.perf_counter() - overall_started, 3),
            "parent_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "child_peak_rss_max_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "rss_scope": "per-process maxima, not aggregate concurrent memory",
        },
        **graph,
    }
    temporary = output / f".manifest.{generation.name}.json.tmp"
    temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output / "manifest.json")
    return manifest


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _corpus_identities(corpus: CorpusConfig, output: Path) -> dict:
    by_alias = corpus.by_alias
    identities = {}

    def identity(alias: str):
        if alias in identities:
            return identities[alias]
        snapshot = by_alias[alias]
        dependencies = tuple(identity(dep).revision for dep in snapshot.dependencies)
        identities[alias] = snapshot_identity(snapshot, output, dependency_identities=dependencies)
        return identities[alias]

    for snapshot in corpus.snapshots:
        identity(snapshot.alias)
    return identities


def _import_sql_objects(database: sqlite3.Connection, alias: str, snapshot_output: Path) -> None:
    """Copy normalized SQL object identities from the existing resolver stage."""
    registry_path = snapshot_output / ".building" / "resolver.sqlite"
    if not registry_path.is_file():
        return
    source = sqlite3.connect(f"file:{registry_path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        rows = source.execute(
            "SELECT key,schema_name,kind,object_name,signature,owner_path FROM sqlobjects "
            "ORDER BY key"
        )
        database.executemany(
            "INSERT OR REPLACE INTO sqlobjects VALUES (?,?,?,?,?,?,?)",
            ((alias, *row) for row in rows),
        )
        database.commit()
    except sqlite3.OperationalError as error:
        # Python-only single-root exports do not create the SQL resolver schema.
        if "no such table" not in str(error):
            raise
    finally:
        source.close()


def _import_python_owners(database: sqlite3.Connection, alias: str, search_stage: Path) -> None:
    source = sqlite3.connect(f"file:{search_stage.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        rows = source.execute("SELECT key,path,qname,start_line FROM documents ORDER BY key")
        database.executemany(
            "INSERT OR REPLACE INTO python_owners VALUES (?,?,?,?,?)",
            ((alias, *row) for row in rows),
        )
        database.commit()
    finally:
        source.close()


_CORPUS_NODE_COLUMNS = {
    "CorpusSnapshot": (
        ("key", "key:ID(CodeKG)"),
        ("alias", "alias"),
        ("logical_repo", "logical_repo"),
        ("version", "version"),
        ("role", "role"),
        ("git_commit", "git_commit"),
        ("revision", "revision"),
        ("source_digest", "source_digest"),
        ("fingerprint", "fingerprint"),
        ("root_path", "root_path"),
    ),
    "NativeSymbol": (
        ("key", "key:ID(CodeKG)"),
        ("snapshot_alias", "snapshot_alias"),
        ("name", "name"),
        ("kind", "kind"),
        ("logical_id", "logical_id"),
        ("signature", "signature"),
        ("language", "language"),
        ("path", "path"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
        ("start_column", "start_column:int"),
        ("end_column", "end_column:int"),
        ("static", "static:boolean"),
        ("declaration", "declaration:boolean"),
        ("condition", "condition"),
        ("body_hash", "body_hash"),
        ("return_type", "return_type"),
        ("definition_hash", "definition_hash"),
        ("coverage", "coverage"),
        ("comparison_primary", "comparison_primary:boolean"),
    ),
    "Routine": (
        ("key", "key:ID(CodeKG)"),
        ("snapshot_alias", "snapshot_alias"),
        ("name", "name"),
        ("kind", "kind"),
        ("logical_id", "logical_id"),
        ("signature", "signature"),
        ("language", "language"),
        ("path", "path"),
        ("start_line", "start_line:int"),
        ("end_line", "end_line:int"),
        ("start_column", "start_column:int"),
        ("end_column", "end_column:int"),
        ("arity", "arity:int"),
        ("out_arg_count", "out_arg_count:int"),
        ("library", "library"),
        ("entrypoint", "entrypoint"),
        ("body_hash", "body_hash"),
        ("return_type", "return_type"),
        ("definition_hash", "definition_hash"),
        ("coverage", "coverage"),
        ("condition", "condition"),
        ("comparison_primary", "comparison_primary:boolean"),
    ),
    "SourceEvidence": (
        ("key", "key:ID(CodeKG)"),
        ("snapshot_alias", "snapshot_alias"),
        ("name", "name"),
        ("path", "path"),
        ("origin", "origin"),
        ("receiver_status", "receiver_status"),
        ("routine_kind", "routine_kind"),
        ("start_line", "start_line:int"),
        ("start_column", "start_column:int"),
        ("end_line", "end_line:int"),
        ("end_column", "end_column:int"),
        ("owner_key", "owner_key"),
        ("status", "status"),
        ("dynamic", "dynamic:boolean"),
        ("candidate_count", "candidate_count:int"),
        ("candidate_keys_json", "candidate_keys_json"),
        ("condition", "condition"),
    ),
    "CorpusDiagnostic": (
        ("key", "key:ID(CodeKG)"),
        ("snapshot_alias", "snapshot_alias"),
        ("path", "path"),
        ("category", "category"),
        ("severity", "severity"),
        ("line", "line:int"),
        ("column", "column:int"),
        ("message", "message"),
    ),
}
_SUPPLEMENTAL_REL_COLUMNS = (
    ("key", "key"),
    ("start", ":START_ID(CodeKG)"),
    ("end", ":END_ID(CodeKG)"),
    ("status", "status"),
    ("path", "path"),
    ("line", "line:int"),
    ("column", "column:int"),
    ("condition", "condition"),
    ("type", ":TYPE"),
)


def _write_supplemental_graph(
    db: sqlite3.Connection, records: list[dict], generation: Path
) -> dict:
    from codekg.bulk_export import _NODE_COLUMNS

    out = generation / "supplemental"
    out.mkdir(parents=True, exist_ok=True)
    nodes: dict[str, tuple[Path, int]] = {}
    relationships: dict[str, tuple[Path, int]] = {}
    snapshots = {record["alias"]: record for record in records}

    def owner_diagnostic(alias: str, path: str, message: str, fact: dict) -> None:
        ordinal = db.execute(
            "SELECT coalesce(max(ordinal),-1)+1 FROM diagnostics WHERE snapshot_alias=? AND path=?",
            (alias, path),
        ).fetchone()[0]
        diagnostic = {
            "category": "evidence_owner_unresolved",
            "severity": "warning",
            "line": fact.get("start_line"),
            "column": fact.get("start_column"),
            "message": message,
        }
        raw = json.dumps(diagnostic, sort_keys=True)
        db.execute(
            "INSERT OR IGNORE INTO diagnostics VALUES (?,?,?,?)",
            (alias, path, ordinal, raw),
        )
        material = json.dumps(
            (alias, snapshots[alias]["revision"], path, "diagnostics", ordinal),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        db.execute(
            "INSERT OR IGNORE INTO fact_keys VALUES (?,?,?,?,?)",
            (
                "fact:" + hashlib.sha256(material).hexdigest(),
                alias,
                path,
                "diagnostics",
                ordinal,
            ),
        )

    def key(alias: str, path: str, table: str, ordinal: int) -> str:
        row = db.execute(
            "SELECT key FROM fact_keys WHERE snapshot_alias=? AND path=? "
            "AND table_name=? AND ordinal=?",
            (alias, path, table, ordinal),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing corpus occurrence key: {alias}/{path}/{table}/{ordinal}")
        return row[0]

    def write_nodes(label: str, columns, rows) -> None:
        path = out / f"nodes_{label}.csv"
        count = _write_records(path, columns, rows, label=label)
        nodes[label] = (path, count)

    def write_rels(kind: str, rows) -> None:
        path = out / f"relationships_{kind}.csv"
        count = _write_records(path, _SUPPLEMENTAL_REL_COLUMNS, rows)
        relationships[kind] = (path, count)

    write_nodes(
        "CorpusSnapshot",
        _CORPUS_NODE_COLUMNS["CorpusSnapshot"],
        (
            {
                "key": f"{r['alias']}@{r['revision']}:corpus-snapshot",
                **{
                    k: r[k]
                    for k in (
                        "alias",
                        "logical_repo",
                        "version",
                        "role",
                        "git_commit",
                        "revision",
                        "source_digest",
                        "fingerprint",
                        "root_path",
                    )
                },
            }
            for r in records
        ),
    )

    def snapshot_links():
        for record in records:
            snapkey = f"{record['alias']}@{record['revision']}:corpus-snapshot"
            yield {
                "key": f"{snapkey}:repository",
                "start": snapkey,
                "end": record["alias"],
                "type": "SNAPSHOT_OF",
            }

    def dependency_links():
        for record in records:
            snapkey = f"{record['alias']}@{record['revision']}:corpus-snapshot"
            for dep in record["dependencies"]:
                target = snapshots[dep]
                yield {
                    "key": f"{snapkey}:depends:{dep}",
                    "start": snapkey,
                    "end": f"{dep}@{target['revision']}:corpus-snapshot",
                    "type": "DEPENDS_ON_SNAPSHOT",
                }

    def file_rows():
        for alias, path, language, loc in db.execute(
            "SELECT snapshot_alias,path,language,loc FROM files ORDER BY snapshot_alias,path"
        ):
            if language in {"python", "sql"}:
                continue
            yield {
                "key": f"{alias}@{snapshots[alias]['revision']}:{path}",
                "path": path,
                "language": language,
                "loc": loc,
                "parse_status": "native_staged",
                "diagnostic_count": 0,
            }

    write_nodes("File", _NODE_COLUMNS["File"], file_rows())

    for label, table, kind in (
        ("NativeSymbol", "symbols", "native"),
        ("Routine", "routines", "routine"),
        ("SourceEvidence", "evidence", "evidence"),
        ("CorpusDiagnostic", "diagnostics", "diagnostic"),
    ):

        def facts(table=table, label=label, kind=kind):
            for alias, path, ordinal, raw in db.execute(
                f"SELECT snapshot_alias,path,ordinal,fact FROM {table} "
                "ORDER BY snapshot_alias,path,ordinal"
            ):
                fact = json.loads(raw)
                if label == "NativeSymbol":
                    name = fact["name"]
                    primary = True
                    if fact.get("declaration"):
                        definition = db.execute(
                            "SELECT 1 FROM symbols WHERE snapshot_alias=? "
                            "AND json_extract(fact,'$.name')=? "
                            "AND json_extract(fact,'$.kind')=? "
                            "AND json_extract(fact,'$.declaration')=0 LIMIT 1",
                            (alias, name, fact.get("kind", "function")),
                        ).fetchone()
                        earlier_declaration = db.execute(
                            "SELECT 1 FROM symbols WHERE snapshot_alias=? AND path=? "
                            "AND json_extract(fact,'$.name')=? "
                            "AND json_extract(fact,'$.kind')=? "
                            "AND json_extract(fact,'$.signature')=? "
                            "AND json_extract(fact,'$.declaration')=1 AND ordinal<? LIMIT 1",
                            (
                                alias,
                                path,
                                name,
                                fact.get("kind", "function"),
                                fact.get("signature"),
                                ordinal,
                            ),
                        ).fetchone()
                        primary = definition is None and earlier_declaration is None
                    if not fact.get("declaration") and not fact.get("static"):
                        earlier = db.execute(
                            "SELECT 1 FROM symbols WHERE snapshot_alias=? "
                            "AND json_extract(fact,'$.name')=? "
                            "AND json_extract(fact,'$.declaration')=0 "
                            "AND coalesce(json_extract(fact,'$.signature'),'')=? "
                            "AND coalesce(json_extract(fact,'$.body_hash'),'')=? "
                            "AND coalesce(json_extract(fact,'$.condition'),'')=? "
                            "AND coalesce(json_extract(fact,'$.kind'),'function')=? "
                            "AND (path<? OR (path=? AND ordinal<?)) LIMIT 1",
                            (
                                alias,
                                name,
                                fact.get("signature", ""),
                                fact.get("body_hash") or "",
                                fact.get("condition") or "",
                                fact.get("kind", "function"),
                                path,
                                path,
                                ordinal,
                            ),
                        ).fetchone()
                        primary = earlier is None
                    row = {
                        **fact,
                        "key": key(alias, path, table, ordinal),
                        "snapshot_alias": alias,
                        "logical_id": (
                            f"c:{fact.get('kind', 'function')}:{path}:{name}"
                            if fact.get("static")
                            else f"c:{fact.get('kind', 'function')}:{name}"
                        ),
                        "language": "c",
                        "path": path,
                        "coverage": "declaration" if fact.get("declaration") else "source",
                        "comparison_primary": primary,
                    }
                elif label == "Routine":
                    name = fact["name"]
                    earlier = db.execute(
                        "SELECT 1 FROM routines WHERE snapshot_alias=? "
                        "AND json_extract(fact,'$.schema_name') IS ? "
                        "AND json_extract(fact,'$.name')=? "
                        "AND json_extract(fact,'$.signature') IS ? "
                        "AND json_extract(fact,'$.kind') IS ? "
                        "AND json_extract(fact,'$.language') IS ? "
                        "AND json_extract(fact,'$.library') IS ? "
                        "AND json_extract(fact,'$.entrypoint') IS ? "
                        "AND json_extract(fact,'$.body_hash') IS ? "
                        "AND json_extract(fact,'$.definition_hash') IS ? "
                        "AND json_extract(fact,'$.return_type') IS ? "
                        "AND json_extract(fact,'$.condition') IS ? "
                        "AND coalesce(json_extract(fact,'$.default_arg_count'),0)=? "
                        "AND coalesce(json_extract(fact,'$.variadic_arg_count'),0)=? "
                        "AND (path<? OR (path=? AND ordinal<?)) LIMIT 1",
                        (
                            alias,
                            fact.get("schema_name"),
                            name,
                            fact.get("signature"),
                            fact.get("kind", "function"),
                            fact.get("language"),
                            fact.get("library"),
                            fact.get("entrypoint"),
                            fact.get("body_hash"),
                            fact.get("definition_hash"),
                            fact.get("return_type"),
                            fact.get("condition"),
                            fact.get("default_arg_count", 0),
                            fact.get("variadic_arg_count", 0),
                            path,
                            path,
                            ordinal,
                        ),
                    ).fetchone()
                    row = {
                        **fact,
                        "key": key(alias, path, table, ordinal),
                        "snapshot_alias": alias,
                        "logical_id": (
                            f"{fact.get('language', 'unknown')}:{fact.get('kind', 'function')}:"
                            f"{fact.get('schema_name')}.{name}"
                        ),
                        "language": fact.get("language", "unknown"),
                        "path": path,
                        "comparison_primary": earlier is None,
                    }
                elif label == "SourceEvidence":
                    name = fact.get("object_name") or "<dynamic>"
                    evidence_key = key(alias, path, table, ordinal)
                    resolved = db.execute(
                        "SELECT target_key,status FROM edges WHERE source_key=? "
                        "ORDER BY target_key LIMIT 33",
                        (evidence_key,),
                    ).fetchall()
                    candidate_keys = [item[0] for item in resolved[:32]]
                    status = (
                        "dynamic"
                        if fact.get("dynamic")
                        else resolved[0][1]
                        if len(resolved) == 1
                        else "ambiguous"
                        if resolved
                        else "unresolved"
                    )
                    if (
                        fact.get("origin") == "python_execute"
                        and fact.get("receiver_status") != "verified"
                    ):
                        status = "receiver_unverified"
                    owner_key = None
                    if fact.get("origin") == "native_call" and fact.get("owner_qname"):
                        owners = db.execute(
                            "SELECT ordinal FROM symbols WHERE snapshot_alias=? AND path=? "
                            "AND json_extract(fact,'$.name')=? "
                            "AND json_extract(fact,'$.start_line')=? "
                            "AND json_extract(fact,'$.declaration')=0 LIMIT 2",
                            (
                                alias,
                                path,
                                fact["owner_qname"],
                                fact.get("owner_line"),
                            ),
                        ).fetchall()
                        if len(owners) == 1:
                            owner_key = key(alias, path, "symbols", owners[0][0])
                    elif fact.get("origin") == "routine_body" and fact.get("owner_qname"):
                        owners = db.execute(
                            "SELECT ordinal FROM routines WHERE snapshot_alias=? AND path=? "
                            "AND (json_extract(fact,'$.schema_name') || '.' || "
                            "json_extract(fact,'$.name'))=? "
                            "AND json_extract(fact,'$.start_line')=? LIMIT 2",
                            (alias, path, fact["owner_qname"], fact.get("owner_line")),
                        ).fetchall()
                        if len(owners) == 1:
                            owner_key = key(alias, path, "routines", owners[0][0])
                    elif fact.get("owner_qname"):
                        owners = db.execute(
                            "SELECT key FROM python_owners WHERE snapshot_alias=? AND path=? "
                            "AND start_line=? AND (qname=? OR qname LIKE '%.' || ?) LIMIT 2",
                            (
                                alias,
                                path,
                                fact.get("owner_line"),
                                fact["owner_qname"],
                                fact["owner_qname"],
                            ),
                        ).fetchall()
                        if len(owners) == 1:
                            owner_key = owners[0][0]
                    if (
                        fact.get("owner_qname")
                        and owner_key is None
                        and fact.get("origin") != "native_call"
                    ):
                        owner_diagnostic(
                            alias,
                            path,
                            f"could not uniquely resolve evidence owner {fact['owner_qname']}",
                            fact,
                        )
                    row = {
                        "key": evidence_key,
                        "snapshot_alias": alias,
                        "name": name,
                        "path": path,
                        "origin": fact.get("origin"),
                        "receiver_status": fact.get("receiver_status"),
                        "routine_kind": fact.get("routine_kind"),
                        "start_line": fact.get("start_line"),
                        "start_column": fact.get("start_column"),
                        "end_line": fact.get("end_line"),
                        "end_column": fact.get("end_column"),
                        "owner_key": owner_key,
                        "status": status,
                        "dynamic": bool(fact.get("dynamic")),
                        "candidate_count": len(resolved),
                        "candidate_keys_json": json.dumps(candidate_keys),
                        "condition": fact.get("condition"),
                    }
                    file_key = f"{alias}@{snapshots[alias]['revision']}:{path}"
                    db.execute(
                        "INSERT OR IGNORE INTO edges "
                        "(source_key,target_key,kind,status,path,line,column_no,condition,ordinal) "
                        "VALUES (?,?,?,?,?,?,?,?,0)",
                        (
                            file_key,
                            evidence_key,
                            "HAS_EVIDENCE",
                            "conditional" if fact.get("condition") else "exact",
                            path,
                            fact.get("start_line"),
                            fact.get("start_column"),
                            fact.get("condition"),
                        ),
                    )
                    if owner_key:
                        owner_status = "conditional" if fact.get("condition") else "exact"
                        db.execute(
                            "INSERT OR IGNORE INTO edges "
                            "(source_key,target_key,kind,status,path,line,column_no,condition,"
                            "ordinal) "
                            "VALUES (?,?,?,?,?,?,?,?,0)",
                            (
                                owner_key,
                                evidence_key,
                                "HAS_EVIDENCE",
                                owner_status,
                                path,
                                fact.get("start_line"),
                                fact.get("start_column"),
                                fact.get("condition"),
                            ),
                        )
                else:
                    name = fact.get("category", "diagnostic")
                    row = {
                        "key": key(alias, path, table, ordinal),
                        "snapshot_alias": alias,
                        "path": path,
                        **fact,
                    }
                yield row

        write_nodes(label, _CORPUS_NODE_COLUMNS[label], facts())
    for relkind, table, fact_kind in (
        ("HAS_NATIVE_SYMBOL", "symbols", "native"),
        ("HAS_ROUTINE", "routines", "routine"),
        ("HAS_EVIDENCE", "evidence", "evidence"),
        ("HAS_CORPUS_DIAGNOSTIC", "diagnostics", "diagnostic"),
    ):

        def links(table=table, fact_kind=fact_kind, relkind=relkind):
            for alias, path, ordinal, raw in db.execute(
                f"SELECT snapshot_alias,path,ordinal,fact FROM {table} "
                "ORDER BY snapshot_alias,path,ordinal"
            ):
                end = key(alias, path, table, ordinal)
                fact = json.loads(raw)
                yield {
                    "key": f"{end}:file",
                    "start": f"{alias}@{snapshots[alias]['revision']}:{path}",
                    "end": end,
                    "status": "conditional" if fact.get("condition") else "exact",
                    "path": path,
                    "line": fact.get("start_line", fact.get("line")),
                    "column": fact.get("start_column", fact.get("column")),
                    "condition": fact.get("condition"),
                    "type": relkind,
                }

            if relkind == "HAS_EVIDENCE":
                for source, target, status, path, line, column, condition, ordinal in db.execute(
                    "SELECT source_key,target_key,status,path,line,column_no,condition,ordinal "
                    "FROM edges WHERE kind='HAS_EVIDENCE' AND source_key IN "
                    "(SELECT key FROM fact_keys UNION SELECT key FROM python_owners) "
                    "ORDER BY source_key,target_key,path,line,column_no,ordinal"
                ):
                    identity = (source, target, path, line, column, condition, ordinal)
                    stable = (
                        "owner:"
                        + hashlib.sha256(
                            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
                        ).hexdigest()
                    )
                    yield {
                        "key": stable,
                        "start": source,
                        "end": target,
                        "status": status,
                        "path": path,
                        "line": line,
                        "column": column,
                        "condition": condition,
                        "type": relkind,
                    }

        write_rels(relkind, links())

    write_rels("SNAPSHOT_OF", snapshot_links())
    write_rels("DEPENDS_ON_SNAPSHOT", dependency_links())

    edge_kinds = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT kind FROM edges WHERE kind<>'HAS_EVIDENCE' ORDER BY kind"
        )
    ]
    for relkind in edge_kinds:
        rows = (
            {
                "key": "edge:"
                + hashlib.sha256(
                    json.dumps(
                        (source, target, kind, path, line, column, condition, ordinal),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "start": source,
                "end": target,
                "status": status,
                "path": path,
                "line": line,
                "column": column,
                "condition": condition,
                "type": kind,
            }
            for source, target, kind, status, path, line, column, condition, ordinal in db.execute(
                "SELECT source_key,target_key,kind,status,path,line,column_no,condition,ordinal "
                "FROM edges WHERE kind=? "
                "ORDER BY source_key,target_key,path,line,column_no,ordinal",
                (relkind,),
            )
        )
        write_rels(relkind, rows)
    return {"nodes": nodes, "relationships": relationships}


def _write_records(path: Path, columns, rows, *, label: str | None = None) -> int:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([column for _, column in columns] + ([":LABEL"] if label else []))
        count = 0
        for row in rows:
            writer.writerow(
                [_csv_value(row.get(name)) for name, _ in columns] + ([label] if label else [])
            )
            count += 1
    return count


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ";".join(map(str, value))
    return str(value)


def _compose_graph(records: list[dict], supplemental: dict, generation: Path) -> dict:
    from codekg.bulk_export import _NODE_COLUMNS, _REL_COLUMNS, load_bulk_export

    graph_dir = generation / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    sources: dict[str, list[Path]] = {}
    rel_sources: dict[str, list[Path]] = {}
    for record in records:
        exported = load_bulk_export(generation.parent.parent / record["bulk_manifest"])
        for label, paths in exported.node_groups.items():
            sources.setdefault(label, []).extend(paths)
        for kind, paths in exported.relationship_groups.items():
            rel_sources.setdefault(kind, []).extend(paths)
    for label, (path, _) in supplemental["nodes"].items():
        sources.setdefault(label, []).append(path)
    for kind, (path, _) in supplemental["relationships"].items():
        rel_sources.setdefault(kind, []).append(path)

    node_manifest = {}
    rel_manifest = {}
    counts = {"nodes": 0, "relationships": 0}
    max_csv_field_size = 0
    for label, paths in sources.items():
        columns = (
            _CORPUS_NODE_COLUMNS[label] if label in _CORPUS_NODE_COLUMNS else _NODE_COLUMNS[label]
        )
        dest = graph_dir / f"nodes_{label}.csv"
        count, field_size = _merge_csv_group(paths, dest, columns, label=label)
        max_csv_field_size = max(max_csv_field_size, field_size)
        node_manifest[label] = {"file": str(dest.relative_to(generation)), "count": count}
        counts["nodes"] += count
    for kind, paths in rel_sources.items():
        columns = _REL_COLUMNS.get(kind, _SUPPLEMENTAL_REL_COLUMNS)
        dest = graph_dir / f"relationships_{kind}.csv"
        count, field_size = _merge_csv_group(paths, dest, columns)
        max_csv_field_size = max(max_csv_field_size, field_size)
        rel_manifest[kind] = {"file": str(dest.relative_to(generation)), "count": count}
        counts["relationships"] += count
    _validate_composed_graph(node_manifest, rel_manifest, graph_dir)
    counts.update({f"nodes_{name}": value["count"] for name, value in node_manifest.items()})
    counts.update({f"relationships_{name}": value["count"] for name, value in rel_manifest.items()})
    search_count = _merge_search_stages(records, generation)
    return {
        "nodes": node_manifest,
        "relationships": rel_manifest,
        "counts": counts,
        "max_csv_field_size_bytes": validate_csv_field_size(max_csv_field_size),
        "output_dir": str(generation.relative_to(generation.parent.parent)),
        "search_stage": {
            "version": 1,
            "file": "search.sqlite",
            "documents": search_count,
        },
    }


def _validate_composed_graph(nodes: dict, relationships: dict, graph_dir: Path) -> None:
    """Validate IDs/endpoints in a disk-backed index before manifest publication."""
    check_path = graph_dir / "endpoint-validation.sqlite"
    check = sqlite3.connect(check_path)
    try:
        check.executescript(
            "CREATE TABLE node_keys (key TEXT PRIMARY KEY);"
            "CREATE TABLE endpoints (start TEXT NOT NULL,end TEXT NOT NULL);"
        )
        for entry in nodes.values():
            with (graph_dir / entry["file"].split("graph/", 1)[-1]).open(
                encoding="utf-8", newline=""
            ) as source:
                reader = _bounded_csv_reader(source)
                header = next(reader)
                key_index = header.index("key:ID(CodeKG)")
                for row in reader:
                    check.execute("INSERT INTO node_keys VALUES (?)", (row[key_index],))
        for entry in relationships.values():
            with (graph_dir / entry["file"].split("graph/", 1)[-1]).open(
                encoding="utf-8", newline=""
            ) as source:
                reader = _bounded_csv_reader(source)
                header = next(reader)
                start_index = header.index(":START_ID(CodeKG)")
                end_index = header.index(":END_ID(CodeKG)")
                for row in reader:
                    check.execute(
                        "INSERT INTO endpoints VALUES (?,?)",
                        (row[start_index], row[end_index]),
                    )
        dangling = check.execute(
            "SELECT count(*) FROM endpoints e "
            "LEFT JOIN node_keys s ON s.key=e.start "
            "LEFT JOIN node_keys t ON t.key=e.end "
            "WHERE s.key IS NULL OR t.key IS NULL"
        ).fetchone()[0]
        if dangling:
            sample = check.execute(
                "SELECT e.start,e.end FROM endpoints e "
                "LEFT JOIN node_keys s ON s.key=e.start "
                "LEFT JOIN node_keys t ON t.key=e.end "
                "WHERE s.key IS NULL OR t.key IS NULL LIMIT 3"
            ).fetchall()
            raise ValueError(f"composed corpus graph has {dangling} dangling endpoints: {sample!r}")
    finally:
        check.close()
        check_path.unlink(missing_ok=True)


def _merge_search_stages(records: list[dict], generation: Path) -> int:
    from codekg.bulk_export import load_bulk_export
    from codekg.bulk_search import _abort_stage, _new_stage, _publish_stage

    stage_path = generation / "search.sqlite"
    partial, destination = _new_stage(stage_path)
    count = 0
    try:
        for record in records:
            exported = load_bulk_export(generation.parent.parent / record["bulk_manifest"])
            if exported.search_stage is None:
                raise ValueError(f"snapshot {record['alias']} has no search stage")
            source = sqlite3.connect(
                f"file:{exported.search_stage.resolve()}?mode=ro&immutable=1", uri=True
            )
            try:
                repositories = source.execute(
                    "SELECT repo,commit_value FROM repositories"
                ).fetchall()
                if repositories != [(record["alias"], record["revision"])]:
                    raise ValueError(f"search-stage identity mismatch for {record['alias']}")
                destination.executemany("INSERT INTO repositories VALUES (?,?)", repositories)
                rows = source.execute(
                    "SELECT key,repo,commit_value,path,qname,kind,signature,start_line,end_line,"
                    "text "
                    "FROM documents ORDER BY key"
                )
                for row in rows:
                    destination.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                    count += 1
            finally:
                source.close()
        _publish_stage(stage_path, partial, destination, count)
    except BaseException:
        _abort_stage(destination, partial)
        raise
    return count


def _merge_csv_group(
    paths: list[Path], destination: Path, columns, *, label: str | None = None
) -> tuple[int, int]:
    headers = [value for _, value in columns] + ([":LABEL"] if label else [])
    property_headers = {
        header.split(":", 1)[0]: header for header in headers if not header.startswith(":")
    }
    from codekg.bulk_export import _NODE_COLUMNS, _PYTHON_DEFINES_COLUMNS, _REL_COLUMNS

    known_headers = {tuple(headers)}
    known_headers.update(
        tuple([header for _, header in columns] + [":LABEL"]) for columns in _NODE_COLUMNS.values()
    )
    known_headers.update(
        tuple(header for _, header in columns) for columns in _REL_COLUMNS.values()
    )
    known_headers.add(tuple(header for _, header in _PYTHON_DEFINES_COLUMNS))
    known_headers.update(
        tuple([header for _, header in columns] + [":LABEL"])
        for columns in _CORPUS_NODE_COLUMNS.values()
    )
    known_headers.add(tuple(header for _, header in _SUPPLEMENTAL_REL_COLUMNS))
    with destination.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(headers)
        total = 0
        max_field_size = 0
        # Header sidecars describe the remaining parts of this one logical
        # group. The default is re-established for every new group call.
        active_headers = headers
        for path in paths:
            with path.open(encoding="utf-8", newline="") as source:
                reader = _bounded_csv_reader(source)
                try:
                    first = next(reader)
                except StopIteration:
                    continue
                # A headerless data row can legitimately begin with a key such
                # as "keyboard"; only exact known layouts are schema headers.
                first_is_header = tuple(first) in known_headers
                if "/headers/" in path.as_posix() and first_is_header:
                    active_headers = first
                    continue
                if first_is_header:
                    active_headers = first
                    rows = reader
                else:
                    rows = chain((first,), reader)
                indexes = {}
                for index, header in enumerate(active_headers):
                    base = header.split(":", 1)[0] if not header.startswith(":") else header
                    indexes[property_headers.get(base, header)] = index
                for row in rows:
                    values = [
                        row[indexes[h]] if h in indexes and indexes[h] < len(row) else ""
                        for h in headers
                    ]
                    max_field_size = max(
                        max_field_size,
                        *(serialized_csv_field_size_bytes(value) for value in values),
                    )
                    writer.writerow(values)
                    total += 1
    return total, max_field_size
