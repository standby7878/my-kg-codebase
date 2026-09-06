from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codekg.bulk_spool import create_spool, iter_spool_files
from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    InheritanceIR,
    LocalBindingIR,
    ModuleInitIR,
    ParseDiagnosticIR,
    SymbolIR,
)

pytestmark = pytest.mark.unit


def test_spool_round_trip_preserves_complete_file_ir(tmp_path: Path) -> None:
    files = (
        FileIR(
            path="pkg/module.py",
            language="python",
            loc=42,
            module_qname="pkg.module",
            module_init=ModuleInitIR("pkg.module.__module__", 1, 42),
            imports=(
                ImportIR("collections", "defaultdict"),
                ImportIR("pkg.helpers", "build", "make"),
            ),
            symbols=(
                SymbolIR(
                    kind="method",
                    name="run",
                    qname="pkg.module.Worker.run",
                    signature="def run(self, value: str) -> str",
                    start_line=10,
                    end_line=14,
                    cyclomatic=3,
                    parent_qname="pkg.module.Worker",
                    docstring="Run the worker and return its result.",
                    return_annotation="str",
                ),
            ),
            inheritance=(
                InheritanceIR("pkg.module.Worker", "BaseWorker", "pkg.base.BaseWorker"),
            ),
            calls=(
                CallIR(
                    owner_qname="pkg.module.__module__",
                    raw_callee="build",
                    callee_name="build",
                    callee_qname_hint="pkg.helpers.build",
                    receiver_kind="none",
                    start_line=3,
                    start_column=0,
                    end_line=3,
                    end_column=5,
                    ordinal=1,
                ),
                CallIR(
                    owner_qname="pkg.module.Worker.run",
                    raw_callee="self.client.send",
                    callee_name="send",
                    callee_qname_hint=None,
                    receiver_kind="self",
                    start_line=13,
                    start_column=8,
                    end_line=13,
                    end_column=24,
                    ordinal=2,
                ),
            ),
            local_bindings=(
                LocalBindingIR(
                    owner_qname="pkg.module.Worker.run",
                    target_name="result",
                    value_kind="call",
                    value_name="build",
                    value_qname_hint="pkg.helpers.build",
                    annotation="str",
                    start_line=11,
                    start_column=4,
                    guarded=True,
                ),
                LocalBindingIR(
                    owner_qname="pkg.module.Worker.run",
                    target_name="fallback",
                    value_kind="unknown",
                    value_name=None,
                    value_qname_hint=None,
                    annotation=None,
                    start_line=12,
                    start_column=4,
                ),
            ),
        ),
        FileIR(
            path="broken.py",
            language="python",
            loc=1,
            module_qname="broken",
            parse_status="error",
            diagnostics=(ParseDiagnosticIR("syntax_error", "error", None, 7, "invalid syntax"),),
        ),
    )
    spool = tmp_path / "files.sqlite"

    create_spool(spool, files)

    assert list(iter_spool_files(spool)) == list(files)
    restored = list(iter_spool_files(spool))
    assert [file.path for file in restored] == ["pkg/module.py", "broken.py"]
    assert restored[0].imports == files[0].imports
    assert restored[0].calls == files[0].calls
    assert restored[0].local_bindings == files[0].local_bindings
    assert restored[0].symbols[0].docstring == files[0].symbols[0].docstring
    assert restored[1].module_init is None
    assert restored[1].diagnostics[0].line is None


def test_v2_spool_is_normalized_and_has_no_file_payload(tmp_path: Path) -> None:
    spool = tmp_path / "normalized.sqlite"
    create_spool(
        spool,
        [FileIR(path="module.py", language="python", loc=1, module_qname="module")],
    )

    connection = sqlite3.connect(spool)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "metadata",
            "files",
            "moduleinit",
            "symbols",
            "imports",
            "inheritance",
            "calls",
            "localbindings",
            "diagnostics",
        } <= tables
        assert "payload" not in {
            row[1] for row in connection.execute("PRAGMA table_info(files)")
        }
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("2",)
    finally:
        connection.close()
