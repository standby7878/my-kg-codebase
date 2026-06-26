from __future__ import annotations

import pytest

from codekg.ir import CallIR, FileIR, InheritanceIR, RepositoryIR, SymbolIR
from codekg.loader import delete_repository_by_name, load_repository

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, write_results: list[list[dict[str, int]]] | None = None) -> None:
        self.writes: list[tuple[str, dict[str, object]]] = []
        self.write_results = list(write_results or [])

    def execute_write(
        self,
        query: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, int]]:
        self.writes.append((query, params or {}))
        if self.write_results:
            return self.write_results.pop(0)
        return [{"deleted": 0}]


def test_load_repository_batches_files_and_symbols() -> None:
    repo = RepositoryIR(
        repo_name="sample",
        commit="abc123",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=10,
                module_qname="worker",
                symbols=(
                    SymbolIR(
                        kind="type",
                        name="BaseWorker",
                        qname="worker.BaseWorker",
                        signature="class BaseWorker",
                        start_line=1,
                        end_line=1,
                    ),
                    SymbolIR(
                        kind="type",
                        name="Worker",
                        qname="worker.Worker",
                        signature="class Worker",
                        start_line=2,
                        end_line=4,
                    ),
                    SymbolIR(
                        kind="method",
                        name="run",
                        qname="worker.Worker.run",
                        signature="def run(self)",
                        start_line=3,
                        end_line=3,
                        parent_qname="worker.Worker",
                    ),
                    SymbolIR(
                        kind="function",
                        name="build",
                        qname="worker.build",
                        signature="def build()",
                        start_line=5,
                        end_line=6,
                    ),
                ),
                inheritance=(
                    InheritanceIR(
                        type_qname="worker.Worker",
                        base_name="BaseWorker",
                        base_qname="worker.BaseWorker",
                    ),
                ),
                calls=(
                    CallIR(
                        caller_qname="worker.build",
                        callee_name="run",
                        callee_qname="worker.Worker.run",
                        line=6,
                    ),
                ),
            ),
        ),
    )
    client = FakeClient()

    result = load_repository(repo, replace=False, client=client)  # type: ignore[arg-type]

    assert result["nodes"] == 6
    assert result["inherits"] == 1
    assert result["calls"] == 1
    file_params = client.writes[1][1]
    assert file_params["rows"][0]["path"] == "worker.py"  # type: ignore[index]
    has_method_params = client.writes[5][1]
    assert has_method_params["rows"] == [  # type: ignore[index]
        {
            "type_key": "sample@abc123:worker.py:worker.Worker:2",
            "method_key": "sample@abc123:worker.py:worker.Worker.run:3",
        }
    ]
    inherits_params = client.writes[7][1]
    assert inherits_params["rows"] == [  # type: ignore[index]
        {
            "child_key": "sample@abc123:worker.py:worker.Worker:2",
            "parent_key": "sample@abc123:worker.py:worker.BaseWorker:1",
        }
    ]
    call_params = client.writes[-1][1]
    assert call_params["rows"][0]["resolution"] == "heuristic"  # type: ignore[index]


def test_self_calls_resolve_only_within_callers_class() -> None:
    repo = RepositoryIR(
        repo_name="sample",
        commit="abc123",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=20,
                module_qname="worker",
                symbols=(
                    SymbolIR(
                        kind="type",
                        name="Worker",
                        qname="worker.Worker",
                        signature="class Worker",
                        start_line=1,
                        end_line=6,
                    ),
                    SymbolIR(
                        kind="method",
                        name="run",
                        qname="worker.Worker.run",
                        signature="def run(self)",
                        start_line=2,
                        end_line=3,
                        parent_qname="worker.Worker",
                    ),
                    SymbolIR(
                        kind="type",
                        name="Other",
                        qname="worker.Other",
                        signature="class Other",
                        start_line=8,
                        end_line=12,
                    ),
                    SymbolIR(
                        kind="method",
                        name="save",
                        qname="worker.Other.save",
                        signature="def save(self)",
                        start_line=9,
                        end_line=10,
                        parent_qname="worker.Other",
                    ),
                ),
                calls=(
                    CallIR(
                        caller_qname="worker.Worker.run",
                        callee_name="save",
                        callee_qname="worker.Worker.save",
                        receiver="self",
                        line=3,
                    ),
                ),
            ),
        ),
    )
    client = FakeClient()

    result = load_repository(repo, replace=False, client=client)  # type: ignore[arg-type]

    assert result["calls"] == 0
    call_params = client.writes[-1][1]
    assert call_params["rows"] == []  # type: ignore[index]


def test_delete_repository_by_name_uses_key_prefix_delete() -> None:
    client = FakeClient(write_results=[[{"deleted": 7}]])

    deleted = delete_repository_by_name("sample", client=client)  # type: ignore[arg-type]

    assert deleted == 7
    query, params = client.writes[0]
    assert "CONTAINS*0..2" not in query
    assert "n.key STARTS WITH prefix" in query
    assert params == {"repo_name": "sample"}


def test_replace_load_deletes_before_reindexing() -> None:
    repo = RepositoryIR(
        repo_name="sample",
        commit="abc123",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=1,
                module_qname="worker",
                symbols=(),
            ),
        ),
    )
    client = FakeClient(write_results=[[{"deleted": 1}]])

    result = load_repository(repo, replace=True, client=client)  # type: ignore[arg-type]

    assert result["nodes"] == 2
    assert "n.key STARTS WITH prefix" in client.writes[0][0]
    assert "MERGE (r:Repository" in client.writes[1][0]
