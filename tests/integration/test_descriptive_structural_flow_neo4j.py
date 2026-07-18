from __future__ import annotations

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.ingest import index_repository, scan_repository
from codekg.neo4j_client import Neo4jClient
from codekg.queries.code import (
    SymbolResolutionError,
    find_callees,
    find_callers,
    get_definition,
    search_symbols,
    trace_call_path,
)
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def _write_alpha_source(root: Path) -> None:
    (root / "api.py").write_text(
        '''import thirdparty


def calibrate_target():
    """Calibrate the rare zircon ion thermostat."""
    return "ready"


def dispatch_temperature():
    return calibrate_target()


def external_dispatch():
    return thirdparty.send()


def dynamic_dispatch():
    return make_client().send()
''',
        encoding="utf-8",
    )


def _write_alpha_replacement(root: Path) -> None:
    (root / "api.py").write_text(
        '''def refresh_thermostat():
    """Refresh the newly minted cobalt actuator."""
    return "current"
''',
        encoding="utf-8",
    )


def _write_beta_source(root: Path) -> None:
    (root / "api.py").write_text(
        '''def calibrate_target():
    """Calibrate the beta-only target."""
    return "beta"


def dispatch_temperature():
    return calibrate_target()
''',
        encoding="utf-8",
    )


def _lexical(
    query: str,
    *,
    repo: str,
    zvec_path: str,
    client: Neo4jClient,
) -> list[dict[str, object]]:
    return search_symbols(
        query,
        mode="lexical",
        repo=repo,
        zvec_path=zvec_path,
        client=client,
    )


