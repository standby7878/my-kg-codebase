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
    return CallIR(
        "cases.__module__",
        name,
        name,
        "cases." + name,
        "none",
        1,
        ordinal,
        1,
        ordinal + 2,
        ordinal,
    )


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


def _resolve_repository(repo: RepositoryIR):
    refs = [
        SymbolRef(
            "key:" + file.path + ":" + symbol.qname + ":" + str(symbol.start_line),
            symbol.qname,
            file.path,
            symbol.kind,
            symbol.parent_qname,
            symbol.return_annotation,
        )
        for file in repo.files
        for symbol in file.symbols
    ]
    owners = {(ref.path, ref.qname): (ref,) for ref in refs if ref.kind in {"function", "method"}}
    return resolve_call_sites(
        repo,
        owners_by_file_qname=owners,
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


def test_local_receiver_flow_resolves_requests_constructor_alias_and_factory(tmp_path):
    from codekg.ingest import scan_repository

    package = tmp_path / "src" / "requests"
    package.mkdir(parents=True)
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "class PreparedRequest:\n"
        "    def prepare(self):\n"
        "        pass\n"
        "\n"
        "def make_prepared() -> PreparedRequest:\n"
        "    return PreparedRequest()\n",
        encoding="utf-8",
    )
    (package / "sessions.py").write_text(
        "from .models import PreparedRequest as PR, make_prepared\n"
        "\n"
        "class Session:\n"
        "    def prepare_request(self, request):\n"
        "        p = PR()\n"
        "        p.prepare()\n"
        "        alias = p\n"
        "        alias.prepare()\n"
        "\n"
        "    def factory_request(self):\n"
        "        prepared = make_prepared()\n"
        "        prepared.prepare()\n"
        "\n"
        "    def annotated_request(self, prepared: PR):\n"
        "        prepared.prepare()\n"
        "\n"
        "    def annotated_local(self):\n"
        "        prepared: PR = PR()\n"
        "        prepared.prepare()\n",
        encoding="utf-8",
    )

    results = _resolve_repository(scan_repository(tmp_path))
    by_owner_and_call = {
        (result.call.owner_qname, result.call.raw_callee): result for result in results
    }

    expected = "key:src/requests/models.py:src.requests.models.PreparedRequest.prepare:2"
    keys = (
        ("src.requests.sessions.Session.prepare_request", "p.prepare"),
        ("src.requests.sessions.Session.prepare_request", "alias.prepare"),
        ("src.requests.sessions.Session.factory_request", "prepared.prepare"),
        ("src.requests.sessions.Session.annotated_request", "prepared.prepare"),
        ("src.requests.sessions.Session.annotated_local", "prepared.prepare"),
    )
    assert all(by_owner_and_call[key].target_key == expected for key in keys)
    assert all(by_owner_and_call[key].status == "local_receiver" for key in keys)


def test_local_receiver_flow_invalidates_unknown_and_guarded_bindings(tmp_path):
    from codekg.ingest import scan_repository

    (tmp_path / "cases.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def unknown_reassignment(value):\n"
        "    worker = Worker()\n"
        "    worker = value\n"
        "    worker.run()\n"
        "\n"
        "def guarded_reassignment(flag, value):\n"
        "    worker = Worker()\n"
        "    if flag:\n"
        "        worker = value\n"
        "    worker.run()\n"
        "\n"
        "def branch_only(flag):\n"
        "    if flag:\n"
        "        worker = Worker()\n"
        "    worker.run()\n"
        "\n"
        "def destructured(value):\n"
        "    worker = Worker()\n"
        "    worker, other = value\n"
        "    worker.run()\n"
        "\n"
        "def nested_attribute():\n"
        "    worker = Worker()\n"
        "    worker.child.run()\n"
        "\n"
        "def make_worker():\n"
        "    return Worker()\n"
        "\n"
        "def untyped_factory():\n"
        "    worker = make_worker()\n"
        "    worker.run()\n"
        "\n"
        "def container_receiver():\n"
        "    worker = [Worker()][0]\n"
        "    worker.run()\n"
        "\n"
        "def union_receiver(worker: Worker | None):\n"
        "    worker.run()\n"
        "\n"
        "def closure_receiver():\n"
        "    worker = Worker()\n"
        "    def inner():\n"
        "        worker.run()\n"
        "    inner()\n",
        encoding="utf-8",
    )

    results = _resolve_repository(scan_repository(tmp_path))
    receiver_calls = [
        result for result in results if result.call.raw_callee in {"worker.run", "worker.child.run"}
    ]

    assert len(receiver_calls) == 9
    assert all(not result.is_exact for result in receiver_calls)
    assert all(result.target_key is None for result in receiver_calls)


