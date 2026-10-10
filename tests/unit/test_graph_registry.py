import json

import pytest

from codekg.corpus_registry import create_native_registry
from codekg.graph_artifacts import freeze_generation_manifest
from codekg.graph_registry import GraphRegistry, GraphRegistryError


def _registry(tmp_path, *, kind="application", role="application", registry_toml_extra=""):
    generation = tmp_path / "generations" / "g1"
    generation.mkdir(parents=True)
    db = create_native_registry(generation / "corpus.sqlite")
    db.execute("INSERT INTO metadata VALUES ('src.revision','r1')")
    db.commit()
    db.close()
    manifest = generation / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "corpus.sqlite",
                "snapshots": [{"alias": "src", "role": role, "revision": "r1"}],
            }
        )
    )
    (tmp_path / "registry.toml").write_text(
        "schema_version = 1\ndefault_application_graph = 'app'\n"
        "[[graphs]]\nid = 'app'\nkind = 'application'\n"
        "generation_manifest = 'generations/g1/manifest.json'\n"
        "endpoint_env = 'CODEKG_APP_NEO4J_URI'\ncredential_env_prefix = 'CODEKG_APP_NEO4J'\n"
        + registry_toml_extra
    )
    return manifest, tmp_path / "registry.toml"


def test_registry_pins_manifest_generation_and_does_not_connect(tmp_path):
    manifest, registry_path = _registry(tmp_path)
    registry = GraphRegistry.load(registry_path)
    handle = registry.handle()
    assert handle.generation_id.startswith("app:")
    assert handle.corpus_path == manifest.parent / "corpus.sqlite"
    assert handle.ref("opaque-local-key").generation_id == handle.generation_id
    assert registry.list_graphs()[0]["generation_id"] == handle.generation_id
    assert handle._client is None
    with pytest.raises(TypeError):
        handle.generation.manifest["version"] = 2
    with pytest.raises(TypeError):
        registry.graphs["other"] = registry.graphs["app"]


def test_handle_revalidates_marker_without_managed_retries(tmp_path, monkeypatch):
    _, registry_path = _registry(tmp_path)
    handle = GraphRegistry.load(registry_path).handle()
    monkeypatch.setenv("CODEKG_APP_NEO4J_URI", "bolt://example.invalid:7687")
    monkeypatch.setenv("CODEKG_APP_NEO4J_PASSWORD", "unit-test-only")
    state = {"generation": handle.generation_id, "reads": 0, "closed": False}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_transaction_retry_time_seconds"] == 0.0
            assert kwargs["connection_timeout_seconds"] <= 2.0

        def execute_read(self, query, **kwargs):
            state["reads"] += 1
            assert kwargs["timeout_seconds"] <= 2.0
            return [{"graph_id": "app", "generation_id": state["generation"]}]

        def close(self):
            state["closed"] = True

    monkeypatch.setattr("codekg.neo4j_client.Neo4jClient", Client)
    handle.verify_generation()
    assert state["reads"] == 1
    state["generation"] = "different-generation"
    with pytest.raises(GraphRegistryError, match="marker mismatch"):
        handle.verify_generation()
    assert state["reads"] == 2
    handle.close()
    assert state["closed"]


def test_handle_auth_none_is_graph_local_and_needs_no_password(tmp_path, monkeypatch):
    _, registry_path = _registry(tmp_path)
    handle = GraphRegistry.load(registry_path).handle()
    monkeypatch.setenv("CODEKG_APP_NEO4J_URI", "bolt://example.invalid:7687")
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "global-password-must-not-be-used")
    monkeypatch.setenv("CODEKG_APP_NEO4J_AUTH", "none")
    monkeypatch.delenv("CODEKG_APP_NEO4J_PASSWORD", raising=False)
    received = {}

    class Client:
        def __init__(self, **kwargs):
            received.update(kwargs)

        def execute_read(self, query, **kwargs):
            return [{"graph_id": "app", "generation_id": handle.generation_id}]

        def close(self):
            pass

    monkeypatch.setattr("codekg.neo4j_client.Neo4jClient", Client)
    handle.verify_generation()

    assert received["auth_enabled"] is False
    assert received["password"] is None


def test_unset_graph_auth_requires_graph_password_even_if_global_auth_none(tmp_path, monkeypatch):
    _, registry_path = _registry(tmp_path)
    handle = GraphRegistry.load(registry_path).handle()
    monkeypatch.setenv("CODEKG_APP_NEO4J_URI", "bolt://example.invalid:7687")
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "global-password-must-not-be-used")
    monkeypatch.delenv("CODEKG_APP_NEO4J_AUTH", raising=False)
    monkeypatch.delenv("CODEKG_APP_NEO4J_PASSWORD", raising=False)

    with pytest.raises(GraphRegistryError, match="CODEKG_APP_NEO4J_PASSWORD"):
        handle.verify_generation()


def test_database_can_be_prepared_before_application_registry_exists(tmp_path):
    _, path = _registry(tmp_path, role="postgres")
    path.write_text(
        path.read_text()
        .replace("default_application_graph = 'app'\n", "")
        .replace("kind = 'application'", "kind = 'database'")
    )
    registry = GraphRegistry.load(path)
    assert registry.default_application_graph is None
    assert registry.handle("app").spec.kind == "database"
    with pytest.raises(GraphRegistryError, match="graph_id is required"):
        registry.handle()


