"""Persist the structural CodeKG intermediate representation in Neo4j.

``CallSite`` nodes are the source-of-truth for syntactic calls.  The temporary
``CALLS`` relationships written here are a compatibility projection for legacy
queries. ``EXACT_CALLS`` is the bounded-traversal projection: it is emitted
only from an exact ``CallSite`` resolution in the current snapshot.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from codekg.ir import CallIR, FileIR, RepositoryIR
from codekg.neo4j_client import Neo4jClient, get_client
from codekg.resolver import CallResolution, SymbolRef, resolve_call_sites

DEFAULT_BATCH_SIZE = 1_000


def load_repository(
    repo: RepositoryIR,
    *,
    replace: bool,
    client: Neo4jClient | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Load one repository snapshot and return observable ingestion counts."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    db = client or get_client()
    batch_count = 0
    if replace:
        delete_repository_by_name(repo.repo_name, client=db)

    db.execute_write(
        """
        MERGE (r:Repository {key: $key})
        SET r.repo_name = $repo_name,
            r.commit = $commit,
            r.root_path = $root_path,
            r.indexed_at = datetime()
        """,
        {
            "key": repo.repo_name,
            "repo_name": repo.repo_name,
            "commit": repo.commit,
            "root_path": repo.root_path,
        },
        operation="load repository metadata",
    )

    file_rows = [_file_row(repo, file) for file in repo.files]
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (r:Repository {key: row.repo_key})
        MERGE (f:File {key: row.key})
        SET f.path = row.path,
            f.language = row.language,
            f.loc = row.loc,
            f.parse_status = row.parse_status,
            f.diagnostic_count = row.diagnostic_count
        MERGE (r)-[:CONTAINS]->(f)
        MERGE (m:Module {key: row.module_key})
        SET m.name = row.module_name, m.qname = row.module_qname, m.language = row.language
        MERGE (f)-[:DEFINES]->(m)
        """,
        file_rows,
        batch_size,
        operation="load files and modules",
    )

    diagnostic_rows = _diagnostic_rows(repo)
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file_key})
        MERGE (diagnostic:ParseDiagnostic {key: row.key})
        SET diagnostic.category = row.category,
            diagnostic.severity = row.severity,
            diagnostic.line = row.line,
            diagnostic.column = row.column,
            diagnostic.message = row.message
        MERGE (f)-[:HAS_DIAGNOSTIC]->(diagnostic)
        """,
        diagnostic_rows,
        batch_size,
        operation="load parse diagnostics",
    )

    module_init_rows = [_module_init_row(repo, file) for file in repo.files if file.module_init]
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file_key})
        MATCH (m:Module {key: row.module_key})
        MERGE (init:ModuleInit {key: row.key})
        SET init.qname = row.qname,
            init.name = '<module>',
            init.start_line = row.start_line,
            init.end_line = row.end_line
        MERGE (f)-[:CONTAINS]->(init)
        MERGE (m)-[:INITIALIZES]->(init)
        """,
        module_init_rows,
        batch_size,
        operation="load module initializers",
    )

    type_rows: list[dict[str, object]] = []
    callable_rows: list[dict[str, object]] = []
    owner_rows_by_file_qname: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in module_init_rows:
        owner_rows_by_file_qname[(str(row["path"]), str(row["qname"]))].append(row)
    for file in repo.files:
        file_key = _key(repo, file.path)
        for symbol in file.symbols:
            row: dict[str, object] = {
                "key": _symbol_key(repo, file.path, symbol.qname, symbol.start_line),
                "file_key": file_key,
                "path": file.path,
                "name": symbol.name,
                "qname": symbol.qname,
                "signature": symbol.signature,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "cyclomatic": symbol.cyclomatic,
                "parent_qname": symbol.parent_qname,
            }
            if symbol.kind == "type":
                type_rows.append({**row, "kind": "class"})
            else:
                callable_row = {**row, "label": "Method" if symbol.kind == "method" else "Function"}
                callable_rows.append(callable_row)
                owner_rows_by_file_qname[(file.path, symbol.qname)].append(callable_row)

    type_rows_by_qname: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in type_rows:
        type_rows_by_qname[str(row["qname"])].append(row)

    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file_key})
        MERGE (t:Type {key: row.key})
        SET t.name = row.name,
            t.qname = row.qname,
            t.signature = row.signature,
            t.kind = row.kind,
            t.start_line = row.start_line,
            t.end_line = row.end_line,
            t.cyclomatic = row.cyclomatic
        MERGE (f)-[:CONTAINS]->(t)
        """,
        type_rows,
        batch_size,
        operation="load types",
    )

    batch_count += _merge_callables(
        db,
        "Function",
        [row for row in callable_rows if row["label"] == "Function"],
        batch_size,
        operation="load functions",
    )
    batch_count += _merge_callables(
        db,
        "Method",
        [row for row in callable_rows if row["label"] == "Method"],
        batch_size,
        operation="load methods",
    )

    has_method_rows = _has_method_rows(type_rows_by_qname, callable_rows)
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (t:Type {key: row.type_key})
        MATCH (m:Method {key: row.method_key})
        MERGE (t)-[:HAS_METHOD]->(m)
        """,
        has_method_rows,
        batch_size,
        operation="load method ownership",
    )

    import_rows = [
        {
            "file_key": _key(repo, file.path),
            "module_key": _key(repo, f"external-module:{import_ir.module}"),
            "module": import_ir.module,
            "name": import_ir.name,
            "alias": import_ir.alias,
            "key": _key(
                repo,
                ":".join(
                    [
                        file.path,
                        "import",
                        import_ir.module,
                        import_ir.name,
                        import_ir.alias or "<none>",
                    ]
                ),
            ),
        }
        for file in repo.files
        for import_ir in file.imports
    ]
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file_key})
        MERGE (m:Module {key: row.module_key})
        SET m.name = row.module, m.qname = row.module
        MERGE (f)-[rel:IMPORTS {key: row.key}]->(m)
        SET rel.name = row.name,
            rel.alias = row.alias
        """,
        import_rows,
        batch_size,
        operation="load imports",
    )

    inheritance_rows = _inheritance_rows(repo, type_rows_by_qname)
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (child:Type {key: row.child_key})
        MATCH (parent:Type {key: row.parent_key})
        MERGE (child)-[:INHERITS]->(parent)
        """,
        inheritance_rows,
        batch_size,
        operation="load inheritance",
    )

    callsite_rows, resolutions = _callsite_rows(
        repo,
        owner_rows_by_file_qname,
        callable_rows,
        type_rows,
    )
    resolved_rows = _resolved_call_rows(callsite_rows, resolutions)
    # Re-loading the same snapshot must converge even if resolution behavior
    # changes.  Raw CallSite facts remain; only derived resolution projections
    # are removed and rebuilt.
    db.execute_write(
        """
        MATCH ()-[rel:RESOLVES_TO|CALLS|EXACT_CALLS|CONSTRUCTS]->()
        WHERE rel.key STARTS WITH $prefix
        DELETE rel
        """,
        {"prefix": f"{repo.repo_name}@{repo.commit}:"},
        operation="clear current snapshot call resolutions",
    )
    batch_count += 1
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MERGE (site:CallSite {key: row.key})
        SET site.path = row.path,
            site.owner_key = row.owner_key,
            site.owner_qname = row.owner_qname,
            site.raw_callee = row.raw_callee,
            site.callee_name = row.callee_name,
            site.callee_qname_hint = row.callee_qname_hint,
            site.receiver_kind = row.receiver_kind,
            site.start_line = row.start_line,
            site.start_column = row.start_column,
            site.end_line = row.end_line,
            site.end_column = row.end_column,
            site.ordinal = row.ordinal,
            site.status = row.status,
            site.resolution_strategy = row.resolution_strategy,
            site.candidate_count = row.candidate_count,
            site.candidate_keys = row.candidate_keys,
            site.initializer_candidate_count = row.initializer_candidate_count,
            site.initializer_candidate_keys = row.initializer_candidate_keys
        """,
        callsite_rows,
        batch_size,
        operation="load call sites",
    )
    linked_callsite_rows = [row for row in callsite_rows if row["owner_key"] is not None]
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (owner {key: row.owner_key})
        MATCH (site:CallSite {key: row.key})
        MERGE (owner)-[:HAS_CALLSITE]->(site)
        """,
        linked_callsite_rows,
        batch_size,
        operation="link call site owners",
    )
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (caller {key: row.caller_key})
        MATCH (callee {key: row.callee_key})
        MERGE (caller)-[rel:CALLS {key: row.callsite_key}]->(callee)
        SET rel.resolution = row.resolution,
            rel.line = row.line,
            rel.column = row.column
        """,
        resolved_rows,
        batch_size,
        operation="load resolved call projections",
    )

    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (caller {key: row.caller_key})
        MATCH (callee {key: row.callee_key})
        MERGE (caller)-[rel:EXACT_CALLS {key: row.callsite_key}]->(callee)
        SET rel.resolution = row.resolution,
            rel.line = row.line,
            rel.column = row.column
        """,
        resolved_rows,
        batch_size,
        operation="load exact call traversal projections",
    )

    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (site:CallSite {key: row.callsite_key})
        MATCH (callee {key: row.callee_key})
        MERGE (site)-[rel:RESOLVES_TO {key: row.callsite_key}]->(callee)
        SET rel.strategy = row.resolution,
            rel.confidence = 'exact'
        """,
        resolved_rows,
        batch_size,
        operation="link resolved call sites",
    )

    construction_rows = _construction_rows(callsite_rows, resolutions)
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (site:CallSite {key: row.callsite_key})
        MATCH (type:Type {key: row.type_key})
        MERGE (site)-[rel:CONSTRUCTS {key: row.callsite_key}]->(type)
        SET rel.resolution = row.resolution,
            rel.line = row.line,
            rel.column = row.column
        """,
        construction_rows,
        batch_size,
        operation="link resolved constructions",
    )
    batch_count += _write_batched(
        db,
        """
        UNWIND $rows AS row
        MATCH (owner {key: row.owner_key})
        MATCH (type:Type {key: row.type_key})
        MERGE (owner)-[rel:CONSTRUCTS {key: row.callsite_key}]->(type)
        SET rel.resolution = row.resolution,
            rel.line = row.line,
            rel.column = row.column
        """,
        construction_rows,
        batch_size,
        operation="load construction owner projections",
    )

    status_counts = dict(sorted(Counter(str(row["status"]) for row in callsite_rows).items()))
    return {
        "nodes": (
            1
            + len(file_rows)
            + len(diagnostic_rows)
            + len(module_init_rows)
            + len(type_rows)
            + len(callable_rows)
        ),
        "module_inits": len(module_init_rows),
        "imports": len(import_rows),
        "inherits": len(inheritance_rows),
        "call_sites": len(callsite_rows),
        "resolved_call_sites": len(resolved_rows),
        "callsite_statuses": status_counts,
        "files_with_parse_errors": sum(file.parse_status == "error" for file in repo.files),
        "parse_diagnostics": sum(len(file.diagnostics) for file in repo.files),
        "batches": batch_count,
    }


