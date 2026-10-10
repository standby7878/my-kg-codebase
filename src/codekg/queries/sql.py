"""Bounded SQL-graph investigation queries used by the MCP server."""

from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from codekg.neo4j_client import Neo4jClient, get_client

SqlObjectKind = Literal[
    "table",
    "view",
    "materialized_view",
    "sequence",
    "index",
    "statistics",
    "function",
    "procedure",
    "schema",
    "extension",
    "type",
]
SqlUsageRole = Literal["read", "write", "call", "alter", "drop", "define", "all"]
SqlReferenceResolution = Literal["exact", "ambiguous", "unresolved", "dynamic", "all"]

_SEARCH_DEFAULT_LIMIT = 5
_SEARCH_MAX_LIMIT = 20
_USAGE_DEFAULT_LIMIT = 50
_FILE_DEFAULT_LIMIT = 100

_DERIVED_USAGE_ROLES = frozenset({"read", "write", "call", "alter", "drop", "define"})
_REFERENCE_STATUSES = frozenset({"ambiguous", "unresolved", "dynamic"})


class SqlObjectResolutionError(ValueError):
    """A selector cannot identify exactly one SQL object in the requested snapshot."""


def search_sql_objects(
    query: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    kind: SqlObjectKind | None = None,
    commit: str | None = None,
    limit: int = _SEARCH_DEFAULT_LIMIT,
    client: Neo4jClient | None = None,
) -> dict[str, object]:
    """Return compact, repository-scoped SQL object discovery results."""

    normalized_query = query.strip()
    if not normalized_query:
        return {"status": "invalid_query", "results": []}
    if not 1 <= int(limit) <= _SEARCH_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_SEARCH_MAX_LIMIT}")

    db = client or get_client()
    context = _resolve_repository_context(db, repository, commit)
    if context.get("status") != "ok":
        return {**context, "results": []}

    repo = str(context["repository"])
    resolved_commit = str(context["commit"])
    rows = db.execute_read(
        """
        // codekg: search-sql-objects
        MATCH (r:Repository {repo_name: $repo, commit: $commit})
              -[:HAS_DATABASE]->(:Database)-[:HAS_OBJECT]->(o:SqlObject)
        WHERE ($database IS NULL OR o.database_name = $database)
          AND ($schema IS NULL OR o.schema_name = $schema)
          AND ($kind IS NULL OR o.kind = $kind)
          AND (
            toLower(o.object_name) CONTAINS toLower($query)
            OR toLower(o.schema_name + '.' + o.object_name) CONTAINS toLower($query)
          )
        RETURN o.key AS object_key,
               o.database_name AS database_name,
               o.schema_name AS schema_name,
               o.object_name AS object_name,
               o.kind AS kind,
               o.signature AS signature,
               o.definition_count AS definition_count,
               o.owner_path AS owner_path,
               r.repo_name AS repo,
               r.commit AS commit,
               CASE
                 WHEN toLower(o.object_name) = toLower($query) THEN 100
                 WHEN toLower(o.schema_name + '.' + o.object_name) = toLower($query) THEN 95
                 WHEN toLower(o.object_name) STARTS WITH toLower($query) THEN 80
                 ELSE 50
               END AS score
        ORDER BY score DESC, o.schema_name, o.object_name, o.kind, o.key
        LIMIT $limit
        """,
        {
            "repo": repo,
            "commit": resolved_commit,
            "query": normalized_query,
            "database": database,
            "schema": schema,
            "kind": kind,
            "limit": _search_limit(limit),
        },
        max_rows=_search_limit(limit),
    )
    return {
        "status": "ok",
        "repository": repo,
        "commit": resolved_commit,
        "results": rows,
        "recommended_next_tool": "get_sql_object",
    }


def get_sql_object(
    identifier: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    commit: str | None = None,
    client: Neo4jClient | None = None,
) -> dict[str, object]:
    """Return SQL object metadata and inbound DEFINES sites."""

    db = client or get_client()
    resolved = _resolve_sql_object(
        db,
        identifier,
        repo=repository,
        database=database,
        commit=commit,
    )
    definitions = db.execute_read(
        """
        // codekg: sql-object-definitions
        MATCH (stmt:SqlStatement)-[rel:DEFINES]->(o:SqlObject {key: $key})
        RETURN stmt.key AS statement_key,
               stmt.path AS file,
               rel.role AS role,
               rel.line AS line,
               rel.column AS column,
               stmt.kind AS statement_kind
        ORDER BY file, line, column, statement_key
        """,
        {"key": resolved["key"]},
        max_rows=500,
    )
    return {
        "status": "ok",
        "repository": resolved["repo"],
        "commit": resolved["commit"],
        "object": {
            "object_key": resolved["key"],
            "database_name": resolved["database_name"],
            "schema_name": resolved["schema_name"],
            "object_name": resolved["object_name"],
            "kind": resolved["kind"],
            "signature": resolved["signature"],
            "definition_count": resolved["definition_count"],
            "owner_path": resolved["owner_path"],
        },
        "definitions": definitions,
        "recommended_next_tool": "find_sql_usages",
    }


