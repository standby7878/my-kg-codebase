from __future__ import annotations

import csv
from pathlib import Path

import pytest

from codekg.bulk_export import export_repositories, load_bulk_export
from codekg.ir import CallIR, FileIR, ImportIR, RepositoryIR, SymbolIR

pytestmark = pytest.mark.unit


def _repo(
    name: str = "sample",
    *,
    with_call: bool = False,
    imports: tuple[ImportIR, ...] = (),
) -> RepositoryIR:
    return RepositoryIR(
        repo_name=name,
        commit="abc",
        root_path=f"/repos/{name}",
        files=(
            FileIR(
                path="mod.py",
                language="python",
                loc=4,
                module_qname="mod",
                imports=imports,
                symbols=(
                    SymbolIR("type", "Worker", "mod.Worker", "class Worker", 1, 3),
                    SymbolIR(
                        "method",
                        "run",
                        "mod.Worker.run",
                        "def run()",
                        2,
                        2,
                        parent_qname="mod.Worker",
                    ),
                ),
                calls=(
                    CallIR(
                        owner_qname="mod.Worker.run",
                        raw_callee="run",
                        callee_name="run",
                        callee_qname_hint="mod.Worker.run",
                        receiver_kind="none",
                        start_line=2,
                        start_column=4,
                        end_line=2,
                        end_column=7,
                        ordinal=1,
                    ),
                )
                if with_call
                else (),
            ),
        ),
    )


def test_export_is_deterministic_and_uses_neo4j_headers(tmp_path: Path) -> None:
    first = export_repositories([_repo()], tmp_path / "first")
    second = export_repositories([_repo()], tmp_path / "second")

    assert sorted(first.node_files) == ["File", "Method", "Module", "Repository", "Type"]
    assert "CONTAINS" in first.relationship_files
    assert "DEFINES" in first.relationship_files
    assert first.counts["relationships_HAS_METHOD"] == 1
    for label in first.node_files:
        assert first.node_files[label].read_bytes() == second.node_files[label].read_bytes()
    for relationship in first.relationship_files:
        assert (
            first.relationship_files[relationship].read_bytes()
            == second.relationship_files[relationship].read_bytes()
        )

    with first.node_files["Method"].open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == [
            "key:ID(CodeKG)",
            "name",
            "qname",
            "signature",
            "start_line:int",
            "end_line:int",
            "cyclomatic:int",
            ":LABEL",
        ]
    with first.relationship_files["CONTAINS"].open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == [":START_ID(CodeKG)", ":END_ID(CodeKG)", ":TYPE"]


def test_callsite_candidate_keys_use_neo4j_string_array_csv_encoding(tmp_path: Path) -> None:
    exported = export_repositories([_repo(with_call=True)], tmp_path / "export")

    with exported.node_files["CallSite"].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert rows[0][-5:-1] == [
        "candidate_count:int",
        "candidate_keys:string[]",
        "initializer_candidate_count:int",
        "initializer_candidate_keys:string[]",
    ]
    assert rows[1][-5] == "1"
    assert ";" not in rows[1][-4]
    assert rows[1][-4].startswith("sample@abc:")


def test_projected_relationship_keys_equal_callsite_key(tmp_path: Path) -> None:
    exported = export_repositories([_repo(with_call=True)], tmp_path / "export")

    with exported.node_files["CallSite"].open(newline="", encoding="utf-8") as handle:
        callsite_key = next(csv.reader(handle))[0]
        callsite_key = next(csv.reader(handle))[0]
    for relationship in ("CALLS", "EXACT_CALLS", "RESOLVES_TO"):
        with exported.relationship_files[relationship].open(newline="", encoding="utf-8") as handle:
            assert next(csv.reader(handle))[0] == "key"
            assert next(csv.reader(handle))[0] == callsite_key


def test_duplicate_node_keys_are_rejected_before_manifest(tmp_path: Path) -> None:
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="duplicate node key"):
        export_repositories([_repo(), _repo()], output)
    assert not (output / "manifest.json").exists()


def test_repeated_identical_imports_are_exported_once(tmp_path: Path) -> None:
    exported = export_repositories(
        [_repo(imports=(ImportIR("shutil", "shutil"), ImportIR("shutil", "shutil")))],
        tmp_path / "export",
    )

    with exported.relationship_files["IMPORTS"].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert len(rows) == 2
    assert exported.counts["relationships_IMPORTS"] == 1


def test_duplicate_import_keys_with_different_aliases_are_rejected(tmp_path: Path) -> None:
    imports = (ImportIR("shutil", "shutil"), ImportIR("shutil", "shutil", ""))

    with pytest.raises(ValueError, match="duplicate relationship key: IMPORTS"):
        export_repositories([_repo(imports=imports)], tmp_path / "export")


def test_dangling_relationship_endpoints_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import codekg.bulk_export as bulk_export

    monkeypatch.setattr(
        bulk_export,
        "_resolved_call_rows",
        lambda calls, resolutions: [
            {
                "callsite_key": "missing-site",
                "caller_key": "missing-caller",
                "callee_key": "missing-callee",
                "resolution": "exact",
                "line": 1,
                "column": 1,
            }
        ],
    )
    with pytest.raises(ValueError, match="dangling"):
        export_repositories([_repo()], tmp_path / "export")


def test_manifest_reload_exposes_same_export(tmp_path: Path) -> None:
    exported = export_repositories([_repo()], tmp_path / "export")
    loaded = load_bulk_export(exported.manifest_path)

    assert loaded == exported
