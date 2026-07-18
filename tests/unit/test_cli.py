from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codekg.cli import app

pytestmark = pytest.mark.unit


def test_index_command_replaces_existing_snapshot(monkeypatch) -> None:
    calls: list[tuple[Path, bool]] = []

    def fake_index_repository(path: Path, *, replace: bool) -> dict[str, object]:
        calls.append((path, replace))
        return {"repo_name": path.name, "commit": "abc123", "files": 0, "nodes": 0}

    monkeypatch.setattr("codekg.ingest.index_repository", fake_index_repository)

    result = CliRunner().invoke(app, ["index", "sample-repo"])

    assert result.exit_code == 0
    assert calls == [(Path("sample-repo"), True)]


def test_index_search_command_uses_configured_zvec_path(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_rebuild_repo_search_index(
        *, repo: str | None, zvec_path: str | None
    ) -> dict[str, int]:
        calls.append({"repo": repo, "zvec_path": zvec_path})
        return {"symbols": 1, "repositories": 1}

    monkeypatch.setattr(
        "codekg.search_index.rebuild_repo_search_index", fake_rebuild_repo_search_index
    )

    result = CliRunner().invoke(
        app, ["index-search", "--repo", "sample", "--zvec-path", "/tmp/zvec"]
    )

    assert result.exit_code == 0
    assert calls == [{"repo": "sample", "zvec_path": "/tmp/zvec"}]
