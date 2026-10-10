from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from codekg.bulk_projection import ProjectionValidator, project_partition, project_repository
from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import FileIR, ImportIR, RepositoryIR, SymbolIR

pytestmark = pytest.mark.unit


def test_projection_validator_uses_rebuildable_scratch_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite"
    validator = ProjectionValidator(path)
    assert validator.connection.execute("PRAGMA journal_mode").fetchone()[0] == "off"
    assert validator.connection.execute("PRAGMA synchronous").fetchone()[0] == 0
    assert validator.connection.execute("PRAGMA cache_size").fetchone()[0] == -8192
    validator.node("File", "file-key")
    validator.relationship("CONTAINS", "contains-key", "repo", "file-key")
    with pytest.raises(ValueError, match="dangling"):
        validator.close()
    assert not path.exists()


def test_projection_validator_merge_failure_discards_only_destination_scratch(
    tmp_path: Path,
) -> None:
    sources = []
    for index in range(2):
        path = tmp_path / f"source-{index}.sqlite"
        validator = ProjectionValidator(path)
        validator.node("Repository", "same-key")
        validator.close(validate_endpoints=False)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            validator.connection.execute("SELECT 1")
        sources.append(path)

    destination = tmp_path / "merged.sqlite"
    with pytest.raises(ValueError, match="duplicate node key"):
        ProjectionValidator.merge(destination, sources)
    assert not destination.exists()
    assert all(path.exists() for path in sources)


def test_partition_error_is_preserved_while_owned_validator_scratch_is_removed(
    monkeypatch, tmp_path: Path
) -> None:
    spool = tmp_path / "one.sqlite"
    create_spool(spool, [FileIR(path="one.py", language="python", loc=1, module_qname="one")])
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, [spool], repo_prefix="repo@abc")
    output = tmp_path / "projection"
    scratch = output / ".projection-validation.sqlite"

    def fail_projection(*args, **kwargs):
        raise RuntimeError("projection sentinel")

    monkeypatch.setattr("codekg.bulk_projection._project_file", fail_projection)
    with pytest.raises(RuntimeError, match="projection sentinel"):
        project_partition(RepositoryIR("repo", "abc", "/repo"), [spool], registry, output)

    assert not scratch.exists()


def test_projection_writes_header_then_headerless_shard(tmp_path: Path) -> None:
    spool = tmp_path / "one.sqlite"
    file = FileIR(
        path="pkg/mod.py",
        language="python",
        loc=2,
        module_qname="pkg.mod",
        symbols=(SymbolIR("function", "run", "pkg.mod.run", "def run()", 1, 2),),
    )
    create_spool(spool, [file])
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, [spool], repo_prefix="repo@abc")
    result = project_repository(
        RepositoryIR("repo", "abc", "/repo"), [spool], registry, tmp_path / "out"
    )

    manifest = result.manifest_path.read_text(encoding="utf-8")
    assert '"version": 2' in manifest
    header = tmp_path / "out" / "headers" / "nodes_Function.header.csv"
    shard = tmp_path / "out" / "nodes" / "Function" / "part-000000.csv"
    assert next(csv.reader(header.open(encoding="utf-8")))[0] == "key:ID(CodeKG)"
    assert next(csv.reader(shard.open(encoding="utf-8")))[0].startswith("repo@abc:")


def test_projection_uses_multiple_partitions_and_one_external_module_owner(tmp_path: Path) -> None:
    spools = [tmp_path / f"{ordinal}.sqlite" for ordinal in range(2)]
    files = [
        FileIR(
            path=f"pkg/mod{ordinal}.py",
            language="python",
            loc=1,
            module_qname=f"pkg.mod{ordinal}",
            imports=(ImportIR("requests", "get"),),
        )
        for ordinal in range(2)
    ]
    for spool, file in zip(spools, files, strict=True):
        create_spool(spool, [file])
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, spools, repo_prefix="repo@abc")

    output = tmp_path / "out"
    project_repository(RepositoryIR("repo", "abc", "/repo"), spools, registry, output, workers=2)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["partitions"] == 2
    assert len(manifest["nodes"]["File"]["files"]) == 3
    module_files = manifest["nodes"]["Module"]["files"]
    rows = []
    for relative in module_files[1:]:
        with (output / relative).open(encoding="utf-8", newline="") as handle:
            rows.extend(csv.reader(handle))
    assert [row[2] for row in rows].count("requests") == 1


def test_projection_of_empty_repository_publishes_repository_node(tmp_path: Path) -> None:
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, [], repo_prefix="repo@abc")

    result = project_repository(
        RepositoryIR("repo", "abc", "/repo"), [], registry, tmp_path / "out", workers=2
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["counts"] == {"nodes": 1, "nodes_Repository": 1}
    assert manifest["nodes"]["Repository"]["count"] == 1


def test_staged_projection_tracks_max_serialized_csv_field_without_rescanning(
    tmp_path: Path,
) -> None:
    from codekg.bulk_export import load_bulk_export
    from codekg.bulk_import import build_import_command

    signature = '"λ, value"' * 530_000
    spool = tmp_path / "large.sqlite"
    create_spool(
        spool,
        [
            FileIR(
                path="large.py",
                language="python",
                loc=1,
                module_qname="large",
                symbols=(SymbolIR("function", "large", "large.large", signature, 1, 1),),
            )
        ],
    )
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, [spool], repo_prefix="repo@abc")
    output = tmp_path / "out"

    project_repository(RepositoryIR("repo", "abc", "/repo"), [spool], registry, output)

    loaded = load_bulk_export(output / "manifest.json")
    expected_size = len(signature.encode("utf-8")) + signature.count('"') + 2
    assert loaded.max_csv_field_size_bytes == expected_size
    assert any(arg.startswith("--read-buffer-size=") for arg in build_import_command(loaded))
