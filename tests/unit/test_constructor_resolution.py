from __future__ import annotations

from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    InheritanceIR,
    ModuleInitIR,
    RepositoryIR,
    SymbolIR,
)
from codekg.resolver import SymbolRef, resolve_call_sites


def _call(name: str, ordinal: int) -> CallIR:
    return CallIR(name, name, name, "cases." + name, "none", 1, ordinal, 1, ordinal + 2, ordinal)


def _resolve(
    *, symbols: tuple[SymbolIR, ...], calls: tuple[CallIR, ...], imports=(), inheritance=()
):
    file = FileIR(
        "cases.py",
        "python",
        20,
        "cases",
        ModuleInitIR("cases.__module__", 1, 20),
        imports=imports,
        symbols=symbols,
        inheritance=inheritance,
        calls=calls,
    )
    repo = RepositoryIR("repo", "commit", "/tmp/repo", (file,))
    owner = SymbolRef("owner", "cases.__module__", "cases.py", "module_init")
    refs = [
        SymbolRef(
            "key:" + symbol.qname + ":" + str(symbol.start_line),
            symbol.qname,
            "cases.py",
            symbol.kind,
            symbol.parent_qname,
        )
        for symbol in symbols
    ]
    return resolve_call_sites(
        repo,
        owners_by_file_qname={("cases.py", "cases.__module__"): (owner,)},
        callables=(ref for ref in refs if ref.kind in {"function", "method"}),
        types=(ref for ref in refs if ref.kind == "type"),
    )


def test_constructor_resolves_local_and_direct_init_without_generic_type_call():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Worker", "cases.Worker", "class Worker", 2, 8),
            SymbolIR(
                "method",
                "__init__",
                "cases.Worker.__init__",
                "def __init__",
                3,
                4,
                parent_qname="cases.Worker",
            ),
        ),
        calls=(_call("Worker", 1),),
    )
    result = results[0]
    assert result.status == "constructor_exact_local"
    assert result.construction_target_key == "key:cases.Worker:2"
    assert result.initializer_target_key == "key:cases.Worker.__init__:3"
    assert result.initializer_status == "exact_local"


def test_constructor_uses_imported_type_and_inherited_init():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Worker", "helpers.Worker", "class Worker", 2, 8),
            SymbolIR("type", "Child", "cases.Child", "class Child", 2, 8),
            SymbolIR(
                "method",
                "__init__",
                "helpers.Worker.__init__",
                "def __init__",
                3,
                4,
                parent_qname="helpers.Worker",
            ),
        ),
        calls=(_call("Worker", 1),),
        imports=(ImportIR("helpers", "Worker"),),
    )
    assert results[0].status == "constructor_exact_import"
    assert results[0].initializer_status == "exact_local"


def test_constructor_uses_c3_inherited_init():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Base", "cases.Base", "class Base", 2, 8),
            SymbolIR("type", "Child", "cases.Child", "class Child", 10, 16),
            SymbolIR(
                "method",
                "__init__",
                "cases.Base.__init__",
                "def __init__",
                3,
                4,
                parent_qname="cases.Base",
            ),
        ),
        calls=(_call("Child", 1),),
        inheritance=(InheritanceIR("cases.Child", "Base", "cases.Base"),),
    )
    assert results[0].initializer_target_key == "key:cases.Base.__init__:3"
    assert results[0].initializer_status == "inherited_method"


def test_constructor_without_init_keeps_construction_and_ordinary_function_is_unchanged():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Worker", "cases.Worker", "class Worker", 2, 8),
            SymbolIR("function", "build", "cases.build", "def build", 10, 12),
        ),
        calls=(_call("Worker", 1), _call("build", 2)),
    )
    assert results[0].is_constructor
    assert results[0].initializer_target_key is None
    assert results[1].status == "exact_local"


def test_constructor_keeps_type_candidates_for_ambiguity():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Worker", "cases.Worker", "class Worker", 2, 8),
            SymbolIR("type", "Worker", "cases.Worker", "class Worker", 10, 12),
        ),
        calls=(_call("Worker", 1),),
    )
    assert results[0].status == "constructor_ambiguous"
    assert len(results[0].candidate_keys) == 2


def test_constructor_keeps_construction_when_init_is_ambiguous():
    results = _resolve(
        symbols=(
            SymbolIR("type", "Worker", "cases.Worker", "class Worker", 2, 8),
            SymbolIR(
                "method",
                "__init__",
                "cases.Worker.__init__",
                "def __init__",
                3,
                4,
                parent_qname="cases.Worker",
            ),
            SymbolIR(
                "method",
                "__init__",
                "cases.Worker.__init__",
                "def __init__",
                5,
                6,
                parent_qname="cases.Worker",
            ),
        ),
        calls=(_call("Worker", 1),),
    )
    assert results[0].is_construction_exact
    assert results[0].initializer_target_key is None
    assert len(results[0].initializer_candidate_keys) == 2