def _fresh_reader_counts(zvec_path: str, repo: str) -> tuple[int, int]:
    """Read in a new process to prove optimize/flush publication completed."""

    script = """
from codekg.zvec_store import open_read, search_symbols
import sys
collection = open_read(sys.argv[1])
old_hits = search_symbols(collection, 'rare zircon ion', repo=sys.argv[2])
new_hits = search_symbols(collection, 'newly minted cobalt', repo=sys.argv[2])
print(len(old_hits), len(new_hits))
"""
    root = Path(__file__).parents[2]
    environment = {
        **os.environ,
        "PYTHONPATH": f"{root / 'src'}:{os.environ.get('PYTHONPATH', '')}",
    }
    completed = subprocess.run(
        [sys.executable, "-c", script, zvec_path, repo],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(int(value) for value in completed.stdout.split())  # type: ignore[return-value]


def test_descriptive_discovery_returns_an_exact_key_for_snapshot_safe_navigation_and_replace(
    tmp_path: Path,
) -> None:
    """Exercise the real source -> Neo4j + zvec -> public-query workflow."""

    pytest.importorskip("zvec")
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    _write_alpha_source(alpha)
    _write_beta_source(beta)
    zvec_path = str(tmp_path / "zvec")

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            alpha_result = index_repository(
                alpha, replace=False, client=client, zvec_path=zvec_path
            )
            beta_result = index_repository(beta, replace=False, client=client, zvec_path=zvec_path)
            alpha_repo = scan_repository(alpha)
            beta_repo = scan_repository(beta)

            # Only real Function/Method nodes enter zvec; every file still
            # has a structural ModuleInit node for top-level executable scope.
            assert alpha_result["descriptions"] == 4
            assert beta_result["descriptions"] == 2
            module_init = client.execute_read(
                """
                MATCH (:Repository {repo_name: $repo})-[:CONTAINS]->(:File {path: 'api.py'})
                    -[:CONTAINS]->(init:ModuleInit)
                RETURN init.qname AS qname
                """,
                {"repo": alpha_repo.repo_name},
            )
            assert module_init == [{"qname": "api.__module__"}]

            lexical_hit = _lexical(
                "rare zircon ion thermostat",
                repo=alpha_repo.repo_name,
                zvec_path=zvec_path,
                client=client,
            )
            assert [row["qname"] for row in lexical_hit] == ["api.calibrate_target"]
            target_key = str(lexical_hit[0]["key"])
            assert target_key.startswith(f"{alpha_repo.repo_name}@{alpha_repo.commit}:")
            assert all("__module__" not in str(row["qname"]) for row in lexical_hit)

            # The zvec hit's exact Neo4j key is passed unchanged into every
            # structural query; the resulting edges are snapshot-local facts.
            expected_caller_key = (
                f"{alpha_repo.repo_name}@{alpha_repo.commit}:api.py:api.dispatch_temperature:9"
            )
            callers = find_callers(target_key, client=client)
            assert callers == [
                {
                    "key": expected_caller_key,
                    "qname": "api.dispatch_temperature",
                    "signature": "def dispatch_temperature()",
                    "depth": 1,
                    "resolution": "exact_local",
                }
            ]
            caller_key = str(callers[0]["key"])
            assert find_callees(caller_key, client=client) == [
                {
                    "key": target_key,
                    "qname": "api.calibrate_target",
                    "signature": "def calibrate_target()",
                    "depth": 1,
                    "resolution": "exact_local",
                }
            ]
            assert trace_call_path(caller_key, target_key, client=client) == [
                {
                    "path": [
                        {"key": caller_key, "qname": "api.dispatch_temperature"},
                        {"key": target_key, "qname": "api.calibrate_target"},
                    ],
                    "depth": 1,
                }
            ]

            # Raw calls that cannot be proved static remain visible as facts,
            # but must not create a false structural target edge.
            unresolved = client.execute_read(
                """
                MATCH (site:CallSite)
                WHERE site.owner_qname IN ['api.external_dispatch', 'api.dynamic_dispatch']
                  AND site.raw_callee IN ['thirdparty.send', 'make_client().send']
                OPTIONAL MATCH (site)-[resolution:RESOLVES_TO]->()
                RETURN site.owner_qname AS owner,
                       site.raw_callee AS raw_callee,
                       site.status AS status,
                       count(resolution) AS resolved_edges
                ORDER BY owner
                """
            )
            assert unresolved == [
                {
                    "owner": "api.dynamic_dispatch",
                    "raw_callee": "make_client().send",
                    "status": "dynamic",
                    "resolved_edges": 0,
                },
                {
                    "owner": "api.external_dispatch",
                    "raw_callee": "thirdparty.send",
                    "status": "external",
                    "resolved_edges": 0,
                },
            ]

            # Identical qnames in a second repository never form a fallback
            # identity.  A qname needs a repo; an exact key rejects mismatch.
            beta_target = _lexical(
                "beta-only target",
                repo=beta_repo.repo_name,
                zvec_path=zvec_path,
                client=client,
            )[0]
            with pytest.raises(SymbolResolutionError, match="requires an explicit repository"):
                get_definition("api.calibrate_target", client=client)
            with pytest.raises(SymbolResolutionError, match="does not belong"):
                find_callees(target_key, repo=beta_repo.repo_name, client=client)
            with pytest.raises(SymbolResolutionError, match="same repository and commit"):
                trace_call_path(target_key, str(beta_target["key"]), client=client)

            _write_alpha_replacement(alpha)
            replacement = index_repository(alpha, replace=True, client=client, zvec_path=zvec_path)
            current_alpha = scan_repository(alpha)
            assert replacement["descriptions"] == 1
            assert (
                _lexical(
                    "rare zircon ion thermostat",
                    repo=current_alpha.repo_name,
                    zvec_path=zvec_path,
                    client=client,
                )
                == []
            )
            replacement_hits = _lexical(
                "newly minted cobalt actuator",
                repo=current_alpha.repo_name,
                zvec_path=zvec_path,
                client=client,
            )
            assert [row["qname"] for row in replacement_hits] == ["api.refresh_thermostat"]
            assert (
                client.execute_read(
                    "MATCH (s {key: $key}) RETURN s.key AS key", {"key": target_key}
                )
                == []
            )
            gc.collect()
            assert _fresh_reader_counts(zvec_path, current_alpha.repo_name) == (0, 1)
        finally:
            client.close()
