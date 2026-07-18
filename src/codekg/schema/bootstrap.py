"""Apply the CodeKG Neo4j schema."""

from __future__ import annotations

from codekg.neo4j_client import Neo4jClient, get_client

BOOTSTRAP_CYPHER = [
    "CREATE CONSTRAINT repository_key IF NOT EXISTS FOR (n:Repository) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT file_key IF NOT EXISTS FOR (n:File) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT function_key IF NOT EXISTS FOR (n:Function) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT method_key IF NOT EXISTS FOR (n:Method) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT type_key IF NOT EXISTS FOR (n:Type) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT module_key IF NOT EXISTS FOR (n:Module) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT reference_key IF NOT EXISTS FOR (n:Reference) REQUIRE n.key IS UNIQUE",
    "CREATE INDEX repository_name IF NOT EXISTS FOR (n:Repository) ON (n.repo_name)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (n:File) ON (n.path)",
    "CREATE INDEX symbol_name IF NOT EXISTS FOR (n:Function) ON (n.name)",
    "CREATE INDEX function_qname IF NOT EXISTS FOR (n:Function) ON (n.qname)",
    "CREATE INDEX method_name IF NOT EXISTS FOR (n:Method) ON (n.name)",
    "CREATE INDEX method_qname IF NOT EXISTS FOR (n:Method) ON (n.qname)",
    "CREATE INDEX type_name IF NOT EXISTS FOR (n:Type) ON (n.name)",
    "CREATE INDEX type_qname IF NOT EXISTS FOR (n:Type) ON (n.qname)",
    "CREATE INDEX module_name IF NOT EXISTS FOR (n:Module) ON (n.name)",
    """
    CREATE FULLTEXT INDEX code_symbol_search IF NOT EXISTS
    FOR (n:Function|Method|Type)
    ON EACH [n.name, n.qname, n.signature]
    """,
]


def bootstrap_schema(client: Neo4jClient | None = None) -> None:
    """Apply all schema statements idempotently."""

    db = client or get_client()
    for statement in BOOTSTRAP_CYPHER:
        db.execute_write(statement)


def main() -> None:
    bootstrap_schema()
    print("CodeKG schema bootstrap complete")


if __name__ == "__main__":
    main()
