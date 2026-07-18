from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ingest import scan_repository
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def test_constructor_edges_and_replacement_cleanup(tmp_path) -> None:
    (tmp_path / "worker.py").write_text(
        "class Worker:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "def build():\n"
        "    return Worker()\n",
        encoding="utf-8",
    )
    repository = scan_repository(tmp_path)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            first = load_repository(repository, replace=False, client=client)
            rows = client.execute_read(
                """
                MATCH (site:CallSite {raw_callee: 'Worker'})
                OPTIONAL MATCH (site)-[constructs:CONSTRUCTS]->(type:Type)
                OPTIONAL MATCH (owner)-[owner_constructs:CONSTRUCTS {key: site.key}]->(type)
                WHERE owner.key = site.owner_key
                OPTIONAL MATCH (site)-[resolves:RESOLVES_TO]->(initializer:Method)
                OPTIONAL MATCH (owner)-[calls:CALLS {key: site.key}]->(initializer)
                RETURN site.status AS status,
                       type.qname AS type_qname,
                       initializer.qname AS initializer_qname,
                       count(DISTINCT constructs) AS construction_edges,
                       count(DISTINCT owner_constructs) AS owner_construction_edges,
                       count(DISTINCT resolves) AS resolution_edges,
                       count(DISTINCT calls) AS call_edges
                """
            )
            replacement = load_repository(
                repository.__class__(
                    repository.repo_name,
                    repository.commit,
                    repository.root_path,
                    (),
                ),
                replace=False,
                client=client,
            )
            stale = client.execute_read(
                """
                MATCH ()-[rel:CONSTRUCTS]->()
                WHERE rel.key STARTS WITH $prefix
                RETURN count(rel) AS count
                """,
                {"prefix": f"{repository.repo_name}@{repository.commit}:"},
            )
        finally:
            client.close()

    assert first["callsite_statuses"] == {"constructor_exact_local": 1}
    assert rows == [
        {
            "status": "constructor_exact_local",
            "type_qname": "worker.Worker",
            "initializer_qname": "worker.Worker.__init__",
            "construction_edges": 1,
            "owner_construction_edges": 1,
            "resolution_edges": 1,
            "call_edges": 1,
        }
    ]
    assert replacement["call_sites"] == 0
    assert stale == [{"count": 0}]
