from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_index_all_indexes_sorted_immediate_directories_and_skips_hidden_files(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "README.md").write_text("not a repository")
    (tmp_path / "alpha" / "nested").mkdir()
    calls: list[tuple[Path, bool]] = []

    def fake_index_repository(path: Path, *, replace: bool) -> dict[str, object]:
        calls.append((path, replace))
        return {"repo_name": path.name}

    monkeypatch.setattr("codekg.ingest.index_repository", fake_index_repository)

    result = CliRunner().invoke(app, ["index-all", str(tmp_path)])

    assert result.exit_code == 0
    assert calls == [
        (tmp_path / "alpha", True),
        (tmp_path / "zeta", True),
    ]
    assert "alpha" in result.stdout
    assert "zeta" in result.stdout
    assert "hidden" not in result.stdout


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_index_all_requires_directory_root(tmp_path: Path, root_kind: str) -> None:
    root = tmp_path / "missing"
    if root_kind == "file":
        root.write_text("not a directory")

    result = CliRunner().invoke(app, ["index-all", str(root)])

    assert result.exit_code != 0
    assert "index root must be an existing directory" in result.output


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


def test_bulk_export_scans_paths_and_prints_manifest_and_counts(
    monkeypatch, tmp_path: Path
) -> None:
    scanned: list[Path] = []
    exported: list[tuple[list[object], Path]] = []
    manifest = tmp_path / "manifest.json"

    def fake_scan_repository(path: Path) -> object:
        scanned.append(path)
        return f"repository:{path.name}"

    def fake_export_repositories(repositories: list[object], output: Path) -> object:
        exported.append((repositories, output))
        return SimpleNamespace(manifest_path=manifest, counts={"repositories": 2, "files": 5})

    monkeypatch.setattr("codekg.ingest.scan_repository", fake_scan_repository)
    monkeypatch.setattr("codekg.bulk_export.export_repositories", fake_export_repositories)

    result = CliRunner().invoke(
        app,
        ["bulk-export", str(tmp_path / "export"), "first", "second"],
    )

    assert result.exit_code == 0
    assert scanned == [Path("first"), Path("second")]
    assert exported == [(["repository:first", "repository:second"], tmp_path / "export")]
    assert str(manifest) in result.stdout
    assert "repositories" in result.stdout
    assert "files" in result.stdout


def test_bulk_import_passes_options_and_prints_result(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, str, str]] = []
    expected = SimpleNamespace(command=["neo4j-admin"], returncode=0, stdout="ok", stderr="")

    def fake_run_bulk_import(manifest: Path, *, database: str, neo4j_admin: str) -> object:
        calls.append((manifest, database, neo4j_admin))
        return expected

    monkeypatch.setattr("codekg.bulk_import.run_bulk_import", fake_run_bulk_import)

    result = CliRunner().invoke(
        app,
        [
            "bulk-import",
            str(tmp_path / "manifest.json"),
            "--database",
            "analytics",
            "--neo4j-admin",
            "/usr/local/bin/neo4j-admin",
        ],
    )

    assert result.exit_code == 0
    assert calls == [(tmp_path / "manifest.json", "analytics", "/usr/local/bin/neo4j-admin")]
    assert "neo4j-admin" in result.stdout
    assert "returncode=0" in result.stdout


def test_bulk_zvec_scans_and_indexes_descriptions_without_graph_calls(monkeypatch) -> None:
    scanned: list[Path] = []
    docs_for: list[object] = []
    upserts: list[tuple[object, list[object]]] = []
    optimized: list[object] = []
    collection = object()

    def fake_scan_repository(path: Path) -> object:
        scanned.append(path)
        return f"repository:{path.name}"

    def fake_callable_docs(repository: object) -> list[object]:
        docs_for.append(repository)
        return [f"doc:{repository}"]

    def fake_open_write() -> object:
        return collection

    def fake_upsert_symbol_docs(target: object, docs: list[object]) -> int:
        upserts.append((target, docs))
        return len(docs)

    def fake_optimize_and_flush(target: object) -> None:
        optimized.append(target)

    monkeypatch.setattr("codekg.ingest.scan_repository", fake_scan_repository)
    monkeypatch.setattr(
        "codekg.search_index.callable_docs_from_repository",
        fake_callable_docs,
    )
    monkeypatch.setattr("codekg.zvec_store.open_write", fake_open_write)
    monkeypatch.setattr("codekg.zvec_store.upsert_symbol_docs", fake_upsert_symbol_docs)
    monkeypatch.setattr("codekg.zvec_store.optimize_and_flush", fake_optimize_and_flush)

    result = CliRunner().invoke(app, ["bulk-zvec", "first", "second"])

    assert result.exit_code == 0
    assert scanned == [Path("first"), Path("second")]
    assert docs_for == ["repository:first", "repository:second"]
    assert upserts == [(collection, ["doc:repository:first", "doc:repository:second"])]
    assert optimized == [collection]
    assert "repositories" in result.stdout
    assert "documents" in result.stdout


@pytest.mark.parametrize(
    ("consistency", "expected_exit_code"),
    [
        ({"ok": True, "verified_callables": 1}, 0),
        ({"ok": False, "missing_in_zvec": ["key-1"]}, 1),
    ],
)
def test_validate_bulk_index_checks_live_keys_and_exit_status(
    monkeypatch,
    tmp_path: Path,
    consistency: dict[str, object],
    expected_exit_code: int,
) -> None:
    repository = SimpleNamespace(repo_name="sample")
    collection = object()
    scanned: list[Path] = []
    graph_repos: list[str] = []
    validation: list[tuple[list[object], set[str], object]] = []

    def fake_scan_repository(path: Path) -> object:
        scanned.append(path)
        return repository

    def fake_callable_docs(repository_value: object) -> list[object]:
        return ["doc-1"]

    def fake_iter_callable_rows(*, repo: str) -> list[dict[str, str]]:
        graph_repos.append(repo)
        return [{"key": "key-1"}]

    def fake_open_write() -> object:
        return collection

    def fake_validate(
        docs: list[object], *, live_graph_keys: set[str], collection: object
    ) -> dict[str, object]:
        validation.append((docs, live_graph_keys, collection))
        return consistency

    monkeypatch.setattr("codekg.ingest.scan_repository", fake_scan_repository)
    monkeypatch.setattr(
        "codekg.search_index.callable_docs_from_repository",
        fake_callable_docs,
    )
    monkeypatch.setattr("codekg.search_index.iter_callable_rows", fake_iter_callable_rows)
    monkeypatch.setattr("codekg.search_index.validate_search_index_consistency", fake_validate)
    monkeypatch.setattr("codekg.zvec_store.open_write", fake_open_write)

    result = CliRunner().invoke(app, ["validate-bulk-index", str(tmp_path / "repo")])

    assert result.exit_code == expected_exit_code
    assert scanned == [tmp_path / "repo"]
    assert graph_repos == ["sample"]
    assert validation == [(["doc-1"], {"key-1"}, collection)]
    assert str(consistency["ok"]) in result.stdout
