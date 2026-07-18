from __future__ import annotations

from pathlib import Path

import pytest

from codekg.ingest import scan_repository
from codekg.ir import ModuleInitIR, ParseDiagnosticIR

pytestmark = pytest.mark.unit

_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "structural_phase1"


def _scan_fixture_repository():
    return scan_repository(_FIXTURE_ROOT)


def _file(repository, path: str):
    return next(item for item in repository.files if item.path == path)


def test_module_scope_is_an_explicit_module_init_and_owns_top_level_calls() -> None:
    repository = _scan_fixture_repository()
    source_file = _file(repository, "module_init.py")

    assert source_file.module_init == ModuleInitIR(
        qname="module_init.__module__",
        start_line=1,
        end_line=5,
    )
    assert [symbol.qname for symbol in source_file.symbols] == ["module_init.bootstrap"]

    call = source_file.calls[0]
    assert call.owner_qname == "module_init.__module__"
    assert call.raw_callee == "bootstrap"
    assert call.receiver_kind == "none"
    assert (call.start_line, call.start_column, call.end_line, call.end_column) == (5, 0, 5, 11)
    assert call.ordinal == 1


def test_call_sites_keep_individual_source_spans_and_ordinals_on_one_line() -> None:
    repository = _scan_fixture_repository()
    source_file = _file(repository, "same_line_calls.py")
    calls = [call for call in source_file.calls if call.owner_qname == "same_line_calls.run"]

    assert [
        (
            call.raw_callee,
            call.receiver_kind,
            (call.start_line, call.start_column, call.end_line, call.end_column),
            call.ordinal,
        )
        for call in calls
    ] == [
        ("first", "none", (10, 4, 10, 11), 1),
        ("second", "none", (10, 13, 10, 21), 2),
    ]


def test_call_site_contract_captures_receiver_forms_without_discarding_dynamic_calls() -> None:
    repository = _scan_fixture_repository()
    source_file = _file(repository, "call_forms.py")
    calls = {(call.owner_qname, call.raw_callee): call for call in source_file.calls}

    expected = {
        ("call_forms.Child.via_cls", "cls.class_action"): ("cls", (24, 8, 24, 26)),
        ("call_forms.Child.run", "bare_target"): ("none", (27, 8, 27, 21)),
        ("call_forms.Child.run", "self.instance"): ("self", (28, 8, 28, 23)),
        ("call_forms.Child.run", "super().save"): ("super", (29, 8, 29, 22)),
        ("call_forms.Child.run", "client.send"): ("attribute", (30, 8, 30, 21)),
        ("call_forms.Child.run", "factory().run"): ("dynamic", (31, 8, 31, 23)),
    }
    for identity, (receiver_kind, span) in expected.items():
        call = calls[identity]
        assert call.receiver_kind == receiver_kind
        assert (call.start_line, call.start_column, call.end_line, call.end_column) == span
        assert call.ordinal >= 0

    assert ("call_forms.Child.run", "super") in calls
    assert ("call_forms.Child.run", "factory") in calls


def test_syntax_errors_are_reported_without_hiding_valid_neighbor_files() -> None:
    repository = _scan_fixture_repository()
    broken = _file(repository, "syntax_error.py")
    valid = _file(repository, "valid_neighbor.py")

    assert broken.diagnostics == (
        ParseDiagnosticIR(
            category="syntax_error",
            severity="error",
            line=1,
            column=12,
            message="invalid syntax",
        ),
    )
    assert broken.parse_status == "error"
    assert broken.symbols == ()
    assert broken.calls == ()
    assert valid.parse_status == "ok"
    assert valid.diagnostics == ()
    assert [symbol.qname for symbol in valid.symbols] == ["valid_neighbor.still_indexed"]


def test_lexical_scopes_and_definition_time_calls_have_correct_owners() -> None:
    repository = _scan_fixture_repository()
    source_file = _file(repository, "scopes_and_definition_time.py")

    assert [(symbol.kind, symbol.qname, symbol.parent_qname) for symbol in source_file.symbols] == [
        ("function", "scopes_and_definition_time.helper", None),
        ("function", "scopes_and_definition_time.decorate", None),
        ("function", "scopes_and_definition_time.outer", None),
        ("function", "scopes_and_definition_time.outer.<locals>.middle", None),
        ("type", "scopes_and_definition_time.outer.<locals>.middle.<locals>.Inner", None),
        (
            "method",
            "scopes_and_definition_time.outer.<locals>.middle.<locals>.Inner.method",
            "scopes_and_definition_time.outer.<locals>.middle.<locals>.Inner",
        ),
        (
            "function",
            "scopes_and_definition_time.outer.<locals>.middle.<locals>.deepest",
            None,
        ),
    ]

    helper_calls = [call for call in source_file.calls if call.raw_callee == "helper"]
    assert [call.owner_qname for call in helper_calls] == [
        "scopes_and_definition_time.__module__",
        "scopes_and_definition_time.__module__",
        "scopes_and_definition_time.__module__",
        "scopes_and_definition_time.outer.<locals>.middle.<locals>.Inner.method",
        "scopes_and_definition_time.outer.<locals>.middle.<locals>.deepest",
    ]
