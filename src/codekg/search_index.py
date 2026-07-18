from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from codekg.docs import DocChunk, chunk_docs
from codekg.ingest import iter_doc_files
from codekg.neo4j_client import Neo4jClient, get_client
from codekg.source_text import SourceText, extract_symbol_text
from codekg.zvec_store import (
    DocChunkDoc,
    SymbolDoc,
    delete_ids,
    list_doc_ids,
    list_symbol_ids,
    open_read,
    open_write,
    upsert_doc_chunks,
    upsert_symbol_docs,
)

NAME_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\W]+")


def rebuild_repo_search_index(
    repo: str | None = None,
    *,
    zvec_path: str | None = None,
    client: Neo4jClient | None = None,
) -> dict[str, int]:
    db = client or get_client()
    collection = open_write(zvec_path)
    rows = list(iter_symbol_rows(repo=repo, client=db))
    repo_rows = list(iter_repository_rows(repo=repo, client=db))
    repos = {str(row["repo"]) for row in repo_rows}
    if repo:
        repos.add(repo)
    docs = [symbol_doc_from_row(row) for row in rows]
    doc_chunks = [doc for repo_row in repo_rows for doc in doc_chunk_docs_from_repo(repo_row)]
    existing_ids = list_symbol_ids(collection, repo=repo) | list_doc_ids(collection, repo=repo)
    live_ids = {doc.id for doc in docs} | {doc.id for doc in doc_chunks}
    symbol_count = upsert_symbol_docs(collection, docs)
    doc_count = upsert_doc_chunks(collection, doc_chunks)
    delete_ids(collection, existing_ids - live_ids)
    if hasattr(collection, "flush"):
        collection.flush()
    return {"symbols": symbol_count, "docs": doc_count, "repositories": len(repos)}


def validate_search_index_consistency(
    repo: str | None = None,
    *,
    zvec_path: str | None = None,
    client: Neo4jClient | None = None,
) -> dict[str, object]:
    graph_ids = {str(row["key"]) for row in iter_symbol_rows(repo=repo, client=client)}
    graph_doc_ids = {str(row["key"]) for row in iter_doc_chunk_rows(repo=repo, client=client)}
    collection = open_read(zvec_path)
    zvec_ids = list_symbol_ids(collection, repo=repo)
    zvec_doc_ids = list_doc_ids(collection, repo=repo)
    missing_in_zvec = sorted(graph_ids - zvec_ids)
    orphaned_in_zvec = sorted(zvec_ids - graph_ids)
    missing_docs_in_zvec = sorted(graph_doc_ids - zvec_doc_ids)
    orphaned_docs_in_zvec = sorted(zvec_doc_ids - graph_doc_ids)
    return {
        "ok": not (
            missing_in_zvec or orphaned_in_zvec or missing_docs_in_zvec or orphaned_docs_in_zvec
        ),
        "graph_symbols": len(graph_ids),
        "zvec_symbols": len(zvec_ids),
        "graph_docs": len(graph_doc_ids),
        "zvec_docs": len(zvec_doc_ids),
        "missing_in_zvec": missing_in_zvec,
        "orphaned_in_zvec": orphaned_in_zvec,
        "missing_docs_in_zvec": missing_docs_in_zvec,
        "orphaned_docs_in_zvec": orphaned_docs_in_zvec,
    }


def iter_symbol_rows(
    *,
    repo: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, Any]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method OR s:Type)
          AND ($repo IS NULL OR r.repo_name = $repo)
        RETURN s.key AS key,
               CASE
                   WHEN s:Method THEN 'method'
                   WHEN s:Function THEN 'function'
                   WHEN s:Type THEN 'type'
                   ELSE 'symbol'
               END AS kind,
               s.name AS name,
               s.qname AS qname,
               s.signature AS signature,
               s.start_line AS start_line,
               s.end_line AS end_line,
               f.path AS path,
               r.repo_name AS repo,
               r.commit AS commit,
               r.root_path AS root_path
        ORDER BY r.repo_name, f.path, s.start_line
        """,
        {"repo": repo},
        max_rows=1_000_000,
    )


def iter_repository_rows(
    *,
    repo: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, Any]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository)
        WHERE $repo IS NULL OR r.repo_name = $repo
        RETURN r.repo_name AS repo,
               r.commit AS commit,
               r.root_path AS root_path
        ORDER BY repo
        """,
        {"repo": repo},
        max_rows=10000,
    )


