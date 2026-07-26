from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
COMPOSE_FILE = Path(__file__).parents[2] / "compose" / "dev-local" / "docker-compose.yml"


def _compose_or_skip() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    if (
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        != 0
    ):
        pytest.skip("Docker Compose is unavailable")


@pytest.mark.asyncio
async def test_http_mcp_transport_supports_protocol_client_session() -> None:
    _compose_or_skip()
    fastmcp = pytest.importorskip("fastmcp")
    project = f"codekg-mcp-http-{uuid.uuid4().hex[:12]}"
    port = str(20000 + (uuid.uuid4().int % 1000))
    env = os.environ.copy()
    env["MCP_TRANSPORT"] = "http"
    env["MCP_PORT"] = port
    env["MCP_HOST"] = "0.0.0.0"
    env["NEO4J_HTTP_PORT"] = str(21000 + (uuid.uuid4().int % 1000))
    env["NEO4J_BOLT_PORT"] = str(22000 + (uuid.uuid4().int % 1000))
    command = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    try:
        built = subprocess.run(
            [*command, "build", "app-image-build"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if built.returncode != 0:
            pytest.skip(f"Required Compose image could not be built: {built.stderr.strip()}")
        started = subprocess.run(
            [*command, "up", "-d", "mcp"], capture_output=True, text=True, check=False, env=env
        )
        if started.returncode != 0:
            pytest.skip(f"Required Compose image or service unavailable: {started.stderr.strip()}")

        async def list_tools_when_ready() -> list[object]:
            deadline = asyncio.get_running_loop().time() + 60
            while True:
                try:
                    async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as client:
                        return list(await client.list_tools())
                except Exception:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    await asyncio.sleep(0.5)

        tools = await asyncio.wait_for(list_tools_when_ready(), timeout=65)
        assert tools
        assert any(tool.name == "list_repositories" for tool in tools)

        # The clean Compose graph has no repositories, which makes this a deterministic
        # protocol-level exercise of the typed error envelope without seeding graph data.
        # Use the raw MCP result so this asserts the actual wire response, not FastMCP's
        # client-side parsing convenience layer.
        async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as client:
            result = await client.call_tool_mcp("search_symbols", {"query": "prepare request"})

        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["status"] == "repository_required"
        assert result.structuredContent["results"] == []
        assert result.structuredContent["next_cursor"] is None
        assert len(result.content) == 1
        text = result.content[0].text
        structured_bytes = len(json.dumps(result.structuredContent, separators=(",", ":")).encode())
        assert len(text.encode()) < structured_bytes
        assert "available_repositories" not in text
    finally:
        subprocess.run(
            [*command, "down", "-v", "--remove-orphans"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
