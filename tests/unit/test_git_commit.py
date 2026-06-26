from __future__ import annotations

from pathlib import Path

import pytest

from codekg.ingest import scan_repository

pytestmark = pytest.mark.unit


def test_scan_repository_reads_git_head_without_git_binary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git_refs = repo / ".git" / "refs" / "heads"
    git_refs.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_refs / "main").write_text(
        "1234567890abcdef1234567890abcdef12345678\n",
        encoding="utf-8",
    )
    (repo / "module.py").write_text("def fn():\n    return 1\n", encoding="utf-8")

    scanned = scan_repository(repo)

    assert scanned.commit == "1234567890ab"
