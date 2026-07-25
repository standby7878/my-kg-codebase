from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

COMPOSE_FILE = Path(__file__).parents[2] / "compose" / "dev-local" / "docker-compose.yml"


def _docker_compose_available() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("Docker Compose is unavailable")


def _run_compose(project: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def isolated_project() -> tuple[str, dict[str, str]]:
    _docker_compose_available()
    project = f"codekg-isolation-{uuid.uuid4().hex[:12]}"
    env = os.environ.copy()
    env["NEO4J_HTTP_PORT"] = str(17000 + (uuid.uuid4().int % 1000))
    env["NEO4J_BOLT_PORT"] = str(18000 + (uuid.uuid4().int % 1000))
    env["MCP_PORT"] = str(19000 + (uuid.uuid4().int % 1000))
    yield project, env
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(COMPOSE_FILE),
            "down",
            "-v",
            "--remove-orphans",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_service_network_topology_and_loopback_published_ports(
    isolated_project: tuple[str, dict[str, str]],
) -> None:
    project, env = isolated_project
    config = _run_compose(project, "config", "--format", "json", env=env)
    if config.returncode != 0:
        pytest.skip(f"Compose configuration unavailable: {config.stderr.strip()}")
    model = json.loads(config.stdout)

    services = model["services"]
    # Neo4j and MCP publish only loopback-bound ports and therefore attach to
    # the frontend network as well as the internal backend. Ingestion remains
    # reachable only through the backend.
    assert set(services["neo4j"]["networks"]) == {"backend", "frontend"}
    assert set(services["ingestion"]["networks"]) == {"backend"}
    assert set(services["mcp"]["networks"]) == {"backend", "frontend"}
    assert services["neo4j"]["ports"]
    assert services["mcp"]["ports"]
    assert all(port.get("host_ip") == "127.0.0.1" for port in services["neo4j"]["ports"])
    assert all(port.get("host_ip") == "127.0.0.1" for port in services["mcp"]["ports"])

    started = _run_compose(project, "up", "-d", "neo4j", "mcp", env=env)
    if started.returncode != 0:
        pytest.skip(f"Required Compose image or service unavailable: {started.stderr.strip()}")

    network = subprocess.run(
        ["docker", "network", "inspect", f"{project}_backend"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert network.returncode == 0, network.stderr
    network_data = json.loads(network.stdout)[0]
    # Docker's Internal flag removes the default external route. This is a
    # topology proof; it does not prove that every host-level escape path is
    # impossible if the Docker daemon or host networking is compromised.
    assert network_data["Internal"] is True
    assert network_data["Options"].get("com.docker.network.bridge.enable_ip_masquerade") != "true"


def test_dynamic_ingestion_source_and_mcp_mounts_preserve_isolation(
    isolated_project: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    project, env = isolated_project
    source = tmp_path / "source"
    source.mkdir()
    container = f"{project}-ingestion-mount"
    try:
        run = _run_compose(
            project,
            "run",
            "--no-deps",
            "-d",
            "--name",
            container,
            "-v",
            f"{source}:/repos/source:ro",
            "--entrypoint",
            "sleep",
            "ingestion",
            "60",
            env=env,
        )
        if run.returncode != 0:
            pytest.skip(f"Required ingestion image unavailable: {run.stderr.strip()}")

        inspect = subprocess.run(
            ["docker", "inspect", container], capture_output=True, text=True, check=False, env=env
        )
        assert inspect.returncode == 0, inspect.stderr
        mounts = json.loads(inspect.stdout)[0]["Mounts"]
        source_mount = next(mount for mount in mounts if mount["Destination"] == "/repos/source")
        assert source_mount["RW"] is False
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False, env=env)

    config = _run_compose(project, "config", "--format", "json", env=env)
    assert config.returncode == 0, config.stderr
    mcp_service = json.loads(config.stdout)["services"]["mcp"]
    mcp_mounts = mcp_service["volumes"]
    assert {mount["target"] for mount in mcp_mounts} == {"/data/zvec"}
    assert not any(mount["target"].startswith("/repos") for mount in mcp_mounts)
    # zvec 0.5.1 opens /data/zvec/codekg/LOCK even when its collection API is
    # read-only, so this data mount must remain writable.
    assert mcp_mounts[0].get("read_only", False) is False
    assert mcp_service["read_only"] is True


def test_mcp_opens_seeded_zvec_collection_read_only_with_production_mount(
    isolated_project: tuple[str, dict[str, str]],
) -> None:
    """Exercise zvec's reader in the MCP service's actual Compose sandbox."""

    project, env = isolated_project
    env = env.copy()
    # Keep this runtime fixture separate from the developer's persistent local index.
    env["CODEKG_ZVEC_DATA_VOLUME"] = f"{project}-zvec"
    seed = """
from codekg.zvec_store import SymbolDoc, open_write, optimize_and_flush, upsert_symbol_docs

collection = open_write()
upsert_symbol_docs(collection, [SymbolDoc(
    key="runtime:read-only",
    repo="runtime",
    commit="test",
    path="runtime.py",
    qname="runtime_reader",
    kind="function",
    signature="()",
    start_line=1,
    end_line=1,
    text="runtime read only collection",
)])
optimize_and_flush(collection)
"""
    try:
        seeded = _run_compose(
            project,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "ingestion",
            "-c",
            seed,
            env=env,
        )
        if seeded.returncode != 0:
            pytest.skip(
                f"Required Compose image or zvec runtime unavailable: {seeded.stderr.strip()}"
            )

        read = """
from codekg.zvec_store import fetch_symbol_docs, open_read

collection = open_read()
docs = fetch_symbol_docs(collection, {"runtime:read-only"})
assert docs["runtime:read-only"]["key"] == "runtime:read-only"
"""
        opened = _run_compose(
            project,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "mcp",
            "-c",
            read,
            env=env,
        )
        assert opened.returncode == 0, opened.stderr
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(COMPOSE_FILE),
                "down",
                "-v",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
