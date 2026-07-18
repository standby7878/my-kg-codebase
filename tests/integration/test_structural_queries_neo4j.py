from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ir import CallIR, FileIR, InheritanceIR, ModuleInitIR, RepositoryIR, SymbolIR
from codekg.loader import load_repository, symbol_key
from codekg.neo4j_client import Neo4jClient
from codekg.queries.code import (
    SymbolResolutionError,
    find_callees,
    find_callers,
    find_dead_code,
    get_class_hierarchy,
    get_complexity,
    get_definition,
    trace_call_path,
)
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def _repository(name: str, commit: str) -> RepositoryIR:
    module_qname = "shared"
    file = FileIR(
        path="shared.py",
        language="python",
        loc=40,
        module_qname=module_qname,
        module_init=ModuleInitIR("shared.__module__", 1, 30),
        symbols=(
            SymbolIR("function", "caller", "shared.caller", "def caller()", 2, 5),
            SymbolIR("function", "target", "shared.target", "def target()", 7, 8),
            SymbolIR("function", "orphan", "shared.orphan", "def orphan()", 10, 11),
            SymbolIR("function", "duplicate", "shared.duplicate", "def duplicate()", 13, 14),
            SymbolIR("function", "duplicate", "shared.duplicate", "def duplicate()", 16, 17),
            SymbolIR("type", "Base", "shared.Base", "class Base", 19, 20),
            SymbolIR("type", "Child", "shared.Child", "class Child(Base)", 22, 23),
        ),
        inheritance=(InheritanceIR("shared.Child", "Base", "shared.Base"),),
        calls=(
            CallIR(
                owner_qname="shared.__module__",
                raw_callee="caller",
                callee_name="caller",
                callee_qname_hint="shared.caller",
                receiver_kind="none",
                start_line=1,
                start_column=0,
                end_line=1,
                end_column=6,
                ordinal=1,
            ),
            CallIR(
                owner_qname="shared.caller",
                raw_callee="target",
                callee_name="target",
                callee_qname_hint="shared.target",
                receiver_kind="none",
                start_line=3,
                start_column=4,
                end_line=3,
                end_column=10,
                ordinal=2,
            ),
        ),
    )
    return RepositoryIR(repo_name=name, commit=commit, root_path=f"/repos/{name}", files=(file,))


def _key(repo: RepositoryIR, qname: str, line: int) -> str:
    return symbol_key(repo, "shared.py", qname, line)


def _chain_repository() -> RepositoryIR:
    file = FileIR(
        path="flow.py",
        language="python",
        loc=20,
        module_qname="flow",
        module_init=ModuleInitIR("flow.__module__", 1, 20),
        symbols=(
            SymbolIR("function", "source", "flow.source", "def source()", 2, 4),
            SymbolIR("function", "bridge", "flow.bridge", "def bridge()", 6, 8),
            SymbolIR("function", "target", "flow.target", "def target()", 10, 11),
        ),
        calls=(
            CallIR(
                owner_qname="flow.source",
                raw_callee="bridge",
                callee_name="bridge",
                callee_qname_hint="flow.bridge",
                receiver_kind="none",
                start_line=3,
                start_column=4,
                end_line=3,
                end_column=10,
                ordinal=1,
            ),
            CallIR(
                owner_qname="flow.bridge",
                raw_callee="target",
                callee_name="target",
                callee_qname_hint="flow.target",
                receiver_kind="none",
                start_line=7,
                start_column=4,
                end_line=7,
                end_column=10,
                ordinal=1,
            ),
        ),
    )
    return RepositoryIR(repo_name="chain", commit="ccc", root_path="/repos/chain", files=(file,))


def _chain_key(repo: RepositoryIR, qname: str, line: int) -> str:
    return symbol_key(repo, "flow.py", qname, line)


