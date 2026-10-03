from __future__ import annotations

import json
import logging

import pytest

from codekg.mcp import server
from codekg.mcp.server import SearchScope, mcp

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_registers_fourteen_tools() -> None:
    tools = await mcp.get_tools()

    assert len(tools) == 14
    assert sorted(tools) == [
        "find_callees",
        "find_callers",
        "find_dead_code",
        "find_importers",
        "find_sql_usages",
        "get_class_hierarchy",
        "get_complexity",
        "get_definition",
        "get_sql_in_file",
        "get_sql_object",
        "list_repositories",
        "search_sql_objects",
        "search_symbols",
        "trace_call_path",
    ]
    assert all(tool.description for tool in tools.values())
    assert "line bounds" in tools["get_definition"].description.lower()
    assert "static callers" in tools["find_callers"].description.lower()
    assert "static callees" in tools["find_callees"].description.lower()
    assert "unreferenced candidates" in tools["find_dead_code"].description.lower()
    assert "commit-pinned" in mcp.instructions.lower()
    wrapped_list_tools = set(tools) - {
        "search_symbols",
        "search_sql_objects",
        "get_sql_object",
        "get_sql_in_file",
    }
    assert tools["search_symbols"].output_schema is None
    assert tools["search_sql_objects"].output_schema is None
    assert tools["get_sql_object"].output_schema is None
    assert tools["get_sql_in_file"].output_schema is None
    assert all(
        tools[name].output_schema == server._WRAPPED_LIST_OUTPUT_SCHEMA
        for name in wrapped_list_tools
    )


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
    caplog: pytest.LogCaptureFixture,
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
                "match_type": "exact_qualified_name",
                "scope": "source",
            }
        ],
        "next_cursor": "opaque-next-page",
        "diagnostics": {
            "candidate_pool": 100,
            "scoped_candidate_count": 7,
            "ranked_count": 7,
            "exact_match_count": 1,
            "query_terms": ["prepare", "session"],
            "ignored_terms": [],
        },
    }
    captured: dict[str, object] = {}

    def fake_discover_symbols(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return response

    monkeypatch.setattr(server, "query_discover_symbols", fake_discover_symbols)
    caplog.set_level(logging.INFO, logger=server.__name__)
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
                "match_type": "exact_qualified_name",
            }
        ],
        "next_cursor": "opaque-next-page",
        "scope": "source",
        "recommended_next_tool": "get_definition",
    }
    text = result.content[0].text
    assert text.startswith(
        "Found 1 symbol candidate(s) in repository=requests. More results are available."
    )
    assert "requests.sessions.Session.prepare_request" in text
    assert "requests/sessions.py:450" in text
    assert len(text.encode()) < len(json.dumps(response, separators=(",", ":")).encode())
    assert result.structured_content["next_cursor"] == "opaque-next-page"
    assert "diagnostics" not in result.structured_content
    assert "scope" not in result.structured_content["results"][0]
    log = next(
        record.message for record in caplog.records if record.message.startswith("codekg_search")
    )
    assert '"candidate_pool":100' in log
    assert '"query_terms_count":2' in log
    assert "prepare session" not in log


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


def test_discovery_guidance_never_emits_recommended_symbol_id() -> None:
    """B4 (codekg-ranking-presentation-spec.md): removed via the B4.4 escape
    hatch after calibration found no margin threshold separates correct from
    wrong rank-1 recommendations. An absent field, not a confidently wrong
    one."""
    response: dict[str, object] = {
        "status": "ok",
        "results": [
            {"symbol_id": "requests@abc:sessions.py:Session.request:1", "score": 9010},
            {"symbol_id": "requests@abc:sessions.py:Session.other:2", "score": 10},
        ],
    }

    server._add_discovery_guidance(response, SearchScope.SOURCE)

    assert response["recommended_next_tool"] == "get_definition"
    assert "recommended_symbol_id" not in response
    assert "rank_margin" not in response
    assert "ambiguous" not in response


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


