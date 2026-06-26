from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ir import CallIR, FileIR, InheritanceIR, RepositoryIR, SymbolIR
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.queries.code import find_callees, find_callers, find_dead_code, trace_call_path
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def _sample_repo(*, commit: str, module_qname: str) -> RepositoryIR:
    module_name = module_qname.rsplit(".", maxsplit=1)[-1]
    return RepositoryIR(
        repo_name="sample",
        commit=commit,
        root_path="/repos/sample",
        files=(
            FileIR(
                path=f"{module_name}.py",
                language="python",
                loc=20,
                module_qname=module_qname,
                symbols=(
                    SymbolIR(
                        kind="function",
                        name="<module>",
                        qname=f"{module_qname}.__module__",
                        signature="<module>",
                        start_line=1,
                        end_line=1,
                    ),
                    SymbolIR(
                        kind="type",
                        name="BaseWorker",
                        qname=f"{module_qname}.BaseWorker",
                        signature="class BaseWorker",
                        start_line=1,
                        end_line=1,
                    ),
                    SymbolIR(
                        kind="type",
                        name="Worker",
                        qname=f"{module_qname}.Worker",
                        signature="class Worker",
                        start_line=3,
                        end_line=8,
                    ),
                    SymbolIR(
                        kind="method",
                        name="run",
                        qname=f"{module_qname}.Worker.run",
                        signature="def run(self)",
                        start_line=4,
                        end_line=5,
                        parent_qname=f"{module_qname}.Worker",
                    ),
                    SymbolIR(
                        kind="function",
                        name="build",
                        qname=f"{module_qname}.build",
                        signature="def build()",
                        start_line=10,
                        end_line=11,
                    ),
                ),
                inheritance=(
                    InheritanceIR(
                        type_qname=f"{module_qname}.Worker",
                        base_name="BaseWorker",
                        base_qname=f"{module_qname}.BaseWorker",
                    ),
                ),
                calls=(
                    CallIR(
                        caller_qname=f"{module_qname}.__module__",
                        callee_name="build",
                        callee_qname=f"{module_qname}.build",
                        line=13,
                    ),
                    CallIR(
                        caller_qname=f"{module_qname}.Worker.run",
                        callee_name="missing",
                        callee_qname=f"{module_qname}.Worker.missing",
                        receiver="self",
                        line=5,
                    ),
                ),
            ),
        ),
    )


def test_loader_writes_python_relationships_to_neo4j() -> None:
    repo = _sample_repo(commit="abc123", module_qname="worker")

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            result = load_repository(repo, replace=False, client=client)
            repeated = load_repository(repo, replace=False, client=client)
            before_rows = client.execute_read(
                """
                MATCH (:Type {qname: 'worker.Worker'})
                    -[:INHERITS]->(:Type {qname: 'worker.BaseWorker'})
                WITH count(*) AS inherits
                MATCH (:Type {qname: 'worker.Worker'})
                    -[:HAS_METHOD]->(:Method {qname: 'worker.Worker.run'})
                WITH inherits, count(*) AS has_method
                MATCH (:Function {qname: 'worker.__module__'})
                    -[:CALLS]->(:Function {qname: 'worker.build'})
                WITH inherits, has_method, count(*) AS module_calls
                OPTIONAL MATCH (:Method {qname: 'worker.Worker.run'})-[rel:CALLS]->()
                RETURN inherits, has_method, module_calls, count(rel) AS bogus_self_calls
                """
            )
            callers = find_callers("worker.build", client=client)
            callees = find_callees("worker.__module__", client=client)
            call_path = trace_call_path("worker.__module__", "worker.build", client=client)
            replaced = load_repository(
                _sample_repo(commit="def456", module_qname="worker.v2"),
                replace=True,
                client=client,
            )
            after_rows = client.execute_read(
                """
                MATCH (:Type {qname: 'worker.v2.Worker'})
                    -[:INHERITS]->(:Type {qname: 'worker.v2.BaseWorker'})
                WITH count(*) AS inherits
                MATCH (:Type {qname: 'worker.v2.Worker'})
                    -[:HAS_METHOD]->(:Method {qname: 'worker.v2.Worker.run'})
                WITH inherits, count(*) AS has_method
                MATCH (:Function {qname: 'worker.v2.__module__'})
                    -[:CALLS]->(:Function {qname: 'worker.v2.build'})
                WITH inherits, has_method, count(*) AS module_calls
                OPTIONAL MATCH (:Method {qname: 'worker.v2.Worker.run'})-[rel:CALLS]->()
                WITH inherits, has_method, module_calls, count(rel) AS bogus_self_calls
                MATCH (m:Module)
                WITH inherits, has_method, module_calls, bogus_self_calls,
                     collect(DISTINCT m.qname) AS modules
                RETURN inherits, has_method, module_calls, bogus_self_calls, modules
                """
            )
            dead_code = find_dead_code("sample", client=client)
        finally:
            client.close()

    assert result["inherits"] == 1
    assert result["calls"] == 1
    assert repeated == result
    assert before_rows == [
        {
            "inherits": 1,
            "has_method": 1,
            "module_calls": 1,
            "bogus_self_calls": 0,
        }
    ]
    assert callers == [
        {
            "key": "sample@abc123:worker.py:worker.__module__:1",
            "qname": "worker.__module__",
            "signature": "<module>",
            "depth": 1,
        }
    ]
    assert callees == [
        {
            "key": "sample@abc123:worker.py:worker.build:10",
            "qname": "worker.build",
            "signature": "def build()",
            "depth": 1,
        }
    ]
    assert call_path == [{"path": ["worker.__module__", "worker.build"], "depth": 1}]
    assert replaced["calls"] == 1
    assert after_rows == [
        {
            "inherits": 1,
            "has_method": 1,
            "module_calls": 1,
            "bogus_self_calls": 0,
            "modules": ["worker.v2"],
        }
    ]
    assert {row["qname"] for row in dead_code} == {"worker.v2.Worker.run"}
