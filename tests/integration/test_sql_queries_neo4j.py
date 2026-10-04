from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ingest import scan_repository
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.queries.sql import find_sql_usages, get_sql_in_file, get_sql_object, search_sql_objects
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def test_sql_query_functions_run_against_loaded_graph_and_respect_scopes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "codekg.toml").write_text("[sql]\nenabled=true\ndatabase='app'\n", encoding="utf-8")
    (root / "schema.sql").write_text("CREATE TABLE public.users (id int);\n", encoding="utf-8")
    (root / "query.sql").write_text("SELECT id FROM public.users;\n", encoding="utf-8")
    repo = scan_repository(root)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client)
            load_repository(repo, replace=False, client=client)
            found = search_sql_objects("users", repository=repo.repo_name, client=client)
            assert found["status"] == "ok" and len(found["results"]) == 1
            key = found["results"][0]["object_key"]
            object_result = get_sql_object(key, repository=repo.repo_name, client=client)
            assert object_result["status"] == "ok"
            assert object_result["object"]["object_name"] == "users"
            assert any(row["path"] == "schema.sql" for row in object_result["definitions"])
            usages = find_sql_usages(key, repository=repo.repo_name, client=client)
            assert any(row["file"] == "query.sql" for row in usages)
            file_result = get_sql_in_file("query.sql", repository=repo.repo_name, client=client)
            assert file_result["status"] == "ok"
            assert file_result["references"] and file_result["artifacts"][0]["text"] is None
            with_text = get_sql_in_file(
                "query.sql", repository=repo.repo_name, include_text=True, client=client
            )
            assert "SELECT id" in with_text["artifacts"][0]["text"]
            assert search_sql_objects("users", repository="other", client=client)["results"] == []
        finally:
            client.close()
