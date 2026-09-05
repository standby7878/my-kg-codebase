from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from codekg.bulk_projection import project_repository
from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import FileIR, ImportIR, RepositoryIR, SymbolIR

pytestmark = pytest.mark.unit


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
