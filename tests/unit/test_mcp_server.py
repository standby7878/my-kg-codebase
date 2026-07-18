from __future__ import annotations

import pytest

from codekg.mcp.server import mcp

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
    assert "exact symbol key" in tools["get_definition"].description.lower()
    assert "callsite" in tools["find_callers"].description.lower()
    assert "unreferenced candidates" in tools["find_dead_code"].description.lower()
