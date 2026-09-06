from __future__ import annotations

import ast
import codecs
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from codekg.ingest import _PythonExtractor, _scan_file

pytestmark = pytest.mark.unit


def test_source_segments_match_ast_for_unicode_multiline_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_bytes(
        b"# coding: utf-8\r\n"
        b"def run(\xc3\xa9: list[\r\n"
        b"    caf\xc3\xa9.Type,\r\n"
        b"]) -> tuple[\r\n"
        b"    str,\r\n"
        b"]:\r\n"
        b"    value = prefix(\r\n"
        b'        "\xc3\xa9",\r\n'
        b"        other,\r\n"
        b"    )\r\n"
        b"    return value\r\n"
    )

    source = path.read_bytes().decode("utf-8")
    assert "\r\n" in source
    tree = ast.parse(source, filename=str(path))
    extractor = _PythonExtractor("module.py", "module", source)

    compared = 0
    for node in ast.walk(tree):
        expected = ast.get_source_segment(source, node)
        if expected:
            assert extractor._source_for(node) == expected
            compared += 1

    assert compared >= 10


def test_scan_file_honors_latin1_cookie_and_bom(tmp_path: Path) -> None:
    latin1 = tmp_path / "latin1.py"
    latin1.write_bytes(b'# coding: latin-1\ndef run():\n    return caf\xe9_factory("\xe9")\n')
    bom = tmp_path / "bom.py"
    bom.write_bytes(codecs.BOM_UTF8 + b'def run():\n    return make("caf\xc3\xa9")\n')

    latin1_file = _scan_file(tmp_path, latin1)
    bom_file = _scan_file(tmp_path, bom)

    assert latin1_file.parse_status == "ok"
    assert [(call.raw_callee, call.ordinal) for call in latin1_file.calls] == [("café_factory", 1)]
    assert bom_file.parse_status == "ok"
    assert [symbol.name for symbol in bom_file.symbols] == ["run"]
    assert [(call.raw_callee, call.ordinal) for call in bom_file.calls] == [("make", 1)]


@pytest.mark.parametrize(
    "payload",
    [
        b"def run():\n    return \xff\n",
        b"# coding: definitely-not-an-encoding\npass\n",
    ],
)
def test_scan_file_reports_invalid_source_encoding_without_replacement(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "invalid.py"
    path.write_bytes(payload)

    file = _scan_file(tmp_path, path)

    assert file.parse_status == "error"
    assert file.symbols == ()
    assert file.calls == ()
    assert file.diagnostics
    assert file.diagnostics[0].category == "syntax_error"
    assert "\ufffd" not in file.diagnostics[0].message


def test_sorted_calls_and_bindings_keep_frozen_ir_and_source_ordinals(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text(
        "def run(value: Thing):\n    later = make()\n    earlier = make()\n    outer(inner())\n",
        encoding="utf-8",
    )

    file = _scan_file(tmp_path, path)

    assert [(call.raw_callee, call.ordinal) for call in file.calls] == [
        ("make", 1),
        ("make", 2),
        ("outer", 3),
        ("inner", 4),
    ]
    assert [binding.target_name for binding in file.local_bindings] == [
        "value",
        "later",
        "earlier",
    ]
    with pytest.raises(FrozenInstanceError):
        file.calls[0].ordinal = 99  # type: ignore[misc]
