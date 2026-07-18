from __future__ import annotations

import pytest

from codekg.queries.code import (
    find_callees,
    find_callers,
    find_dead_code,
    get_complexity,
    search_symbols,
    trace_call_path,
)
from codekg.queries.repositories import list_repositories

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []

    def execute_read(
        self,
        query: str,
        params: dict[str, object] | None = None,
        *,
        max_rows: int = 1000,
    ) -> list[dict[str, object]]:
        self.calls.append((query, params or {}, max_rows))
        return [{"ok": True}]


def test_list_repositories_uses_read_query() -> None:
    client = FakeClient()

    rows = list_repositories(client=client)  # type: ignore[arg-type]

    assert rows == [{"ok": True}]
    assert "MATCH (r:Repository)" in client.calls[0][0]


def test_search_symbols_caps_limit_and_filters_kind() -> None:
    client = FakeClient()

    search_symbols("backup", kind="method", repo="patroni", limit=999, client=client)  # type: ignore[arg-type]

    query, params, max_rows = client.calls[0]
    assert 'db.index.fulltext.queryNodes("code_symbol_search", $fulltext_query)' in query
    assert "s:Method" in query
    assert params["fulltext_query"] == "backup"
    assert params["repo"] == "patroni"
    assert params["limit"] == 500
    assert max_rows == 500


def test_search_symbols_sanitizes_simple_fulltext_input() -> None:
    client = FakeClient()

    search_symbols("pkg.mod.run", client=client)  # type: ignore[arg-type]

    query, params, _ = client.calls[0]
    assert 'db.index.fulltext.queryNodes("code_symbol_search", $fulltext_query)' in query
    assert params["fulltext_query"] == "pkg AND mod AND run"


def test_search_symbols_lexical_resolves_zvec_hits(monkeypatch) -> None:
    client = FakeClient()

    def fake_open_read(path):
        assert path is None
        return object()

    def fake_zvec_search(collection, q, *, repo, kind, limit):
        assert q == "promote standby"
        assert repo == "patroni"
        assert kind == "method"
        assert limit == 25
        return [
            {
                "key": "patroni@abc:ha.py:patroni.ha.Ha.promote:10",
                "score": 1.5,
                "fields": {
                    "text": "promote standby\nChoose a node to promote.",
                },
            }
        ]

    def fake_execute_read(query, params=None, *, max_rows=1000):
        client.calls.append((query, params or {}, max_rows))
        return [
            {
                "key": "patroni@abc:ha.py:patroni.ha.Ha.promote:10",
                "labels": ["Method"],
                "name": "promote",
                "qname": "patroni.ha.Ha.promote",
                "signature": "def promote(self)",
                "start_line": 10,
                "end_line": 20,
                "file": "ha.py",
                "repo": "patroni",
                "commit": "abc",
            }
        ]

    client.execute_read = fake_execute_read  # type: ignore[method-assign]
    monkeypatch.setattr("codekg.queries.code.open_zvec_read", fake_open_read)
    monkeypatch.setattr("codekg.queries.code.zvec_search_symbols", fake_zvec_search)

    rows = search_symbols(
        "promote standby",
        kind="method",
        repo="patroni",
        mode="lexical",
        client=client,  # type: ignore[arg-type]
    )

    assert rows[0]["key"] == "patroni@abc:ha.py:patroni.ha.Ha.promote:10"
    assert rows[0]["score"] == 1.5
    assert "Choose a node" in rows[0]["snippet"]
    assert client.calls[0][1]["keys"] == ["patroni@abc:ha.py:patroni.ha.Ha.promote:10"]


def test_search_symbols_lexical_omits_stale_hits_and_preserves_zvec_rank(monkeypatch) -> None:
    client = FakeClient()

    monkeypatch.setattr("codekg.queries.code.open_zvec_read", lambda path: object())
    monkeypatch.setattr(
        "codekg.queries.code.zvec_search_symbols",
        lambda *args, **kwargs: [
            {
                "key": "stale",
                "score": 5.0,
                "fields": {"text": "stale"},
            },
            {"key": "two", "score": 4.0, "fields": {"text": "second result"}},
            {"key": "one", "score": 3.0, "fields": {"text": "first result"}},
        ],
    )

    def fake_execute_read(query, params=None, *, max_rows=1000):
        client.calls.append((query, params or {}, max_rows))
        return [
            {"key": "one", "labels": ["Function"], "name": "one"},
            {"key": "two", "labels": ["Method"], "name": "two"},
        ]

    client.execute_read = fake_execute_read  # type: ignore[method-assign]

    rows = search_symbols("standby primary", mode="lexical", client=client)  # type: ignore[arg-type]

    assert [row["key"] for row in rows] == ["two", "one"]
    assert [row["snippet"] for row in rows] == ["second result", "first result"]
    assert client.calls[0][1]["keys"] == ["stale", "two", "one"]


def test_variable_depth_queries_inline_bounded_depth() -> None:
    client = FakeClient()

    find_callers("pkg.fn", depth=999, client=client)  # type: ignore[arg-type]
    find_callees("pkg.fn", depth=999, client=client)  # type: ignore[arg-type]
    trace_call_path("pkg.fn", "pkg.other", max_depth=999, client=client)  # type: ignore[arg-type]

    caller_query = client.calls[0][0]
    callee_query = client.calls[1][0]
    trace_query = client.calls[2][0]
    assert "[:CALLS*1..10]" in caller_query
    assert "MATCH (callee:Function {qname: $qname})" in caller_query
    assert "MATCH (callee:Method {qname: $qname})" in caller_query
    assert "MATCH (caller:Function {qname: $qname})" in callee_query
    assert "MATCH (caller:Method {qname: $qname})" in callee_query
    assert "MATCH (source:Function {qname: $from_qname})" in trace_query
    assert "MATCH (target:Method {qname: $to_qname})" in trace_query
    assert trace_query.count("UNION") == 3


def test_find_dead_code_filters_module_pseudo_functions() -> None:
    client = FakeClient()

    find_dead_code("sample", client=client)  # type: ignore[arg-type]

    query = client.calls[0][0]
    assert "s.name <> '<module>'" in query
    assert "AND NOT s.qname ENDS WITH '.__module__'" in query


def test_get_complexity_supports_top_n_mode() -> None:
    client = FakeClient()

    get_complexity(repo="patroni", top_n=5, client=client)  # type: ignore[arg-type]

    assert client.calls[0][1]["limit"] == 5
