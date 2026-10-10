from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SCRIPT = Path(__file__).parents[2] / "run-compose.sh"
COMPOSE_FILE = SCRIPT.parent / "compose" / "dev-local" / "docker-compose.yml"


def run_index_sources(
    tmp_path: Path,
    repos_root: str | None = None,
    *,
    mode: str = "transactional",
    fail_validation: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf \'%s\\0\' "$@" >> "$DOCKER_LOG"\nprintf \'\\n\' >> "$DOCKER_LOG"\n'
        'if [ "$FAIL_VALIDATION" = 1 ]; then\n'
        '    for argument in "$@"; do\n'
        '        if [ "$argument" = validate-bulk-index ]; then\n'
        "            exit 23\n"
        "        fi\n"
        "    done\n"
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_LOG"] = str(docker_log)
    environment["CODEKG_INGEST_MODE"] = mode
    environment["CODEKG_RUNTIME_ENV_FILE"] = str(tmp_path / "runtime.env")
    environment["FAIL_VALIDATION"] = "1" if fail_validation else "0"
    if repos_root is None:
        environment.pop("CODEKG_REPOS_ROOT", None)
    else:
        environment["CODEKG_REPOS_ROOT"] = repos_root
    result = subprocess.run(
        ["bash", str(SCRIPT), "dev-local", "index-sources"],
        cwd=SCRIPT.parent,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, docker_log


def docker_invocations(log: Path) -> list[list[str]]:
    return [
        [arg.decode() for arg in line.split(b"\0") if arg] for line in log.read_bytes().splitlines()
    ]


def test_index_sources_mounts_and_indexes_each_repository(tmp_path: Path) -> None:
    first = tmp_path / "first"
    spaced = tmp_path / "repo with spaces"
    first.mkdir()
    spaced.mkdir()

    result, log = run_index_sources(tmp_path, f"{first};{spaced}")

    assert result.returncode == 0
    invocations = docker_invocations(log)
    assert len(invocations) == 2
    assert f"{first.resolve()}:/repos/first:ro" in invocations[0]
    assert invocations[0][-2:] == ["reindex", "/repos/first"]
    assert f"{spaced.resolve()}:/repos/repo with spaces:ro" in invocations[1]
    assert invocations[1][-2:] == ["reindex", "/repos/repo with spaces"]


def test_index_sources_defaults_to_staged_bulk_publish(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    result, log = run_index_sources(tmp_path, str(repository), mode="auto")

    assert result.returncode == 0
    invocations = docker_invocations(log)
    assert any("bulk-exporter" in invocation for invocation in invocations)
    assert any("bulk-importer" in invocation for invocation in invocations)
    zvec = next(invocation for invocation in invocations if "bulk-zvec" in invocation)
    validation = next(
        invocation for invocation in invocations if "validate-bulk-index" in invocation
    )
    assert zvec[-2:] == ["bulk-zvec", "/data/bulk/manifest.json"]
    assert validation[-2:] == ["validate-bulk-index", "/data/bulk/manifest.json"]
    assert not any(":/repos/" in argument for argument in (*zvec, *validation))
    assert (tmp_path / "runtime.env").is_file()
    for phase in ("export", "zvec", "import", "publish", "validation", "activation"):
        assert f"CODEKG_PHASE_START {phase}" in result.stdout
        assert f"CODEKG_PHASE_END {phase}" in result.stdout
    assert (
        result.stdout.index("CODEKG_PHASE_START publish")
        < result.stdout.index("CODEKG_PHASE_END publish")
        < result.stdout.index("CODEKG_PHASE_START validation")
    )
    assert (
        result.stdout.index("CODEKG_PHASE_END validation")
        < result.stdout.index("CODEKG_PHASE_START activation")
        < result.stdout.index("CODEKG_PHASE_END activation")
    )
    assert invocations[-1][-3:] == ["up", "-d", "mcp"]


def test_bulk_validation_failure_restores_runtime_env_and_restarts_services(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_env = tmp_path / "runtime.env"
    original_runtime_env = "# existing runtime configuration\nCODEKG_ZVEC_DATA_VOLUME=old-zvec"
    runtime_env.write_text(original_runtime_env, encoding="utf-8")

    result, log = run_index_sources(
        tmp_path,
        str(repository),
        mode="bulk",
        fail_validation=True,
    )

    assert result.returncode != 0
    assert runtime_env.read_text(encoding="utf-8") == original_runtime_env
    assert any(
        invocation[-5:] == ["up", "-d", "neo4j", "schema_bootstrap", "mcp"]
        for invocation in docker_invocations(log)
    )


def test_exported_repos_root_overrides_profile_config(tmp_path: Path) -> None:
    exported = tmp_path / "exported"
    exported.mkdir()

    result, log = run_index_sources(tmp_path, str(exported))

    assert result.returncode == 0
    invocations = docker_invocations(log)
    assert len(invocations) == 1
    assert f"{exported.resolve()}:/repos/exported:ro" in invocations[0]


@pytest.mark.parametrize("value", [";", "{0};{0}"])
def test_invalid_or_duplicate_repositories_fail_before_docker(tmp_path: Path, value: str) -> None:
    repository = tmp_path / "same"
    repository.mkdir()
    value = value.format(repository)

    result, log = run_index_sources(tmp_path, value)

    assert result.returncode != 0
    assert not log.exists() or log.read_bytes() == b""


def test_duplicate_checkout_basenames_fail_before_docker(tmp_path: Path) -> None:
    first = tmp_path / "one" / "checkout"
    second = tmp_path / "two" / "checkout"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    result, log = run_index_sources(tmp_path, f"{first};{second}")

    assert result.returncode != 0
    assert not log.exists() or log.read_bytes() == b""


def test_mcp_has_frontend_network_for_loopback_port_publishing() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    services_block = compose.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    service_blocks = {
        service: block
        for service, block in re.findall(
            r"(?ms)^  ([a-z0-9_-]+):\n(.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
            services_block,
        )
    }

    def service_networks(block: str) -> set[str]:
        networks = re.search(
            r"(?ms)^    networks:\n(?P<networks>.*?)(?=^    \S|\Z)",
            block,
        )
        if networks is None:
            return set()
        return {
            network
            for pair in re.findall(
                r"^      - ([a-z0-9-]+)$|^      ([a-z0-9-]+):$", networks["networks"], re.M
            )
            for network in pair
            if network
        }

    service_network_membership = {
        service: service_networks(block) for service, block in service_blocks.items()
    }
    mcp_block = service_blocks["mcp"]
    networks_block = compose.split("\nnetworks:\n", 1)[1]

    assert service_network_membership["mcp"] == {"backend", "frontend"}
    assert service_network_membership["neo4j"] == {"backend", "frontend"}
    assert all(
        "frontend" not in networks
        for service, networks in service_network_membership.items()
        if service not in {"mcp", "neo4j"}
    )
    assert '      - "127.0.0.1:${MCP_PORT:-8765}:${MCP_PORT:-8765}"' in mcp_block
    assert "      - zvec_data:/data/zvec\n" in mcp_block
    assert "zvec_data:/data/zvec:ro" not in mcp_block
    assert "  backend:\n    driver: bridge\n    internal: true\n" in networks_block
    assert "  frontend:\n    driver: bridge\n" in networks_block
    frontend_network = re.search(r"(?ms)^  frontend:\n(?P<config>.*?)(?=^  \S|\Z)", networks_block)
    assert frontend_network is not None
    assert "    internal:" not in frontend_network["config"]


def test_compose_propagates_auth_mode_and_healthcheck_branches_without_auth(
    tmp_path: Path,
) -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    services_block = compose.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    python_services = ("schema_bootstrap", "ingestion", "bulk-exporter", "mcp")

    for service in python_services:
        block = re.search(
            rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
            services_block,
        )
        assert block is not None
        assert "NEO4J_AUTH: ${NEO4J_AUTH:-neo4j/change-me-123}" in block[1]
        assert "NEO4J_PASSWORD: ${NEO4J_PASSWORD:-change-me-123}" in block[1]

    healthcheck = re.search(r'(?m)^\s+("if \[.*\")\s*,?$', compose)
    assert healthcheck is not None
    shell_command = json.loads(healthcheck[1]).replace("$$", "$")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    cypher_shell = fake_bin / "cypher-shell"
    cypher_shell.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CYPHER_ARGS"\n', encoding="utf-8")
    cypher_shell.chmod(0o755)
    args_file = tmp_path / "cypher-args"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "CYPHER_ARGS": str(args_file),
            "NEO4J_AUTH": "none",
        }
    )
    no_auth = subprocess.run(["sh", "-c", shell_command], env=environment, check=False)
    assert no_auth.returncode == 0
    assert args_file.read_text(encoding="utf-8").splitlines() == ["RETURN 1"]

    environment.update(
        {
            "NEO4J_AUTH": "neo4j/test-password",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "test-password",
        }
    )
    authenticated = subprocess.run(["sh", "-c", shell_command], env=environment, check=False)
    assert authenticated.returncode == 0
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "-u",
        "neo4j",
        "-p",
        "test-password",
        "RETURN 1",
    ]
