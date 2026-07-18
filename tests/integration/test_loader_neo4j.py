from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ingest import scan_repository
from codekg.ir import CallIR, FileIR, ModuleInitIR, ParseDiagnosticIR, RepositoryIR, SymbolIR
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def _call(
    owner_qname: str,
    raw_callee: str,
    hint: str | None,
    *,
    column: int,
    ordinal: int,
    receiver_kind: str = "none",
) -> CallIR:
    return CallIR(
        owner_qname=owner_qname,
        raw_callee=raw_callee,
        callee_name=raw_callee.rsplit(".", maxsplit=1)[-1],
        callee_qname_hint=hint,
        receiver_kind=receiver_kind,  # type: ignore[arg-type]
        start_line=8,
        start_column=column,
        end_line=8,
        end_column=column + len(raw_callee),
        ordinal=ordinal,
    )


def _sample_repo(*, commit: str, module_qname: str, include_error: bool = True) -> RepositoryIR:
    path = f"{module_qname.rsplit('.', maxsplit=1)[-1]}.py"
    working_file = FileIR(
        path=path,
        language="python",
        loc=20,
        module_qname=module_qname,
        module_init=ModuleInitIR(f"{module_qname}.__module__", 1, 20),
        symbols=(
            SymbolIR("function", "build", f"{module_qname}.build", "def build()", 2, 10),
            SymbolIR("function", "target", f"{module_qname}.target", "def target()", 12, 12),
        ),
        calls=(
            _call(
                f"{module_qname}.__module__",
                "build",
                f"{module_qname}.build",
                column=0,
                ordinal=1,
            ),
            _call(
                f"{module_qname}.build",
                "target",
                f"{module_qname}.target",
                column=4,
                ordinal=2,
            ),
            _call(
                f"{module_qname}.build",
                "target",
                f"{module_qname}.target",
                column=14,
                ordinal=3,
            ),
            _call(f"{module_qname}.build", "missing", None, column=24, ordinal=4),
        ),
    )
    error_file = FileIR(
        path="broken.py",
        language="python",
        loc=1,
        module_qname="broken",
        module_init=ModuleInitIR("broken.__module__", 1, 1),
        parse_status="error",
        diagnostics=(ParseDiagnosticIR("syntax_error", "error", 1, 12, "invalid syntax"),),
    )
    return RepositoryIR(
        repo_name="sample",
        commit=commit,
        root_path="/repos/sample",
        files=(working_file, error_file) if include_error else (working_file,),
    )


def test_loader_persists_authoritative_call_sites_and_replaces_them() -> None:
    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            first = load_repository(
                _sample_repo(commit="abc123", module_qname="worker"),
                replace=False,
                client=client,
                batch_size=2,
            )
            persisted = client.execute_read(
                """
                MATCH (f:File {path: 'worker.py'})
                MATCH (m:Module {qname: 'worker'})-[:INITIALIZES]->(init:ModuleInit)
                OPTIONAL MATCH (init)-[:HAS_CALLSITE]->(module_site:CallSite)
                OPTIONAL MATCH (:Function {qname: 'worker.build'})
                    -[:HAS_CALLSITE]->(build_site:CallSite)
                OPTIONAL MATCH (:Function {qname: 'worker.build'})-[calls:CALLS]->
                    (:Function {qname: 'worker.target'})
                OPTIONAL MATCH (:Function {qname: 'worker.build'})-[exact_calls:EXACT_CALLS]->
                    (:Function {qname: 'worker.target'})
                RETURN f.parse_status AS parse_status,
                       f.diagnostic_count AS diagnostic_count,
                       init.key AS module_init_key,
                       count(DISTINCT module_site) AS module_sites,
                       count(DISTINCT build_site) AS build_sites,
                       count(DISTINCT calls) AS derived_build_calls,
                       count(DISTINCT exact_calls) AS exact_build_calls,
                       collect(DISTINCT build_site.status) AS build_statuses,
                       collect(DISTINCT calls.key) AS derived_keys
                """
            )
            parse_error = client.execute_read(
                """
                MATCH (f:File {path: 'broken.py'})-[:CONTAINS]->(init:ModuleInit)
                RETURN f.parse_status AS status, f.diagnostic_count AS diagnostics,
                       init.qname AS init_qname
                """
            )
            second = load_repository(
                _sample_repo(commit="def456", module_qname="worker.v2", include_error=False),
                replace=True,
                client=client,
                batch_size=2,
            )
            after_replace = client.execute_read(
                """
                MATCH (r:Repository {repo_name: 'sample'})-[:CONTAINS]->(f:File)
                OPTIONAL MATCH (old_site:CallSite)
                WHERE old_site.key STARTS WITH 'sample@abc123:'
                WITH collect(DISTINCT f.path) AS paths, count(old_site) AS old_site_count
                MATCH (init:ModuleInit)
                RETURN paths, old_site_count, collect(DISTINCT init.qname) AS module_inits
                """
            )
        finally:
            client.close()

    assert first["call_sites"] == 4
    assert first["resolved_call_sites"] == 3
    assert first["files_with_parse_errors"] == 1
    assert first["parse_diagnostics"] == 1
    assert first["batches"] > 1
    assert len(persisted) == 1
    record = persisted[0]
    assert record["parse_status"] == "ok"
    assert record["diagnostic_count"] == 0
    assert record["module_init_key"] == "sample@abc123:worker.py:module-init"
    assert record["module_sites"] == 1
    assert record["build_sites"] == 3
    assert record["derived_build_calls"] == 2
    assert record["exact_build_calls"] == 2
    assert set(record["build_statuses"]) == {"exact_local", "unresolved"}
    assert set(record["derived_keys"]) == {
        "sample@abc123:worker.py:callsite:sample@abc123:worker.py:worker.build:2:8:4:8:10:2",
        "sample@abc123:worker.py:callsite:sample@abc123:worker.py:worker.build:2:8:14:8:20:3",
    }
    assert parse_error == [{"status": "error", "diagnostics": 1, "init_qname": "broken.__module__"}]
    assert second["call_sites"] == 4
    assert after_replace == [
        {"paths": ["v2.py"], "old_site_count": 0, "module_inits": ["worker.v2.__module__"]}
    ]


