from __future__ import annotations

from pathlib import Path

import pytest

from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    InheritanceIR,
    LocalBindingIR,
    RepositoryIR,
    SymbolIR,
)
from codekg.resolver import (
    SqliteResolverIndex,
    SymbolRef,
    _Resolver,
    resolve_call_sites,
)

pytestmark = pytest.mark.unit


def _repository_files() -> tuple[FileIR, ...]:
    return (
        FileIR(
            path="base.py",
            language="python",
            loc=10,
            module_qname="base",
            symbols=(
                SymbolIR("type", "Base", "base.Base", "class Base", 1, 8),
                SymbolIR(
                    "method",
                    "run",
                    "base.Base.run",
                    "def run(self)",
                    2,
                    3,
                    parent_qname="base.Base",
                ),
            ),
        ),
        FileIR(
            path="child.py",
            language="python",
            loc=12,
            module_qname="child",
            symbols=(
                SymbolIR("type", "Child", "child.Child", "class Child", 1, 10),
                SymbolIR(
                    "method",
                    "call_base",
                    "child.Child.call_base",
                    "def call_base(self)",
                    2,
                    3,
                    parent_qname="child.Child",
                ),
            ),
            inheritance=(InheritanceIR("child.Child", "Base", "base.Base"),),
            calls=(
                CallIR(
                    "child.Child.call_base",
                    "self.run",
                    "run",
                    "child.Child.run",
                    "self",
                    3,
                    8,
                    3,
                    16,
                    1,
                ),
            ),
        ),
        FileIR(
            path="factory.py",
            language="python",
            loc=5,
            module_qname="factory",
            symbols=(
                SymbolIR(
                    "function",
                    "make",
                    "factory.make",
                    "def make() -> models.Worker",
                    1,
                    2,
                    return_annotation="models.Worker",
                ),
            ),
        ),
        FileIR(
            path="models.py",
            language="python",
            loc=8,
            module_qname="models",
            symbols=(
                SymbolIR("type", "Worker", "models.Worker", "class Worker", 1, 5),
                SymbolIR(
                    "method",
                    "run",
                    "models.Worker.run",
                    "def run(self)",
                    2,
                    3,
                    parent_qname="models.Worker",
                ),
            ),
        ),
        FileIR(
            path="caller.py",
            language="python",
            loc=10,
            module_qname="caller",
            imports=(ImportIR("factory", "make"),),
            symbols=(
                SymbolIR(
                    "function",
                    "use",
                    "caller.use",
                    "def use()",
                    1,
                    5,
                ),
            ),
            calls=(
                CallIR(
                    "caller.use",
                    "worker.run",
                    "run",
                    "models.Worker.run",
                    "attribute",
                    3,
                    4,
                    3,
                    14,
                    1,
                ),
            ),
            local_bindings=(
                LocalBindingIR(
                    "caller.use",
                    "worker",
                    "call",
                    "make",
                    "factory.make",
                    None,
                    2,
                    4,
                ),
            ),
        ),
    )


def _refs(
    files: tuple[FileIR, ...],
) -> tuple[
    dict[tuple[str, str], tuple[SymbolRef, ...]],
    tuple[SymbolRef, ...],
    tuple[SymbolRef, ...],
]:
    refs = tuple(
        SymbolRef(
            f"memory:{file.path}:{symbol.qname}:{symbol.start_line}",
            symbol.qname,
            file.path,
            symbol.kind,
            symbol.parent_qname,
            symbol.return_annotation,
        )
        for file in files
        for symbol in file.symbols
    )
    owners = {
        (ref.path, ref.qname): (ref,)
        for ref in refs
        if ref.kind in {"function", "method"}
    }
    return owners, tuple(ref for ref in refs if ref.kind in {"function", "method"}), tuple(
        ref for ref in refs if ref.kind == "type"
    )


def test_registry_copy_is_sql_only_and_preserves_cross_file_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _repository_files()
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    create_spool(first, files[:2])
    create_spool(second, files[2:])
    registry = tmp_path / "registry.sqlite"

    def fail_reconstruction(_data: object) -> object:
        raise AssertionError("registry copy must not reconstruct FileIR")

    monkeypatch.setattr("codekg.bulk_spool._file_from_payload", fail_reconstruction)
    build_registry(registry, [first, second], repo_prefix="repo@commit")

    owners, callables, types = _refs(files)
    in_memory = resolve_call_sites(
        RepositoryIR("repo", "commit", "/repo", files),
        owners_by_file_qname=owners,
        callables=callables,
        types=types,
    )

    backend = SqliteResolverIndex(str(registry))
    try:
        sqlite_resolver = _Resolver(backend)
        from_sqlite = tuple(
            sqlite_resolver.resolve(file, call)
            for file in backend.files()
            for call in file.calls
        )
        assert [(item.status, item.call.raw_callee) for item in from_sqlite] == [
            (item.status, item.call.raw_callee) for item in in_memory
        ]
        assert from_sqlite[0].status == "inherited_method"
        assert from_sqlite[1].status == "local_receiver"
        assert backend.file("caller.py").imports == (ImportIR("factory", "make"),)
    finally:
        backend.close()


def test_registry_duplicate_qnames_keep_distinct_keys(tmp_path: Path) -> None:
    files = (
        FileIR(
            path="one.py",
            language="python",
            loc=1,
            module_qname="one",
            symbols=(SymbolIR("function", "same", "shared.same", "def same", 1, 1),),
        ),
        FileIR(
            path="two.py",
            language="python",
            loc=1,
            module_qname="two",
            symbols=(SymbolIR("function", "same", "shared.same", "def same", 1, 1),),
        ),
    )
    spool = tmp_path / "spool.sqlite"
    registry = tmp_path / "registry.sqlite"
    create_spool(spool, files)
    build_registry(registry, [spool], repo_prefix="repo@commit")

    backend = SqliteResolverIndex(str(registry))
    try:
        refs = backend.callables("shared.same")
        assert len(refs) == 2
        assert [ref.key for ref in refs] == [
            "repo@commit:one.py:shared.same:1",
            "repo@commit:two.py:shared.same:1",
        ]
    finally:
        backend.close()
