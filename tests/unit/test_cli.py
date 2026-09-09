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
    assert "scan_seconds" in result.stdout
    assert "export_seconds" in result.stdout
    assert "elapsed_seconds" in result.stdout
    assert "peak_rss_kib" in result.stdout


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


def test_bulk_zvec_streams_validated_search_stages_without_scanning(monkeypatch) -> None:
    manifests = [Path("first.json"), Path("second.json")]
    stages = [Path("first.sqlite"), Path("second.sqlite")]
    validated: list[list[Path]] = []
    docs_for: list[Path] = []
    upserts: list[tuple[object, list[object]]] = []
    optimized: list[object] = []
    collection = object()

    def fake_validate(values: list[Path]) -> tuple[Path, ...]:
        validated.append(values)
        return tuple(stages)

    def fake_iter_stage_docs(stage: Path):
        docs_for.append(stage)
        yield f"doc:{stage.stem}"

    def fake_open_write() -> object:
        return collection

    def fake_upsert_symbol_docs(target: object, docs) -> int:
        doc_list = list(docs)
        upserts.append((target, doc_list))
        return len(doc_list)

    def fake_optimize_and_flush(target: object) -> None:
        optimized.append(target)

    monkeypatch.setattr("codekg.bulk_search.validate_search_manifests", fake_validate)
    monkeypatch.setattr("codekg.bulk_search.iter_search_stage_docs", fake_iter_stage_docs)
    monkeypatch.setattr("codekg.zvec_store.open_write", fake_open_write)
    monkeypatch.setattr("codekg.zvec_store.upsert_symbol_docs", fake_upsert_symbol_docs)
    monkeypatch.setattr("codekg.zvec_store.optimize_and_flush", fake_optimize_and_flush)

    result = CliRunner().invoke(app, ["bulk-zvec", *map(str, manifests)])

    assert result.exit_code == 0
    assert validated == [manifests]
    assert docs_for == stages
    assert upserts == [(collection, ["doc:first", "doc:second"])]
    assert optimized == [collection]
    assert "manifests" in result.stdout
    assert "documents" in result.stdout


@pytest.mark.parametrize(
    ("consistency", "expected_exit_code"),
    [
        ({"ok": True, "verified_callables": 1}, 0),
        ({"ok": False, "missing_in_zvec": ["key-1"]}, 1),
    ],
)
def test_validate_bulk_index_uses_stage_and_exit_status(
    monkeypatch,
    tmp_path: Path,
    consistency: dict[str, object],
    expected_exit_code: int,
) -> None:
    collection = object()
    client = object()
    manifest = tmp_path / "manifest.json"
    stage = tmp_path / "search.sqlite"
    validation: list[tuple[tuple[Path, ...], object, object]] = []

    def fake_validate_manifests(values: list[Path]) -> tuple[Path, ...]:
        assert values == [manifest]
        return (stage,)

    def fake_open_write() -> object:
        return collection

    def fake_validate(stages, *, collection: object, client: object) -> dict[str, object]:
        validation.append((stages, collection, client))
        return consistency

    monkeypatch.setattr("codekg.bulk_search.validate_search_manifests", fake_validate_manifests)
    monkeypatch.setattr("codekg.bulk_search.validate_staged_search", fake_validate)
    monkeypatch.setattr("codekg.neo4j_client.get_client", lambda: client)
    monkeypatch.setattr("codekg.zvec_store.open_write", fake_open_write)

    result = CliRunner().invoke(app, ["validate-bulk-index", str(manifest)])

    assert result.exit_code == expected_exit_code
    assert validation == [((stage,), collection, client)]
    assert str(consistency["ok"]) in result.stdout