def test_source_scan_to_neo4j_preserves_scopes_definition_time_imports_and_diagnostics(
    tmp_path,
) -> None:
    (tmp_path / "scope_source.py").write_text(
        "import os\n"
        "\n"
        "def helper():\n"
        "    return object()\n"
        "\n"
        "def decorate(value):\n"
        "    return value\n"
        "\n"
        "@decorate(helper())\n"
        "def outer(value: helper() = helper()) -> helper():\n"
        "    class Inner:\n"
        "        def method(self):\n"
        "            return helper()\n"
        "    return Inner().method()\n",
        encoding="utf-8",
    )
    with (tmp_path / "scope_source.py").open("a", encoding="utf-8") as source:
        source.write(
            "\n"
            "def lexical_outer():\n"
            "    def middle():\n"
            "        def deepest():\n"
            "            return helper()\n"
            "        return deepest()\n"
            "    return middle()\n"
        )
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    repository = scan_repository(tmp_path)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            result = load_repository(repository, replace=False, client=client, batch_size=2)
            graph = client.execute_read(
                """
                MATCH (f:File {path: 'scope_source.py'})-[:CONTAINS]->(init:ModuleInit)
                OPTIONAL MATCH (init)-[:HAS_CALLSITE]->(site:CallSite)
                WITH collect(site.owner_qname) AS init_owners,
                     collect(site.raw_callee) AS init_callees
                MATCH (inner:Type {qname: 'scope_source.outer.<locals>.Inner'})
                    -[:HAS_METHOD]->(method:Method)
                MATCH (import_file:File {path: 'scope_source.py'})
                    -[import_rel:IMPORTS]->(:Module {qname: 'os'})
                MATCH (broken:File {path: 'broken.py'})
                    -[:HAS_DIAGNOSTIC]->(diagnostic:ParseDiagnostic)
                RETURN init_owners,
                       init_callees,
                       method.qname AS method_qname,
                       import_rel.key AS import_key,
                       import_rel.alias AS import_alias,
                       diagnostic.category AS diagnostic_category,
                       diagnostic.severity AS diagnostic_severity,
                       diagnostic.line AS diagnostic_line,
                       diagnostic.column AS diagnostic_column,
                       diagnostic.message AS diagnostic_message
                """
            )
            lexical_resolutions = client.execute_read(
                """
                MATCH (site:CallSite)-[:RESOLVES_TO]->(target)
                WHERE site.owner_qname IN [
                    'scope_source.lexical_outer',
                    'scope_source.lexical_outer.<locals>.middle'
                ]
                  AND site.raw_callee IN ['middle', 'deepest']
                RETURN site.owner_qname AS owner_qname,
                       site.raw_callee AS raw_callee,
                       site.status AS status,
                       target.qname AS target_qname
                ORDER BY owner_qname
                """
            )
        finally:
            client.close()

    assert result["files_with_parse_errors"] == 1
    assert result["parse_diagnostics"] == 1
    assert len(graph) == 1
    record = graph[0]
    assert record["init_owners"] == ["scope_source.__module__"] * 5
    assert sorted(record["init_callees"]) == ["decorate", "helper", "helper", "helper", "helper"]
    assert record["method_qname"] == "scope_source.outer.<locals>.Inner.method"
    assert record["import_key"] == (
        f"{repository.repo_name}@{repository.commit}:scope_source.py:import:os:os:<none>"
    )
    assert record["import_alias"] is None
    assert record["diagnostic_category"] == "syntax_error"
    assert record["diagnostic_severity"] == "error"
    assert record["diagnostic_line"] == 1
    assert record["diagnostic_column"] == 12
    assert record["diagnostic_message"] == "invalid syntax"
    assert lexical_resolutions == [
        {
            "owner_qname": "scope_source.lexical_outer",
            "raw_callee": "middle",
            "status": "exact_local",
            "target_qname": "scope_source.lexical_outer.<locals>.middle",
        },
        {
            "owner_qname": "scope_source.lexical_outer.<locals>.middle",
            "raw_callee": "deepest",
            "status": "exact_local",
            "target_qname": "scope_source.lexical_outer.<locals>.middle.<locals>.deepest",
        },
    ]


