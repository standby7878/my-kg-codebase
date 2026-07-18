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


def test_delete_command_removes_zvec_records_before_graph(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_delete_repo_records(repo_name: str) -> None:
        calls.append(("zvec", repo_name))

    def fake_delete_repository_by_name(repo_name: str) -> int:
        calls.append(("graph", repo_name))
        return 3

    monkeypatch.setattr("codekg.zvec_store.delete_repo_records", fake_delete_repo_records)
    monkeypatch.setattr("codekg.loader.delete_repository_by_name", fake_delete_repository_by_name)

    result = CliRunner().invoke(app, ["delete", "sample"])

    assert result.exit_code == 0
    assert calls == [("zvec", "sample"), ("graph", "sample")]
