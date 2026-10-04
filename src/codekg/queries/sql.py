"""Bounded SQL-graph investigation queries used by the MCP server."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from codekg.neo4j_client import Neo4jClient, get_client


def _limit(value: int, maximum: int = 500) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _repository(repository: str | None, commit: str | None, client: Neo4jClient) -> dict[str, Any]:
    if repository is not None and (not repository.strip() or "@" in repository):
        raise ValueError("repository must be a non-empty repository name")
    rows = client.execute_read(
        """MATCH (r:Repository)
        WHERE ($repository IS NULL OR r.repo_name=$repository)
          AND ($commit IS NULL OR r.commit=$commit)
        RETURN r.repo_name AS repository, r.commit AS commit, r.key AS repo_key
        ORDER BY repository, commit LIMIT 3""",
        {"repository": repository, "commit": commit},
        max_rows=3,
        timeout_seconds=5.0,
    )
    if not rows:
        return {"status": "not_found", "repository": repository, "commit": commit}
    if repository is None and len(rows) > 1:
        names = sorted({str(row["repository"]) for row in rows})
        return {"status": "ambiguous_repository", "repositories": names}
    if len(rows) > 1:
        return {
            "status": "ambiguous_snapshot",
            "repository": repository,
            "commits": sorted(str(row["commit"]) for row in rows),
        }
    return {"status": "ok", **rows[0]}


def _object_match(identifier: str, repository: str, commit: str | None, database: str | None):
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 512:
        raise ValueError("identifier must be non-empty and at most 512 characters")
    if identifier.count(".") == 1 and not any(c in identifier for c in "/\\@:"):
        schema, name = identifier.split(".")
        return "o.schema_name=$schema AND o.object_name=$name", {"schema": schema, "name": name}
    return "o.key=$object_key", {"object_key": identifier}


def search_sql_objects(
    query: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    schema: str | None = None,
    kind: str | None = None,
    commit: str | None = None,
    limit: int = 5,
    client: Neo4jClient | None = None,
) -> dict[str, Any]:
    _limit(limit, 20)
    if not isinstance(query, str) or not query.strip() or len(query) > 256:
        raise ValueError("query must be non-empty and at most 256 characters")
    db = client or get_client()
    scope = _repository(repository, commit, db)
    if scope["status"] != "ok":
        return {**scope, "results": []}
    rows = db.execute_read(
        """MATCH (r:Repository {repo_name:$repository, commit:$commit})
        -[:HAS_DATABASE]->(d:Database)-[:HAS_OBJECT]->(o:SqlObject)
        WHERE ($database IS NULL OR d.database_name=$database OR o.database_name=$database)
          AND ($schema IS NULL OR o.schema_name=$schema)
          AND ($kind IS NULL OR o.kind=$kind)
          AND (o.object_name CONTAINS $query OR o.schema_name CONTAINS $query)
        RETURN o.key AS object_key, o.database_name AS database_name,
               o.schema_name AS schema_name, o.kind AS kind, o.object_name AS object_name,
               o.signature AS signature, o.definition_count AS definition_count,
               o.owner_path AS owner_path
        ORDER BY o.schema_name,o.object_name,o.signature LIMIT $limit""",
        {
            "repository": scope["repository"],
            "commit": scope["commit"],
            "database": database,
            "schema": schema,
            "kind": kind,
            "query": query,
            "limit": limit,
        },
        max_rows=limit,
        timeout_seconds=10.0,
    )
    return {
        "status": "ok",
        "repository": scope["repository"],
        "commit": scope["commit"],
        "results": rows,
    }


def get_sql_object(
    identifier: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    commit: str | None = None,
    client: Neo4jClient | None = None,
) -> dict[str, Any]:
    db = client or get_client()
    scope = _repository(repository, commit, db)
    if scope["status"] != "ok":
        return {**scope, "object": None, "definitions": []}
    predicate, values = _object_match(identifier, scope["repository"], scope["commit"], database)
    rows = db.execute_read(
        f"""MATCH (r:Repository {{repo_name:$repository, commit:$commit}})
        -[:HAS_DATABASE]->(d:Database)-[:HAS_OBJECT]->(o:SqlObject)
        WHERE {predicate} AND ($database IS NULL OR d.database_name=$database
          OR o.database_name=$database)
        CALL {{ WITH o
          OPTIONAL MATCH (s:SqlStatement)-[edge:DEFINES]->(o)
          OPTIONAL MATCH (f:File)-[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL]->(s)
          WITH f,s,edge ORDER BY f.path,s.start_line,s.start_column LIMIT 20
          RETURN collect(CASE WHEN s IS NULL THEN null ELSE {{path:f.path,kind:s.kind,
            role:type(edge),line:s.start_line,column:s.start_column,
            statement_ordinal:s.ordinal}} END) AS definitions
        }}
        RETURN o.key AS object_key,o.database_name AS database_name,o.schema_name AS schema_name,
          o.kind AS kind,o.object_name AS object_name,o.signature AS signature,
          o.definition_count AS definition_count,o.owner_path AS owner_path,definitions
        LIMIT 20""",
        {
            "repository": scope["repository"],
            "commit": scope["commit"],
            "database": database,
            **values,
        },
        max_rows=20,
        timeout_seconds=10.0,
    )
    if not rows:
        return {
            "status": "not_found",
            "object": None,
            "definitions": [],
            "repository": scope["repository"],
            "commit": scope["commit"],
        }
    if len(rows) > 1:
        return {
            "status": "ambiguous",
            "object": None,
            "definitions": [],
            "repository": scope["repository"],
            "commit": scope["commit"],
        }
    row = rows[0]
    return {
        "status": "ok",
        "repository": scope["repository"],
        "commit": scope["commit"],
        "object": {k: v for k, v in row.items() if k != "definitions"},
        "definitions": row.get("definitions", [])[:20],
    }


def find_sql_usages(
    identifier: str,
    *,
    repository: str | None = None,
    database: str | None = None,
    commit: str | None = None,
    role: str = "all",
    resolution: str = "exact",
    limit: int = 50,
    client: Neo4jClient | None = None,
) -> list[dict[str, Any]]:
    _limit(limit)
    if role not in {"all", "read", "write", "call", "alter", "drop", "define"}:
        raise ValueError("invalid SQL role")
    if resolution not in {"exact", "ambiguous", "unresolved", "dynamic", "all"}:
        raise ValueError("invalid SQL resolution")
    db = client or get_client()
    scope = _repository(repository, commit, db)
    if scope["status"] != "ok":
        return [{"status": scope["status"], **{k: v for k, v in scope.items() if k != "status"}}]
    pred, values = _object_match(identifier, scope["repository"], scope["commit"], database)
    exact = resolution in {"exact", "all"}
    candidates = resolution != "exact"
    rows = db.execute_read(
        f"""MATCH (r:Repository {{repo_name:$repository,commit:$commit}})
      -[:HAS_DATABASE]->(d:Database)-[:HAS_OBJECT]->(o:SqlObject)
      WHERE {pred} AND ($database IS NULL OR d.database_name=$database OR o.database_name=$database)
      CALL {{ WITH o
        MATCH (s:SqlStatement)-[e:DEFINES|READS_FROM|WRITES_TO|INVOKES_SQL|ALTERS|DROPS]->(o)
          MATCH (r:Repository {{repo_name:$repository,commit:$commit}})-[:CONTAINS]->(f:File)
              -[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL]->(s)
        WITH s,e,f WHERE $want_exact AND ($role='all' OR e.role=$role)
        RETURN f.path AS file,s.start_line AS line,s.start_column AS column,e.role AS role,
          type(e) AS relationship,'exact' AS resolution,s.kind AS statement_kind
        ORDER BY file,line,column LIMIT $limit
        UNION ALL WITH o
        MATCH (r:Repository {{repo_name:$repository,commit:$commit}})-[:CONTAINS]->(f:File)
              -[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL]->(s:SqlStatement)
              -[:HAS_REFERENCE]->(ref:Reference)
        WHERE $want_candidates
          AND ($resolution='all' OR ref.status=$resolution
               OR ($resolution='dynamic' AND ref.dynamic=true))
          AND ($role='all' OR ref.role=$role)
          AND (ref.candidate_keys_json CONTAINS ('"' + o.key + '"') OR
            (ref.status IN ['unresolved','dynamic'] AND ref.object_name=o.object_name
             AND (ref.schema_name IS NULL OR ref.schema_name=o.schema_name)
             AND (ref.database_name IS NULL OR ref.database_name=o.database_name)
             AND (ref.object_kind_hint IS NULL OR ref.object_kind_hint=o.kind)
             AND (ref.signature_hint IS NULL OR ref.signature_hint=o.signature)))
        RETURN f.path AS file,ref.start_line AS line,ref.start_column AS column,ref.role AS role,
          'HAS_REFERENCE' AS relationship,ref.status AS resolution,s.kind AS statement_kind
        ORDER BY file,line,column LIMIT $limit
      }} RETURN file,line,column,role,relationship,resolution,statement_kind
      ORDER BY file,line,column LIMIT $limit""",
        {
            "repository": scope["repository"],
            "commit": scope["commit"],
            "database": database,
            **values,
            "want_exact": exact,
            "want_candidates": candidates,
            "resolution": resolution,
            "role": role,
            "limit": limit,
        },
        max_rows=limit,
        timeout_seconds=10.0,
    )
    return rows


def get_sql_in_file(
    file: str,
    *,
    repository: str,
    commit: str | None = None,
    include_text: bool = False,
    limit: int = 100,
    client: Neo4jClient | None = None,
) -> dict[str, Any]:
    _limit(limit)
    if (
        not isinstance(file, str)
        or not file
        or "\\" in file
        or PurePosixPath(file).is_absolute()
        or ".." in PurePosixPath(file).parts
    ):
        raise ValueError("file must be a repository-relative path without traversal")
    db = client or get_client()
    scope = _repository(repository, commit, db)
    if scope["status"] != "ok":
        return {**scope, "file": file, "artifacts": [], "statements": [], "references": []}
    params = {
        "repository": scope["repository"],
        "commit": scope["commit"],
        "file": file,
        "limit": limit,
        "include_text": include_text,
    }
    artifacts = db.execute_read(
        """MATCH (r:Repository {repo_name:$repository,commit:$commit})
        -[:CONTAINS]->(f:File {path:$file})
        MATCH (f)-[:CONTAINS_SQL]->(a:SqlArtifact)
        WITH a ORDER BY a.ordinal LIMIT $limit
        RETURN a.key AS key,a.path AS path,a.ordinal AS ordinal,a.origin AS origin,
          a.dialect AS dialect,CASE WHEN $include_text THEN a.text ELSE null END AS text,
          a.text_hash AS text_hash,a.start_line AS start_line,a.end_line AS end_line""",
        params,
        max_rows=limit,
        timeout_seconds=10.0,
    )
    statements = db.execute_read(
        """MATCH (r:Repository {repo_name:$repository,commit:$commit})
        -[:CONTAINS]->(f:File {path:$file})
        MATCH (f)-[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL]->(s:SqlStatement)
        WITH s ORDER BY s.artifact_ordinal,s.ordinal LIMIT $limit
        RETURN s.key AS key,s.ordinal AS ordinal,s.kind AS kind,
          s.artifact_ordinal AS artifact_ordinal,s.parent_key AS parent_key,
          s.control_context AS control_context,s.start_line AS start_line,
          s.start_column AS start_column,s.end_line AS end_line,s.end_column AS end_column""",
        params,
        max_rows=limit,
        timeout_seconds=10.0,
    )
    references = db.execute_read(
        """MATCH (r:Repository {repo_name:$repository,commit:$commit})
        -[:CONTAINS]->(f:File {path:$file})
        MATCH (f)-[:CONTAINS_SQL]->(:SqlArtifact)-[:CONTAINS_SQL]->(:SqlStatement)
          -[:HAS_REFERENCE]->(ref:Reference)
        WITH ref ORDER BY ref.artifact_ordinal,ref.statement_ordinal,ref.ordinal LIMIT $limit
        RETURN ref.key AS key,ref.ordinal AS ordinal,ref.raw_name AS raw_name,
          ref.database_name AS database_name,ref.schema_name AS schema_name,
          ref.object_name AS object_name,ref.object_kind_hint AS object_kind_hint,
          ref.signature_hint AS signature_hint,ref.status AS status,ref.role AS role,
          ref.dynamic AS dynamic,ref.candidate_count AS candidate_count,
          ref.start_line AS start_line,ref.start_column AS start_column,
          ref.end_line AS end_line,ref.end_column AS end_column""",
        params,
        max_rows=limit,
        timeout_seconds=10.0,
    )
    data = {"artifacts": artifacts, "statements": statements, "references": references}
    if not data.get("artifacts"):
        return {
            "status": "not_found",
            "repository": scope["repository"],
            "commit": scope["commit"],
            "file": file,
            **data,
        }
    return {
        "status": "ok",
        "repository": scope["repository"],
        "commit": scope["commit"],
        "file": file,
        **data,
    }
