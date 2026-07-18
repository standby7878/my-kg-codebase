from __future__ import annotations

import asyncio
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
    finally:
        subprocess.run(
            [*command, "down", "-v", "--remove-orphans"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
