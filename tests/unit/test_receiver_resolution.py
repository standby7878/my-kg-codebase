from __future__ import annotations

from codekg.ir import CallIR, FileIR, RepositoryIR, SymbolIR
from codekg.resolver import SymbolRef, resolve_call_sites


def _call(raw_callee: str, receiver_kind: str, ordinal: int) -> CallIR:
    return CallIR(
        owner_qname="cases.Worker.run",
        raw_callee=raw_callee,
        callee_name="connect",
        callee_qname_hint="cases.Worker.connect",
        receiver_kind=receiver_kind,  # type: ignore[arg-type]
        start_line=10 + ordinal,
        start_column=8,
        end_line=10 + ordinal,
        end_column=8 + len(raw_callee),
        ordinal=ordinal,
    )


def test_receiver_resolution_rejects_chains_mislabeled_as_self_direct() -> None:
    symbols = (
        SymbolIR("type", "Worker", "cases.Worker", "class Worker", 1, 20),
        SymbolIR(
            "method",
            "connect",
            "cases.Worker.connect",
            "def connect(self)",
            2,
            3,
            parent_qname="cases.Worker",
        ),
        SymbolIR(
            "method",
            "run",
            "cases.Worker.run",
            "def run(self)",
            10,
            20,
            parent_qname="cases.Worker",
        ),
    )
    file = FileIR(
        path="cases.py",
        language="python",
        loc=20,
        module_qname="cases",
        symbols=symbols,
        calls=(
            _call("self.connect", "self", 1),
            _call("self.pool.connect", "self", 2),
            _call("cls.connect", "cls", 3),
            _call("cls.factory.connect", "cls", 4),
        ),
    )
    repository = RepositoryIR("repo", "commit", "/tmp/repo", (file,))
    type_ref = SymbolRef("type", "cases.Worker", "cases.py", "type")
    connect_ref = SymbolRef("connect", "cases.Worker.connect", "cases.py", "method", "cases.Worker")
    run_ref = SymbolRef("run", "cases.Worker.run", "cases.py", "method", "cases.Worker")

    self_direct, self_chained, cls_direct, cls_chained = resolve_call_sites(
        repository,
        owners_by_file_qname={("cases.py", "cases.Worker.run"): (run_ref,)},
        callables=(connect_ref, run_ref),
        types=(type_ref,),
    )

    assert self_direct.status == "self_direct"
    assert self_direct.target_key == "connect"
    assert cls_direct.status == "cls_direct"
    assert cls_direct.target_key == "connect"
    for chained in (self_chained, cls_chained):
        assert chained.status == "dynamic"
        assert chained.target_key is None
        assert chained.candidate_keys == ()
