from __future__ import annotations

import pytest

from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    ModuleInitIR,
    ParseDiagnosticIR,
    RepositoryIR,
    SymbolIR,
)
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
        **_: object,
    ) -> list[dict[str, int]]:
        self.writes.append((query, params or {}))
        if self.write_results:
            return self.write_results.pop(0)
        return [{"deleted": 0}]


def _call(
    owner_qname: str,
    raw_callee: str,
    hint: str | None,
    *,
    receiver_kind: str = "none",
    ordinal: int,
) -> CallIR:
    return CallIR(
        owner_qname=owner_qname,
        raw_callee=raw_callee,
        callee_name=raw_callee.rsplit(".", maxsplit=1)[-1],
        callee_qname_hint=hint,
        receiver_kind=receiver_kind,  # type: ignore[arg-type]
        start_line=10,
        start_column=ordinal * 2,
        end_line=10,
        end_column=ordinal * 2 + len(raw_callee),
        ordinal=ordinal,
    )


def _repo(*, parse_error: bool = False) -> RepositoryIR:
    return RepositoryIR(
        repo_name="sample",
        commit="abc123",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=20,
                module_qname="worker",
                module_init=ModuleInitIR("worker.__module__", 1, 20),
                parse_status="error" if parse_error else "ok",
                diagnostics=(ParseDiagnosticIR("syntax_error", "error", 2, 4, "bad syntax"),)
                if parse_error
                else (),
                symbols=(
                    SymbolIR("type", "Worker", "worker.Worker", "class Worker", 1, 8),
                    SymbolIR(
                        "method",
                        "run",
                        "worker.Worker.run",
                        "def run(self)",
                        2,
                        6,
                        parent_qname="worker.Worker",
                    ),
                    SymbolIR("function", "build", "worker.build", "def build()", 10, 12),
                    SymbolIR("function", "target", "worker.target", "def target()", 14, 14),
                    SymbolIR("function", "target", "worker.target", "def target()", 16, 16),
                ),
                calls=(
                    _call("worker.build", "missing", "worker.missing", ordinal=1),
                    _call("worker.build", "factory()", None, receiver_kind="dynamic", ordinal=2),
                    _call("worker.build", "target", "worker.target", ordinal=3),
                    _call("worker.__module__", "run", "worker.Worker.run", ordinal=4),
                ),
            ),
        ),
    )


def _rows_for_query(client: FakeClient, needle: str) -> list[dict[str, object]]:
    return [
        row
        for query, params in client.writes
        if needle in query
        for row in params.get("rows", [])  # type: ignore[union-attr]
    ]