def test_structural_queries_are_key_first_snapshot_safe_and_callsite_authoritative() -> None:
    alpha = _repository("alpha", "aaa")
    beta = _repository("beta", "bbb")
    alpha_caller = _key(alpha, "shared.caller", 2)
    alpha_target = _key(alpha, "shared.target", 7)
    beta_target = _key(beta, "shared.target", 7)
    chain = _chain_repository()
    chain_source = _chain_key(chain, "flow.source", 2)
    chain_bridge = _chain_key(chain, "flow.bridge", 6)
    chain_target = _chain_key(chain, "flow.target", 10)

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            load_repository(alpha, replace=False, client=client, batch_size=2)
            load_repository(beta, replace=False, client=client, batch_size=2)
            load_repository(chain, replace=False, client=client, batch_size=2)
            # A bogus projected edge must never affect the depth-one CallSite query.
            client.execute_write(
                """
                MATCH (target:Function {key: $target_key})
                CREATE (noise:Function {key: 'noise', qname: 'noise.caller', signature: 'def x()'})
                CREATE (noise)-[:CALLS {key: 'noise-edge', resolution: 'fabricated'}]->(target)
                """,
                {"target_key": alpha_target},
            )
            client.execute_write(
                """
                MATCH (source:Function {key: $source_key})
                MATCH (target:Function {key: $target_key})
                CREATE (source)-[:CALLS {
                    key: 'generic-shortcut', resolution: 'fabricated'
                }]->(target)
                """,
                {"source_key": chain_source, "target_key": chain_target},
            )
            client.execute_write(
                """
                MATCH (caller:Function {key: $caller_key})
                MATCH (target:Function {key: $target_key})
                MATCH (orphan:Function {key: $orphan_key})
                MATCH (foreign:Function {key: $foreign_key})
                CREATE (caller)-[:CALLS {
                    key: 'legacy-outbound', resolution: 'legacy_heuristic'
                }]->(orphan)
                CREATE (orphan)-[:CALLS {
                    key: 'legacy-inbound', resolution: 'legacy_heuristic'
                }]->(target)
                CREATE (caller)-[:CALLS {key: 'cross-edge', resolution: 'exact_local'}]->(foreign)
                """,
                {
                    "caller_key": alpha_caller,
                    "target_key": alpha_target,
                    "orphan_key": _key(alpha, "shared.orphan", 10),
                    "foreign_key": beta_target,
                },
            )

            definition = get_definition("shared.target", repo="alpha", commit="aaa", client=client)
            callers = find_callers(alpha_target, depth=1, client=client)
            callees = find_callees(alpha_caller, depth=1, client=client)
            deep_callers = find_callers(alpha_target, depth=2, client=client)
            deep_callees = find_callees(alpha_caller, depth=2, client=client)
            path = trace_call_path(alpha_caller, alpha_target, max_depth=2, client=client)
            exact_path = trace_call_path(chain_source, chain_target, max_depth=3, client=client)
            candidates = find_dead_code("alpha", client=client)
            hierarchy = get_class_hierarchy(
                "shared.Child", repo="alpha", commit="aaa", client=client
            )
            complexity = get_complexity(alpha_target, client=client)
            with pytest.raises(SymbolResolutionError, match="same repository and commit"):
                trace_call_path(alpha_caller, beta_target, client=client)
        finally:
            client.close()

    assert definition[0]["key"] == alpha_target
    assert callers == [
        {
            "key": alpha_caller,
            "qname": "shared.caller",
            "signature": "def caller()",
            "depth": 1,
            "resolution": "exact_local",
        }
    ]
    assert callees == [
        {
            "key": alpha_target,
            "qname": "shared.target",
            "signature": "def target()",
            "depth": 1,
            "resolution": "exact_local",
        }
    ]
    assert deep_callers == [
        {
            "key": alpha_caller,
            "qname": "shared.caller",
            "signature": "def caller()",
            "depth": 1,
        }
    ]
    assert deep_callees == [
        {
            "key": alpha_target,
            "qname": "shared.target",
            "signature": "def target()",
            "depth": 1,
        }
    ]
    assert path == [
        {
            "path": [
                {"key": alpha_caller, "qname": "shared.caller"},
                {"key": alpha_target, "qname": "shared.target"},
            ],
            "depth": 1,
        }
    ]
    assert exact_path == [
        {
            "path": [
                {"key": chain_source, "qname": "flow.source"},
                {"key": chain_bridge, "qname": "flow.bridge"},
                {"key": chain_target, "qname": "flow.target"},
            ],
            "depth": 2,
        }
    ]
    assert candidates == [
        {
            "key": _key(alpha, "shared.duplicate", 13),
            "labels": ["Function"],
            "qname": "shared.duplicate",
            "signature": "def duplicate()",
            "file": "shared.py",
            "start_line": 13,
            "incoming_resolved_calls": 0,
            "confidence": "unreferenced_candidate",
        },
        {
            "key": _key(alpha, "shared.duplicate", 16),
            "labels": ["Function"],
            "qname": "shared.duplicate",
            "signature": "def duplicate()",
            "file": "shared.py",
            "start_line": 16,
            "incoming_resolved_calls": 0,
            "confidence": "unreferenced_candidate",
        },
        {
            "key": _key(alpha, "shared.orphan", 10),
            "labels": ["Function"],
            "qname": "shared.orphan",
            "signature": "def orphan()",
            "file": "shared.py",
            "start_line": 10,
            "incoming_resolved_calls": 0,
            "confidence": "unreferenced_candidate",
        },
    ]
    assert hierarchy == [
        {
            "key": _key(alpha, "shared.Base", 19),
            "qname": "shared.Base",
            "name": "Base",
            "kind": "class",
            "depth": 1,
        }
    ]
    assert complexity[0]["key"] == alpha_target
    assert complexity[0]["cyclomatic"] == 1

    assert beta_target != alpha_target


def test_selector_reports_real_graph_ambiguity_and_snapshot_mismatch() -> None:
    alpha = _repository("alpha", "aaa")
    alpha_target = _key(alpha, "shared.target", 7)
    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            load_repository(alpha, replace=False, client=client, batch_size=2)
            with pytest.raises(SymbolResolutionError, match="requires an explicit repository"):
                get_definition("shared.target", client=client)
            with pytest.raises(SymbolResolutionError, match="ambiguous") as ambiguity:
                get_definition("shared.duplicate", repo="alpha", client=client)
            with pytest.raises(SymbolResolutionError, match="does not belong"):
                find_callees(alpha_target, repo="alpha", commit="other", client=client)
        finally:
            client.close()

    assert _key(alpha, "shared.duplicate", 13) in str(ambiguity.value)
    assert _key(alpha, "shared.duplicate", 16) in str(ambiguity.value)