def test_source_scan_to_neo4j_resolves_only_snapshot_local_call_sites(tmp_path) -> None:
    """Exercise every Phase 3 resolver outcome through the real pipeline."""

    (tmp_path / "helpers.py").write_text(
        "def imported():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "cases.py").write_text(
        "from helpers import imported as imported_alias\n"
        "import external_library as ext\n"
        "\n"
        "def local():\n"
        "    return None\n"
        "\n"
        "def duplicate():\n"
        "    return None\n"
        "\n"
        "def duplicate():\n"
        "    return None\n"
        "\n"
        "def direct_cases():\n"
        "    local()\n"
        "    imported_alias()\n"
        "    ext.not_indexed()\n"
        "    unknown()\n"
        "    factory().run()\n"
        "    duplicate()\n"
        "\n"
        "class Base:\n"
        "    def inherited(self):\n"
        "        return None\n"
        "\n"
        "class Child(Base):\n"
        "    def own(self):\n"
        "        return None\n"
        "\n"
        "    @classmethod\n"
        "    def class_target(cls):\n"
        "        return None\n"
        "\n"
        "    @classmethod\n"
        "    def class_caller(cls):\n"
        "        cls.class_target()\n"
        "\n"
        "    def runner(self, client):\n"
        "        self.own()\n"
        "        self.inherited()\n"
        "        super().inherited()\n"
        "        client.send()\n"
        "\n"
        "class Incomplete(ExternalBase):\n"
        "    def runner(self):\n"
        "        self.missing()\n"
        "\n"
        "class Root:\n"
        "    def pick(self):\n"
        "        return None\n"
        "\n"
        "class Left(Root):\n"
        "    def pick(self):\n"
        "        return 'left'\n"
        "\n"
        "class Right(Root):\n"
        "    def pick(self):\n"
        "        return 'right'\n"
        "\n"
        "class Diamond(Left, Right):\n"
        "    def runner(self):\n"
        "        self.pick()\n"
        "\n"
        "class Shadowing:\n"
        "    def own(self):\n"
        "        return None\n"
        "\n"
        "    def runner(self):\n"
        "        def nested(self):\n"
        "            self.own()\n"
        "        return nested(object())\n",
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
            first = load_repository(repository, replace=False, client=client, batch_size=2)
            second = load_repository(repository, replace=False, client=client, batch_size=2)
            callsites = client.execute_read(
                """
                MATCH (site:CallSite {path: 'cases.py'})
                WHERE site.owner_qname <> 'cases.Shadowing.runner.<locals>.nested'
                OPTIONAL MATCH (site)-[resolved:RESOLVES_TO]->(target)
                OPTIONAL MATCH (owner)-[calls:CALLS {key: site.key}]->(target)
                OPTIONAL MATCH (owner)-[exact_calls:EXACT_CALLS {key: site.key}]->(target)
                RETURN site.raw_callee AS raw_callee,
                       site.status AS status,
                       site.resolution_strategy AS resolution_strategy,
                       site.candidate_keys AS candidate_keys,
                       count(DISTINCT resolved) AS resolved_edges,
                       count(DISTINCT calls) AS call_edges,
                       count(DISTINCT exact_calls) AS exact_call_edges,
                       collect(DISTINCT target.qname) AS targets
                ORDER BY raw_callee
                """
            )
            shadowed_self = client.execute_read(
                """
                MATCH (site:CallSite {
                    owner_qname: 'cases.Shadowing.runner.<locals>.nested',
                    raw_callee: 'self.own'
                })
                OPTIONAL MATCH (site)-[resolved:RESOLVES_TO]->()
                OPTIONAL MATCH ()-[calls:CALLS {key: site.key}]->()
                OPTIONAL MATCH ()-[exact_calls:EXACT_CALLS {key: site.key}]->()
                RETURN site.status AS status,
                       site.candidate_keys AS candidate_keys,
                       count(DISTINCT resolved) AS resolved_edges,
                       count(DISTINCT calls) AS call_edges,
                       count(DISTINCT exact_calls) AS exact_call_edges
                """
            )
            replacement = load_repository(
                RepositoryIR(
                    repo_name=repository.repo_name,
                    commit=repository.commit,
                    root_path=repository.root_path,
                    files=(),
                ),
                replace=True,
                client=client,
                batch_size=2,
            )
            stale = client.execute_read(
                """
                MATCH ()-[rel:RESOLVES_TO|CALLS|EXACT_CALLS]->()
                WHERE rel.key STARTS WITH $prefix
                RETURN count(rel) AS count
                """,
                {"prefix": f"{repository.repo_name}@{repository.commit}:"},
            )
        finally:
            client.close()

    by_raw = {row["raw_callee"]: row for row in callsites}
    expected = {
        "local": ("exact_local", "cases.local"),
        "imported_alias": ("exact_import", "helpers.imported"),
        "ext.not_indexed": ("external", None),
        "unknown": ("unresolved", None),
        "factory().run": ("dynamic", None),
        "duplicate": ("ambiguous", None),
        "self.own": ("self_direct", "cases.Child.own"),
        "cls.class_target": ("cls_direct", "cases.Child.class_target"),
        "self.inherited": ("inherited_method", "cases.Base.inherited"),
        "super().inherited": ("super_method", "cases.Base.inherited"),
        "client.send": ("dynamic", None),
        "self.missing": ("mro_incomplete", None),
        "self.pick": ("inherited_method", "cases.Left.pick"),
    }
    assert first["resolved_call_sites"] == second["resolved_call_sites"]
    assert first["callsite_statuses"] == second["callsite_statuses"]
    for raw_callee, (status, target) in expected.items():
        row = by_raw[raw_callee]
        assert row["status"] == status
        assert row["resolution_strategy"] == status
        if target is None:
            assert row["resolved_edges"] == 0
            assert row["call_edges"] == 0
            assert row["exact_call_edges"] == 0
            assert row["targets"] == []
        else:
            assert row["resolved_edges"] == 1
            assert row["call_edges"] == 1
            assert row["exact_call_edges"] == 1
            assert row["targets"] == [target]
            assert len(row["candidate_keys"]) == 1
    assert replacement["call_sites"] == 0
    assert stale == [{"count": 0}]
    assert shadowed_self == [
        {
            "status": "unresolved",
            "candidate_keys": [],
            "resolved_edges": 0,
            "call_edges": 0,
            "exact_call_edges": 0,
        }
    ]


def test_resolver_does_not_cross_repository_snapshot_boundaries(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "shared.py").write_text(
        "def target():\n    return None\n",
        encoding="utf-8",
    )
    (second_root / "shared.py").write_text(
        "def caller():\n    target()\n",
        encoding="utf-8",
    )
    first_repository = scan_repository(first_root)
    second_repository = scan_repository(second_root)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            load_repository(first_repository, replace=False, client=client, batch_size=2)
            result = load_repository(second_repository, replace=False, client=client, batch_size=2)
            site = client.execute_read(
                """
                MATCH (site:CallSite {owner_qname: 'shared.caller', raw_callee: 'target'})
                OPTIONAL MATCH (site)-[resolved:RESOLVES_TO]->()
                OPTIONAL MATCH ()-[calls:CALLS {key: site.key}]->()
                RETURN site.status AS status,
                       site.candidate_keys AS candidate_keys,
                       count(DISTINCT resolved) AS resolved_edges,
                       count(DISTINCT calls) AS call_edges
                """
            )
        finally:
            client.close()

    assert result["callsite_statuses"] == {"unresolved": 1}
    assert site == [
        {"status": "unresolved", "candidate_keys": [], "resolved_edges": 0, "call_edges": 0}
    ]
