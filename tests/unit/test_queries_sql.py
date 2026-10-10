from __future__ import annotations

import pytest

from codekg.queries.sql import (
    SqlObjectResolutionError,
    find_sql_usages,
    get_sql_in_file,
    get_sql_object,
    search_sql_objects,
)

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, responses: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.responses = responses or {}

    def execute_read(
        self,
        query: str,
        params: dict[str, object] | None = None,
        *,
        max_rows: int = 1000,
    ) -> list[dict[str, object]]:
        self.calls.append((query, params or {}, max_rows))
        for marker, rows in self.responses.items():
            if marker in query:
                return rows
        if "MATCH (r:Repository)" in query and "repo_name" in query:
            return [{"repo_name": "demo", "commit": "abc123"}]
        return []


def test_search_sql_objects_requires_nonempty_query() -> None:
    client = FakeClient()

    response = search_sql_objects("   ", repository="demo", client=client)  # type: ignore[arg-type]

    assert response == {"status": "invalid_query", "results": []}
    assert client.calls == []


def test_search_sql_objects_caps_limit_and_passes_filters() -> None:
    client = FakeClient(
        {
            "search-sql-objects": [
                {
                    "object_key": "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n",
                    "object_name": "users",
                    "score": 100,
                }
            ]
        }
    )

    response = search_sql_objects(
        "users",
        repository="demo",
        database="management",
        schema="public",
        kind="table",
        commit="abc123",
        limit=20,
        client=client,  # type: ignore[arg-type]
    )

    assert response["status"] == "ok"
    assert response["repository"] == "demo"
    assert response["commit"] == "abc123"
    assert response["recommended_next_tool"] == "get_sql_object"
    query, params, max_rows = client.calls[-1]
    assert "search-sql-objects" in query
    assert params["repo"] == "demo"
    assert params["commit"] == "abc123"
    assert params["database"] == "management"
    assert params["schema"] == "public"
    assert params["kind"] == "table"
    assert params["limit"] == 20
    assert max_rows == 20


def test_search_sql_objects_repository_required() -> None:
    client = FakeClient(
        {
            "MATCH (r:Repository)": [
                {"repo_name": "one", "commit": "a"},
                {"repo_name": "two", "commit": "b"},
            ]
        }
    )

    response = search_sql_objects("users", client=client)  # type: ignore[arg-type]

    assert response["status"] == "repository_required"
    assert response["available_repositories"] == ["one", "two"]


def test_get_sql_object_exact_key_and_definitions() -> None:
    object_key = "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n"
    client = FakeClient(
        {
            "exact-sql-object-selector": [
                {
                    "key": object_key,
                    "database_name": "db",
                    "schema_name": "public",
                    "object_name": "users",
                    "kind": "table",
                    "signature": None,
                    "definition_count": 1,
                    "owner_path": "schema.sql",
                    "repo": "demo",
                    "commit": "abc123",
                }
            ],
            "sql-object-definitions": [
                {
                    "statement_key": "demo@abc123:schema.sql:sql-statement:0:0",
                    "file": "schema.sql",
                    "role": "define",
                    "line": 1,
                    "column": 13,
                    "statement_kind": "CREATE TABLE",
                }
            ],
        }
    )

    response = get_sql_object(object_key, client=client)  # type: ignore[arg-type]

    assert response["status"] == "ok"
    assert response["object"]["object_key"] == object_key
    assert response["definitions"][0]["file"] == "schema.sql"
    assert response["recommended_next_tool"] == "find_sql_usages"


def test_get_sql_object_qualified_name_requires_repository() -> None:
    client = FakeClient()

    with pytest.raises(SqlObjectResolutionError, match="requires an explicit repository"):
        get_sql_object("public.users", client=client)  # type: ignore[arg-type]


def test_find_sql_usages_exact_edges_only_by_default() -> None:
    object_key = "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n"
    client = FakeClient(
        {
            "exact-sql-object-selector": [
                {
                    "key": object_key,
                    "database_name": "db",
                    "schema_name": "public",
                    "object_name": "users",
                    "kind": "table",
                    "signature": None,
                    "definition_count": 1,
                    "owner_path": "schema.sql",
                    "repo": "demo",
                    "commit": "abc123",
                }
            ],
            "sql-object-derived-usages": [
                {
                    "file": "queries.sql",
                    "line": 1,
                    "column": 18,
                    "role": "read",
                    "statement_kind": "SELECT",
                    "statement_key": "demo@abc123:queries.sql:sql-statement:0:0",
                    "reference_key": None,
                    "reference_status": "exact",
                    "candidate_keys": None,
                }
            ],
        }
    )

    rows = find_sql_usages(object_key, client=client)  # type: ignore[arg-type]

    assert len(rows) == 1
    assert rows[0]["reference_status"] == "exact"
    assert "sql-object-reference-usages" not in client.calls[-1][0]


@pytest.mark.parametrize("path", ["../secrets.sql", "/tmp/x.sql", "a\\..\\x.sql", ""])
def test_get_sql_in_file_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        get_sql_in_file(path, repository="demo", client=FakeClient())  # type: ignore[arg-type]


def test_get_sql_in_file_returns_structured_sections() -> None:
    client = FakeClient(
        {
            "MATCH (r:Repository)": [{"repo_name": "demo", "commit": "abc123"}],
            "sql-file-exists": [{"file": "schema.sql", "repo": "demo", "commit": "abc123"}],
            "sql-file-artifacts": [{"artifact_key": "artifact-0", "ordinal": 0}],
            "sql-file-statements": [{"statement_key": "stmt-0", "ordinal": 0, "kind": "SELECT"}],
            "sql-file-references": [
                {
                    "reference_key": "ref-0",
                    "statement_key": "stmt-0",
                    "raw_name": "public.users",
                    "role": "read",
                    "reference_status": "exact",
                    "dynamic": False,
                    "candidate_count": 1,
                    "candidate_keys": '["demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n"]',
                    "object_key": "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n",
                    "start_line": 1,
                    "start_column": 18,
                    "end_line": 1,
                    "end_column": 29,
                }
            ],
        }
    )

    response = get_sql_in_file("schema.sql", repository="demo", client=client)  # type: ignore[arg-type]

    assert response["status"] == "ok"
    assert response["file"] == "schema.sql"
    assert response["artifacts"][0]["artifact_key"] == "artifact-0"
    assert response["references"][0]["candidate_keys"] == [
        "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n"
    ]
