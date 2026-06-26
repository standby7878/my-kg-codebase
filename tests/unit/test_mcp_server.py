from __future__ import annotations

import pytest

from codekg.mcp.server import mcp

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_registers_exactly_ten_tools() -> None:
    tools = await mcp.get_tools()

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