def iter_doc_chunk_rows(
    *,
    repo: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, Any]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository)-[:CONTAINS]->(:Document)-[:HAS_CHUNK]->(c:DocChunk)
        WHERE $repo IS NULL OR r.repo_name = $repo
        RETURN c.key AS key
        ORDER BY c.key
        """,
        {"repo": repo},
        max_rows=1_000_000,
    )


def symbol_doc_from_row(row: dict[str, Any]) -> SymbolDoc:
    return SymbolDoc(
        id=str(row["key"]),
        source="symbol",
        repo=str(row["repo"]),
        commit=str(row.get("commit") or ""),
        path=str(row.get("path") or ""),
        qname=str(row.get("qname") or ""),
        kind=str(row.get("kind") or _kind(row.get("labels"))),
        signature=str(row.get("signature") or ""),
        start_line=int(row.get("start_line") or 0),
        end_line=int(row.get("end_line") or 0),
        text=build_symbol_text(row),
    )


def doc_chunk_docs_from_repo(row: dict[str, Any]) -> list[DocChunkDoc]:
    root_path = row.get("root_path")
    if not root_path:
        return []
    root = Path(str(root_path))
    if not root.is_dir():
        return []
    repo = str(row.get("repo") or root.name)
    commit = str(row.get("commit") or "")
    return [
        doc_chunk_doc_from_chunk(chunk, root=root, repo=repo, commit=commit)
        for path in iter_doc_files(root)
        for chunk in chunk_docs([path], [])
    ]


def doc_chunk_doc_from_chunk(
    chunk: DocChunk,
    *,
    root: Path,
    repo: str,
    commit: str,
) -> DocChunkDoc:
    path = _relative_doc_path(chunk.path, root)
    key = f"{repo}@{commit}:doc:{path}:{chunk.start_line}"
    mention_text = " ".join(chunk.mentions)
    text = "\n".join(
        part
        for part in [
            chunk.heading_path,
            mention_text,
            chunk.text,
        ]
        if part
    )
    return DocChunkDoc(
        id=key,
        source="doc",
        repo=repo,
        commit=commit,
        path=path,
        qname=chunk.heading_path,
        kind="doc",
        signature=chunk.heading_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        text=text,
    )


def build_symbol_text(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("name") or ""),
        normalize_name(str(row.get("name") or row.get("qname") or "")),
        str(row.get("qname") or ""),
        str(row.get("signature") or ""),
    ]
    source = _source_text(row)
    if source.docstring:
        parts.append(source.docstring)
    if source.leading_comment:
        parts.append(source.leading_comment)
    parts.extend(source.inline_comments[:8])
    return "\n".join(part for part in parts if part).strip()


def normalize_name(value: str) -> str:
    tail = value.rsplit(".", maxsplit=1)[-1]
    words = [word.lower() for word in NAME_BOUNDARY_RE.split(tail) if word]
    return " ".join(words)


def _source_text(row: dict[str, Any]) -> SourceText:
    root = row.get("root_path")
    path = row.get("path")
    if not root or not path:
        return SourceText()
    return _source_text_for_path(
        str(root),
        str(path),
        str(row.get("qname") or ""),
        int(row.get("start_line") or 0),
        int(row.get("end_line") or 0),
    )


@lru_cache(maxsize=4096)
def _source_text_for_path(
    root_path: str,
    path: str,
    qname: str,
    start_line: int,
    end_line: int,
) -> SourceText:
    source_path = Path(root_path) / path
    return extract_symbol_text(source_path, qname, start_line, end_line)


def _kind(labels: object) -> str:
    label_set = set(labels or [])
    if "Method" in label_set:
        return "method"
    if "Function" in label_set:
        return "function"
    if "Type" in label_set:
        return "type"
    return "symbol"


def _relative_doc_path(path: str, root: Path) -> str:
    doc_path = Path(path)
    try:
        return doc_path.relative_to(root).as_posix()
    except ValueError:
        return doc_path.as_posix()
