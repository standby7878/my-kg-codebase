from __future__ import annotations

import re
from typing import Literal

from codekg.neo4j_client import Neo4jClient, get_client
from codekg.zvec_store import ZvecUnavailableError
from codekg.zvec_store import open_read as open_zvec_read
from codekg.zvec_store import search_symbols as zvec_search_symbols

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]
SearchMode = Literal["graph", "lexical"]


def search_symbols(
    q: str,
    *,
    kind: SymbolKind | None = None,
    repo: str | None = None,
    limit: int = 25,
    mode: SearchMode = "graph",
    zvec_path: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    if mode == "lexical":
        return _search_symbols_lexical(
            q,
            kind=kind,
            repo=repo,
            limit=limit,
            zvec_path=zvec_path,
            client=client,
        )

    db = client or get_client()
    label_filter = _label_filter(kind)
    fulltext_query = _fulltext_query(q)
    query = f"""
    CALL db.index.fulltext.queryNodes("code_symbol_search", $fulltext_query)
    YIELD node AS s, score
    MATCH (f:File)-[:CONTAINS]->(s)
    MATCH (r:Repository)-[:CONTAINS]->(f)
    WHERE (s:Function OR s:Method OR s:Type)
      {label_filter}
      AND ($repo IS NULL OR r.repo_name = $repo)
    RETURN s.key AS key,
           labels(s) AS labels,
           s.name AS name,
           s.qname AS qname,
           s.signature AS signature,
           s.start_line AS start_line,
           s.end_line AS end_line,
           f.path AS file,
           r.repo_name AS repo,
           r.commit AS commit
    ORDER BY score DESC, s.qname
    LIMIT $limit
    """
    return db.execute_read(
        query,
        {"fulltext_query": fulltext_query, "repo": repo, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def _search_symbols_lexical(
    q: str,
    *,
    kind: SymbolKind | None,
    repo: str | None,
    limit: int,
    zvec_path: str | None,
    client: Neo4jClient | None,
) -> list[dict[str, object]]:
    if kind == "type":
        return []
    try:
        collection = open_zvec_read(zvec_path)
        hits = zvec_search_symbols(collection, q, repo=repo, kind=kind, limit=_limit(limit))
    except ZvecUnavailableError as exc:
        raise ZvecUnavailableError(
            "zvec lexical search is unavailable; run normal `codekg index`."
        ) from exc

    if not hits:
        return []
    hits = hits[: _limit(limit)]
    keys = list(dict.fromkeys(str(hit["key"]) for hit in hits))
    rows = _symbol_rows_by_key(client or get_client(), keys, limit)
    rows_by_key = {str(row["key"]): row for row in rows}
    merged: list[dict[str, object]] = []
    for hit in hits:
        key = str(hit["key"])
        if key in rows_by_key:
            merged.append(_merge_zvec_hit(hit, rows_by_key))
    return merged


def _symbol_rows_by_key(
    db: Neo4jClient,
    ids: list[str],
    limit: int,
) -> list[dict[str, object]]:
    return db.execute_read(
        """
        MATCH (f:File)-[:CONTAINS]->(s)
        MATCH (r:Repository)-[:CONTAINS]->(f)
        WHERE (s:Function OR s:Method)
          AND s.key IN $keys
        RETURN s.key AS key,
               labels(s) AS labels,
               s.name AS name,
               s.qname AS qname,
               s.signature AS signature,
               s.start_line AS start_line,
               s.end_line AS end_line,
               f.path AS file,
               r.repo_name AS repo,
               r.commit AS commit
        """,
        {"keys": ids},
        max_rows=_limit(limit),
    )


def _merge_zvec_hit(
    hit: dict[str, object],
    rows_by_key: dict[str, dict[str, object]],
) -> dict[str, object]:
    key = str(hit["key"])
    row = dict(rows_by_key[key])
    fields = hit.get("fields")
    if isinstance(fields, dict):
        row["snippet"] = _snippet(str(fields.get("text") or ""))
    row["score"] = hit.get("score")
    return row


def _snippet(text: str, *, max_len: int = 240) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= max_len:
        return collapsed
    return f"{collapsed[: max_len - 1].rstrip()}..."


def get_definition(
    identifier: str,
    *,
    repo: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (f:File)-[:CONTAINS]->(s)
        MATCH (r:Repository)-[:CONTAINS]->(f)
        WHERE (s:Function OR s:Method OR s:Type)
          AND (s.key = $identifier OR s.qname = $identifier)
          AND ($repo IS NULL OR r.repo_name = $repo)
        RETURN s.key AS key,
               labels(s) AS labels,
               s.name AS name,
               s.qname AS qname,
               s.signature AS signature,
               s.start_line AS start_line,
               s.end_line AS end_line,
               s.cyclomatic AS cyclomatic,
               f.path AS file,
               r.repo_name AS repo,
               r.commit AS commit
        ORDER BY r.repo_name, f.path, s.start_line
        LIMIT 10
        """,
        {"identifier": identifier, "repo": repo},
        max_rows=10,
    )


def find_importers(
    module_qname: str,
    *,
    repo: str | None = None,
    limit: int = 100,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (f:File)-[rel:IMPORTS]->(m:Module)
        MATCH (r:Repository)-[:CONTAINS]->(f)
        WHERE m.qname = $module_qname
          AND ($repo IS NULL OR r.repo_name = $repo)
        RETURN r.repo_name AS repo,
               r.commit AS commit,
               f.path AS file,
               m.qname AS module,
               rel.name AS imported_name,
               rel.alias AS alias
        ORDER BY repo, file, imported_name
        LIMIT $limit
        """,
        {"module_qname": module_qname, "repo": repo, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def find_callers(
    qname: str,
    *,
    depth: int = 1,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        f"""
        CALL {{
          MATCH (callee:Function {{qname: $qname}})
          MATCH path = (caller)-[:CALLS*1..{_depth(depth)}]->(callee)
          WHERE caller:Function OR caller:Method
          RETURN caller.key AS key,
                 caller.qname AS qname,
                 caller.signature AS signature,
                 length(path) AS depth
          UNION
          MATCH (callee:Method {{qname: $qname}})
          MATCH path = (caller)-[:CALLS*1..{_depth(depth)}]->(callee)
          WHERE caller:Function OR caller:Method
          RETURN caller.key AS key,
                 caller.qname AS qname,
                 caller.signature AS signature,
                 length(path) AS depth
        }}
        RETURN DISTINCT key, qname, signature, depth
        ORDER BY depth, qname
        LIMIT $limit
        """,
        {"qname": qname, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def find_callees(
    qname: str,
    *,
    depth: int = 1,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        f"""
        CALL {{
          MATCH (caller:Function {{qname: $qname}})
          MATCH path = (caller)-[:CALLS*1..{_depth(depth)}]->(callee)
          WHERE callee:Function OR callee:Method
          RETURN callee.key AS key,
                 callee.qname AS qname,
                 callee.signature AS signature,
                 length(path) AS depth
          UNION
          MATCH (caller:Method {{qname: $qname}})
          MATCH path = (caller)-[:CALLS*1..{_depth(depth)}]->(callee)
          WHERE callee:Function OR callee:Method
          RETURN callee.key AS key,
                 callee.qname AS qname,
                 callee.signature AS signature,
                 length(path) AS depth
        }}
        RETURN DISTINCT key, qname, signature, depth
        ORDER BY depth, qname
        LIMIT $limit
        """,
        {"qname": qname, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def trace_call_path(
    from_qname: str,
    to_qname: str,
    *,
    max_depth: int = 8,
    limit: int = 5,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        f"""
        CALL {{
          MATCH (source:Function {{qname: $from_qname}})
          MATCH (target:Function {{qname: $to_qname}})
          MATCH path = shortestPath((source)-[:CALLS*1..{_depth(max_depth)}]->(target))
          RETURN [node IN nodes(path) | node.qname] AS path,
                 length(path) AS depth
          UNION
          MATCH (source:Function {{qname: $from_qname}})
          MATCH (target:Method {{qname: $to_qname}})
          MATCH path = shortestPath((source)-[:CALLS*1..{_depth(max_depth)}]->(target))
          RETURN [node IN nodes(path) | node.qname] AS path,
                 length(path) AS depth
          UNION
          MATCH (source:Method {{qname: $from_qname}})
          MATCH (target:Function {{qname: $to_qname}})
          MATCH path = shortestPath((source)-[:CALLS*1..{_depth(max_depth)}]->(target))
          RETURN [node IN nodes(path) | node.qname] AS path,
                 length(path) AS depth
          UNION
          MATCH (source:Method {{qname: $from_qname}})
          MATCH (target:Method {{qname: $to_qname}})
          MATCH path = shortestPath((source)-[:CALLS*1..{_depth(max_depth)}]->(target))
          RETURN [node IN nodes(path) | node.qname] AS path,
                 length(path) AS depth
        }}
        RETURN DISTINCT path, depth
        ORDER BY depth
        LIMIT $limit
        """,
        {"from_qname": from_qname, "to_qname": to_qname, "limit": _limit(limit, 10)},
        max_rows=_limit(limit, 10),
    )


def get_class_hierarchy(
    type_qname: str,
    *,
    direction: HierarchyDirection = "ancestors",
    depth: int = 5,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    pattern = (
        f"(t)-[:INHERITS|IMPLEMENTS*1..{_depth(depth)}]->(related)"
        if direction == "ancestors"
        else f"(related)-[:INHERITS|IMPLEMENTS*1..{_depth(depth)}]->(t)"
    )
    return db.execute_read(
        f"""
        MATCH (t:Type {{qname: $type_qname}})
        MATCH path = {pattern}
        WHERE related:Type
        RETURN DISTINCT related.key AS key,
               related.qname AS qname,
               related.name AS name,
               related.kind AS kind,
               length(path) AS depth
        ORDER BY depth, qname
        LIMIT $limit
        """,
        {"type_qname": type_qname, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def find_dead_code(
    repo: str,
    *,
    limit: int = 100,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository {repo_name: $repo})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method)
          AND s.name <> '<module>'
          AND NOT s.qname ENDS WITH '.__module__'
          AND NOT (()-[:CALLS]->(s))
        RETURN s.key AS key,
               labels(s) AS labels,
               s.qname AS qname,
               s.signature AS signature,
               f.path AS file,
               s.start_line AS start_line,
               'candidate' AS confidence
        ORDER BY s.qname
        LIMIT $limit
        """,
        {"repo": repo, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def get_complexity(
    identifier: str | None = None,
    *,
    repo: str | None = None,
    top_n: int | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    if identifier:
        return db.execute_read(
            """
            MATCH (f:File)-[:CONTAINS]->(s)
            MATCH (r:Repository)-[:CONTAINS]->(f)
            WHERE (s:Function OR s:Method)
              AND (s.key = $identifier OR s.qname = $identifier)
              AND ($repo IS NULL OR r.repo_name = $repo)
            RETURN s.key AS key,
                   s.qname AS qname,
                   s.signature AS signature,
                   s.cyclomatic AS cyclomatic,
                   f.path AS file,
                   r.repo_name AS repo
            ORDER BY r.repo_name, f.path, s.start_line
            LIMIT 10
            """,
            {"identifier": identifier, "repo": repo},
            max_rows=10,
        )
    limit = _limit(top_n or 25)
    return db.execute_read(
        """
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method)
          AND ($repo IS NULL OR r.repo_name = $repo)
        RETURN s.key AS key,
               s.qname AS qname,
               s.signature AS signature,
               s.cyclomatic AS cyclomatic,
               f.path AS file,
               r.repo_name AS repo
        ORDER BY s.cyclomatic DESC, s.qname
        LIMIT $limit
        """,
        {"repo": repo, "limit": limit},
        max_rows=limit,
    )


def _label_filter(kind: SymbolKind | None) -> str:
    if kind == "function":
        return "AND s:Function"
    if kind == "method":
        return "AND s:Method"
    if kind == "type":
        return "AND s:Type"
    return ""


def _fulltext_query(query: str) -> str:
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return "*:*"
    return " AND ".join(terms)


def _limit(value: int, max_value: int = 500) -> int:
    return max(1, min(int(value), max_value))


def _depth(value: int) -> int:
    return max(1, min(int(value), 10))
