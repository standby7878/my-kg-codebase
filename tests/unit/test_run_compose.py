from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SCRIPT = Path(__file__).parents[2] / "run-compose.sh"


def run_index_sources(
    tmp_path: Path, repos_root: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf \'%s\\0\' "$@" >> "$DOCKER_LOG"\nprintf \'\\n\' >> "$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_LOG"] = str(docker_log)
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
