"""Non-Docker contract tests for the small rebuild shell entry point."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "rebuild-kg.sh"


def _git_repo(path: Path) -> None:
    path.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(("git", "-C", str(path), *command), check=True)
    (path / "source.py").write_text("def f(): pass\n")
    subprocess.run(("git", "-C", str(path), "add", "source.py"), check=True)
    subprocess.run(("git", "-C", str(path), "commit", "-qm", "initial"), check=True)


def _fixture(tmp: Path) -> tuple[dict[str, str], list[str], Path]:
    app, pg, extension = (tmp / name for name in ("app", "pg", "extension"))
    for source in (app, pg, extension):
        _git_repo(source)
    for ref in ("REL_18_0", "REL_19_BETA4"):
        subprocess.run(("git", "-C", str(pg), "tag", ref), check=True)
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n')
    docker.chmod(0o755)
    log = tmp / "docker.log"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", DOCKER_LOG=str(log), TMPDIR=str(tmp))
    args = [
        "--application", "app", str(app), "WORKTREE",
        "--postgres", "pg18", str(pg), "REL_18_0",
        "--postgres", "pg19", str(pg), "REL_19_BETA4",
        "--extension", "cron", str(extension), "WORKTREE",
    ]
    return env, args, log


def _run(env: dict[str, str], args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("bash", str(SCRIPT), *args), env=env, text=True, capture_output=True)


def test_dry_run_has_no_docker_calls_or_worktrees() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env, args, log = _fixture(tmp)
        result = _run(env, [*args, "--dry-run"])
        assert result.returncode == 0, result.stderr
        assert not log.exists()
        assert not list(tmp.glob("codekg-rebuild.*"))


def test_invalid_ref_does_not_delete_existing_stack() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env, args, log = _fixture(tmp)
        args[7] = "NO_SUCH_REF"  # first PostgreSQL ref
        result = _run(env, [*args, "--yes"])
        assert result.returncode != 0
        assert "unknown local ref" in result.stderr
        assert not log.exists()


def test_rebuild_sequence_and_multi_pg_manifest() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        env, args, log = _fixture(tmp)
        second_extension = tmp / "postgis"
        _git_repo(second_extension)
        args.extend(("--extension", "postgis", str(second_extension), "WORKTREE"))
        result = _run(env, [*args, "--skip-build", "--yes"])
        assert result.returncode == 0, result.stderr
        calls = log.read_text().splitlines()
        expected = (
            "down --volumes --remove-orphans",
            "bulk-export-corpus /inputs/corpus.toml /data/bulk",
            "bulk-zvec /data/bulk/manifest.json",
            "bulk-importer",
            "up -d --wait neo4j",
            "schema_bootstrap",
            "up -d mcp",
        )
        assert len(calls) == len(expected)
        assert all(marker in call for marker, call in zip(expected, calls))
        match = re.search(r"Generated corpus config: (.+)", result.stdout)
        assert match is not None
        config = Path(match.group(1))
        assert config.parent.stat().st_mode & 0o777 == 0o755
        assert config.stat().st_mode & 0o777 == 0o644
        assert (config.parent / "worktrees").stat().st_mode & 0o777 == 0o755
        with config.open("rb") as stream:
            snapshots = tomllib.load(stream)["snapshots"]
        by_alias = {item["alias"]: item for item in snapshots}
        assert set(by_alias) == {
            "pg18", "pg19", "cron-pg18", "cron-pg19", "postgis-pg18",
            "postgis-pg19", "app-pg18", "app-pg19",
        }
        assert by_alias["app-pg18"]["dependencies"] == ["pg18", "cron-pg18", "postgis-pg18"]
        assert by_alias["app-pg19"]["dependencies"] == ["pg19", "cron-pg19", "postgis-pg19"]
        assert by_alias["postgis-pg18"]["dependencies"] == ["pg18"]
        assert by_alias["postgis-pg19"]["dependencies"] == ["pg19"]
        assert by_alias["pg18"]["version"].startswith("REL_18_0@")
        assert by_alias["pg18"]["logical_repo"] == by_alias["pg19"]["logical_repo"] == "postgres"
        assert by_alias["app-pg18"]["sql"]["enabled"] is True
        for alias in ("pg18", "pg19"):
            subprocess.run(
                ("git", "-C", str(tmp / "pg"), "worktree", "remove", "--force", str(config.parent / "worktrees" / alias)),
                check=True,
            )
