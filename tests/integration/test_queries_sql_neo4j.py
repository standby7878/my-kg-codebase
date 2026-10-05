from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.neo4j_client import Neo4jClient
from codekg.queries.sql import (
    find_sql_usages,
    get_sql_in_file,
    get_sql_object,
    search_sql_objects,
)
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration

_REPO = "demo"
_COMMIT = "abc123"
_PREFIX = f"{_REPO}@{_COMMIT}"
_USERS_KEY = f"{_PREFIX}:sql:n:v6:public:v5:table:v5:users:n"
_ACTIVE_USERS_KEY = f"{_PREFIX}:sql:n:v6:public:v4:view:v12:active_users:n"
_SCHEMA_FILE_KEY = f"{_PREFIX}:schema.sql"
_QUERIES_FILE_KEY = f"{_PREFIX}:queries.sql"
_ARTIFACT_KEY = f"{_SCHEMA_FILE_KEY}:sql-artifact:0"
_QUERY_ARTIFACT_KEY = f"{_QUERIES_FILE_KEY}:sql-artifact:0"
_DEFINE_STMT_KEY = f"{_SCHEMA_FILE_KEY}:sql-statement:0:0"
_SELECT_STMT_KEY = f"{_QUERIES_FILE_KEY}:sql-statement:0:0"
_REFERENCE_KEY = f"{_QUERIES_FILE_KEY}:sql-reference:0:0:0"
_DATABASE_KEY = f"{_PREFIX}:sql:database:n"


def _seed_sql_graph(client: Neo4jClient) -> None:
    client.execute_write(
        """
        CREATE (r:Repository {repo_name: $repo, commit: $commit, root_path: '/repos/demo'})
        CREATE (d:Database {
            key: $database_key,
            database_name: '<default>',
            owner_path: 'schema.sql'
        })
        CREATE (users:SqlObject {
            key: $users_key,
            database_name: '<default>',
            schema_name: 'public',
            kind: 'table',
            object_name: 'users',
            signature: null,
            definition_count: 1,
            owner_path: 'schema.sql'
        })
        CREATE (active_users:SqlObject {
            key: $active_users_key,
            database_name: '<default>',
            schema_name: 'public',
            kind: 'view',
            object_name: 'active_users',
            signature: null,
            definition_count: 1,
            owner_path: 'schema.sql'
        })
        CREATE (schema_file:File {key: $schema_file_key, path: 'schema.sql'})
        CREATE (queries_file:File {key: $queries_file_key, path: 'queries.sql'})
        CREATE (schema_artifact:SqlArtifact {
            key: $artifact_key,
            path: 'schema.sql',
            ordinal: 0,
            origin: 'file',
            dialect: 'postgresql',
            text: 'CREATE TABLE public.users (id int);',
            text_hash: 'hash',
            start_line: 1,
            start_column: 1,
            end_line: 1,
            end_column: 35
        })
        CREATE (queries_artifact:SqlArtifact {
            key: $query_artifact_key,
            path: 'queries.sql',
            ordinal: 0,
            origin: 'file',
            dialect: 'postgresql',
            text: 'SELECT id FROM public.users;',
            text_hash: 'hash2',
            start_line: 1,
            start_column: 1,
            end_line: 1,
            end_column: 28
        })
        CREATE (define_stmt:SqlStatement {
            key: $define_stmt_key,
            path: 'schema.sql',
            artifact_ordinal: 0,
            ordinal: 0,
            kind: 'CREATE TABLE',
            parent_key: null,
            control_context: null,
            start_line: 1,
            start_column: 1,
            end_line: 1,
            end_column: 35
        })
        CREATE (select_stmt:SqlStatement {
            key: $select_stmt_key,
            path: 'queries.sql',
            artifact_ordinal: 0,
            ordinal: 0,
            kind: 'SELECT',
            parent_key: null,
            control_context: null,
            start_line: 1,
            start_column: 1,
            end_line: 1,
            end_column: 28
        })
        CREATE (reference:Reference {
            key: $reference_key,
            path: 'queries.sql',
            artifact_ordinal: 0,
            statement_ordinal: 0,
            ordinal: 0,
            raw_name: 'public.users',
            database_name: null,
            schema_name: 'public',
            object_name: 'users',
            object_kind_hint: 'table',
            signature_hint: null,
            status: 'exact',
            role: 'read',
            dynamic: false,
            candidate_count: 1,
            candidate_keys_json: $users_key_json,
            start_line: 1,
            start_column: 18,
            end_line: 1,
            end_column: 29
        })
        CREATE (r)-[:HAS_DATABASE]->(d)
        CREATE (d)-[:HAS_OBJECT]->(users)
        CREATE (d)-[:HAS_OBJECT]->(active_users)
        CREATE (r)-[:CONTAINS]->(schema_file)
        CREATE (r)-[:CONTAINS]->(queries_file)
        CREATE (schema_file)-[:CONTAINS_SQL]->(schema_artifact)
        CREATE (queries_file)-[:CONTAINS_SQL]->(queries_artifact)
        CREATE (schema_artifact)-[:CONTAINS_SQL]->(define_stmt)
        CREATE (queries_artifact)-[:CONTAINS_SQL]->(select_stmt)
        CREATE (define_stmt)-[:DEFINES {role: 'define', line: 1, column: 13}]->(users)
        CREATE (select_stmt)-[:READS_FROM {role: 'read', line: 1, column: 18}]->(users)
        CREATE (select_stmt)-[:HAS_REFERENCE]->(reference)
        CREATE (reference)-[:REFERS_TO {status: 'exact'}]->(users)
        """,
        {
            "repo": _REPO,
            "commit": _COMMIT,
            "database_key": _DATABASE_KEY,
            "users_key": _USERS_KEY,
            "active_users_key": _ACTIVE_USERS_KEY,
            "schema_file_key": _SCHEMA_FILE_KEY,
            "queries_file_key": _QUERIES_FILE_KEY,
            "artifact_key": _ARTIFACT_KEY,
            "query_artifact_key": _QUERY_ARTIFACT_KEY,
            "define_stmt_key": _DEFINE_STMT_KEY,
            "select_stmt_key": _SELECT_STMT_KEY,
            "reference_key": _REFERENCE_KEY,
            "users_key_json": f'["{_USERS_KEY}"]',
        },
    )


def test_sql_query_tools_read_loaded_graph() -> None:
    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client)
            _seed_sql_graph(client)

            search = search_sql_objects("users", repository=_REPO, client=client)
            assert search["status"] == "ok"
            assert any(row["object_key"] == _USERS_KEY for row in search["results"])

            detail = get_sql_object(_USERS_KEY, client=client)
            assert detail["status"] == "ok"
            assert detail["object"]["object_name"] == "users"
            assert detail["definitions"]
            assert detail["definitions"][0]["file"] == "schema.sql"

            qualified = get_sql_object("public.active_users", repository=_REPO, client=client)
            assert qualified["object"]["object_key"] == _ACTIVE_USERS_KEY

            usages = find_sql_usages(_USERS_KEY, role="read", client=client)
            assert usages
            assert usages[0]["file"] == "queries.sql"
            assert usages[0]["reference_status"] == "exact"

            file_view = get_sql_in_file("queries.sql", repository=_REPO, client=client)
            assert file_view["status"] == "ok"
            assert file_view["statements"]
            assert file_view["references"]
            assert file_view["references"][0]["object_key"] == _USERS_KEY
        finally:
            client.close()
