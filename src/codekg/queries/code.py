from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Literal

from codekg.neo4j_client import Neo4jClient, get_client
from codekg.zvec_store import ZvecUnavailableError
from codekg.zvec_store import open_read as open_zvec_read
from codekg.zvec_store import search_symbols as zvec_search_symbols

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]
SearchMode = Literal["graph", "lexical"]
DiscoverySearchMode = Literal["graph", "lexical", "hybrid"]

_DISCOVERY_DEFAULT_LIMIT = 5
_DISCOVERY_MAX_LIMIT = 20
_DISCOVERY_CANDIDATE_POOL = 100


class SymbolResolutionError(ValueError):
    """A selector cannot identify exactly one symbol in the requested snapshot."""


def search_symbols(
    q: str,
    *,
    kind: SymbolKind | None = None,
    repo: str | None = None,
    commit: str | None = None,
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
            commit=commit,
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
      AND ($commit IS NULL OR r.commit = $commit)
    RETURN s.key AS key,
           score AS score,
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
        {
            "fulltext_query": fulltext_query,
            "repo": repo,
            "commit": commit,
            "limit": _limit(limit),
        },
        max_rows=_limit(limit),
    )


def _search_symbols_lexical(
    q: str,
    *,
    kind: SymbolKind | None,
    repo: str | None,
    commit: str | None,
    limit: int,
    zvec_path: str | None,
    client: Neo4jClient | None,
) -> list[dict[str, object]]:
    if kind == "type":
        return []
    try:
        collection = open_zvec_read(zvec_path)
        hits = zvec_search_symbols(
            collection, q, repo=repo, commit=commit, kind=kind, limit=_limit(limit)
        )
    except ZvecUnavailableError as exc:
        raise ZvecUnavailableError(
            "zvec lexical search is unavailable; run normal `codekg index`."
        ) from exc

    if not hits:
        return []
    hits = hits[: _limit(limit)]
    keys = list(dict.fromkeys(str(hit["key"]) for hit in hits))
    rows = _symbol_rows_by_key(client or get_client(), keys, limit, repo=repo, commit=commit)
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
    *,
    repo: str | None = None,
    commit: str | None = None,
) -> list[dict[str, object]]:
    return db.execute_read(
        """
        MATCH (f:File)-[:CONTAINS]->(s)
        MATCH (r:Repository)-[:CONTAINS]->(f)
        WHERE (s:Function OR s:Method)
          AND s.key IN $keys
          AND ($repo IS NULL OR r.repo_name = $repo)
          AND ($commit IS NULL OR r.commit = $commit)
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
        {"keys": ids, "repo": repo, "commit": commit},
        max_rows=_limit(limit),
    )


def discover_symbols(
    query: str,
    *,
    repository: str | None = None,
    commit: str | None = None,
    kind: SymbolKind | None = None,
    mode: DiscoverySearchMode = "hybrid",
    limit: int = _DISCOVERY_DEFAULT_LIMIT,
    cursor: str | None = None,
    zvec_path: str | None = None,
    client: Neo4jClient | None = None,
) -> dict[str, object]:
    """Return compact, repository-scoped symbol discovery results.

    This is deliberately separate from :func:`search_symbols`, the lower-level
    list API retained for evaluation and direct graph callers.
    """

    if not 1 <= int(limit) <= _DISCOVERY_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_DISCOVERY_MAX_LIMIT}")
    db = client or get_client()
    repositories = _repository_snapshots(db)
    available = sorted({str(row["repo_name"]) for row in repositories})
    if repository is None:
        if len(available) != 1:
            return {
                "status": "repository_required",
                "available_repositories": available,
                "results": [],
                "next_cursor": None,
            }
        repository = available[0]
    snapshots = [row for row in repositories if row["repo_name"] == repository]
    if not snapshots:
        return {
            "status": "repository_not_found",
            "repository": repository,
            "available_repositories": available,
            "results": [],
            "next_cursor": None,
        }
    if commit is not None and not any(row["commit"] == commit for row in snapshots):
        return {
            "status": "repository_revision_not_found",
            "repository": repository,
            "commit": commit,
            "available_commits": sorted({str(row["commit"]) for row in snapshots}),
            "results": [],
            "next_cursor": None,
        }
    if commit is None:
        # A repository name normally identifies one snapshot.  If retention has
        # left several snapshots, make the selection deterministic and expose it.
        commit = str(sorted(str(row["commit"]) for row in snapshots)[-1])

    offset = _decode_cursor(cursor, repository, commit, query, mode, kind)
    # Every page reranks the same bounded, repository-scoped candidate pool.
    # This makes cursor exhaustion deterministic rather than silently changing
    # the candidate universe on a later page.
    candidate_limit = _DISCOVERY_CANDIDATE_POOL
    graph_rows: list[dict[str, object]] = []
    lexical_rows: list[dict[str, object]] = []
    if mode in ("graph", "hybrid"):
        graph_rows = search_symbols(
            query, kind=kind, repo=repository, commit=commit, limit=candidate_limit, client=db
        )
    if mode in ("lexical", "hybrid"):
        lexical_rows = _search_symbols_lexical(
            query,
            kind=kind,
            repo=repository,
            commit=commit,
            limit=candidate_limit,
            zvec_path=zvec_path,
            client=db,
        )
    ranked = _rank_discovery_rows(query, [*graph_rows, *lexical_rows])
    page = ranked[offset : offset + limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    return {
        "status": "ok",
        "repository": repository,
        "commit": commit,
        "results": [_compact_discovery_row(row, query) for row in page],
        "next_cursor": (
            _encode_cursor(repository, commit, query, mode, kind, offset + limit)
            if has_more
            else None
        ),
    }


def _repository_snapshots(db: Neo4jClient) -> list[dict[str, object]]:
    return db.execute_read(
        """
        MATCH (r:Repository)
        RETURN r.repo_name AS repo_name, r.commit AS commit
        ORDER BY repo_name, commit
        """,
        max_rows=100,
    )


def _encode_cursor(
    repository: str,
    commit: str,
    query: str,
    mode: DiscoverySearchMode,
    kind: SymbolKind | None,
    offset: int,
) -> str:
    payload = {"v": 1, "r": repository, "c": commit, "q": query, "m": mode, "k": kind, "o": offset}
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode_cursor(
    cursor: str | None,
    repository: str,
    commit: str,
    query: str,
    mode: DiscoverySearchMode,
    kind: SymbolKind | None,
) -> int:
    if cursor is None:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or (
            payload.get("v"),
            payload.get("r"),
            payload.get("c"),
            payload.get("q"),
            payload.get("m"),
            payload.get("k"),
        ) != (1, repository, commit, query, mode, kind):
            raise ValueError
        offset = payload.get("o")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("cursor does not match this repository search") from exc


def _rank_discovery_rows(query: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    terms = _query_terms(query)
    by_key: dict[str, dict[str, object]] = {}
    for row in rows:
        key = str(row.get("key", ""))
        if not key:
            continue
        name_terms = set(_query_terms(str(row.get("name") or "")))
        qname_terms = set(_query_terms(str(row.get("qname") or "")))
        matched = set(terms) & (name_terms | qname_terms)
        description_matched = set(terms) & set(_query_terms(str(row.get("snippet") or "")))
        # Exact names and qualified names dominate description-only lexical hits.
        score = min(float(row.get("score") or 0), 10) + len(matched) * 100
        score += len(description_matched - matched) * 20
        if " ".join(terms) in str(row.get("qname") or "").lower().replace("_", " "):
            score += 200
        row = dict(row)
        row["_discovery_score"] = score
        row["_matched_terms"] = sorted(matched | description_matched)
        current = by_key.get(key)
        if current is None or float(row["_discovery_score"]) > float(current["_discovery_score"]):
            by_key[key] = row
    return sorted(
        by_key.values(),
        key=lambda row: (
            -float(row["_discovery_score"]),
            str(row.get("qname", "")),
            str(row.get("key", "")),
        ),
    )


def _query_terms(value: str) -> list[str]:
    return [
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", value.replace("_", " ").replace(".", " "))
        if len(term) > 1
    ]


def _compact_discovery_row(row: dict[str, object], query: str) -> dict[str, object]:
    return {
        "symbol_id": row["key"],
        "qualified_name": row.get("qname"),
        "file": row.get("file"),
        "start_line": row.get("start_line"),
        "end_line": row.get("end_line"),
        "score": row.get("_discovery_score", row.get("score", 0)),
        "matched_terms": row.get("_matched_terms", _query_terms(query)),
    }


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
    commit: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return [
        _resolve_symbol(
            db,
            identifier,
            repo=repo,
            commit=commit,
            labels=("Function", "Method", "Type"),
        )
    ]


def find_importers(
    module_identifier: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    limit: int = 100,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    _require_repo_for_qname(module_identifier, repo)
    exact_key = _looks_like_exact_key(module_identifier)
    rows = db.execute_read(
        """
        MATCH (f:File)-[rel:IMPORTS]->(m:Module)
        MATCH (r:Repository)-[:CONTAINS]->(f)
        WHERE (m.key = $module_identifier OR (NOT $exact_key AND m.qname = $module_identifier))
          AND ($repo IS NULL OR r.repo_name = $repo)
          AND ($commit IS NULL OR r.commit = $commit)
        RETURN r.repo_name AS repo,
               r.commit AS commit,
               f.path AS file,
               m.qname AS module,
               rel.name AS imported_name,
               rel.alias AS alias
        ORDER BY repo, file, imported_name
        LIMIT $limit
        """,
        {
            "module_identifier": module_identifier,
            "exact_key": exact_key,
            "repo": repo,
            "commit": commit,
            "limit": _limit(limit),
        },
        max_rows=_limit(limit),
    )
    return rows


def find_callers(
    identifier: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    depth: int = 1,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    callee = _resolve_symbol(
        db,
        identifier,
        repo=repo,
        commit=commit,
        labels=("Function", "Method"),
    )
    if _depth(depth) == 1:
        return db.execute_read(
            """
            MATCH (callee {key: $key})<-[res:RESOLVES_TO]-(site:CallSite)
            MATCH (caller)-[:HAS_CALLSITE]->(site)
            WHERE caller:Function OR caller:Method
              AND caller.key STARTS WITH $snapshot_prefix
              AND site.key STARTS WITH $snapshot_prefix
            RETURN DISTINCT caller.key AS key,
                   caller.qname AS qname,
                   caller.signature AS signature,
                   1 AS depth,
                   res.strategy AS resolution
            ORDER BY qname, key
            LIMIT $limit
            """,
            {
                "key": callee["key"],
                "snapshot_prefix": _snapshot_prefix(callee),
                "limit": _limit(limit),
            },
            max_rows=_limit(limit),
        )
    return db.execute_read(
        f"""
        MATCH (callee {{key: $key}})
        MATCH path = (caller)-[:EXACT_CALLS*1..{_depth(depth)}]->(callee)
        WHERE caller:Function OR caller:Method
          AND ALL(node IN nodes(path) WHERE node.key STARTS WITH $snapshot_prefix)
        RETURN DISTINCT caller.key AS key,
               caller.qname AS qname,
               caller.signature AS signature,
               length(path) AS depth
        ORDER BY depth, qname, key
        LIMIT $limit
        """,
        {
            "key": callee["key"],
            "snapshot_prefix": _snapshot_prefix(callee),
            "limit": _limit(limit),
        },
        max_rows=_limit(limit),
    )


def find_callees(
    identifier: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    depth: int = 1,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    caller = _resolve_symbol(
        db,
        identifier,
        repo=repo,
        commit=commit,
        labels=("Function", "Method"),
    )
    if _depth(depth) == 1:
        return db.execute_read(
            """
            MATCH (caller {key: $key})-[:HAS_CALLSITE]->(site:CallSite)-[res:RESOLVES_TO]->(callee)
            WHERE callee:Function OR callee:Method
              AND callee.key STARTS WITH $snapshot_prefix
              AND site.key STARTS WITH $snapshot_prefix
            RETURN DISTINCT callee.key AS key,
                   callee.qname AS qname,
                   callee.signature AS signature,
                   1 AS depth,
                   res.strategy AS resolution
            ORDER BY qname, key
            LIMIT $limit
            """,
            {
                "key": caller["key"],
                "snapshot_prefix": _snapshot_prefix(caller),
                "limit": _limit(limit),
            },
            max_rows=_limit(limit),
        )
    return db.execute_read(
        f"""
        MATCH (caller {{key: $key}})
        MATCH path = (caller)-[:EXACT_CALLS*1..{_depth(depth)}]->(callee)
        WHERE callee:Function OR callee:Method
          AND ALL(node IN nodes(path) WHERE node.key STARTS WITH $snapshot_prefix)
        RETURN DISTINCT callee.key AS key,
               callee.qname AS qname,
               callee.signature AS signature,
               length(path) AS depth
        ORDER BY depth, qname, key
        LIMIT $limit
        """,
        {
            "key": caller["key"],
            "snapshot_prefix": _snapshot_prefix(caller),
            "limit": _limit(limit),
        },
        max_rows=_limit(limit),
    )


def trace_call_path(
    from_identifier: str,
    to_identifier: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    max_depth: int = 8,
    limit: int = 5,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    source = _resolve_symbol(
        db,
        from_identifier,
        repo=repo,
        commit=commit,
        labels=("Function", "Method"),
    )
    target = _resolve_symbol(
        db,
        to_identifier,
        repo=repo,
        commit=commit,
        labels=("Function", "Method"),
    )
    if (source["repo"], source["commit"]) != (target["repo"], target["commit"]):
        raise SymbolResolutionError(
            "Call-path endpoints must belong to the same repository and commit."
        )
    return db.execute_read(
        f"""
        MATCH (source {{key: $source_key}})
        MATCH (target {{key: $target_key}})
        MATCH path = shortestPath((source)-[:EXACT_CALLS*1..{_depth(max_depth)}]->(target))
        WHERE ALL(node IN nodes(path) WHERE node.key STARTS WITH $snapshot_prefix)
        RETURN [node IN nodes(path) | {{key: node.key, qname: node.qname}}] AS path,
               length(path) AS depth
        ORDER BY depth
        LIMIT $limit
        """,
        {
            "source_key": source["key"],
            "target_key": target["key"],
            "snapshot_prefix": _snapshot_prefix(source),
            "limit": _limit(limit, 10),
        },
        max_rows=_limit(limit, 10),
    )


def get_class_hierarchy(
    identifier: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    direction: HierarchyDirection = "ancestors",
    depth: int = 5,
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    selected = _resolve_symbol(
        db,
        identifier,
        repo=repo,
        commit=commit,
        labels=("Type",),
    )
    pattern = (
        f"(t)-[:INHERITS|IMPLEMENTS*1..{_depth(depth)}]->(related)"
        if direction == "ancestors"
        else f"(related)-[:INHERITS|IMPLEMENTS*1..{_depth(depth)}]->(t)"
    )
    return db.execute_read(
        f"""
        MATCH (t:Type {{key: $key}})
        MATCH path = {pattern}
        WHERE related:Type
          AND ALL(node IN nodes(path) WHERE node.key STARTS WITH $snapshot_prefix)
        RETURN DISTINCT related.key AS key,
               related.qname AS qname,
               related.name AS name,
               related.kind AS kind,
               length(path) AS depth
        ORDER BY depth, qname, key
        LIMIT $limit
        """,
        {
            "key": selected["key"],
            "snapshot_prefix": _snapshot_prefix(selected),
            "limit": _limit(limit),
        },
        max_rows=_limit(limit),
    )


def find_dead_code(
    repo: str,
    *,
    commit: str | None = None,
    limit: int = 100,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository {repo_name: $repo})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method)
          AND ($commit IS NULL OR r.commit = $commit)
        OPTIONAL MATCH (site:CallSite)-[:RESOLVES_TO]->(s)
        WHERE site.key STARTS WITH (r.repo_name + '@' + r.commit + ':')
        WITH s, f, count(DISTINCT site) AS incoming_resolved_calls
        WHERE incoming_resolved_calls = 0
        RETURN s.key AS key,
               labels(s) AS labels,
               s.qname AS qname,
               s.signature AS signature,
               f.path AS file,
               s.start_line AS start_line,
               incoming_resolved_calls,
               'unreferenced_candidate' AS confidence
        ORDER BY s.qname, s.key
        LIMIT $limit
        """,
        {"repo": repo, "commit": commit, "limit": _limit(limit)},
        max_rows=_limit(limit),
    )


