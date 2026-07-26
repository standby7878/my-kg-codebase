from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
COMPOSE_FILE = Path(__file__).parents[2] / "compose" / "dev-local" / "docker-compose.yml"
WRAPPED_LIST_OUTPUT_SCHEMA = {
    "description": "Generic wrapper for non-object return types.",
    "properties": {
        "result": {
            "items": {"additionalProperties": True, "type": "object"},
            "type": "array",
        }
    },
    "required": ["result"],
    "type": "object",
    "x-fastmcp-wrap-result": True,
}
WRAPPED_LIST_TOOLS = {
    "list_repositories",
    "get_definition",
    "find_callers",
    "find_callees",
    "trace_call_path",
    "find_importers",
    "get_class_hierarchy",
    "find_dead_code",
    "get_complexity",
}
REPOSITORY = "requests"
COMMIT = "f361ead047be"
CALLER_ID = (
    "requests@f361ead047be:src/requests/sessions.py:src.requests.sessions.Session.request:557"
)
CALLEE_ID = (
    "requests@f361ead047be:src/requests/sessions.py:"
    "src.requests.sessions.Session.prepare_request:511"
)
REQUEST_SIGNATURE = (
    "def request(self, method, url, params=None, data=None, headers=None, "
    "cookies=None, files=None, auth=None, timeout=None, allow_redirects=True, "
    "proxies=None, hooks=None, stream=None, verify=None, cert=None, json=None)"
)


def _run_compose(
    command: list[str],
    *args: str,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*command, *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _seed_transport_graph(command: list[str], env: dict[str, str]) -> None:
    candidate_rows = ",\n".join(
        (
            "{"
            f"key: 'requests@{COMMIT}:src/requests/candidates.py:"
            f"src.requests.candidates.candidate_{index}:{index + 1}', "
            "name: 'candidate', "
            f"qname: 'src.requests.candidates.candidate_{index}', "
            "signature: 'def candidate()', "
            f"start_line: {index + 1}, end_line: {index + 1}, cyclomatic: 1"
            "}"
        )
        for index in range(7)
    )
    cypher = f"""
    CREATE (r:Repository {{
        repo_name: '{REPOSITORY}',
        commit: '{COMMIT}',
        root_path: '/repos/{REPOSITORY}'
    }})
    CREATE (sessions:File {{
        key: '{REPOSITORY}@{COMMIT}:src/requests/sessions.py',
        path: '/repos/{REPOSITORY}/src/requests/sessions.py'
    }})
    CREATE (candidates:File {{
        key: '{REPOSITORY}@{COMMIT}:src/requests/candidates.py',
        path: '/repos/{REPOSITORY}/src/requests/candidates.py'
    }})
    CREATE (caller:Method {{
        key: '{CALLER_ID}',
        name: 'request',
        qname: 'src.requests.sessions.Session.request',
        signature: '{REQUEST_SIGNATURE}',
        start_line: 557,
        end_line: 653,
        cyclomatic: 4
    }})
    CREATE (callee:Method {{
        key: '{CALLEE_ID}',
        name: 'prepare_request',
        qname: 'src.requests.sessions.Session.prepare_request',
        signature: 'def prepare_request(self, request)',
        start_line: 511,
        end_line: 555,
        cyclomatic: 2
    }})
    CREATE (site:CallSite {{
        key: '{REPOSITORY}@{COMMIT}:src/requests/sessions.py:call:600:0'
    }})
    CREATE (r)-[:CONTAINS]->(sessions)
    CREATE (r)-[:CONTAINS]->(candidates)
    CREATE (sessions)-[:CONTAINS]->(caller)
    CREATE (sessions)-[:CONTAINS]->(callee)
    CREATE (caller)-[:HAS_CALLSITE]->(site)
    CREATE (site)-[:RESOLVES_TO {{strategy: 'self_direct'}}]->(callee)
    CREATE (caller)-[:EXACT_CALLS]->(callee)
    WITH r, candidates
    UNWIND [{candidate_rows}] AS row
    CREATE (candidate:Function)
    SET candidate = row
    CREATE (candidates)-[:CONTAINS]->(candidate);
    CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh();
    """
    username = env.get("NEO4J_USERNAME", "neo4j")
    password = env.get("NEO4J_PASSWORD", "change-me-123")
    seeded = _run_compose(
        command,
        "exec",
        "-T",
        "neo4j",
        "cypher-shell",
        "-u",
        username,
        "-p",
        password,
        env=env,
        input_text=cypher,
    )
    assert seeded.returncode == 0, seeded.stderr


def _compact_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


def _wire_bytes(result: Any) -> int:
    structured_content = result.structuredContent
    return _compact_bytes(
        {
            "content": [
                content.model_dump(by_alias=True, exclude_none=True) for content in result.content
            ],
            "structuredContent": structured_content,
            "isError": result.isError,
        }
    )


def _assert_compact_result(
    result: Any,
    *,
    expected_rows: int,
    forbidden_text: tuple[str, ...] = (),
) -> tuple[int, int, int]:
    assert result.isError is False
    assert result.structuredContent is not None
    rows = result.structuredContent["result"]
    assert len(rows) == expected_rows
    assert len(result.content) == 1
    text = result.content[0].text
    assert not text.lstrip().startswith(("[", "{"))
    assert text != json.dumps(rows, separators=(",", ":"), default=str)
    for value in forbidden_text:
        assert value not in text
    text_bytes = len(text.encode())
    structured_bytes = _compact_bytes(result.structuredContent)
    wire_bytes = _wire_bytes(result)
    return text_bytes, structured_bytes, wire_bytes


def _legacy_duplicated_wire_bytes(result: Any) -> int:
    assert result.structuredContent is not None
    rows = result.structuredContent["result"]
    return _compact_bytes(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(rows, separators=(",", ":"), default=str),
                }
            ],
            "structuredContent": result.structuredContent,
            "isError": result.isError,
        }
    )


