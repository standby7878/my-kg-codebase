from __future__ import annotations

from dataclasses import replace

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ingest import scan_repository
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def test_sql_loader_is_idempotent_and_preserves_source_provenance(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "codekg.toml").write_text("[sql]\nenabled = true\n", encoding="utf-8")
    (root / "a.sql").write_text(
        "CREATE TABLE public.users (id int);\nSELECT id FROM public.users;\n",
        encoding="utf-8",
    )
    (root / "b.sql").write_text("SELECT id FROM public.users;\n", encoding="utf-8")
    repo = scan_repository(root)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client)
            first = load_repository(repo, replace=False, client=client, batch_size=2)
            second = load_repository(repo, replace=False, client=client, batch_size=2)

            def sql_snapshot():
                result = client.execute_read(
                    """
                    MATCH (r:Repository {repo_name: $repo})-[:CONTAINS]->(f:File)
                    OPTIONAL MATCH (f)-[:CONTAINS_SQL]->(artifact:SqlArtifact)
                    OPTIONAL MATCH (artifact)-[:CONTAINS_SQL]->(statement:SqlStatement)
                    OPTIONAL MATCH (ref:Reference)-[:REFERS_TO]->(obj:SqlObject)
                    RETURN count(DISTINCT f) AS files,
                           count(DISTINCT artifact) AS artifacts,
                           count(DISTINCT statement) AS statements,
                           count(DISTINCT ref) AS references,
                           count(DISTINCT obj) AS objects,
                           collect(DISTINCT artifact.path) AS paths
                    """,
                    {"repo": repo.repo_name},
                )
                for row in result:
                    row["paths"] = sorted(row["paths"])
                return result

            before_removed = sql_snapshot()
            (root / "a.sql").write_text("CREATE TABLE public.users (id int);\n", encoding="utf-8")
            rescanned = scan_repository(root)
            partial = replace(repo, files=(rescanned.files[0], repo.files[1]))
            load_repository(partial, replace=False, client=client, batch_size=2)
            after_partial = sql_snapshot()

            no_sql = replace(repo, files=())
            removed = load_repository(no_sql, replace=False, client=client, batch_size=2)
            rows = sql_snapshot()
        finally:
            client.close()

    assert first["sql_nodes"] == second["sql_nodes"]
    assert first["sql_relationships"] == second["sql_relationships"]
    assert before_removed == [
        {
            "files": 2,
            "artifacts": 2,
            "statements": 3,
            "references": 3,
            "objects": 1,
            "paths": ["a.sql", "b.sql"],
        }
    ]
    assert after_partial == [
        {
            "files": 2,
            "artifacts": 2,
            "statements": 2,
            "references": 2,
            "objects": 1,
            "paths": ["a.sql", "b.sql"],
        }
    ]
    assert removed["sql_nodes"] == 0
    assert removed["sql_relationships"] == 0
    assert rows == [
        {
            # Repository replacement clears SQL facts but intentionally
            # retains the existing File nodes.
            "files": 2,
            "artifacts": 0,
            "statements": 0,
            "references": 0,
            "objects": 0,
            "paths": [],
        }
    ]
