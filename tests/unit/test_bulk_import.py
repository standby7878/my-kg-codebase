from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codekg.bulk_import import BulkImportError, build_import_command, run_bulk_import

pytestmark = pytest.mark.unit


def test_build_import_command_places_database_before_input_flags(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.csv"
    relationships = tmp_path / "calls.csv"
    nodes.write_text("", encoding="utf-8")
    relationships.write_text("", encoding="utf-8")
    export = SimpleNamespace(
        node_files={"Function": nodes}, relationship_files={"CALLS": relationships}
    )

    command = build_import_command(export, database="graph", neo4j_admin="admin")

    assert command == [
        "admin",
        "database",
        "import",
        "full",
        "graph",
        "--id-type=string",
        "--multiline-fields=true",
        "--overwrite-destination=true",
        f"--nodes=Function={nodes}",
        f"--relationships=CALLS={relationships}",
    ]


def test_build_import_command_keeps_header_and_shards_in_one_ordered_group(tmp_path: Path) -> None:
    header = tmp_path / "header.csv"
    shard_one = tmp_path / "part-000.csv"
    shard_two = tmp_path / "part-001.csv"
    for path in (header, shard_one, shard_two):
        path.write_text("", encoding="utf-8")
    export = SimpleNamespace(
        node_files={"Function": (header, shard_one, shard_two)}, relationship_files={}
    )

    command = build_import_command(export)

    assert command[-1] == f"--nodes=Function={header},{shard_one},{shard_two}"


def test_build_import_command_rejects_comma_in_group_filename(tmp_path: Path) -> None:
    node = tmp_path / "nodes,part.csv"
    node.write_text("", encoding="utf-8")

    with pytest.raises(BulkImportError, match="unsupported comma"):
        build_import_command(SimpleNamespace(node_files={"Function": node}, relationship_files={}))


def test_run_bulk_import_loads_manifest_and_passes_expected_runner_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    node_file = tmp_path / "nodes.csv"
    node_file.write_text("", encoding="utf-8")
    export = SimpleNamespace(node_files={"File": node_file}, relationship_files={})
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr("codekg.bulk_import.load_bulk_export", lambda path: export)

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = run_bulk_import(manifest, runner=runner)

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert calls == [
        (
            [
                "neo4j-admin",
                "database",
                "import",
                "full",
                "neo4j",
                "--id-type=string",
                "--multiline-fields=true",
                "--overwrite-destination=true",
                f"--nodes=File={node_file}",
            ],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


def test_docker_bulk_import_enables_multiline_fields() -> None:
    script = Path(__file__).parents[2] / "docker" / "bulk-import.sh"

    content = script.read_text(encoding="utf-8")

    assert (
        "set -- neo4j-admin database import full neo4j --id-type=string --multiline-fields=true"
    ) in content
    assert "/import/manifest.json" in content
    assert "jq -r --arg kind" in content
    assert "codekg-import-entries" in content


def test_invalid_manifest_fails_before_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner_called = False

    def runner(*args: object, **kwargs: object) -> None:
        nonlocal runner_called
        runner_called = True

    with pytest.raises(BulkImportError, match="does not exist"):
        run_bulk_import(tmp_path / "missing.json", runner=runner)
    assert not runner_called


def test_nonzero_import_includes_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    node_file = tmp_path / "nodes.csv"
    node_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "codekg.bulk_import.load_bulk_export",
        lambda path: SimpleNamespace(node_files={"File": node_file}, relationship_files={}),
    )

    with pytest.raises(BulkImportError, match="bad import"):
        run_bulk_import(
            manifest,
            runner=lambda *args, **kwargs: SimpleNamespace(
                returncode=2, stdout="", stderr="bad import"
            ),
        )
