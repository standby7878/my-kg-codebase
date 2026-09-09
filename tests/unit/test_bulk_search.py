from __future__ import annotations

import json
from pathlib import Path

import pytest

from codekg import bulk_search
from codekg.bulk_export import export_repositories, export_repository_path
from codekg.bulk_search import (
    create_search_stage_from_repositories,
    iter_search_stage_docs,
    load_search_stage,
    validate_search_manifests,
    validate_staged_search,
)
from codekg.ir import FileIR, RepositoryIR, SymbolIR

pytestmark = pytest.mark.unit


def _repo(name: str = "sample", commit: str = "abc") -> RepositoryIR:
    return RepositoryIR(
        repo_name=name,
        commit=commit,
        root_path=f"/repos/{name}",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=2,
                module_qname="worker",
                symbols=(
                    SymbolIR(
                        kind="function",
                        name="choose_standby",
                        qname="worker.choose_standby",
                        signature="def choose_standby()",
                        start_line=1,
                        end_line=2,
                        docstring="Select the newest replica.",
                    ),
                ),
            ),
        ),
        markdown_descriptions={
            "worker.choose_standby": ("Promote worker.choose_standby during failover.",)
        },
    )


def test_legacy_export_publishes_round_trip_search_stage(tmp_path: Path) -> None:
    exported = export_repositories([_repo()], tmp_path / "export")

    assert exported.search_stage == tmp_path / "export" / "search.sqlite"
    assert load_search_stage(exported.manifest_path) == exported.search_stage
    docs = list(iter_search_stage_docs(exported.search_stage))
    assert len(docs) == 1
    assert docs[0].key == "sample@abc:worker.py:worker.choose_standby:1"
    assert docs[0].signature == "def choose_standby()"
    assert "newest replica" in docs[0].text
    assert "during failover" in docs[0].text


def test_sharded_stage_survives_source_removal_and_preserves_markdown(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    (root / "worker.py").write_text(
        'def choose_standby():\n    """Select the newest replica."""\n', encoding="utf-8"
    )
    (root / "README.md").write_text(
        "Use worker.choose_standby during failover.\n", encoding="utf-8"
    )

    exported = export_repository_path(root, tmp_path / "export", workers=1)
    (root / "worker.py").unlink()
    (root / "README.md").unlink()
    root.rmdir()

    stage = load_search_stage(exported.manifest_path)
    docs = list(iter_search_stage_docs(stage))
    assert [doc.qname for doc in docs] == ["worker.choose_standby"]
    assert "newest replica" in docs[0].text
    assert "during failover" in docs[0].text


def test_sharded_stage_bounds_markdown_enrichment_per_callable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    (root / "worker.py").write_text("def choose_standby():\n    pass\n", encoding="utf-8")
    (root / "README.md").write_text(
        "worker.choose_standby " + "first " * 20 + "omitted-tail\n", encoding="utf-8"
    )
    monkeypatch.setattr(bulk_search, "MAX_MARKDOWN_DESCRIPTION_CHARS", 40)

    exported = export_repository_path(root, tmp_path / "export", workers=1)
    docs = list(iter_search_stage_docs(exported.search_stage))

    assert "worker.choose_standby first" in docs[0].text
    assert "omitted-tail" not in docs[0].text


def test_zero_callable_stage_retains_repository_identity(tmp_path: Path) -> None:
    repo = RepositoryIR(repo_name="empty", commit="zero", root_path="/repos/empty")
    stage = tmp_path / "search.sqlite"

    assert create_search_stage_from_repositories(stage, [repo]) == 0
    assert list(iter_search_stage_docs(stage)) == []

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"search_stage": {"version": 1, "file": stage.name, "documents": 0}}),
        encoding="utf-8",
    )
    assert validate_search_manifests([manifest]) == (stage,)


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {"version": 2, "file": "search.sqlite", "documents": 0},
        {"version": 1, "file": "../search.sqlite", "documents": 0},
        {"version": 1, "file": "missing.sqlite", "documents": 0},
    ],
)
def test_manifest_rejects_missing_invalid_or_escaping_stage(tmp_path: Path, entry: object) -> None:
    manifest = tmp_path / "nested" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"search_stage": entry}), encoding="utf-8")

    with pytest.raises((ValueError, FileNotFoundError)):
        load_search_stage(manifest)


def test_duplicate_repository_manifests_are_rejected_before_use(tmp_path: Path) -> None:
    manifests = []
    for index in range(2):
        directory = tmp_path / str(index)
        stage = directory / "search.sqlite"
        create_search_stage_from_repositories(stage, [_repo(commit=str(index))])
        manifest = directory / "manifest.json"
        manifest.write_text(
            json.dumps({"search_stage": {"version": 1, "file": stage.name, "documents": 1}}),
            encoding="utf-8",
        )
        manifests.append(manifest)

    with pytest.raises(ValueError, match="duplicate staged repository"):
        validate_search_manifests(manifests)


def test_validation_pages_graph_and_uses_bounded_zvec_fetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stage = tmp_path / "search.sqlite"
    create_search_stage_from_repositories(stage, [_repo()])
    key = "sample@abc:worker.py:worker.choose_standby:1"

    class Client:
        def __init__(self) -> None:
            self.operations: list[str] = []

        def execute_read(self, query, params, *, max_rows, operation):
            self.operations.append(operation)
            if operation == "validate staged repositories":
                return [{"repo": "sample", "commit": "abc"}] if not params["after"] else []
            return [{"key": key, "repo": "sample", "commit": "abc"}] if not params["after"] else []

    fetched: list[set[str]] = []

    def fake_fetch(collection, keys: set[str]):
        fetched.append(keys)
        return {value: {"key": value} for value in keys}

    monkeypatch.setattr("codekg.bulk_search.fetch_symbol_docs", fake_fetch)
    client = Client()

    result = validate_staged_search([stage], collection=object(), client=client)

    assert result["ok"] is True
    assert result["expected_callables"] == 1
    assert fetched == [{key}]
    assert client.operations.count("validate staged repositories") == 2
    assert client.operations.count("validate staged callable keys") == 2