@pytest.mark.asyncio
async def test_list_repositories_uses_structured_rows_and_hides_storage_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {
                "repo_name": "requests",
                "commit": "f361ead047be",
                "root_path": "/repos/requests",
                "files": 37,
            }
        ],
    )

    result = await (await mcp.get_tools())["list_repositories"].run({})

    assert result.structured_content == {
        "result": [
            {
                "repo_name": "requests",
                "commit": "f361ead047be",
                "root_path": ".",
                "files": 37,
            }
        ]
    }
    text = result.content[0].text
    assert text.startswith("Found 1 indexed repository.")
    assert "requests @ f361ead047be (37 files)" in text
    assert "/repos/requests" not in text
    assert text != json.dumps(result.structured_content)
    assert server._wrapped_list_result("list_repositories", [{}, {}, {}, {}, {}]).content[
        0
    ].text.startswith("Found 5 indexed repositories.")


@pytest.mark.asyncio
async def test_definition_normalizes_only_file_and_preserves_stable_row_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "key": "requests@f361ead047be:sessions.py:Session.prepare_request:511",
        "qname": "src.requests.sessions.Session.prepare_request",
        "signature": "def prepare_request(self, request)",
        "file": "/repos/requests/src/requests/sessions.py",
        "start_line": 511,
        "end_line": 555,
        "repo": "requests",
        "commit": "f361ead047be",
    }
    monkeypatch.setattr(server, "query_get_definition", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {
                "repo_name": "requests",
                "commit": "f361ead047be",
                "root_path": "/repos/requests",
            }
        ],
    )

    result = await (await mcp.get_tools())["get_definition"].run({"identifier": row["key"]})
    normalized = result.structured_content["result"][0]

    assert normalized == {**row, "file": "src/requests/sessions.py", "symbol_id": row["key"]}
    text = result.content[0].text
    assert text.startswith("Found 1 definition record.")
    assert "src.requests.sessions.Session.prepare_request" in text
    assert "src/requests/sessions.py:511" in text
    assert json.dumps(normalized) not in text


@pytest.mark.asyncio
async def test_relationship_rows_use_symbol_identity_to_normalize_paths_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = {
        "key": "requests@commit:caller.py:caller:1",
        "qname": "caller",
        "file": "/repos/requests/caller.py",
        "start_line": 1,
        "end_line": 2,
        "depth": 1,
    }
    monkeypatch.setattr(server, "query_find_callers", lambda *_args, **_kwargs: [caller])
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {
                "repo_name": "requests",
                "commit": "commit",
                "root_path": "/repos/requests",
            }
        ],
    )

    result = await (await mcp.get_tools())["find_callers"].run(
        {"identifier": "requests@commit:target.py:target:1"}
    )

    assert result.structured_content == {
        "result": [{**caller, "file": "caller.py", "symbol_id": caller["key"]}],
    }
    assert "caller (caller.py:1)" in result.content[0].text
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [{"repo_name": "requests", "root_path": "/repos/requests"}],
    )
    with pytest.raises(ValueError, match="Cannot safely normalize"):
        server._normalize_public_rows([{"repo": "requests", "file": "/unrelated/private.py"}])


@pytest.mark.asyncio
async def test_dead_code_normalizes_absolute_paths_using_tool_scope_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "query_find_dead_code",
        lambda *_args, **_kwargs: [
            {"key": "requests@abc:dead.py:dead:1", "file": "/repos/requests/dead.py"}
        ],
    )
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [{"repo_name": "requests", "commit": "abc", "root_path": "/repos/requests"}],
    )

    result = await (await mcp.get_tools())["find_dead_code"].run(
        {"repository": "requests", "commit": "abc"}
    )

    assert result.structured_content["result"][0]["file"] == "dead.py"


@pytest.mark.asyncio
async def test_complexity_normalizes_absolute_paths_using_commit_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "query_get_complexity",
        lambda *_args, **_kwargs: [{"file": "/repos/new-requests/complex.py", "repo": "requests"}],
    )
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {"repo_name": "requests", "commit": "old", "root_path": "/repos/old-requests"},
            {"repo_name": "requests", "commit": "new", "root_path": "/repos/new-requests"},
        ],
    )

    result = await (await mcp.get_tools())["get_complexity"].run(
        {"repository": "requests", "commit": "new", "top_n": 1}
    )

    assert result.structured_content["result"][0]["file"] == "complex.py"


