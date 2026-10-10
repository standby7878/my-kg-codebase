import pytest

from codekg.neo4j_client import CodeKGNeo4jError, Neo4jClient


def test_global_auth_none_omits_driver_auth(monkeypatch):
    calls = []
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "must-not-be-used")
    monkeypatch.setattr(
        "codekg.neo4j_client.GraphDatabase.driver",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )

    client = Neo4jClient()

    assert client.auth_enabled is False
    assert client.password is None
    assert calls == [(("bolt://neo4j:7687",), {})]


def test_explicit_auth_true_requires_password_even_if_global_auth_is_none(monkeypatch):
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    with pytest.raises(CodeKGNeo4jError, match="NEO4J_PASSWORD must be set"):
        Neo4jClient(auth_enabled=True)


def test_explicit_auth_false_ignores_global_password(monkeypatch):
    calls = []
    monkeypatch.setenv("NEO4J_AUTH", "neo4j/irrelevant")
    monkeypatch.setenv("NEO4J_PASSWORD", "must-not-be-used")
    monkeypatch.setattr(
        "codekg.neo4j_client.GraphDatabase.driver",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )

    client = Neo4jClient(auth_enabled=False)

    assert client.auth_enabled is False
    assert client.password is None
    assert calls == [(("bolt://neo4j:7687",), {})]


def test_auth_enabled_uses_password_credentials(monkeypatch):
    calls = []
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "local-secret")
    monkeypatch.setattr(
        "codekg.neo4j_client.GraphDatabase.driver",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )

    client = Neo4jClient(auth_enabled=True)

    assert client.auth_enabled is True
    assert calls == [(("bolt://neo4j:7687",), {"auth": ("neo4j", "local-secret")})]


def test_fourth_positional_argument_remains_database(monkeypatch):
    calls = []
    monkeypatch.delenv("NEO4J_AUTH", raising=False)
    monkeypatch.setattr(
        "codekg.neo4j_client.GraphDatabase.driver",
        lambda *args, **kwargs: calls.append((args, kwargs)) or object(),
    )

    client = Neo4jClient("bolt://example.invalid:7687", "graph-user", "secret", "custom-db")

    assert client.database == "custom-db"
    assert calls == [(("bolt://example.invalid:7687",), {"auth": ("graph-user", "secret")})]
