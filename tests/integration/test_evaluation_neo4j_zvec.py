from __future__ import annotations

from pathlib import Path

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.evaluation import run_evaluation, write_report
from codekg.neo4j_client import Neo4jClient
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def test_frozen_corpora_run_through_one_neo4j_and_isolated_zvec_indexes(tmp_path: Path) -> None:
    """The checked-in required corpora are an end-to-end correctness gate."""

    pytest.importorskip("zvec")
    root = Path(__file__).parents[2]
    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client=client)
            report = run_evaluation(
                root / "evaluation" / "corpora.json",
                project_root=root,
                zvec_root=tmp_path / "zvec",
                client=client,
            )
        finally:
            client.close()

    write_report(report, tmp_path / "report.json")
    by_id = {corpus["id"]: corpus for corpus in report["corpora"]}
    assert by_id["synthetic-phase1"]["status"] == "passed"
    assert by_id["codekg-dogfood"]["status"] == "passed"
    synthetic = by_id["synthetic-phase1"]
    assert synthetic["structural_metrics"]["has_method"] == {
        "source_declared": 6,
        "graph_edges": 6,
    }
    assert synthetic["structural_metrics"]["inherits"] == {
        "source_clauses": 1,
        "graph_internal_edges": 1,
    }
    assert synthetic["structural_metrics"]["call_sites"]["statuses"] == {
        "cls_direct": 1,
        "dynamic": 3,
        "exact_local": 13,
        "self_direct": 1,
        "super_method": 1,
        "unresolved": 4,
    }
    assert synthetic["truth"]["structural"]["ok"] is True
    assert synthetic["truth"]["call_sites"] == {
        "ok": True,
        "expected_count": 23,
        "actual_count": 23,
        "missing": [],
        "extras": [],
        "mismatches": [],
    }
    assert synthetic["lexical"]["timing_ms"]["sample_count"] == 5
    assert len(synthetic["lexical"]["queries"][0]["samples"]) == 5
    assert by_id["optional-external"] == {
        "id": "optional-external",
        "kind": "external",
        "status": "skipped",
        "reason": "optional path env CODEKG_EVAL_EXTERNAL_PATH is unset",
    }
    assert report["summary"] == {"passed": 2, "failed": 0, "skipped": 1}
    assert (tmp_path / "zvec" / "synthetic-phase1").is_dir()
    assert (tmp_path / "zvec" / "codekg-dogfood").is_dir()
    assert '"format_version": 1' in (tmp_path / "report.json").read_text(encoding="utf-8")
