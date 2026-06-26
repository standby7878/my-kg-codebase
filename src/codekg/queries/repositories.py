from __future__ import annotations

from codekg.neo4j_client import Neo4jClient, get_client


def list_repositories(client: Neo4jClient | None = None) -> list[dict[str, object]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository)
        OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
        RETURN r.repo_name AS repo_name,
               r.commit AS commit,
               r.root_path AS root_path,
               count(f) AS files
        ORDER BY repo_name
        """,
        max_rows=100,
    )