def test_comprehension_targets_shadow_outer_local_receiver_without_leaking(tmp_path):
    from codekg.ingest import scan_repository

    (tmp_path / "cases.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def use(items):\n"
        "    worker = Worker()\n"
        "    [worker.run() for worker in items]\n"
        "    {worker.run() for worker in items}\n"
        "    {worker: worker.run() for worker in items}\n"
        "    tuple(worker.run() for worker in items)\n"
        "    worker.run()\n"
        "\n"
        "def nested(items):\n"
        "    [(outer.run(), inner.run()) for outer in items for inner in outer]\n",
        encoding="utf-8",
    )

    results = _resolve_repository(scan_repository(tmp_path))
    worker_calls = [
        result
        for result in results
        if result.call.owner_qname == "cases.use" and result.call.raw_callee == "worker.run"
    ]
    nested_calls = [
        result
        for result in results
        if result.call.owner_qname == "cases.nested"
        and result.call.raw_callee in {"outer.run", "inner.run"}
    ]

    assert len(worker_calls) == 5
    assert all(not result.is_exact for result in worker_calls[:-1])
    assert all(result.target_key is None for result in worker_calls[:-1])
    assert worker_calls[-1].status == "local_receiver"
    assert worker_calls[-1].target_key is not None
    assert len(nested_calls) == 2
    assert all(not result.is_exact for result in nested_calls)
    assert all(result.target_key is None for result in nested_calls)


def test_lambda_body_calls_are_omitted_without_invalidating_outer_receiver(tmp_path):
    from codekg.ingest import scan_repository

    (tmp_path / "cases.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def make_worker():\n"
        "    return Worker()\n"
        "\n"
        "def use():\n"
        "    worker = Worker()\n"
        "    callback = lambda worker, /, positional, *args, keyword_only=make_worker(), "
        "**kwargs: (\n"
        "        worker.run(), positional.run(), args[0].run(), keyword_only.run(), "
        "kwargs['worker'].run()\n"
        "    )\n"
        "    nested = lambda worker: (lambda worker: worker.run())(worker)\n"
        "    shadow = lambda: (worker := make_worker())\n"
        "    worker.run()\n",
        encoding="utf-8",
    )

    repository = scan_repository(tmp_path)
    cases = next(file for file in repository.files if file.path == "cases.py")
    results = _resolve_repository(repository)
    outer_call = next(
        result
        for result in results
        if result.call.owner_qname == "cases.use" and result.call.raw_callee == "worker.run"
    )

    assert [call.raw_callee for call in cases.calls if call.owner_qname == "cases.use"] == [
        "Worker",
        "make_worker",
        "worker.run",
    ]
    assert outer_call.status == "local_receiver"
    assert outer_call.target_key is not None


def test_loop_backedges_invalidate_rebound_receivers_inside_repeating_regions(tmp_path):
    from codekg.ingest import scan_repository

    (tmp_path / "cases.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def for_loop(items):\n"
        "    worker = Worker()\n"
        "    for item in items:\n"
        "        worker.run()\n"
        "        worker = item\n"
        "\n"
        "async def async_for_loop(items):\n"
        "    worker = Worker()\n"
        "    async for item in items:\n"
        "        worker.run()\n"
        "        worker = item\n"
        "\n"
        "def while_loop(condition, value):\n"
        "    worker = Worker()\n"
        "    while worker.run():\n"
        "        worker = value\n"
        "\n"
        "def while_test(value):\n"
        "    worker = Worker()\n"
        "    while worker.run() and (worker := value):\n"
        "        pass\n"
        "\n"
        "def comprehension(items):\n"
        "    worker = Worker()\n"
        "    [(worker.run(), (worker := item)) for item in items]\n"
        "\n"
        "def stable_loop(items):\n"
        "    worker = Worker()\n"
        "    for item in items:\n"
        "        consume(item)\n"
        "    worker.run()\n",
        encoding="utf-8",
    )

    results = _resolve_repository(scan_repository(tmp_path))
    rebound_owners = {
        "cases.for_loop",
        "cases.async_for_loop",
        "cases.while_loop",
        "cases.while_test",
        "cases.comprehension",
    }
    rebound_calls = [
        result
        for result in results
        if result.call.owner_qname in rebound_owners and result.call.raw_callee == "worker.run"
    ]
    stable_call = next(
        result
        for result in results
        if result.call.owner_qname == "cases.stable_loop" and result.call.raw_callee == "worker.run"
    )

    assert len(rebound_calls) == 5
    assert all(not result.is_exact for result in rebound_calls)
    assert all(result.target_key is None for result in rebound_calls)
    assert stable_call.status == "local_receiver"
    assert stable_call.target_key is not None


def test_local_receiver_flow_rejects_ambiguous_constructor_type(tmp_path):
    from codekg.ingest import scan_repository

    (tmp_path / "cases.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def use():\n"
        "    worker = Worker()\n"
        "    worker.run()\n",
        encoding="utf-8",
    )

    results = _resolve_repository(scan_repository(tmp_path))
    receiver = next(result for result in results if result.call.raw_callee == "worker.run")

    assert receiver.status == "dynamic"
    assert receiver.target_key is None