def get_complexity(
    identifier: str | None = None,
    *,
    repo: str | None = None,
    commit: str | None = None,
    top_n: int | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    db = client or get_client()
    if identifier:
        selected = _resolve_symbol(
            db,
            identifier,
            repo=repo,
            commit=commit,
            labels=("Function", "Method"),
        )
        return [selected]
    limit = _limit(top_n or 25)
    return db.execute_read(
        """
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method)
          AND ($repo IS NULL OR r.repo_name = $repo)
          AND ($commit IS NULL OR r.commit = $commit)
        RETURN s.key AS key,
               s.qname AS qname,
               s.signature AS signature,
               s.cyclomatic AS cyclomatic,
               f.path AS file,
               r.repo_name AS repo
        ORDER BY s.cyclomatic DESC, s.qname
        LIMIT $limit
        """,
        {"repo": repo, "commit": commit, "limit": limit},
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


def _resolve_symbol(
    db: Neo4jClient,
    identifier: str,
    *,
    repo: str | None,
    commit: str | None,
    labels: tuple[str, ...],
) -> dict[str, object]:
    """Resolve one structural symbol without ever crossing a graph snapshot.

    Exact keys are deliberately attempted first without a repository filter. If
    the key exists but the supplied repository or commit disagrees, the caller
    gets a mismatch error rather than a potentially unrelated qname result.
    Qualified names are a convenience fallback only for an explicit repository
    scope; multiple definitions remain visible as an ambiguity.
    """

    label_where = _labels_where(labels)
    rows = db.execute_read(
        f"""
        // codekg: exact-symbol-selector
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE {label_where} AND s.key = $identifier
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
        """,
        {"identifier": identifier},
        max_rows=2,
    )
    if rows:
        row = rows[0]
        if (repo is not None and row.get("repo") != repo) or (
            commit is not None and row.get("commit") != commit
        ):
            raise SymbolResolutionError(
                f"Exact key {identifier!r} does not belong to requested "
                f"repository/commit ({repo!r}, {commit!r})."
            )
        return row

    if _looks_like_exact_key(identifier):
        raise SymbolResolutionError(f"No indexed symbol exists for exact key {identifier!r}.")
    _require_repo_for_qname(identifier, repo)
    candidates = db.execute_read(
        f"""
        // codekg: qname-symbol-selector
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE {label_where}
          AND s.qname = $identifier
          AND r.repo_name = $repo
          AND ($commit IS NULL OR r.commit = $commit)
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
        ORDER BY r.commit, f.path, s.start_line, s.key
        LIMIT 501
        """,
        {"identifier": identifier, "repo": repo, "commit": commit},
        max_rows=501,
    )
    if not candidates:
        raise SymbolResolutionError(
            f"No indexed {', '.join(label.lower() for label in labels)} has qname "
            f"{identifier!r} in repository {repo!r}."
        )
    if len(candidates) > 1:
        keys = ", ".join(str(candidate.get("key")) for candidate in candidates)
        raise SymbolResolutionError(
            f"Qualified name {identifier!r} is ambiguous in repository {repo!r}; "
            f"use one of these exact keys: {keys}."
        )
    return candidates[0]


def _labels_where(labels: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"s:{label}" for label in labels) + ")"


def _looks_like_exact_key(identifier: str) -> bool:
    return "@" in identifier and ":" in identifier


def _require_repo_for_qname(identifier: str, repo: str | None) -> None:
    if repo is None and not _looks_like_exact_key(identifier):
        raise SymbolResolutionError(
            f"Qualified name {identifier!r} requires an explicit repository; "
            "use an exact key or provide repo."
        )


def _snapshot_prefix(symbol: dict[str, object]) -> str:
    return f"{symbol['repo']}@{symbol['commit']}:"
