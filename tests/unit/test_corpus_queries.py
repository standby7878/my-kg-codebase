from __future__ import annotations

import pytest

from codekg.queries.corpus import (
    compare_corpus_snapshots,
    get_dependency_evidence,
    list_corpus_snapshots,
    search_corpus_symbols,
    trace_corpus_path,
)


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def execute_read(self, query, parameters=None, *, max_rows=1000, timeout_seconds=None):
        assert timeout_seconds == 10.0
        self.calls.append((query, parameters, max_rows))
        return self.results.pop(0) if self.results else []


def test_corpus_search_parameters_are_scoped_and_bounded():
    client = FakeClient()
    search_corpus_symbols("x' RETURN 1", snapshot_alias="pg18", client=client, limit=5)
    query, params, bound = client.calls[0]
    assert "x' RETURN 1" not in query
    assert params["query"] == "x' RETURN 1"
    assert params["alias"] == "pg18" and bound == 5
    assert "MATCH (s:NativeSymbol)" in query and "MATCH (s:Routine)" in query
    assert "UNION ALL" in query


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (10, -1)])
def test_corpus_paging_rejects_invalid_bounds(limit, offset):
    client = FakeClient()
    with pytest.raises(ValueError):
        list_corpus_snapshots(limit=limit, offset=offset, client=client)
    assert not client.calls


def test_asserted_trace_excludes_candidates_and_conditions():
    client = FakeClient()
    trace_corpus_path("app:sql", "pg:api", max_depth=4, client=client)
    query, params, bound = client.calls[0]
    assert "*1..4" in query and "NATIVE_CANDIDATE" not in query
    assert "e.status='exact'" in query and "coalesce(e.condition,'')=''" in query
    # Universal predicates are applied by the shortest-path planner before it
    # decides which exact, unguarded route is shortest.
    assert "all(e IN relationships(p)" in query and "HAS_EVIDENCE" in query
    assert "{status:'exact'}" not in query
    assert "MATCH (a:NativeSymbol {key:$from_key})" in query
    assert "MATCH (b:Routine {key:$to_key})" in query
    assert params["from_key"] == "app:sql" and bound == 5
    with pytest.raises(ValueError):
        trace_corpus_path("a", "b", max_depth=99, client=client)


def test_direct_evidence_keeps_candidate_types_visible():
    client = FakeClient()
    get_dependency_evidence("a", direction="incoming", client=client)
    assert "(b)-[e]->(a)" in client.calls[0][0]
    assert "NATIVE_CANDIDATE" in client.calls[0][0]
    assert "HAS_EVIDENCE" in client.calls[0][0]
    assert "b.status AS related_status" in client.calls[0][0]
    assert "b.dynamic AS dynamic" in client.calls[0][0]
    assert "MATCH (a:SourceEvidence {key:$key})" in client.calls[0][0]


def test_comparison_requires_same_logical_repo_and_matches_names_before_signatures():
    client = FakeClient([{"left_repo": "postgres", "right_repo": "postgres"}], [])
    compare_corpus_snapshots("pg18", "pg19", client=client)
    query = client.calls[1][0]
    assert "logical_id=identity" in query
    assert "signature_changed" in query and "ambiguous" in query
    assert "return_type" in query and "definition_changed" in query
    assert "condition_changed" in query and "old[0].condition" in query
    assert "comparison_primary" in query
    assert "MATCH (n)" not in query
    assert "MATCH (a:NativeSymbol)" in query and "MATCH (ar:Routine)" in query
    wrong = FakeClient([{"left_repo": "postgres", "right_repo": "postgis"}])
    with pytest.raises(ValueError, match="logical repository"):
        compare_corpus_snapshots("a", "b", client=wrong)
    assert len(wrong.calls) == 1


def test_comparison_downgrades_absence_when_extraction_is_incomplete():
    client = FakeClient(
        [
            {
                "left_repo": "postgres",
                "right_repo": "postgres",
                "left_incomplete": 1,
                "right_incomplete": 0,
            }
        ],
        [],
    )
    compare_corpus_snapshots("pg18", "pg19", client=client)
    coverage_query, coverage_params, _ = client.calls[0]
    assert "CorpusDiagnostic" in coverage_query
    assert "file_too_large" in coverage_params["incomplete_categories"]
    query, params, _ = client.calls[1]
    assert "(size(old)=0 OR size(new)=0) AND $incomplete THEN 'ambiguous'" in query
    assert params["incomplete"] is True