def test_load_repository_persists_module_init_parse_metadata_and_all_call_sites() -> None:
    client = FakeClient()

    result = load_repository(_repo(parse_error=True), replace=False, client=client, batch_size=2)  # type: ignore[arg-type]

    assert result["module_inits"] == 1
    assert result["call_sites"] == 4
    assert result["resolved_call_sites"] == 1
    assert result["callsite_statuses"] == {
        "ambiguous": 1,
        "dynamic": 1,
        "exact_local": 1,
        "unresolved": 1,
    }
    assert result["files_with_parse_errors"] == 1
    assert result["parse_diagnostics"] == 1
    assert result["batches"] > 1

    file_rows = _rows_for_query(client, "diagnostic_count")
    assert file_rows == [
        {
            "key": "sample@abc123:worker.py",
            "repo_key": "sample",
            "path": "worker.py",
            "language": "python",
            "loc": 20,
            "module_key": "sample@abc123:module:worker",
            "module_qname": "worker",
            "module_name": "worker",
            "parse_status": "error",
            "diagnostic_count": 1,
        }
    ]
    module_init_rows = _rows_for_query(client, "ModuleInit")
    assert module_init_rows[0]["key"] == "sample@abc123:worker.py:module-init"
    diagnostic_rows = _rows_for_query(client, "ParseDiagnostic")
    assert diagnostic_rows == [
        {
            "key": "sample@abc123:worker.py:diagnostic:1",
            "file_key": "sample@abc123:worker.py",
            "category": "syntax_error",
            "severity": "error",
            "line": 2,
            "column": 4,
            "message": "bad syntax",
        }
    ]
    callsite_rows = _rows_for_query(client, "MERGE (site:CallSite")
    assert {row["raw_callee"] for row in callsite_rows} == {"missing", "factory()", "target", "run"}
    assert len({row["key"] for row in callsite_rows}) == 4
    derived_rows = _rows_for_query(client, "rel:CALLS")
    assert derived_rows == [
        {
            "callsite_key": (
                "sample@abc123:worker.py:callsite:sample@abc123:worker.py:module-init:10:8:10:11:4"
            ),
            "caller_key": "sample@abc123:worker.py:module-init",
            "callee_key": "sample@abc123:worker.py:worker.Worker.run:2",
            "resolution": "exact_local",
            "line": 10,
            "column": 8,
        }
    ]
    exact_projection_rows = _rows_for_query(client, "rel:EXACT_CALLS")
    assert exact_projection_rows == derived_rows
    resolution_rows = _rows_for_query(client, "RESOLVES_TO")
    assert resolution_rows == [
        {
            "callsite_key": (
                "sample@abc123:worker.py:callsite:sample@abc123:worker.py:module-init:10:8:10:11:4"
            ),
            "caller_key": "sample@abc123:worker.py:module-init",
            "callee_key": "sample@abc123:worker.py:worker.Worker.run:2",
            "resolution": "exact_local",
            "line": 10,
            "column": 8,
        }
    ]
    assert any("HAS_CALLSITE" in query for query, _ in client.writes)


def test_load_repository_import_rows_use_a_non_nullable_relationship_key() -> None:
    client = FakeClient()
    repository = _repo()
    file = repository.files[0]
    repository = RepositoryIR(
        repo_name=repository.repo_name,
        commit=repository.commit,
        root_path=repository.root_path,
        files=(
            FileIR(
                path=file.path,
                language=file.language,
                loc=file.loc,
                module_qname=file.module_qname,
                module_init=file.module_init,
                imports=(
                    ImportIR("os", "os"),
                    ImportIR("pathlib", "Path", "P"),
                ),
            ),
        ),
    )

    load_repository(repository, replace=False, client=client)  # type: ignore[arg-type]

    import_query, import_params = next(
        (query, params) for query, params in client.writes if "rel:IMPORTS" in query
    )
    assert "MERGE (f)-[rel:IMPORTS {key: row.key}]->(m)" in import_query
    assert "alias: row.alias" not in import_query
    assert import_params["rows"] == [
        {
            "file_key": "sample@abc123:worker.py",
            "module_key": "sample@abc123:external-module:os",
            "module": "os",
            "name": "os",
            "alias": None,
            "key": "sample@abc123:worker.py:import:os:os:<none>",
        },
        {
            "file_key": "sample@abc123:worker.py",
            "module_key": "sample@abc123:external-module:pathlib",
            "module": "pathlib",
            "name": "Path",
            "alias": "P",
            "key": "sample@abc123:worker.py:import:pathlib:Path:P",
        },
    ]


def test_load_repository_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        load_repository(_repo(), replace=False, client=FakeClient(), batch_size=0)  # type: ignore[arg-type]


def test_delete_repository_by_name_uses_key_prefix_delete() -> None:
    client = FakeClient(write_results=[[{"deleted": 7}]])

    deleted = delete_repository_by_name("sample", client=client)  # type: ignore[arg-type]

    assert deleted == 7
    query, params = client.writes[0]
    assert "n.key STARTS WITH prefix" in query
    assert params == {"repo_name": "sample"}


def test_replace_load_deletes_before_reindexing() -> None:
    client = FakeClient(write_results=[[{"deleted": 1}]])

    load_repository(_repo(), replace=True, client=client)  # type: ignore[arg-type]

    assert "n.key STARTS WITH prefix" in client.writes[0][0]
    assert "MERGE (r:Repository" in client.writes[1][0]
