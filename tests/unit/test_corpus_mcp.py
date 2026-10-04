from __future__ import annotations

import pytest

from codekg.mcp import server

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_five_corpus_tools_are_registered_and_return_structured_results(monkeypatch):
    tools = await server.mcp.get_tools()
    names = {
        "list_corpus_snapshots",
        "search_corpus_symbols",
        "get_dependency_evidence",
        "trace_corpus_path",
        "compare_corpus_snapshots",
    }
    assert names <= tools.keys()
    assert all(tools[name].output_schema == server._WRAPPED_LIST_OUTPUT_SCHEMA for name in names)
    monkeypatch.setattr(server, "query_list_corpus_snapshots", lambda **kwargs: [{"alias": "app"}])
    monkeypatch.setattr(
        server, "query_search_corpus_symbols", lambda *args, **kwargs: [{"key": "k"}]
    )
    monkeypatch.setattr(
        server, "query_get_dependency_evidence", lambda *args, **kwargs: [{"key": "e"}]
    )
    monkeypatch.setattr(server, "query_trace_corpus_path", lambda *args, **kwargs: [{"nodes": []}])
    monkeypatch.setattr(
        server, "query_compare_corpus_snapshots", lambda *args, **kwargs: [{"change": "added"}]
    )

    results = [
        server.list_corpus_snapshots.fn(),
        server.search_corpus_symbols.fn("api", "pg18"),
        server.get_dependency_evidence.fn("native-key"),
        server.trace_corpus_path.fn("from", "to"),
        server.compare_corpus_snapshots.fn("pg18", "pg19"),
    ]
    assert [result.structured_content["result"] for result in results] == [
        [{"alias": "app"}],
        [{"key": "k", "symbol_id": "k"}],
        [{"key": "e", "symbol_id": "e"}],
        [{"nodes": []}],
        [{"change": "added"}],
    ]
    assert all(result.content for result in results)


def test_corpus_previews_include_relative_source_locations():
    assert (
        server._format_row_preview(
            1,
            {"name": "api", "evidence_path": "src/runbook.md", "line": 12},
            tool_name="get_dependency_evidence",
        )
        == "  1. api src/runbook.md:12"
    )
    assert (
        server._format_row_preview(
            1, {"name": "api", "path": "src/api.sql"}, tool_name="search_corpus_symbols"
        )
        == "  1. api src/api.sql"
    )
    assert (
        server._format_row_preview(
            1,
            {"nodes": [{"name": "api", "path": "src/api.c"}]},
            tool_name="trace_corpus_path",
        )
        == "  1. api (src/api.c)"
    )