@pytest.mark.asyncio
async def test_path_normalization_supports_windows_paths_and_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {
                "repo_name": "requests",
                "commit": "f361ead047be",
                "root_path": r"C:\repos\requests",
            }
        ],
    )
    normalized = server._normalize_public_rows(
        [{"repo": "requests", "file": r"C:\repos\requests\src\requests\sessions.py"}]
    )

    assert normalized == [{"repo": "requests", "file": "src/requests/sessions.py"}]

    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {"repo_name": "requests", "commit": "old", "root_path": r"C:\old\requests"},
            {
                "repo_name": "requests",
                "commit": "f361ead047be",
                "root_path": r"C:\repos\requests",
            },
        ],
    )

    response = {
        "status": "ok",
        "repository": "requests",
        "commit": "f361ead047be",
        "results": [
            {
                "symbol_id": "requests@f361ead047be:sessions.py:Session.prepare_request:511",
                "qualified_name": "src.requests.sessions.Session.prepare_request",
                "file": r"C:\repos\requests\src\requests\sessions.py",
                "start_line": 511,
                "end_line": 555,
                "score": 1.0,
                "matched_terms": ["prepare"],
            }
        ],
        "next_cursor": "stable-cursor",
    }
    monkeypatch.setattr(server, "query_discover_symbols", lambda **_kwargs: response)

    result = await (await mcp.get_tools())["search_symbols"].run(
        {"repository": "requests", "query": "prepare"}
    )

    hit = result.structured_content["results"][0]
    assert hit["file"] == "src/requests/sessions.py"
    assert "\\" not in hit["file"]
    assert hit["symbol_id"] == response["results"][0]["symbol_id"]
    assert result.structured_content["next_cursor"] == "stable-cursor"


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("src/../private.py", None),
        (r"src\..\private.py", None),
        ("/repos/requests/src/../private.py", "/repos/requests"),
        (r"C:\repos\requests\src\..\private.py", r"C:\repos\requests"),
    ],
)
def test_path_normalization_rejects_traversal_in_relative_and_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    root: str | None,
) -> None:
    if root is not None:
        monkeypatch.setattr(
            server,
            "query_list_repositories",
            lambda: [{"repo_name": "requests", "root_path": root}],
        )
    with pytest.raises(ValueError, match="traversal"):
        server._normalize_public_rows([{"repo": "requests", "file": path}])


@pytest.mark.asyncio
async def test_find_callers_empty_result_includes_callback_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "query_find_callers", lambda *_args, **_kwargs: [])

    result = await (await mcp.get_tools())["find_callers"].run(
        {
            "identifier": "demo@abc:pkg.py:demo.PGService.callback:9",
            "repository": "demo",
        }
    )

    text = result.content[0].text
    assert text.startswith("Found 0 callers for demo@abc:pkg.py:demo.PGService.callback:9.")
    assert "callback" in text.lower()
    assert "list_repositories" in text


def test_wrapped_list_result_includes_bounded_previews() -> None:
    rows = [
        {
            "key": "demo@abc:a.py:demo.fn_one:1",
            "qname": "demo.fn_one",
            "file": "a.py",
            "start_line": 1,
            "resolution": "heuristic",
        },
        {
            "key": "demo@abc:b.py:demo.fn_two:2",
            "qname": "demo.fn_two",
            "file": "b.py",
            "start_line": 2,
        },
    ]

    result = server._wrapped_list_result("find_callers", rows, subject="demo.fn")

    assert result.structured_content["result"][0]["symbol_id"] == rows[0]["key"]
    text = result.content[0].text
    assert "demo.fn_one (a.py:1) [heuristic]" in text
    assert "demo.fn_two (b.py:2)" in text


def test_path_normalization_rejects_ambiguous_snapshot_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(
        server,
        "query_list_repositories",
        lambda: [
            {"repo_name": "requests", "commit": "old", "root_path": "/repos/old-requests"},
            {"repo_name": "requests", "commit": "new", "root_path": "/repos/new-requests"},
        ],
    )
    with pytest.raises(ValueError, match="absolute file path"):
        server._normalize_public_rows(
            [{"repo": "requests", "file": "/repos/new-requests/sessions.py"}]
        )
    assert server._normalize_public_rows(
        [{"repo": "requests", "commit": "new", "file": "/repos/new-requests/sessions.py"}]
    ) == [{"repo": "requests", "commit": "new", "file": "sessions.py"}]