def _assert_relative_public_paths(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"file", "root_path"} and nested is not None:
                path = str(nested)
                assert not path.startswith("/")
                assert "\\" not in path
                assert "/repos/" not in path
            _assert_relative_public_paths(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_relative_public_paths(nested)


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
async def test_http_mcp_transport_supports_protocol_client_session(
    record_property: Any,
) -> None:
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
    env["CODEKG_NEO4J_DATA_VOLUME"] = f"{project}-neo4j-data"
    env["CODEKG_NEO4J_LOGS_VOLUME"] = f"{project}-neo4j-logs"
    env["CODEKG_ZVEC_DATA_VOLUME"] = f"{project}-zvec-data"
    env["CODEKG_BULK_STAGING_VOLUME"] = f"{project}-bulk-staging"
    command = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    try:
        built = _run_compose(command, "build", "app-image-build", env=env)
        if built.returncode != 0:
            pytest.skip(f"Required Compose image could not be built: {built.stderr.strip()}")
        started = _run_compose(command, "up", "-d", "mcp", env=env)
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
        assert {tool.name for tool in tools} == WRAPPED_LIST_TOOLS | {"search_symbols"}
        for tool in tools:
            if tool.name in WRAPPED_LIST_TOOLS:
                assert tool.outputSchema == WRAPPED_LIST_OUTPUT_SCHEMA
            else:
                assert tool.outputSchema is None

        search_tool = next(tool for tool in tools if tool.name == "search_symbols")
        assert search_tool.inputSchema["properties"]["scope"]["enum"] == [
            "source",
            "tests",
            "docs",
            "examples",
            "benchmarks",
            "all",
        ]
        _seed_transport_graph(command, env)

        async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp") as client:
            repositories = await client.call_tool_mcp("list_repositories", {})
            repository_sizes = _assert_compact_result(
                repositories,
                expected_rows=1,
                forbidden_text=("/repos/requests", COMMIT),
            )
            repository_rows = repositories.structuredContent["result"]
            assert all(
                set(row) == {"repo_name", "commit", "root_path", "files"} for row in repository_rows
            )
            repository_row = next(row for row in repository_rows if row["repo_name"] == REPOSITORY)
            assert repository_row["repo_name"] == REPOSITORY
            assert repository_row["commit"] == COMMIT
            assert repository_row["root_path"] == "."
            assert repositories.content[0].text == "Found 1 indexed repository."

            first_page = await client.call_tool_mcp(
                "search_symbols",
                {
                    "repository": REPOSITORY,
                    "query": "candidate",
                    "mode": "graph",
                    "limit": 2,
                },
            )
            assert first_page.isError is False
            first_structured = first_page.structuredContent
            assert first_structured is not None
            assert first_structured["status"] == "ok"
            assert first_structured["repository"] == REPOSITORY
            assert first_structured["commit"] == COMMIT
            assert first_structured["scope"] == "source"
            assert "diagnostics" not in first_structured
            assert len(first_structured["results"]) == 2
            assert set(first_structured["results"][0]) == {
                "symbol_id",
                "qualified_name",
                "file",
                "start_line",
                "end_line",
                "score",
                "matched_terms",
                "match_type",
            }
            assert first_structured["results"][0]["symbol_id"] == (
                f"{REPOSITORY}@{COMMIT}:src/requests/candidates.py:"
                "src.requests.candidates.candidate_0:1"
            )
            cursor = first_structured["next_cursor"]
            assert isinstance(cursor, str)
            assert not cursor.startswith("cur_")
            decoded_cursor = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            assert decoded_cursor == {
                "v": 2,
                "r": REPOSITORY,
                "c": COMMIT,
                "q": "candidate",
                "a": ["candidate"],
                "m": "graph",
                "k": None,
                "s": "source",
                "o": 2,
            }
            assert first_page.content[0].text == (
                "Found 2 symbol candidate(s) in repository=requests. "
                "More results are available. See structured result."
            )

            second_page = await client.call_tool_mcp(
                "search_symbols",
                {
                    "repository": REPOSITORY,
                    "query": "candidate",
                    "mode": "graph",
                    "limit": 2,
                    "cursor": cursor,
                },
            )
            assert second_page.isError is False
            second_structured = second_page.structuredContent
            assert second_structured is not None
            first_ids = {row["symbol_id"] for row in first_structured["results"]}
            second_ids = {row["symbol_id"] for row in second_structured["results"]}
            assert first_ids.isdisjoint(second_ids)

            definition = await client.call_tool_mcp("get_definition", {"identifier": CALLEE_ID})
            definition_sizes = _assert_compact_result(
                definition,
                expected_rows=1,
                forbidden_text=(CALLEE_ID, "prepare_request", "/repos/requests"),
            )
            definition_row = definition.structuredContent["result"][0]
            assert set(definition_row) == {
                "key",
                "labels",
                "name",
                "qname",
                "signature",
                "start_line",
                "end_line",
                "cyclomatic",
                "file",
                "repo",
                "commit",
            }
            assert definition_row["key"] == CALLEE_ID
            assert definition_row["file"] == "src/requests/sessions.py"

            callers = await client.call_tool_mcp("find_callers", {"identifier": CALLEE_ID})
            caller_sizes = _assert_compact_result(
                callers,
                expected_rows=1,
                forbidden_text=(CALLER_ID, "Session.request"),
            )
            caller_row = callers.structuredContent["result"][0]
            assert set(caller_row) == {
                "key",
                "qname",
                "signature",
                "file",
                "start_line",
                "end_line",
                "depth",
                "resolution",
            }
            assert caller_row["key"] == CALLER_ID
            assert caller_row["signature"] == REQUEST_SIGNATURE
            assert caller_row["file"] == "src/requests/sessions.py"
            assert (caller_row["start_line"], caller_row["end_line"]) == (557, 653)
            assert caller_row["resolution"] == "self_direct"

            callees = await client.call_tool_mcp("find_callees", {"identifier": CALLER_ID})
            callee_sizes = _assert_compact_result(
                callees,
                expected_rows=1,
                forbidden_text=(CALLEE_ID, "Session.prepare_request"),
            )
            callee_row = callees.structuredContent["result"][0]
            assert set(callee_row) == {
                "key",
                "qname",
                "signature",
                "file",
                "start_line",
                "end_line",
                "depth",
                "resolution",
            }
            assert callee_row["key"] == CALLEE_ID
            assert callee_row["signature"] == "def prepare_request(self, request)"
            assert callee_row["file"] == "src/requests/sessions.py"
            assert (callee_row["start_line"], callee_row["end_line"]) == (511, 555)
            assert callee_row["resolution"] == "self_direct"

            # Exercise FastMCP's parsed convenience path as well as raw MCP results.
            parsed = await client.call_tool("get_definition", {"identifier": CALLEE_ID})
            assert parsed.is_error is False
            assert parsed.structured_content == definition.structuredContent

        for result in (repositories, first_page, second_page, definition, callers, callees):
            assert result.structuredContent is not None
            _assert_relative_public_paths(result.structuredContent)

        # Record deterministic transport-size dimensions for CI/JUnit logs. The
        # standalone evaluator compares persisted before/after captures.
        size_rows = [
            repository_sizes,
            definition_sizes,
            caller_sizes,
            callee_sizes,
        ]
        text_bytes = sum(row[0] for row in size_rows)
        structured_bytes = sum(row[1] for row in size_rows)
        wire_bytes = sum(row[2] for row in size_rows)
        assert text_bytes < structured_bytes
        assert wire_bytes > structured_bytes
        converted_results = [repositories, definition, callers, callees]
        legacy_wire_bytes = sum(
            _legacy_duplicated_wire_bytes(result) for result in converted_results
        )
        search_wire_bytes = _wire_bytes(first_page)
        after_chain_wire_bytes = wire_bytes + search_wire_bytes
        before_chain_wire_bytes = legacy_wire_bytes + search_wire_bytes
        legacy_text_bytes = 0
        for result in converted_results:
            rows = result.structuredContent["result"]
            legacy_text_bytes += len(json.dumps(rows, separators=(",", ":"), default=str).encode())
        text_reduction = 1 - (text_bytes / legacy_text_bytes)
        assert text_reduction >= 0.80
        assert 1 - (after_chain_wire_bytes / before_chain_wire_bytes) >= 0.25
        record_property(
            "mcp_response_sizes",
            json.dumps(
                {
                    "converted_tools": [
                        "list_repositories",
                        "get_definition",
                        "find_callers",
                        "find_callees",
                    ],
                    "after": {
                        "text_bytes": text_bytes,
                        "structured_bytes": structured_bytes,
                        "wire_bytes": wire_bytes,
                    },
                    "before_legacy_wire_bytes": legacy_wire_bytes,
                    "requests_chain": {
                        "before_wire_bytes": before_chain_wire_bytes,
                        "after_wire_bytes": after_chain_wire_bytes,
                    },
                    "aggregate_text_reduction_percent": round(text_reduction * 100, 2),
                    "wire_reduction_percent": round(
                        (1 - (after_chain_wire_bytes / before_chain_wire_bytes)) * 100,
                        2,
                    ),
                },
                separators=(",", ":"),
            ),
        )
    finally:
        _run_compose(command, "down", "-v", "--remove-orphans", env=env)
