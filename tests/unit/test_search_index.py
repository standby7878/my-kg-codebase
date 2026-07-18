from __future__ import annotations

from pathlib import Path

import pytest

from codekg.docs import DocChunk
from codekg.search_index import (
    build_symbol_text,
    doc_chunk_doc_from_chunk,
    doc_chunk_docs_from_repo,
    normalize_name,
    rebuild_repo_search_index,
    validate_search_index_consistency,
)
from codekg.zvec_store import DocChunkDoc

pytestmark = pytest.mark.unit


def test_normalize_name_splits_snake_and_camel_case() -> None:
    assert normalize_name("pkg.mod.do_failoverDecision") == "do failover decision"


def test_build_symbol_text_includes_docstring_and_comments(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "worker.py"
    source.write_text(
        "\n".join(
            [
                "# Promote the best standby during failover.",
                "def choose_standby(nodes):",
                '    """Choose the candidate with newest WAL."""',
                "    return nodes[0]",
            ]
        ),
        encoding="utf-8",
    )

    text = build_symbol_text(
        {
            "root_path": str(repo),
            "path": "worker.py",
            "qname": "worker.choose_standby",
            "name": "choose_standby",
            "signature": "def choose_standby(nodes)",
            "start_line": 2,
            "end_line": 4,
        }
    )

    assert "choose standby" in text
    assert "newest WAL" in text
    assert "best standby" in text


def test_validate_search_index_consistency_reports_missing_and_orphaned(monkeypatch) -> None:
    class FakeClient:
        def execute_read(self, *args, **kwargs):
            return [{"key": "graph-only"}, {"key": "shared"}]

    monkeypatch.setattr("codekg.search_index.open_read", lambda path: object())
    monkeypatch.setattr(
        "codekg.search_index.list_symbol_ids", lambda collection, repo=None: {"shared", "zvec-only"}
    )
    monkeypatch.setattr(
        "codekg.search_index.iter_doc_chunk_rows", lambda repo=None, client=None: []
    )
    monkeypatch.setattr("codekg.search_index.list_doc_ids", lambda collection, repo=None: set())

    result = validate_search_index_consistency(client=FakeClient())  # type: ignore[arg-type]

    assert result == {
        "ok": False,
        "graph_symbols": 2,
        "zvec_symbols": 2,
        "graph_docs": 0,
        "zvec_docs": 0,
        "missing_in_zvec": ["graph-only"],
        "orphaned_in_zvec": ["zvec-only"],
        "missing_docs_in_zvec": [],
        "orphaned_docs_in_zvec": [],
    }


def test_doc_chunk_doc_uses_repo_commit_anchor(tmp_path: Path) -> None:
    chunk = DocChunk(
        path=(tmp_path / "docs" / "failover.rst").as_posix(),
        heading_path="Failover > Promotion",
        start_line=12,
        end_line=30,
        text="Promotion explains standby behavior.",
        mentions=("patroni.ha.Ha",),
    )

    doc = doc_chunk_doc_from_chunk(chunk, root=tmp_path, repo="patroni", commit="abc")

    assert doc.id == "patroni@abc:doc:docs/failover.rst:12"
    assert doc.source == "doc"
    assert doc.path == "docs/failover.rst"
    assert "patroni.ha.Ha" in doc.text


def test_doc_chunk_docs_from_repo_uses_ingest_doc_selection(tmp_path: Path) -> None:
    (tmp_path / "README.MD").write_text("# Usage\nCall `pkg.run`.\n", encoding="utf-8")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "ignored.md").write_text("# Ignored\nCall `pkg.ignored`.\n", encoding="utf-8")

    docs = doc_chunk_docs_from_repo({"root_path": str(tmp_path), "repo": "sample", "commit": "abc"})

    assert [doc.path for doc in docs] == ["README.MD"]
    assert docs[0].id == "sample@abc:doc:README.MD:1"
    assert "pkg.run" in docs[0].text


def test_rebuild_search_index_deletes_only_stale_ids_after_upsert(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    live_doc = DocChunkDoc(
        id="live-doc",
        source="doc",
        repo="sample",
        commit="abc",
        path="README.md",
        qname="Usage",
        kind="doc",
        signature="Usage",
        start_line=1,
        end_line=2,
        text="Usage",
    )

    monkeypatch.setattr("codekg.search_index.open_write", lambda path: object())
    monkeypatch.setattr(
        "codekg.search_index.iter_symbol_rows",
        lambda repo=None, client=None: [
            {
                "key": "live-symbol",
                "repo": "sample",
                "commit": "abc",
                "path": "worker.py",
                "qname": "worker.run",
                "kind": "function",
                "name": "run",
                "signature": "def run()",
            }
        ],
    )
    monkeypatch.setattr(
        "codekg.search_index.iter_repository_rows",
        lambda repo=None, client=None: [
            {"repo": "sample", "commit": "abc", "root_path": "/missing"}
        ],
    )
    monkeypatch.setattr(
        "codekg.search_index.doc_chunk_docs_from_repo",
        lambda row: [live_doc],
    )
    monkeypatch.setattr(
        "codekg.search_index.list_symbol_ids",
        lambda collection, repo=None: {"old-symbol", "live-symbol"},
    )
    monkeypatch.setattr(
        "codekg.search_index.list_doc_ids",
        lambda collection, repo=None: {"old-doc", "live-doc"},
    )
    monkeypatch.setattr(
        "codekg.search_index.upsert_symbol_docs",
        lambda collection, docs: calls.append(("symbols", [doc.id for doc in docs])) or len(docs),
    )
    monkeypatch.setattr(
        "codekg.search_index.upsert_doc_chunks",
        lambda collection, docs: calls.append(("docs", [doc.id for doc in docs])) or len(docs),
    )
    monkeypatch.setattr(
        "codekg.search_index.delete_ids",
        lambda collection, ids: calls.append(("delete", ids)),
    )

    result = rebuild_repo_search_index(repo="sample", client=object())  # type: ignore[arg-type]

    assert result == {"symbols": 1, "docs": 1, "repositories": 1}
    assert calls == [
        ("symbols", ["live-symbol"]),
        ("docs", ["live-doc"]),
        ("delete", {"old-symbol", "old-doc"}),
    ]
