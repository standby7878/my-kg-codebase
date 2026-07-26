from __future__ import annotations

import json

import pytest

from codekg.mcp import server
from codekg.mcp.server import SearchScope, mcp

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_registers_exactly_ten_tools() -> None:
    tools = await mcp.get_tools()

    assert len(tools) == 10
    assert sorted(tools) == [
        "find_callees",
        "find_callers",
        "find_dead_code",
        "find_importers",
        "get_class_hierarchy",
        "get_complexity",
        "get_definition",
        "list_repositories",
        "search_symbols",
        "trace_call_path",
    ]
    assert all(tool.description for tool in tools.values())
    assert "line bounds" in tools["get_definition"].description.lower()
    assert "verify bounded incoming" in tools["find_callers"].description.lower()
    assert "verify bounded outgoing" in tools["find_callees"].description.lower()
    assert "unreferenced candidates" in tools["find_dead_code"].description.lower()


@pytest.mark.asyncio
async def test_search_symbols_has_compact_repository_scoped_schema() -> None:
    tool = (await mcp.get_tools())["search_symbols"]
    properties = tool.parameters["properties"]

    assert set(
        ("query", "repository", "kind", "commit", "mode", "scope", "limit", "cursor")
    ) <= set(properties)
    assert "q" not in properties
    assert "repo" not in properties
    assert properties["limit"]["default"] == 5
    assert properties["limit"]["maximum"] == 20
    assert properties["mode"]["default"] == "hybrid"
    assert "hybrid" in properties["mode"]["enum"]
    assert properties["scope"]["default"] == "source"
    assert properties["scope"]["enum"] == [
        "source",
        "tests",
        "docs",
        "examples",
        "benchmarks",
        "all",
    ]


@pytest.mark.asyncio
async def test_search_symbols_returns_concise_text_and_canonical_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "status": "ok",
        "repository": "requests",
        "commit": "f361ead047be",
        "results": [
            {
                "symbol_id": "requests@f361ead047be:sessions.py:Session.prepare_request:450",
                "qualified_name": "requests.sessions.Session.prepare_request",
                "file": "requests/sessions.py",
                "start_line": 450,
                "end_line": 500,
                "score": 12.34,
                "matched_terms": ["prepare", "session"],
            }
        ],
        "next_cursor": "opaque-next-page",
    }
    captured: dict[str, object] = {}

    def fake_discover_symbols(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return response

    monkeypatch.setattr(server, "query_discover_symbols", fake_discover_symbols)
    tool = (await mcp.get_tools())["search_symbols"]
    result = await tool.run({"repository": "requests", "query": "prepare session"})

    assert captured == {
        "repository": "requests",
        "query": "prepare session",
        "kind": None,
        "commit": None,
        "mode": "hybrid",
        "scope": "source",
        "limit": 5,
        "cursor": None,
    }
    assert result.structured_content == {
        **response,
        "scope": "source",
        "diagnostics": {"scope": "source", "candidate_count": 1},
        "recommended_next_tool": "get_definition",
        "recommended_symbol_id": "requests@f361ead047be:sessions.py:Session.prepare_request:450",
    }
    text = result.content[0].text
    assert (
        text == "Found 1 symbol candidate(s) in repository=requests. "
        "More results are available. See structured result."
    )
    assert len(text.encode()) < len(json.dumps(response, separators=(",", ":")).encode())
    assert "Session.prepare_request" not in text


def test_discovery_guidance_preserves_query_diagnostics_and_recommendation() -> None:
    response: dict[str, object] = {
        "status": "ok",
        "results": [{"symbol_id": "requests@abc:sessions.py:Session.request:1"}],
        "diagnostics": {"match_types": ["exact_qualified_name"]},
        "recommended_next_tool": "find_callees",
    }

    server._add_discovery_guidance(response, SearchScope.SOURCE)

    assert response["scope"] == "source"
    assert response["diagnostics"] == {"match_types": ["exact_qualified_name"]}
    assert response["recommended_next_tool"] == "find_callees"
    assert response["recommended_symbol_id"] == "requests@abc:sessions.py:Session.request:1"


def test_search_symbols_error_summary_does_not_duplicate_available_repositories() -> None:
    response = {
        "status": "repository_not_found",
        "repository": "patroni",
        "available_repositories": ["click", "requests", "sql"],
    }

    text = server._search_summary(response)

    assert (
        text == "Symbol discovery status=repository_not_found; repository=patroni. "
        "See structured result."
    )
    assert "click" not in text