def find_sql_usages(
    identifier: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    commit: str | None = None,
    role: SqlUsageRole = "all",
    resolution: SqlReferenceResolution = "exact",
    limit: int = _USAGE_DEFAULT_LIMIT,
    client: Neo4jClient | None = None,
) -> list[dict[str, object]]:
    """Return bounded inbound SQL usages for one resolved object."""

    db = client or get_client()
    resolved = _resolve_sql_object(
        db,
        identifier,
        repo=repository,
        database=database,
        commit=commit,
    )
    bounded_limit = _usage_limit(limit)
    rows: list[dict[str, object]] = []
    if resolution in ("exact", "all"):
        rows.extend(
            db.execute_read(
                """
                // codekg: sql-object-derived-usages
                MATCH (o:SqlObject {key: $key})
                MATCH (stmt:SqlStatement)-[rel:READS_FROM|WRITES_TO|INVOKES_SQL|
                      ALTERS|DROPS|DEFINES]->(o)
                WHERE $role = 'all' OR rel.role = $role
                RETURN stmt.path AS file,
                       rel.line AS line,
                       rel.column AS column,
                       rel.role AS role,
                       stmt.kind AS statement_kind,
                       stmt.key AS statement_key,
                       NULL AS reference_key,
                       'exact' AS reference_status,
                       NULL AS candidate_keys
                ORDER BY file, line, column, statement_key
                LIMIT $limit
                """,
                {"key": resolved["key"], "role": role, "limit": bounded_limit},
                max_rows=bounded_limit,
            )
        )
    if resolution in ("ambiguous", "unresolved", "dynamic", "all") and len(rows) < bounded_limit:
        statuses = (
            sorted(_REFERENCE_STATUSES)
            if resolution == "all"
            else [resolution]
            if resolution in _REFERENCE_STATUSES
            else []
        )
        if statuses:
            remaining = bounded_limit - len(rows)
            candidate_rows = db.execute_read(
                """
                // codekg: sql-object-reference-usages
                MATCH (stmt:SqlStatement)-[:HAS_REFERENCE]->(ref:Reference)
                WHERE ref.status IN $statuses
                  AND ref.candidate_keys_json CONTAINS $key
                RETURN stmt.path AS file,
                       ref.start_line AS line,
                       ref.start_column AS column,
                       ref.role AS role,
                       stmt.kind AS statement_kind,
                       stmt.key AS statement_key,
                       ref.key AS reference_key,
                       ref.status AS reference_status,
                       ref.candidate_keys_json AS candidate_keys
                ORDER BY file, line, column, statement_key, reference_key
                LIMIT $limit
                """,
                {"statuses": statuses, "key": resolved["key"], "limit": remaining},
                max_rows=remaining,
            )
            if role != "all":
                candidate_rows = [row for row in candidate_rows if row.get("role") == role]
            rows.extend(candidate_rows[:remaining])
    for row in rows:
        row["repo"] = resolved["repo"]
        row["commit"] = resolved["commit"]
        row["object_key"] = resolved["key"]
        if row.get("candidate_keys"):
            try:
                parsed = json.loads(str(row["candidate_keys"]))
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                row["candidate_keys"] = parsed
    return rows[:bounded_limit]


