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
from codekg.queries.code import search_symbols
from codekg.schema.bootstrap import bootstrap_schema
from codekg.search_index import callable_docs_from_repository
from codekg.zvec_store import (
    doc_id_for_key,
    fetch_symbol_docs,
    open_write,
)

pytestmark = pytest.mark.integration


def _write_initial_repository(repo_root: Path) -> None:
    package = repo_root / "services"
    package.mkdir()
    (package / "worker.py").write_text(
        '''def reconcileStandbyState():
    return "ready"


def forge_credential():
    """Renew the boreal axis credential before it expires."""
    return "credential"


def rebuild_index():
    return "rebuilt"
''',
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text(
        """# Operational guide

The `services.worker.rebuild_index` workflow follows the xylophone ledger protocol.
""",
        encoding="utf-8",
    )


def _write_replacement_source(repo_root: Path) -> None:
    (repo_root / "services" / "worker.py").write_text(
        '''def reconcileStandbyState():
    return "current"


def rebuild_index():
    return "rebuilt"


def current_worker():
    """Create the currently configured worker."""
    return "current"
''',
        encoding="utf-8",
    )


def _lexical_qnames(
    query: str,
    *,
    repo_name: str,
    zvec_path: str,
    client: Neo4jClient,
) -> list[str]:
    rows = search_symbols(
        query,
        mode="lexical",
        repo=repo_name,
        zvec_path=zvec_path,
        client=client,
    )
    assert rows
    assert all(
        {"Function", "Method"}.intersection(row["labels"])
        and not {"Document", "DocChunk"}.intersection(row["labels"])
        for row in rows
    )
    return [str(row["qname"]) for row in rows]


def test_descriptive_search_resolves_zvec_descriptions_to_code_graph_nodes(tmp_path: Path) -> None:
    """Exercise the real index and MCP-equivalent lexical-to-graph lookup."""

    pytest.importorskip("zvec")
    repo_root = tmp_path / "descriptive_repo"
    repo_root.mkdir()
    _write_initial_repository(repo_root)
    zvec_path = str(tmp_path / "zvec")

    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            first = index_repository(repo_root, replace=False, client=client, zvec_path=zvec_path)
            initial_repo = scan_repository(repo_root)
            initial_docs = callable_docs_from_repository(initial_repo)

            # ModuleInit remains structural-only.  It is deliberately not a
            # descriptive zvec document.
            assert first["descriptions"] == 3
            assert _lexical_qnames(
                "boreal axis", repo_name=initial_repo.repo_name, zvec_path=zvec_path, client=client
            ) == ["services.worker.forge_credential"]
            assert _lexical_qnames(
                "reconcile standby state",
                repo_name=initial_repo.repo_name,
                zvec_path=zvec_path,
                client=client,
            ) == ["services.worker.reconcileStandbyState"]
            assert _lexical_qnames(
                "xylophone ledger protocol",
                repo_name=initial_repo.repo_name,
                zvec_path=zvec_path,
                client=client,
            ) == ["services.worker.rebuild_index"]

            collection = open_write(zvec_path)
            for doc in initial_docs:
                safe_id = doc_id_for_key(doc.key)
                assert safe_id != doc.key
                assert ":" not in safe_id
                assert "@" not in safe_id
                assert len(safe_id) == 64
                assert all(character in "0123456789abcdef" for character in safe_id)
                assert fetch_symbol_docs(collection, {doc.key})[doc.key]["key"] == doc.key
            del collection
            gc.collect()

            _write_replacement_source(repo_root)
            replacement = index_repository(
                repo_root,
                replace=True,
                client=client,
                zvec_path=zvec_path,
            )
            current_repo = scan_repository(repo_root)
            current_docs = callable_docs_from_repository(current_repo)
            current_keys = {doc.key for doc in current_docs}
            old_keys = {doc.key for doc in initial_docs}

            assert replacement["descriptions"] == 3
            collection = open_write(zvec_path)
            assert fetch_symbol_docs(collection, old_keys) == {}
            current_records = fetch_symbol_docs(collection, current_keys)
            assert set(current_records) == current_keys
            assert all(current_records[key]["key"] == key for key in current_keys)
            del collection
            gc.collect()
            script = """
from codekg.zvec_store import open_read, search_symbols
import sys
collection = open_read(sys.argv[1])
old_hits = search_symbols(collection, 'boreal axis', repo=sys.argv[2])
current_hits = search_symbols(collection, 'currently configured', repo=sys.argv[2])
print(len(old_hits), len(current_hits))
"""
            root = Path(__file__).parents[2]
            env = {
                **os.environ,
                "PYTHONPATH": f"{root / 'src'}:{os.environ.get('PYTHONPATH', '')}",
            }
            reader = subprocess.run(
                [sys.executable, "-c", script, zvec_path, current_repo.repo_name],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            assert reader.stdout.strip() == "0 1"
        finally:
            client.close()
