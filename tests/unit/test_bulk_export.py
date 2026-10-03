from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from codekg.bulk_export import export_repositories, export_repository_path, load_bulk_export
from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    LocalBindingIR,
    ParseDiagnosticIR,
    RepositoryIR,
    SymbolIR,
)

pytestmark = pytest.mark.unit


def test_single_root_empty_repository_publishes_repository_manifest(tmp_path: Path) -> None:
    root = tmp_path / "markdown-only"
    root.mkdir()
    (root / "README.md").write_text("# Notes\n", encoding="utf-8")

    exported = export_repository_path(root, tmp_path / "output", workers=1)

    assert exported.manifest_path == tmp_path / "output" / "manifest.json"
    assert exported.counts == {"nodes": 1, "nodes_Repository": 1}
    assert exported.node_groups["Repository"]


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


def test_bulk_export_logs_graph_phase_boundaries(caplog, tmp_path: Path) -> None:
    with caplog.at_level(logging.DEBUG, logger="codekg.bulk_export"):
        export_repositories([_repo()], tmp_path)

    assert "codekg_bulk_export_started" in caplog.text
    assert "codekg_bulk_export_graph_built" in caplog.text
    assert '"duration_ms":' in caplog.text


def _local_receiver_repo() -> RepositoryIR:
    return RepositoryIR(
        repo_name="sample",
        commit="abc",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="mod.py",
                language="python",
                loc=10,
                module_qname="mod",
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
                    SymbolIR("function", "build", "mod.build", "def build()", 5, 8),
                ),
                calls=(
                    CallIR(
                        owner_qname="mod.build",
                        raw_callee="worker.run",
                        callee_name="run",
                        callee_qname_hint="mod.worker.run",
                        receiver_kind="attribute",
                        start_line=7,
                        start_column=4,
                        end_line=7,
                        end_column=16,
                        ordinal=1,
                    ),
                ),
                local_bindings=(
                    LocalBindingIR(
                        owner_qname="mod.build",
                        target_name="worker",
                        value_kind="call",
                        value_name="Worker",
                        value_qname_hint="mod.Worker",
                        annotation=None,
                        start_line=6,
                        start_column=21,
                    ),
                ),
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


def test_bulk_export_projects_exact_local_receiver_call(tmp_path: Path) -> None:
    exported = export_repositories([_local_receiver_repo()], tmp_path / "export")

    with exported.relationship_files["EXACT_CALLS"].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1
    assert rows[0]["resolution"] == "local_receiver"
    assert rows[0][":START_ID(CodeKG)"] == "sample@abc:mod.py:mod.build:5"
    assert rows[0][":END_ID(CodeKG)"] == "sample@abc:mod.py:mod.Worker.run:2"


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


def test_diagnostics_are_generated_once_per_repository_and_attached_to_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import codekg.bulk_export as bulk_export

    repo = RepositoryIR(
        repo_name="broken",
        commit="abc",
        root_path="/repos/broken",
        files=(
            FileIR(
                path="first.py",
                language="python",
                loc=1,
                module_qname="first",
                parse_status="error",
                diagnostics=(ParseDiagnosticIR("syntax_error", "error", 1, 1, "first"),),
            ),
            FileIR(
                path="second.py",
                language="python",
                loc=1,
                module_qname="second",
                parse_status="error",
                diagnostics=(ParseDiagnosticIR("syntax_error", "error", 1, 1, "second"),),
            ),
        ),
    )
    original = bulk_export._diagnostic_rows
    calls = 0

    def diagnostic_rows_once(value: RepositoryIR) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(bulk_export, "_diagnostic_rows", diagnostic_rows_once)
    exported = export_repositories([repo], tmp_path / "export")

    assert calls == 1
    with exported.node_files["ParseDiagnostic"].open(newline="", encoding="utf-8") as handle:
        diagnostic_rows = list(csv.DictReader(handle))
    with exported.relationship_files["HAS_DIAGNOSTIC"].open(newline="", encoding="utf-8") as handle:
        relationships = list(csv.DictReader(handle))
    assert [row["message"] for row in diagnostic_rows] == ["first", "second"]
    assert {(row[":START_ID(CodeKG)"], row[":END_ID(CodeKG)"]) for row in relationships} == {
        ("broken@abc:first.py", row["key:ID(CodeKG)"])
        for row in diagnostic_rows
        if row["message"] == "first"
    } | {
        ("broken@abc:second.py", row["key:ID(CodeKG)"])
        for row in diagnostic_rows
        if row["message"] == "second"
    }


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


def test_extract_spool_skips_unreadable_files(tmp_path: Path) -> None:
    from codekg.bulk_export import _extract_spool
    from codekg.bulk_spool import iter_spool_files

    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    blocked = root / "aiven/db/attrs/funcattrs.py"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("def blocked():\n    return 2\n", encoding="utf-8")
    blocked.chmod(0o000)

    spool = tmp_path / "extract.sqlite"
    _extract_spool(
        str(root),
        (str(root / "ok.py"), str(blocked)),
        str(spool),
    )

    assert [file.path for file in iter_spool_files(spool)] == ["ok.py"]
