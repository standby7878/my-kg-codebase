"""Deterministic Neo4j-admin CSV exports for CodeKG snapshots."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class BulkExport:
    """The files and counts published by :func:`export_repositories`."""

    manifest_path: Path
    output_dir: Path
    node_files: Mapping[str, Path]
    relationship_files: Mapping[str, Path]
    counts: Mapping[str, int]


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


def export_repositories(repositories: Iterable[RepositoryIR], output_dir: Path) -> BulkExport:
    """Export snapshots and publish a JSON manifest after validation."""
    graph = _build_graph(tuple(repositories))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    node_files = {label: output_dir / f"nodes_{label}.csv" for label in sorted(graph["nodes"])}
    relationship_files = {
        kind: output_dir / f"relationships_{kind}.csv" for kind in sorted(graph["relationships"])
    }
    for label, rows in graph["nodes"].items():
        _write_csv(node_files[label], _NODE_COLUMNS[label], rows, label)
    for kind, rows in graph["relationships"].items():
        _write_csv(relationship_files[kind], _REL_COLUMNS[kind], rows, kind)
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
    return BulkExport(manifest_path, output_dir, node_files, relationship_files, graph["counts"])


def load_bulk_export(manifest_path: Path) -> BulkExport:
    """Load a previously published export manifest."""
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir = Path(data["output_dir"])
    if not output_dir.is_absolute():
        output_dir = manifest_path.parent / output_dir
    node_files = {label: output_dir / entry["file"] for label, entry in data["nodes"].items()}
    relationship_files = {
        kind: output_dir / entry["file"] for kind, entry in data["relationships"].items()
    }
    return BulkExport(manifest_path, output_dir, node_files, relationship_files, data["counts"])


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
                "key": relationship_key if relationship_key is not None else key,
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
        type_rows: list[dict[str, Any]] = []
        callable_rows: list[dict[str, Any]] = []
        owner_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
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
            node("Module", m)
            rel("DEFINES", f["key"], m["key"], {}, f"{m['key']}:defines")
            for diagnostic in _diagnostic_rows(repo):
                if diagnostic["file_key"] == f["key"]:
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
    path: Path, columns: tuple[tuple[str, str], ...], rows: list[dict[str, Any]], kind: str
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
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
    return str(value)
