from __future__ import annotations

from codekg.ir import RepositoryIR
from codekg.neo4j_client import Neo4jClient, get_client


def load_repository(
    repo: RepositoryIR,
    *,
    replace: bool,
    client: Neo4jClient | None = None,
) -> dict[str, int]:
    db = client or get_client()
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
    )

    file_rows = [
        {
            "key": _key(repo, file.path),
            "repo_key": repo.repo_name,
            "path": file.path,
            "language": file.language,
            "loc": file.loc,
            "module_key": _key(repo, f"module:{file.module_qname}"),
            "module_qname": file.module_qname,
            "module_name": file.module_qname.rsplit(".", maxsplit=1)[-1],
        }
        for file in repo.files
    ]
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (r:Repository {key: row.repo_key})
        MERGE (f:File {key: row.key})
        SET f.path = row.path, f.language = row.language, f.loc = row.loc
        MERGE (r)-[:CONTAINS]->(f)
        MERGE (m:Module {key: row.module_key})
        SET m.name = row.module_name, m.qname = row.module_qname, m.language = row.language
        MERGE (f)-[:DEFINES]->(m)
        """,
        {"rows": file_rows},
    )

    type_rows = []
    type_rows_by_qname: dict[str, dict[str, object]] = {}
    callable_rows = []
    symbol_rows_by_qname: dict[str, dict[str, object]] = {}
    for file in repo.files:
        file_key = _key(repo, file.path)
        for symbol in file.symbols:
            row = {
                "key": _symbol_key(repo, file.path, symbol.qname, symbol.start_line),
                "file_key": file_key,
                "name": symbol.name,
                "qname": symbol.qname,
                "signature": symbol.signature,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "cyclomatic": symbol.cyclomatic,
                "parent_qname": symbol.parent_qname,
                "docstring": symbol.docstring,
            }
            if symbol.kind == "type":
                type_row = {**row, "kind": "class"}
                type_rows.append(type_row)
                type_rows_by_qname.setdefault(symbol.qname, type_row)
            else:
                label = "Method" if symbol.kind == "method" else "Function"
                callable_row = {**row, "label": label}
                callable_rows.append(callable_row)
                symbol_rows_by_qname.setdefault(symbol.qname, callable_row)

    db.execute_write(
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
        {"rows": type_rows},
    )

    function_rows = [row for row in callable_rows if row["label"] == "Function"]
    method_rows = [row for row in callable_rows if row["label"] == "Method"]
    _merge_callables(db, "Function", function_rows)
    _merge_callables(db, "Method", method_rows)

    has_method_rows = _has_method_rows(type_rows_by_qname, method_rows)
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (t:Type {key: row.type_key})
        MATCH (m:Method {key: row.method_key})
        MERGE (t)-[:HAS_METHOD]->(m)
        """,
        {"rows": has_method_rows},
    )

    import_rows = [
        {
            "file_key": _key(repo, file.path),
            "module_key": _key(repo, f"external-module:{import_ir.module}"),
            "module": import_ir.module,
            "name": import_ir.name,
            "alias": import_ir.alias,
        }
        for file in repo.files
        for import_ir in file.imports
    ]
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (f:File {key: row.file_key})
        MERGE (m:Module {key: row.module_key})
        SET m.name = row.module, m.qname = row.module
        MERGE (f)-[rel:IMPORTS]->(m)
        SET rel.name = row.name, rel.alias = row.alias
        """,
        {"rows": import_rows},
    )

    inheritance_rows = _inheritance_rows(repo, type_rows_by_qname)
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (child:Type {key: row.child_key})
        MATCH (parent:Type {key: row.parent_key})
        MERGE (child)-[:INHERITS]->(parent)
        """,
        {"rows": inheritance_rows},
    )

    doc_rows, doc_chunk_rows, mention_rows = _doc_rows(
        repo,
        {**type_rows_by_qname, **symbol_rows_by_qname},
    )
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (r:Repository {key: row.repo_key})
        MERGE (d:Document {key: row.key})
        SET d.path = row.path,
            d.doc_type = row.doc_type
        MERGE (r)-[:CONTAINS]->(d)
        """,
        {"rows": doc_rows},
    )
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (d:Document {key: row.doc_key})
        MERGE (c:DocChunk {key: row.key})
        SET c.path = row.path,
            c.heading_path = row.heading_path,
            c.chunk_index = row.chunk_index,
            c.start_line = row.start_line,
            c.end_line = row.end_line
        MERGE (d)-[:HAS_CHUNK]->(c)
        """,
        {"rows": doc_chunk_rows},
    )
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (c:DocChunk {key: row.chunk_key})
        MATCH (s {key: row.symbol_key})
        WHERE s:Function OR s:Method OR s:Type
        MERGE (c)-[:MENTIONS]->(s)
        """,
        {"rows": mention_rows},
    )

    call_rows = _call_rows(repo, symbol_rows_by_qname)
    db.execute_write(
        """
        UNWIND $rows AS row
        MATCH (caller {key: row.caller_key})
        MATCH (callee {key: row.callee_key})
        MERGE (caller)-[rel:CALLS]->(callee)
        SET rel.resolution = row.resolution,
            rel.line = row.line
        """,
        {"rows": call_rows},
    )

    return {
        "nodes": 1
        + len(file_rows)
        + len(type_rows)
        + len(callable_rows)
        + len(doc_rows)
        + len(doc_chunk_rows),
        "imports": len(import_rows),
        "inherits": len(inheritance_rows),
        "calls": len(call_rows),
        "docs": len(doc_rows),
        "doc_chunks": len(doc_chunk_rows),
        "mentions": len(mention_rows),
    }


def delete_repository_by_name(repo_name: str, client: Neo4jClient | None = None) -> int:
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
    )
    return int(rows[0]["deleted"]) if rows else 0


def _merge_callables(db: Neo4jClient, label: str, rows: list[dict[str, object]]) -> None:
    db.execute_write(
        f"""
        UNWIND $rows AS row
        MATCH (f:File {{key: row.file_key}})
        MERGE (n:{label} {{key: row.key}})
        SET n.name = row.name,
            n.qname = row.qname,
            n.signature = row.signature,
            n.start_line = row.start_line,
            n.end_line = row.end_line,
            n.cyclomatic = row.cyclomatic,
            n.docstring = row.docstring
        MERGE (f)-[:CONTAINS]->(n)
        """,
        {"rows": rows},
    )


def _has_method_rows(
    type_rows_by_qname: dict[str, dict[str, object]],
    method_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows = []
    for method in method_rows:
        parent_qname = method.get("parent_qname")
        if not parent_qname:
            continue
        parent = type_rows_by_qname.get(str(parent_qname))
        if parent:
            rows.append({"type_key": parent["key"], "method_key": method["key"]})
    return rows


def _inheritance_rows(
    repo: RepositoryIR,
    type_rows_by_qname: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()

    for file in repo.files:
        import_aliases = _import_aliases(file)
        for inheritance in file.inheritance:
            child = type_rows_by_qname.get(inheritance.type_qname)
            if not child:
                continue
            for qname in _candidate_qnames(
                inheritance.base_name,
                inheritance.base_qname,
                import_aliases,
            ):
                parent = type_rows_by_qname.get(qname)
                if not parent:
                    continue
                edge_key = (child["key"], parent["key"])
                if edge_key in seen or child["key"] == parent["key"]:
                    continue
                seen.add(edge_key)
                rows.append({"child_key": child["key"], "parent_key": parent["key"]})
                break
    return rows


def _doc_rows(
    repo: RepositoryIR,
    symbol_rows_by_qname: dict[str, dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    documents: list[dict[str, object]] = []
    chunks: list[dict[str, object]] = []
    mentions: list[dict[str, object]] = []
    for doc in repo.docs:
        doc_key = _doc_key(repo, doc.path)
        documents.append(
            {
                "key": doc_key,
                "repo_key": repo.repo_name,
                "path": doc.path,
                "doc_type": doc.doc_type,
            }
        )
        for chunk in doc.chunks:
            chunk_key = _doc_chunk_key(repo, doc.path, chunk.heading_path, chunk.chunk_index)
            chunks.append(
                {
                    "key": chunk_key,
                    "doc_key": doc_key,
                    "path": doc.path,
                    "heading_path": chunk.heading_path,
                    "chunk_index": chunk.chunk_index,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                }
            )
            for mention in chunk.mentions:
                symbol = symbol_rows_by_qname.get(mention)
                if symbol:
                    mentions.append({"chunk_key": chunk_key, "symbol_key": symbol["key"]})
    return documents, chunks, mentions


def _call_rows(
    repo: RepositoryIR,
    symbol_rows_by_qname: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()

    for file in repo.files:
        import_aliases = _import_aliases(file)
        for call in file.calls:
            caller = symbol_rows_by_qname.get(call.caller_qname)
            if not caller:
                continue

            callees = _resolve_callees(
                call.callee_name,
                call.callee_qname,
                call.receiver,
                caller,
                import_aliases,
                symbol_rows_by_qname,
            )
            for callee in callees:
                edge_key = (str(caller["key"]), str(callee["key"]), call.line)
                if edge_key in seen or caller["key"] == callee["key"]:
                    continue
                seen.add(edge_key)
                rows.append(
                    {
                        "caller_key": caller["key"],
                        "callee_key": callee["key"],
                        "resolution": "heuristic",
                        "line": call.line,
                    }
                )
    return rows


def _resolve_callees(
    callee_name: str,
    callee_qname: str | None,
    receiver: str | None,
    caller: dict[str, object],
    import_aliases: dict[str, str],
    symbol_rows_by_qname: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []

    if receiver in {"self", "cls"}:
        parent_qname = caller.get("parent_qname")
        if parent_qname:
            row = symbol_rows_by_qname.get(f"{parent_qname}.{callee_name}")
            if row:
                return [row]
        return []

    for qname in _candidate_qnames(callee_name, callee_qname, import_aliases):
        row = symbol_rows_by_qname.get(qname)
        if row:
            candidates.append(row)

    deduped: list[dict[str, object]] = []
    seen_keys: set[object] = set()
    for candidate in candidates:
        key = candidate["key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(candidate)
    return deduped


def _candidate_qnames(
    callee_name: str,
    callee_qname: str | None,
    import_aliases: dict[str, str],
) -> list[str]:
    candidates = []
    if callee_qname:
        candidates.append(callee_qname)
        first, _, rest = callee_qname.partition(".")
        if rest and first in import_aliases:
            candidates.append(f"{import_aliases[first]}.{rest}")

    if callee_name in import_aliases:
        candidates.append(import_aliases[callee_name])
    return candidates


def _import_aliases(file) -> dict[str, str]:
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


def _doc_key(repo: RepositoryIR, path: str) -> str:
    return _key(repo, f"doc:{path}")


def _doc_chunk_key(repo: RepositoryIR, path: str, heading_path: str, chunk_index: int) -> str:
    return _key(repo, f"{path}#{heading_path}:{chunk_index}")