def get_sql_in_file(
    file: str,
    *,
    repository: str,
    commit: str | None = None,
    include_text: bool = False,
    limit: int = _FILE_DEFAULT_LIMIT,
    client: Neo4jClient | None = None,
) -> dict[str, object]:
    """Return SQL artifacts, statements, and references for one indexed file."""

    normalized_file = _validate_repository_relative_path(file)

    db = client or get_client()
    context = _resolve_repository_context(db, repository, commit)
    if context.get("status") != "ok":
        return {
            **context,
            "file": normalized_file,
            "artifacts": [],
            "statements": [],
            "references": [],
        }

    repo = str(context["repository"])
    resolved_commit = str(context["commit"])
    bounded_limit = _file_limit(limit)
    file_rows = db.execute_read(
        """
        // codekg: sql-file-exists
        MATCH (r:Repository {repo_name: $repo, commit: $commit})-[:CONTAINS]->(f:File {path: $file})
        RETURN f.path AS file, r.repo_name AS repo, r.commit AS commit
        """,
        {"repo": repo, "commit": resolved_commit, "file": normalized_file},
        max_rows=1,
    )
    if not file_rows:
        return {
            "status": "file_not_found",
            "repository": repo,
            "commit": resolved_commit,
            "file": normalized_file,
            "artifacts": [],
            "statements": [],
            "references": [],
        }

    artifact_query = """
        // codekg: sql-file-artifacts
        MATCH (r:Repository {repo_name: $repo, commit: $commit})-[:CONTAINS]->(f:File {path: $file})
        MATCH (f)-[:CONTAINS_SQL]->(a:SqlArtifact)
        RETURN a.key AS artifact_key,
               a.ordinal AS ordinal,
               a.origin AS origin,
               a.dialect AS dialect,
               a.start_line AS start_line,
               a.start_column AS start_column,
               a.end_line AS end_line,
               a.end_column AS end_column
        """
    if include_text:
        artifact_query += ", a.text AS text\n"
    artifact_query += "ORDER BY ordinal, artifact_key"

    artifacts = db.execute_read(
        artifact_query,
        {"repo": repo, "commit": resolved_commit, "file": normalized_file},
        max_rows=bounded_limit,
    )
    statements = db.execute_read(
        """
        // codekg: sql-file-statements
        MATCH (r:Repository {repo_name: $repo, commit: $commit})-[:CONTAINS]->(f:File {path: $file})
        MATCH (f)-[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL*1..5]->(stmt:SqlStatement)
        RETURN stmt.key AS statement_key,
               stmt.artifact_ordinal AS artifact_ordinal,
               stmt.ordinal AS ordinal,
               stmt.kind AS kind,
               stmt.parent_key AS parent_key,
               stmt.control_context AS control_context,
               stmt.start_line AS start_line,
               stmt.start_column AS start_column,
               stmt.end_line AS end_line,
               stmt.end_column AS end_column
        ORDER BY artifact_ordinal, ordinal, statement_key
        LIMIT $limit
        """,
        {"repo": repo, "commit": resolved_commit, "file": normalized_file, "limit": bounded_limit},
        max_rows=bounded_limit,
    )
    references = db.execute_read(
        """
        // codekg: sql-file-references
        MATCH (r:Repository {repo_name: $repo, commit: $commit})-[:CONTAINS]->(f:File {path: $file})
        MATCH (f)-[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL*1..5]->(stmt:SqlStatement)
              -[:HAS_REFERENCE]->(ref:Reference)
        OPTIONAL MATCH (ref)-[:REFERS_TO]->(o:SqlObject)
        RETURN ref.key AS reference_key,
               stmt.key AS statement_key,
               ref.raw_name AS raw_name,
               ref.role AS role,
               ref.status AS reference_status,
               ref.dynamic AS dynamic,
               ref.candidate_count AS candidate_count,
               ref.candidate_keys_json AS candidate_keys,
               o.key AS object_key,
               ref.start_line AS start_line,
               ref.start_column AS start_column,
               ref.end_line AS end_line,
               ref.end_column AS end_column
        ORDER BY statement_key, reference_key
        LIMIT $limit
        """,
        {"repo": repo, "commit": resolved_commit, "file": normalized_file, "limit": bounded_limit},
        max_rows=bounded_limit,
    )
    for row in references:
        candidate_keys = row.pop("candidate_keys", None)
        if candidate_keys:
            try:
                parsed = json.loads(str(candidate_keys))
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                row["candidate_keys"] = parsed
    return {
        "status": "ok",
        "repository": repo,
        "commit": resolved_commit,
        "file": normalized_file,
        "artifacts": artifacts,
        "statements": statements,
        "references": references,
    }


def _resolve_repository_context(
    db: Neo4jClient,
    repository: str | None,
    commit: str | None,
) -> dict[str, object]:
    repositories = _repository_snapshots(db)
    available = sorted({str(row["repo_name"]) for row in repositories})
    if repository is None:
        if len(available) != 1:
            return {
                "status": "repository_required",
                "available_repositories": available,
            }
        repository = available[0]
    snapshots = [row for row in repositories if row["repo_name"] == repository]
    if not snapshots:
        return {
            "status": "repository_not_found",
            "repository": repository,
            "available_repositories": available,
        }
    if commit is not None and not any(row["commit"] == commit for row in snapshots):
        return {
            "status": "repository_revision_not_found",
            "repository": repository,
            "commit": commit,
            "available_commits": sorted({str(row["commit"]) for row in snapshots}),
        }
    if commit is None:
        commit = str(sorted(str(row["commit"]) for row in snapshots)[-1])
    return {"status": "ok", "repository": repository, "commit": commit}


