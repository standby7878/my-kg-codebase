"""Read-only, bounded investigation queries for native PostgreSQL corpora.

These queries expose source-level evidence, not runtime/ABI/security verdicts.
Candidate links are deliberately excluded from asserted dependency paths.
"""

from __future__ import annotations

from typing import Literal

from codekg.neo4j_client import Neo4jClient, get_client

CorpusKind = Literal["native", "routine", "all"]
_PATH_TYPES = "HAS_EVIDENCE|INVOKES_ROUTINE|DOCUMENTS_ROUTINE|BINDS_TO_NATIVE|CALLS_NATIVE"
_KEY_LABELS = (
    "SourceEvidence",
    "NativeSymbol",
    "Routine",
    "Function",
    "Method",
    "ModuleInit",
    "SqlObject",
    "File",
)


def _key_lookup(variable: str, parameter: str) -> str:
    """Use label-specific key indexes; arguments are internal constant identifiers."""
    branches = [
        f"MATCH ({variable}:{label} {{key:${parameter}}}) RETURN {variable}"
        for label in _KEY_LABELS
    ]
    return "CALL { " + " UNION ALL ".join(branches) + " }"


def _page(limit: int, offset: int, *, maximum: int = 100) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
        raise ValueError("offset must be between 0 and 10000")


def list_corpus_snapshots(
    *, limit: int = 100, offset: int = 0, client: Neo4jClient | None = None
) -> list[dict[str, object]]:
    _page(limit, offset)
    return (client or get_client()).execute_read(
        """
        MATCH (s:CorpusSnapshot)
        RETURN s.alias AS alias, s.logical_repo AS logical_repo, s.version AS version,
               s.role AS role, s.git_commit AS git_commit, s.revision AS revision,
               s.source_digest AS source_digest, s.fingerprint AS fingerprint,
               s.root_path AS root_path
        ORDER BY s.alias SKIP $offset LIMIT $limit
        """,
        {"limit": limit, "offset": offset},
        max_rows=limit,
        timeout_seconds=10.0,
    )


