from __future__ import annotations

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codekg.ir import FileIR, RepositoryIR, SymbolIR
from codekg.search_index import (
    build_symbol_text,
    callable_docs_from_repository,
    normalize_name,
    validate_search_index_consistency,
)
from codekg.zvec_store import (
    SymbolDoc,
    delete_repo,
    doc_id_for_key,
    fetch_symbol_docs,
    open_write,
    optimize_and_flush,
    search_symbols,
    upsert_symbol_docs,
)

pytestmark = pytest.mark.unit


def _doc(*, key: str = "sample@abc:worker.py:worker.choose_standby:7") -> SymbolDoc:
    return SymbolDoc(
        key=key,
        repo="sample",
        commit="abc",
        path="worker.py",
        qname="worker.choose_standby",
        kind="function",
        signature="def choose_standby(nodes)",
        start_line=7,
        end_line=9,
        text="choose_standby\nchoose standby\nPromote the reliable standby to primary.",
    )


def test_normalize_name_splits_snake_and_camel_case() -> None:
    assert normalize_name("pkg.mod.do_failoverDecision") == "do failover decision"


def test_callable_docs_fold_docstring_and_markdown_into_callable_text() -> None:
    repo = RepositoryIR(
        repo_name="sample",
        commit="abc",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=9,
                module_qname="worker",
                symbols=(
                    SymbolIR(
                        kind="function",
                        name="<module>",
                        qname="worker.__module__",
                        signature="<module>",
                        start_line=1,
                        end_line=1,
                    ),
                    SymbolIR(
                        kind="function",
                        name="choose_standby",
                        qname="worker.choose_standby",
                        signature="def choose_standby(nodes)",
                        start_line=7,
                        end_line=9,
                        docstring="Select the replica with the newest WAL.",
                    ),
                ),
            ),
        ),
        markdown_descriptions={
            "worker.choose_standby": ("Promote a reliable standby during failover.",)
        },
    )

    docs = callable_docs_from_repository(repo)

    assert [doc.qname for doc in docs] == ["worker.__module__", "worker.choose_standby"]
    assert docs[1].key == "sample@abc:worker.py:worker.choose_standby:7"
    assert "choose standby" in docs[1].text
    assert "newest WAL" in docs[1].text
    assert "reliable standby" in docs[1].text


def test_build_symbol_text_never_reparses_comments() -> None:
    text = build_symbol_text(
        {
            "qname": "worker.choose_standby",
            "name": "choose_standby",
            "signature": "def choose_standby(nodes)",
            "docstring": "Select the newest WAL.",
        },
        ("Promote a reliable standby.",),
    )

    assert "choose standby" in text
    assert "newest WAL" in text
    assert "reliable standby" in text


def test_real_zvec_fts_and_safe_key_liveness(tmp_path) -> None:
    pytest.importorskip("zvec")
    doc = _doc()
    collection = open_write(str(tmp_path / "zvec"))

    assert doc_id_for_key(doc.key) != doc.key
    assert len(doc_id_for_key(doc.key)) == 64
    assert doc_id_for_key(doc.key).isalnum()
    assert "@" not in doc_id_for_key(doc.key)
    assert ":" not in doc_id_for_key(doc.key)

    assert upsert_symbol_docs(collection, [doc]) == 1
    optimize_and_flush(collection)
    hits = search_symbols(collection, "reliable standby", repo="sample")
    assert [hit["key"] for hit in hits] == [doc.key]
    assert fetch_symbol_docs(collection, {doc.key})[doc.key]["key"] == doc.key
    assert (
        validate_search_index_consistency([doc], live_graph_keys={doc.key}, collection=collection)[
            "ok"
        ]
        is True
    )
    graph_mismatch = validate_search_index_consistency(
        [doc],
        live_graph_keys={"graph-only"},
        collection=collection,
    )
    assert graph_mismatch["ok"] is False
    assert graph_mismatch["missing_in_graph"] == [doc.key]
    assert graph_mismatch["unexpected_in_graph"] == ["graph-only"]
    assert graph_mismatch["missing_in_zvec"] == ["graph-only"]

    del collection
    gc.collect()
    script = """
from codekg.zvec_store import open_read, search_symbols
import sys
collection = open_read(sys.argv[1])
print(len(search_symbols(collection, 'reliable standby', repo='sample')))
"""
    root = Path(__file__).parents[2]
    env = {**os.environ, "PYTHONPATH": f"{root / 'src'}:{os.environ.get('PYTHONPATH', '')}"}
    reader = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "zvec")],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert reader.stdout.strip() == "1"

    collection = open_write(str(tmp_path / "zvec"))

    delete_repo(collection, "sample")
    optimize_and_flush(collection)
    result = validate_search_index_consistency(
        [],
        live_graph_keys=set(),
        replaced_keys={doc.key},
        collection=collection,
    )
    assert result["ok"] is True
    assert fetch_symbol_docs(collection, {doc.key}) == {}
