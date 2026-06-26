from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ir import CallIR, FileIR, InheritanceIR, RepositoryIR, SymbolIR
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def test_loader_writes_python_relationships_to_neo4j() -> None:
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
                        kind="function",
                        name="<module>",
                        qname="worker.__module__",
                        signature="<module>",
                        start_line=1,
                        end_line=1,
                    ),
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
                        start_line=3,
                        end_line=8,
                    ),
                    SymbolIR(
                        kind="method",
                        name="run",
                        qname="worker.Worker.run",
                        signature="def run(self)",
                        start_line=4,
                        end_line=5,
                        parent_qname="worker.Worker",
                    ),
                    SymbolIR(
                        kind="function",
                        name="build",
                        qname="worker.build",
                        signature="def build()",
                        start_line=10,
                        end_line=11,
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
                        caller_qname="worker.__module__",
                        callee_name="build",
                        callee_qname="worker.build",
                        line=13,
                    ),
                    CallIR(
                        caller_qname="worker.Worker.run",
                        callee_name="missing",
                        callee_qname="worker.Worker.missing",
                        receiver="self",
                        line=5,
                    ),
                ),
            ),
        ),
    )

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            result = load_repository(repo, replace=False, client=client)

            rows = client.execute_read(
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
        finally:
            client.close()

    assert result["inherits"] == 1
    assert result["calls"] == 1
    assert rows == [
        {
            "inherits": 1,
            "has_method": 1,
            "module_calls": 1,
            "bogus_self_calls": 0,
        }
    ]