def _resolve_sql_object(
    db: Neo4jClient,
    identifier: str,
    *,
    repo: str | None,
    database: str | None,
    commit: str | None,
) -> dict[str, object]:
    if _looks_like_exact_sql_key(identifier):
        rows = db.execute_read(
            """
            // codekg: exact-sql-object-selector
            MATCH (r:Repository)-[:HAS_DATABASE]->(:Database)-[:HAS_OBJECT]->
                  (o:SqlObject {key: $identifier})
            RETURN o.key AS key,
                   o.database_name AS database_name,
                   o.schema_name AS schema_name,
                   o.object_name AS object_name,
                   o.kind AS kind,
                   o.signature AS signature,
                   o.definition_count AS definition_count,
                   o.owner_path AS owner_path,
                   r.repo_name AS repo,
                   r.commit AS commit
            """,
            {"identifier": identifier},
            max_rows=2,
        )
        if not rows:
            raise SqlObjectResolutionError(
                f"No indexed SQL object exists for exact key {identifier!r}."
            )
        row = rows[0]
        if (repo is not None and row.get("repo") != repo) or (
            commit is not None and row.get("commit") != commit
        ):
            raise SqlObjectResolutionError(
                f"Exact key {identifier!r} does not belong to requested "
                f"repository/commit ({repo!r}, {commit!r})."
            )
        return row

    _require_repo_for_qualified_name(identifier, repo)
    schema_name, object_name = _parse_qualified_sql_name(identifier)
    candidates = db.execute_read(
        """
        // codekg: qualified-sql-object-selector
        MATCH (r:Repository {repo_name: $repo})-[:HAS_DATABASE]->(:Database)
              -[:HAS_OBJECT]->(o:SqlObject)
        WHERE o.schema_name = $schema_name
          AND o.object_name = $object_name
          AND ($commit IS NULL OR r.commit = $commit)
          AND ($database IS NULL OR o.database_name = $database)
        RETURN o.key AS key,
               o.database_name AS database_name,
               o.schema_name AS schema_name,
               o.object_name AS object_name,
               o.kind AS kind,
               o.signature AS signature,
               o.definition_count AS definition_count,
               o.owner_path AS owner_path,
               r.repo_name AS repo,
               r.commit AS commit
        ORDER BY r.commit, o.kind, o.key
        LIMIT 501
        """,
        {
            "repo": repo,
            "commit": commit,
            "database": database,
            "schema_name": schema_name,
            "object_name": object_name,
        },
        max_rows=501,
    )
    if not candidates:
        raise SqlObjectResolutionError(
            f"No indexed SQL object matches {identifier!r} in repository {repo!r}."
        )
    if len(candidates) > 1:
        keys = ", ".join(str(candidate.get("key")) for candidate in candidates)
        raise SqlObjectResolutionError(
            f"Qualified SQL name {identifier!r} is ambiguous in repository {repo!r}; "
            f"use one of these exact keys: {keys}."
        )
    return candidates[0]


def _parse_qualified_sql_name(identifier: str) -> tuple[str, str]:
    if "." not in identifier:
        raise SqlObjectResolutionError(
            f"SQL object selector {identifier!r} requires schema.object_name "
            "or an exact object_key."
        )
    schema_name, object_name = identifier.split(".", 1)
    if not schema_name or not object_name:
        raise SqlObjectResolutionError(
            f"SQL object selector {identifier!r} requires schema.object_name "
            "or an exact object_key."
        )
    return schema_name, object_name


def _looks_like_exact_sql_key(identifier: str) -> bool:
    return ":sql:" in identifier or (identifier.count("@") >= 1 and ":" in identifier)


def _require_repo_for_qualified_name(identifier: str, repo: str | None) -> None:
    if repo is None and not _looks_like_exact_sql_key(identifier):
        raise SqlObjectResolutionError(
            f"Qualified SQL name {identifier!r} requires an explicit repository; "
            "use an exact object_key or provide repository."
        )


def _validate_repository_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("file must be a non-empty repository-relative path")
    if (
        PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith("\\")
    ):
        raise ValueError("file must be a repository-relative path")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("Cannot safely normalize an indexed file path containing traversal.")
    return normalized


def _repository_snapshots(db: Neo4jClient) -> list[dict[str, object]]:
    return db.execute_read(
        """
        MATCH (r:Repository)
        RETURN r.repo_name AS repo_name, r.commit AS commit
        ORDER BY repo_name, commit
        """,
        max_rows=100,
    )


def _search_limit(value: int) -> int:
    return max(1, min(int(value), _SEARCH_MAX_LIMIT))


def _usage_limit(value: int) -> int:
    return max(1, min(int(value), 500))


def _file_limit(value: int) -> int:
    return max(1, min(int(value), 500))
