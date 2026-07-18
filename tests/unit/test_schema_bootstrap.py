from __future__ import annotations

import pytest

from codekg.schema.bootstrap import BOOTSTRAP_CYPHER, bootstrap_schema

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_write(self, statement: str) -> list[dict[str, object]]:
        self.statements.append(statement)
        return []


def test_bootstrap_applies_module_init_and_call_site_schema_idempotently() -> None:
    client = FakeClient()

    bootstrap_schema(client=client)  # type: ignore[arg-type]

    assert client.statements == BOOTSTRAP_CYPHER
    assert (
        "CREATE CONSTRAINT module_init_key IF NOT EXISTS FOR (n:ModuleInit) REQUIRE n.key IS UNIQUE"
    ) in client.statements
    assert (
        "CREATE INDEX module_init_qname IF NOT EXISTS FOR (n:ModuleInit) ON (n.qname)"
    ) in client.statements
    assert (
        "CREATE CONSTRAINT call_site_key IF NOT EXISTS FOR (n:CallSite) REQUIRE n.key IS UNIQUE"
    ) in client.statements
    assert (
        "CREATE CONSTRAINT parse_diagnostic_key IF NOT EXISTS "
        "FOR (n:ParseDiagnostic) REQUIRE n.key IS UNIQUE"
    ) in client.statements
    assert "CREATE INDEX call_site_owner_key IF NOT EXISTS FOR (n:CallSite) ON (n.owner_key)" in (
        client.statements
    )
