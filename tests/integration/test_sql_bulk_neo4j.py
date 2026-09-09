from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.bulk_export import export_repository_path, load_bulk_export
from codekg.bulk_import import build_import_command
from codekg.neo4j_client import Neo4jClient

pytestmark = pytest.mark.integration

_NEO4J_IMAGE = "neo4j:5.26-community"


def _container_import_command(export, import_root: Path) -> list[str]:
    command = build_import_command(export, database="neo4j", neo4j_admin="neo4j-admin")
    rewritten: list[str] = []
    for argument in command:
        if not (argument.startswith("--nodes=") or argument.startswith("--relationships=")):
            rewritten.append(argument)
            continue
        option, value = argument.split("=", 1)
        label, paths = value.split("=", 1)
        mounted_paths = []
        for path in paths.split(","):
            relative = Path(path).resolve().relative_to(import_root.resolve())
            mounted_paths.append(f"/var/lib/neo4j/import/{relative.as_posix()}")
        rewritten.append(f"{option}={label}={','.join(mounted_paths)}")
    return rewritten


def _offline_import(export, data_dir: Path) -> None:
    command = _container_import_command(export, export.output_dir)
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "neo4j-admin",
            "--env",
            "NEO4J_server_directories_data=/data",
            "--volume",
            f"{data_dir}:/data",
            "--volume",
            f"{export.output_dir}:/var/lib/neo4j/import:ro",
            _NEO4J_IMAGE,
            *command[1:],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sql_bulk_export_import_preserves_shards_and_sql_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    image = subprocess.run(
        ["docker", "image", "inspect", _NEO4J_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if image.returncode != 0:
        pytest.skip(f"local Docker image {_NEO4J_IMAGE!r} is unavailable")

    source = tmp_path / "repo"
    source.mkdir()
    (source / "codekg.toml").write_text(
        "[sql]\nenabled = true\ndatabase = 'warehouse'\n", encoding="utf-8"
    )
    (source / "schema.sql").write_text(
        'CREATE TABLE public."order;items" (id int);\n'
        "CREATE VIEW public.active_orders AS\n"
        'SELECT id FROM public."order;items";\n'
        "COMMENT ON TABLE public.\"order;items\" IS 'line 1\nline 2';\n",
        encoding="utf-8",
    )
    (source / "queries.sql").write_text(
        'SELECT id FROM public."order;items";\nSELECT id FROM public.active_orders;\n',
        encoding="utf-8",
    )

    # One source file per extraction task makes the bounded pipeline publish
    # multiple node/relationship shards, including the SQL groups.
    monkeypatch.setattr(
        "codekg.bulk_export._source_batches",
        lambda root: ((path,) for path in sorted(root.rglob("*.sql"))),
    )
    export = export_repository_path(source, tmp_path / "export", workers=2)
    manifest_export = load_bulk_export(export.manifest_path)
    assert len(manifest_export.node_groups["SqlArtifact"]) == 3
    assert len(manifest_export.relationship_groups["DEFINES"]) >= 2
    assert manifest_export.counts["nodes_SqlObject"] == 2

    data_dir = tmp_path / "neo4j-data"
    data_dir.mkdir()
    _offline_import(manifest_export, data_dir)

    with (
        Neo4jContainer(_NEO4J_IMAGE, password="password")
        .with_volume_mapping(str(data_dir), "/data", "rw")
        .with_env("NEO4J_server_directories_data", "/data")
    ) as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            counts = client.execute_read(
                """
                MATCH (o:SqlObject)
                WITH count(o) AS objects
                MATCH (a:SqlArtifact), (r:Reference), (d:Database)
                RETURN objects, count(DISTINCT a) AS artifacts,
                       count(DISTINCT r) AS references, count(DISTINCT d) AS databases
                """
            )
            assert counts == [{"objects": 2, "artifacts": 2, "references": 5, "databases": 1}]

            objects = client.execute_read(
                """
                MATCH (o:SqlObject)
                RETURN o.database_name AS database_name, o.schema_name AS schema_name,
                       o.object_name AS object_name, o.kind AS kind,
                       o.definition_count AS definition_count, o.owner_path AS owner_path
                ORDER BY object_name
                """
            )
            assert objects == [
                {
                    "database_name": "warehouse",
                    "schema_name": "public",
                    "object_name": "active_orders",
                    "kind": "view",
                    "definition_count": 1,
                    "owner_path": "schema.sql",
                },
                {
                    "database_name": "warehouse",
                    "schema_name": "public",
                    "object_name": "order;items",
                    "kind": "table",
                    "definition_count": 1,
                    "owner_path": "schema.sql",
                },
            ]

            artifacts = client.execute_read(
                "MATCH (a:SqlArtifact) RETURN a.path AS path, a.text AS text ORDER BY path"
            )
            assert artifacts[0]["path"] == "queries.sql"
            assert "order;items" in artifacts[0]["text"]
            assert "line 1\nline 2" in artifacts[1]["text"]

            references = client.execute_read(
                """
                MATCH (r:Reference)-[:REFERS_TO]->(o:SqlObject)
                WHERE r.path = 'queries.sql' AND r.object_name = 'order;items'
                RETURN r.raw_name AS raw_name, r.candidate_keys_json AS candidates,
                       r.dynamic AS dynamic,
                       o.key AS object_key, o.object_name AS object_name
                """
            )
            assert len(references) == 1
            assert references[0]["raw_name"] == 'public."order;items"'
            assert references[0]["dynamic"] is False
            assert references[0]["object_name"] == "order;items"
            assert json.loads(references[0]["candidates"]) == [references[0]["object_key"]]

            defines = client.execute_read(
                """
                MATCH (statement:SqlStatement)-[rel:DEFINES]->(object:SqlObject)
                RETURN rel.role AS role, rel.line AS line, rel.column AS column,
                       object.object_name AS object_name
                ORDER BY object_name
                """
            )
            assert defines == [
                {
                    "role": "define",
                    "line": 2,
                    "column": len("CREATE VIEW ") + 1,
                    "object_name": "active_orders",
                },
                {
                    "role": "define",
                    "line": 1,
                    "column": len("CREATE TABLE ") + 1,
                    "object_name": "order;items",
                },
            ]
            assert client.execute_read(
                "MATCH ()-[rel:RESOLVES_TO]->() RETURN count(rel) AS count"
            ) == [{"count": 0}]
        finally:
            client.close()