def delete_repository_by_name(repo_name: str, client: Neo4jClient | None = None) -> int:
    """Delete every keyed node belonging to all snapshots of ``repo_name``."""

    db = client or get_client()
    rows = db.execute_write(
        """
        MATCH (r:Repository {repo_name: $repo_name})
        WITH r, $repo_name + '@' AS prefix
        OPTIONAL MATCH (n)
        WHERE n.key STARTS WITH prefix
        WITH r, collect(DISTINCT n) AS nodes, count(n) AS deleted_nodes
        FOREACH (node IN nodes | DETACH DELETE node)
        DETACH DELETE r
        RETURN deleted_nodes + 1 AS deleted
        """,
        {"repo_name": repo_name},
        operation="delete repository snapshot",
    )
    return int(rows[0]["deleted"]) if rows else 0


def _file_row(repo: RepositoryIR, file: FileIR) -> dict[str, object]:
    return {
        "key": _key(repo, file.path),
        "repo_key": repo.repo_name,
        "path": file.path,
        "language": file.language,
        "loc": file.loc,
        "module_key": _key(repo, f"module:{file.module_qname}"),
        "module_qname": file.module_qname,
        "module_name": file.module_qname.rsplit(".", maxsplit=1)[-1],
        "parse_status": file.parse_status,
        "diagnostic_count": len(file.diagnostics),
    }