def test_registry_rejects_unknown_keys_and_non_single_pg_context(tmp_path):
    _, registry_path = _registry(tmp_path, registry_toml_extra="surprise = 3\n")
    with pytest.raises(GraphRegistryError, match="unknown graphs"):
        GraphRegistry.load(registry_path)

    generation = tmp_path / "dbgen"
    generation.mkdir()
    db = create_native_registry(generation / "corpus.sqlite")
    db.execute("INSERT INTO metadata VALUES ('pg1.revision','a')")
    db.execute("INSERT INTO metadata VALUES ('pg2.revision','b')")
    db.commit()
    db.close()
    (generation / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "corpus.sqlite",
                "snapshots": [
                    {"alias": "pg1", "role": "postgres", "revision": "a"},
                    {"alias": "pg2", "role": "postgres", "revision": "b"},
                ],
            }
        )
    )
    from codekg.graph_registry import _load_generation

    with pytest.raises(GraphRegistryError, match="exactly one PostgreSQL"):
        _load_generation("db", "database", generation / "manifest.json")


def test_context_validates_roles_and_explicit_extensions(tmp_path):
    _, path = _registry(tmp_path)
    generation = tmp_path / "dbgen"
    generation.mkdir()
    db = create_native_registry(generation / "corpus.sqlite")
    db.execute("INSERT INTO metadata VALUES ('pg.revision','pg-r')")
    db.commit()
    db.close()
    (generation / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "corpus.sqlite",
                "snapshots": [{"alias": "pg", "role": "postgres", "revision": "pg-r"}],
            }
        )
    )
    path.write_text(
        path.read_text()
        + (
            "[[graphs]]\nid='db'\nkind='database'\ngeneration_manifest='dbgen/manifest.json'\n"
            "endpoint_env='DB_URI'\ncredential_env_prefix='DB'\n"
            "[[contexts]]\nid='bad'\napplication_graph='db'\ndatabase_graph='app'\n"
        )
    )
    with pytest.raises(GraphRegistryError, match="application_graph"):
        GraphRegistry.load(path)


def test_registry_accepts_one_immutable_pg_context_and_optional_zvec(tmp_path):
    app_manifest, path = _registry(tmp_path)
    dbgen = tmp_path / "dbgen"
    dbgen.mkdir()
    db = create_native_registry(dbgen / "corpus.sqlite")
    db.executemany(
        "INSERT INTO metadata VALUES (?,?)", (("pg.revision", "p1"), ("cron.revision", "c1"))
    )
    db.commit()
    db.close()
    (dbgen / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "corpus.sqlite",
                "snapshots": [
                    {"alias": "pg", "role": "postgres", "revision": "p1", "dependencies": []},
                    {
                        "alias": "cron",
                        "role": "extension",
                        "revision": "c1",
                        "dependencies": ["pg"],
                    },
                ],
            }
        )
    )
    path.write_text(
        path.read_text().replace(
            "credential_env_prefix = 'CODEKG_APP_NEO4J'\n",
            "credential_env_prefix = 'CODEKG_APP_NEO4J'\nzvec_path = 'lexical.zvec'\n",
        )
        + (
            "[[graphs]]\nid='db-pg'\nkind='database'\ngeneration_manifest='dbgen/manifest.json'\n"
            "endpoint_env='DB_URI'\ncredential_env_prefix='DB'\n"
            "[[contexts]]\nid='on-pg'\napplication_graph='app'\ndatabase_graph='db-pg'\n"
            "application_database='primary'\nvisible_extensions=['cron']\nsearch_path=['public','pg_catalog']\n"
        )
    )
    registry = GraphRegistry.load(path)
    assert registry.contexts["on-pg"].visible_extensions == ("cron",)
    assert registry.handle().zvec_path == tmp_path / "generations/g1/lexical.zvec"


def test_freeze_generation_manifest_rebases_paths_and_is_immutable(tmp_path):
    export = tmp_path / "export"
    generation = export / "generations/g4"
    generation.mkdir(parents=True)
    db = create_native_registry(generation / "corpus.sqlite")
    db.execute("INSERT INTO metadata VALUES ('src.revision','r4')")
    db.commit()
    db.close()
    (generation / "graph").mkdir()
    (generation / "graph/nodes.csv").write_text("id\n")
    source = export / "manifest.json"
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "generations/g4/corpus.sqlite",
                "nodes": {
                    "X": {
                        "file": "generations/g4/graph/nodes.csv",
                        "files": [
                            "generations/g4/headers/x.csv",
                            "generations/g4/nodes/X/part-000000.csv",
                        ],
                        "count": 0,
                    }
                },
                "relationships": {},
                "search_stage": {"file": "generations/g4/search.sqlite"},
                "snapshots": [
                    {
                        "alias": "src",
                        "role": "application",
                        "revision": "r4",
                        "bulk_manifest": "generations/g4/bulk.json",
                    }
                ],
            }
        )
    )
    target = generation / "manifest.json"
    freeze_generation_manifest(source, target)
    frozen = json.loads(target.read_text())
    assert frozen["registry"] == "corpus.sqlite"
    assert frozen["nodes"]["X"]["file"] == "graph/nodes.csv"
    assert frozen["nodes"]["X"]["files"] == [
        "headers/x.csv",
        "nodes/X/part-000000.csv",
    ]
    assert frozen["snapshots"][0]["bulk_manifest"] == "bulk.json"
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "codekg-corpus",
                "registry": "generations/g4/corpus.sqlite",
                "nodes": {"X": {"file": "generations/g4/graph/nodes.csv", "count": 1}},
                "relationships": {},
                "search_stage": {"file": "generations/g4/search.sqlite"},
                "snapshots": [
                    {
                        "alias": "src",
                        "role": "application",
                        "revision": "r4",
                        "bulk_manifest": "generations/g4/bulk.json",
                    }
                ],
            }
        )
    )
    assert json.loads(target.read_text())["kind"] == "codekg-corpus"
    with pytest.raises(FileExistsError):
        freeze_generation_manifest(source, target)