def search_corpus_symbols(
    query: str,
    *,
    snapshot_alias: str,
    kind: CorpusKind = "all",
    limit: int = 20,
    offset: int = 0,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    _page(limit, offset)
    if not query.strip() or not snapshot_alias.strip():
        raise ValueError("query and snapshot_alias must be non-empty")
    if kind not in {"native", "routine", "all"}:
        raise ValueError("kind must be native, routine, or all")
    return (client or get_client()).execute_read(
        """
        CALL {
          MATCH (s:NativeSymbol)
          WHERE s.snapshot_alias=$alias AND $kind IN ['all','native']
            AND (s.name CONTAINS $query OR s.signature CONTAINS $query)
          RETURN s
          UNION ALL
          MATCH (s:Routine)
          WHERE s.snapshot_alias=$alias AND $kind IN ['all','routine']
            AND (s.name CONTAINS $query OR s.signature CONTAINS $query)
          RETURN s
        }
        RETURN s.key AS key, labels(s) AS labels, s.name AS name,
               s.signature AS signature, s.language AS language, s.path AS path,
               s.start_line AS start_line, s.end_line AS end_line,
               s.condition AS condition, s.coverage AS coverage,
               s.snapshot_alias AS snapshot_alias, s.declaration AS declaration,
               s.kind AS symbol_kind, s.return_type AS return_type
        ORDER BY s.name, s.path, s.start_line, s.key SKIP $offset LIMIT $limit
        """,
        {"query": query, "alias": snapshot_alias, "kind": kind, "limit": limit, "offset": offset},
        max_rows=limit,
        timeout_seconds=10.0,
    )


def get_dependency_evidence(
    key: str,
    *,
    direction: Literal["outgoing", "incoming"] = "incoming",
    limit: int = 50,
    offset: int = 0,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    """Return direct exact/candidate evidence, never fold candidates into calls."""
    _page(limit, offset)
    if not key.strip() or direction not in {"outgoing", "incoming"}:
        raise ValueError("provide a key and direction incoming/outgoing")
    pattern = "(a)-[e]->(b)" if direction == "outgoing" else "(b)-[e]->(a)"
    return (client or get_client()).execute_read(
        f"""
        {_key_lookup("a", "key")}
        MATCH {pattern}
        WHERE a.key = $key AND type(e) IN [
          'HAS_EVIDENCE', 'INVOKES_ROUTINE', 'DOCUMENTS_ROUTINE', 'BINDS_TO_NATIVE', 'CALLS_NATIVE',
          'NATIVE_CANDIDATE', 'ROUTINE_CANDIDATE', 'DESCRIBES_SQL_OBJECT']
        RETURN a.key AS selected_key, b.key AS related_key, type(e) AS relationship,
               b.name AS name, b.path AS path, b.snapshot_alias AS snapshot_alias,
               b.status AS related_status, b.dynamic AS dynamic,
               b.candidate_count AS candidate_count,
               e.status AS status, e.path AS evidence_path, e.line AS line,
               e.column AS column, e.condition AS condition
        ORDER BY relationship, related_key, line SKIP $offset LIMIT $limit
        """,
        {"key": key, "limit": limit, "offset": offset},
        max_rows=limit,
        timeout_seconds=10.0,
    )


def trace_corpus_path(
    from_key: str,
    to_key: str,
    *,
    max_depth: int = 6,
    limit: int = 5,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    _page(limit, 0, maximum=10)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 8:
        raise ValueError("max_depth must be between 1 and 8")
    if not from_key.strip() or not to_key.strip():
        raise ValueError("both exact graph keys are required")
    db = client or get_client()
    # Neo4j rejects relationship property maps on variable-length paths in
    # allShortestPaths(). Universal WHERE predicates are applied during
    # shortest-path planning, so candidate and guarded links are excluded
    # before the shortest admissible paths are selected.
    return db.execute_read(
        f"""
        {_key_lookup("a", "from_key")}
        {_key_lookup("b", "to_key")}
        MATCH p = allShortestPaths((a)-[:{_PATH_TYPES}*1..{max_depth}]->(b))
        WHERE all(e IN relationships(p) WHERE e.status='exact'
                  AND coalesce(e.condition,'')='')
        RETURN [n IN nodes(p) | {{key:n.key, name:n.name, path:n.path,
                 snapshot_alias:n.snapshot_alias, labels:labels(n)}}] AS nodes,
               [e IN relationships(p) | {{kind:type(e), status:e.status,
                 path:e.path, line:e.line, condition:e.condition}}] AS evidence
        LIMIT $limit
        """,
        {"from_key": from_key, "to_key": to_key, "limit": limit},
        max_rows=limit,
        timeout_seconds=10.0,
    )


def compare_corpus_snapshots(
    left_alias: str,
    right_alias: str,
    *,
    limit: int = 100,
    offset: int = 0,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    """Compare source facts by name/scope; overloaded matches stay ambiguous."""
    _page(limit, offset)
    if not left_alias.strip() or not right_alias.strip() or left_alias == right_alias:
        raise ValueError("provide two distinct snapshot aliases")
    db = client or get_client()
    snapshots = db.execute_read(
        """
        MATCH (a:CorpusSnapshot {alias:$left}), (b:CorpusSnapshot {alias:$right})
        CALL {
          WITH a
          MATCH (d:CorpusDiagnostic {snapshot_alias:a.alias})
          WHERE d.category IN $incomplete_categories
          RETURN count(d) AS left_incomplete
        }
        CALL {
          WITH b
          MATCH (d:CorpusDiagnostic {snapshot_alias:b.alias})
          WHERE d.category IN $incomplete_categories
          RETURN count(d) AS right_incomplete
        }
        RETURN a.logical_repo AS left_repo, b.logical_repo AS right_repo
             , left_incomplete, right_incomplete
        """,
        {
            "left": left_alias,
            "right": right_alias,
            "incomplete_categories": [
                "file_too_large",
                "unreadable_file",
                "c_parse_error",
                "c_missing_syntax",
                "routine_sql_parse_error",
                "unsupported_routine_language",
                "catalog_record_unsupported",
            ],
        },
        max_rows=1,
        timeout_seconds=10.0,
    )
    if not snapshots or snapshots[0]["left_repo"] != snapshots[0]["right_repo"]:
        raise ValueError("snapshots must exist and share a logical repository")
    return db.execute_read(
        """
        CALL {
          MATCH (n:NativeSymbol)
          WHERE n.snapshot_alias IN [$left,$right] AND coalesce(n.comparison_primary,true)
               RETURN n.logical_id AS identity
          UNION
          MATCH (n:Routine)
          WHERE n.snapshot_alias IN [$left,$right] AND coalesce(n.comparison_primary,true)
          RETURN n.logical_id AS identity
        }
        WITH DISTINCT identity
        OPTIONAL MATCH (a:NativeSymbol)
        WHERE a.snapshot_alias=$left AND a.logical_id=identity
              AND coalesce(a.comparison_primary,true)
        OPTIONAL MATCH (ar:Routine)
        WHERE ar.snapshot_alias=$left AND ar.logical_id=identity
              AND coalesce(ar.comparison_primary,true)
        WITH identity, collect(DISTINCT a)+collect(DISTINCT ar) AS old
        OPTIONAL MATCH (b:NativeSymbol)
        WHERE b.snapshot_alias=$right AND b.logical_id=identity
              AND coalesce(b.comparison_primary,true)
        OPTIONAL MATCH (br:Routine)
        WHERE br.snapshot_alias=$right AND br.logical_id=identity
              AND coalesce(br.comparison_primary,true)
        WITH identity, old, collect(DISTINCT b)+collect(DISTINCT br) AS new
        WITH identity, old, new,
          CASE WHEN size(old)>1 OR size(new)>1 THEN 'ambiguous'
               WHEN (size(old)=0 OR size(new)=0) AND $incomplete THEN 'ambiguous'
               WHEN size(old)=0 THEN 'added'
               WHEN size(new)=0 THEN 'removed'
               WHEN old[0].signature<>new[0].signature
                    OR coalesce(old[0].return_type,'')<>coalesce(new[0].return_type,'')
                    THEN 'signature_changed'
               WHEN coalesce(old[0].condition,'')<>coalesce(new[0].condition,'')
                    THEN 'condition_changed'
               WHEN coalesce(old[0].body_hash,'')<>coalesce(new[0].body_hash,'')
                    THEN 'body_changed'
               WHEN coalesce(old[0].definition_hash,'')<>
                    coalesce(new[0].definition_hash,'') THEN 'definition_changed'
               ELSE 'unchanged' END AS change
        WHERE change<>'unchanged'
        RETURN identity, change, [n IN old | n.key] AS old_keys,
               [n IN new | n.key] AS new_keys,
               [n IN old | n.signature] AS old_signatures,
               [n IN new | n.signature] AS new_signatures,
               $incomplete AS incomplete_coverage,
               'potential source-level change; not an ABI/security verdict' AS caveat
        ORDER BY identity SKIP $offset LIMIT $limit
        """,
        {
            "left": left_alias,
            "right": right_alias,
            "limit": limit,
            "offset": offset,
            "incomplete": bool(
                snapshots[0].get("left_incomplete", 0) or snapshots[0].get("right_incomplete", 0)
            ),
        },
        max_rows=limit,
        timeout_seconds=10.0,
    )