def _module_init_row(repo: RepositoryIR, file: FileIR) -> dict[str, object]:
    assert file.module_init is not None
    return {
        "key": _key(repo, f"{file.path}:module-init"),
        "file_key": _key(repo, file.path),
        "module_key": _key(repo, f"module:{file.module_qname}"),
        "path": file.path,
        "qname": file.module_init.qname,
        "start_line": file.module_init.start_line,
        "end_line": file.module_init.end_line,
    }


def _diagnostic_rows(repo: RepositoryIR) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file in repo.files:
        for ordinal, diagnostic in enumerate(file.diagnostics, start=1):
            rows.append(
                {
                    "key": _key(repo, f"{file.path}:diagnostic:{ordinal}"),
                    "file_key": _key(repo, file.path),
                    "category": diagnostic.category,
                    "severity": diagnostic.severity,
                    "line": diagnostic.line,
                    "column": diagnostic.column,
                    "message": diagnostic.message,
                }
            )
    return rows


def _merge_callables(
    db: Neo4jClient,
    label: str,
    rows: list[dict[str, object]],
    batch_size: int,
    operation: str,
) -> int:
    return _write_batched(
        db,
        f"""
        UNWIND $rows AS row
        MATCH (f:File {{key: row.file_key}})
        MERGE (n:{label} {{key: row.key}})
        SET n.name = row.name,
            n.qname = row.qname,
            n.signature = row.signature,
            n.start_line = row.start_line,
            n.end_line = row.end_line,
            n.cyclomatic = row.cyclomatic
        MERGE (f)-[:CONTAINS]->(n)
        """,
        rows,
        batch_size,
        operation=operation,
    )


