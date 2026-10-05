from __future__ import annotations

import pytest

from codekg.mcp import server
from codekg.mcp.server import mcp

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_registers_fourteen_tools_including_sql() -> None:
    tools = await mcp.get_tools()

    assert len(tools) == 19
    assert {
        "search_sql_objects",
        "get_sql_object",
        "find_sql_usages",
        "get_sql_in_file",
    } <= set(tools)


@pytest.mark.asyncio
async def test_search_sql_objects_returns_structured_discovery_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "status": "ok",
        "repository": "demo",
        "commit": "abc123",
        "results": [
            {
                "object_key": "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n",
                "schema_name": "public",
                "object_name": "users",
                "kind": "table",
                "owner_path": "schema.sql",
                "repo": "demo",
                "commit": "abc123",
                "score": 100,
            }
        ],
        "recommended_next_tool": "get_sql_object",
    }
    monkeypatch.setattr(server, "query_search_sql_objects", lambda *_args, **_kwargs: response)

    result = await (await mcp.get_tools())["search_sql_objects"].run(
        {"repository": "demo", "query": "users"}
    )

    assert result.structured_content["recommended_next_tool"] == "get_sql_object"
    assert result.structured_content["results"][0]["object_name"] == "users"
    assert "users" not in result.content[0].text


@pytest.mark.asyncio
async def test_get_sql_object_normalizes_definition_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "query_get_sql_object",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "repository": "demo",
            "commit": "abc123",
            "object": {
                "object_key": "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n",
                "schema_name": "public",
                "object_name": "users",
                "owner_path": "schema.sql",
            },
            "definitions": [
                {
                    "file": "/repos/demo/schema.sql",
                    "statement_key": "stmt",
                    "role": "define",
                    "line": 1,
                    "column": 13,
                }
            ],
            "recommended_next_tool": "find_sql_usages",
        },
    )
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [{"repo_name": "demo", "commit": "abc123", "root_path": "/repos/demo"}],
    )

    result = await (await mcp.get_tools())["get_sql_object"].run(
        {"identifier": "demo@abc123:sql:v1:db:v6:public:v5:table:v5:users:n"}
    )

    assert result.structured_content["definitions"][0]["file"] == "schema.sql"
    assert "public.users" in result.content[0].text


@pytest.mark.asyncio
async def test_find_sql_usages_uses_wrapped_list_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server,
        "query_find_sql_usages",
        lambda *_args, **_kwargs: [
            {
                "file": "/repos/demo/queries.sql",
                "line": 1,
                "column": 18,
                "role": "read",
                "reference_status": "exact",
                "repo": "demo",
                "commit": "abc123",
            }
        ],
    )
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [{"repo_name": "demo", "commit": "abc123", "root_path": "/repos/demo"}],
    )

    result = await (await mcp.get_tools())["find_sql_usages"].run(
        {"identifier": "public.users", "repository": "demo"}
    )

    assert result.structured_content == {
        "result": [
            {
                "file": "queries.sql",
                "line": 1,
                "column": 18,
                "role": "read",
                "reference_status": "exact",
                "repo": "demo",
                "commit": "abc123",
            }
        ]
    }
    assert result.content[0].text.startswith("Found 1 SQL usage.")
    assert "queries.sql:1" in result.content[0].text
