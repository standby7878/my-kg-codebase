from __future__ import annotations

import re
from typing import Literal

from codekg.neo4j_client import Neo4jClient, get_client

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]


def search_symbols(
    q: str,
    *,
    kind: SymbolKind | None = None,
    repo: str | None = None,
    limit: int = 25,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
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