def _write_batched(
    db: Neo4jClient,
    query: str,
    rows: list[dict[str, object]],
    batch_size: int,
    *,
    operation: str,
) -> int:
    batches = 0
    for batch in _batched(rows, batch_size):
        db.execute_write(query, {"rows": batch}, operation=operation)
        batches += 1
    return batches


def _batched(rows: list[dict[str, object]], batch_size: int) -> Iterable[list[dict[str, object]]]:
    for start in range(0, len(rows), batch_size):
        yield rows[start : start + batch_size]


def _has_method_rows(
    type_rows_by_qname: dict[str, list[dict[str, object]]],
    callable_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for method in callable_rows:
        if method["label"] != "Method":
            continue
        parent_qname = method.get("parent_qname")
        parents = type_rows_by_qname.get(str(parent_qname), [])
        if len(parents) == 1:
            rows.append({"type_key": parents[0]["key"], "method_key": method["key"]})
    return rows


def _inheritance_rows(
    repo: RepositoryIR,
    type_rows_by_qname: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for file in repo.files:
        import_aliases = _import_aliases(file)
        for inheritance in file.inheritance:
            children = type_rows_by_qname.get(inheritance.type_qname, [])
            if len(children) != 1:
                continue
            for qname in _candidate_qnames(
                inheritance.base_name,
                inheritance.base_qname,
                import_aliases,
            ):
                parents = type_rows_by_qname.get(qname, [])
                if len(parents) != 1:
                    continue
                edge_key = (children[0]["key"], parents[0]["key"])
                if edge_key not in seen and children[0]["key"] != parents[0]["key"]:
                    seen.add(edge_key)
                    rows.append({"child_key": edge_key[0], "parent_key": edge_key[1]})
                break
    return rows


def _callsite_rows(
    repo: RepositoryIR,
    owner_rows_by_file_qname: dict[tuple[str, str], list[dict[str, object]]],
    callable_rows: list[dict[str, object]],
    type_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], tuple[CallResolution, ...]]:
    """Build persistent raw call-site rows and snapshot-local conclusions."""

    owner_refs = {
        key: tuple(_symbol_ref(row, kind=str(row.get("label", "module_init"))) for row in rows)
        for key, rows in owner_rows_by_file_qname.items()
    }
    resolutions = resolve_call_sites(
        repo,
        owners_by_file_qname=owner_refs,
        callables=(_symbol_ref(row, kind=str(row["label"]).lower()) for row in callable_rows),
        types=(_symbol_ref(row, kind="type") for row in type_rows),
    )
    callsite_rows: list[dict[str, object]] = []
    for resolution in resolutions:
        call = resolution.call
        key = _callsite_key(repo, resolution.path, resolution.owner_key, call)
        callsite_rows.append(
            {
                "key": key,
                "path": resolution.path,
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
        )
    return callsite_rows, resolutions


def _resolved_call_rows(
    callsite_rows: list[dict[str, object]],
    resolutions: tuple[CallResolution, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for callsite, resolution in zip(callsite_rows, resolutions, strict=True):
        target_key = resolution.target_key
        resolution_status = resolution.status
        if resolution.is_constructor:
            target_key = resolution.initializer_target_key
            resolution_status = resolution.initializer_status
        if target_key is None or resolution.owner_key is None:
            continue
        rows.append(
            {
                "callsite_key": callsite["key"],
                "caller_key": resolution.owner_key,
                "callee_key": target_key,
                "resolution": resolution_status,
                "line": resolution.call.start_line,
                "column": resolution.call.start_column,
            }
        )
    return rows


def _construction_rows(
    callsite_rows: list[dict[str, object]],
    resolutions: tuple[CallResolution, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for callsite, resolution in zip(callsite_rows, resolutions, strict=True):
        if (
            not resolution.is_constructor
            or resolution.owner_key is None
            or resolution.construction_target_key is None
        ):
            continue
        rows.append(
            {
                "callsite_key": callsite["key"],
                "owner_key": resolution.owner_key,
                "type_key": resolution.construction_target_key,
                "resolution": resolution.status,
                "line": resolution.call.start_line,
                "column": resolution.call.start_column,
            }
        )
    return rows


def _symbol_ref(row: dict[str, object], *, kind: str) -> SymbolRef:
    return SymbolRef(
        key=str(row["key"]),
        qname=str(row["qname"]),
        path=str(row["path"]),
        kind=kind,
        parent_qname=str(row["parent_qname"]) if row.get("parent_qname") is not None else None,
    )


def _candidate_qnames(
    callee_name: str,
    callee_qname: str | None,
    import_aliases: dict[str, str],
) -> list[str]:
    candidates: list[str] = []
    if callee_qname:
        candidates.append(callee_qname)
        first, _, rest = callee_qname.partition(".")
        if rest and first in import_aliases:
            candidates.append(f"{import_aliases[first]}.{rest}")
    if callee_name in import_aliases:
        candidates.append(import_aliases[callee_name])
    return candidates


def _import_aliases(file: FileIR) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for import_ir in file.imports:
        imported_qname = (
            import_ir.module
            if import_ir.name == import_ir.module
            else f"{import_ir.module}.{import_ir.name}"
        )
        if import_ir.alias:
            aliases[import_ir.alias] = imported_qname
        else:
            aliases[import_ir.name] = imported_qname
            aliases.setdefault(import_ir.module.split(".", maxsplit=1)[0], import_ir.module)
    return aliases


def _key(repo: RepositoryIR, suffix: str) -> str:
    return f"{repo.repo_name}@{repo.commit}:{suffix}"


def _symbol_key(repo: RepositoryIR, path: str, qname: str, start_line: int) -> str:
    return _key(repo, f"{path}:{qname}:{start_line}")


def _callsite_key(repo: RepositoryIR, path: str, owner_key: object | None, call: CallIR) -> str:
    owner_identity = str(owner_key) if owner_key is not None else call.owner_qname
    return _key(
        repo,
        ":".join(
            [
                path,
                "callsite",
                owner_identity,
                str(call.start_line),
                str(call.start_column),
                str(call.end_line),
                str(call.end_column),
                str(call.ordinal),
            ]
        ),
    )


def symbol_key(repo: RepositoryIR, path: str, qname: str, start_line: int) -> str:
    """Return the stable Neo4j key shared with the zvec description record."""

    return _symbol_key(repo, path, qname, start_line)
