from __future__ import annotations

import base64
import json

import pytest

from codekg.queries.code import (
    SymbolResolutionError,
    discover_symbols,
    find_callees,
    find_callers,
    find_dead_code,
    get_class_hierarchy,
    get_complexity,
    get_definition,
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


class SelectorClient(FakeClient):
    """Small query double that makes selector resolution explicit."""

    def __init__(self, exact_rows=None, qname_rows=None) -> None:
        super().__init__()
        self.exact_rows = exact_rows or []
        self.qname_rows = qname_rows or []

    def execute_read(self, query, params=None, *, max_rows=1000):  # type: ignore[no-untyped-def]
        self.calls.append((query, params or {}, max_rows))
        if "codekg: exact-symbol-selector" in query:
            if isinstance(self.exact_rows, dict):
                return self.exact_rows.get((params or {})["identifier"], [])
            return self.exact_rows
        if "codekg: qname-symbol-selector" in query:
            return self.qname_rows
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
    assert params["commit"] is None
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

    def fake_zvec_search(collection, q, *, repo, commit, kind, limit):
        assert q == "promote standby"
        assert repo == "patroni"
        assert commit is None
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
    assert client.calls[0][1]["repo"] is None


class DiscoveryClient(FakeClient):
    def __init__(self, repositories, symbol_rows=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.repositories = repositories
        self.symbol_rows = symbol_rows or []

    def execute_read(self, query, params=None, *, max_rows=1000):  # type: ignore[no-untyped-def]
        self.calls.append((query, params or {}, max_rows))
        if "RETURN r.repo_name AS repo_name" in query:
            return self.repositories
        if 'db.index.fulltext.queryNodes("code_symbol_search"' in query:
            return self.symbol_rows
        raise AssertionError("unexpected query")


def _repository(name="requests", commit="abc") -> dict[str, object]:
    return {"repo_name": name, "commit": commit}


def test_discover_symbols_requires_repository_when_multiple_are_indexed() -> None:
    client = DiscoveryClient([_repository("click"), _repository("requests")])

    response = discover_symbols("session", mode="graph", client=client)  # type: ignore[arg-type]

    assert response == {
        "status": "repository_required",
        "available_repositories": ["click", "requests"],
        "results": [],
        "next_cursor": None,
    }
    assert len(client.calls) == 1


def test_discover_symbols_missing_repository_does_not_search_globally() -> None:
    client = DiscoveryClient([_repository("click"), _repository("requests")])

    response = discover_symbols("promote", repository="patroni", client=client)  # type: ignore[arg-type]

    assert response["status"] == "repository_not_found"
    assert response["available_repositories"] == ["click", "requests"]
    assert len(client.calls) == 1


def test_discover_symbols_compacts_results_and_paginates() -> None:
    rows = [
        {
            "key": f"requests@abc:sessions.py:requests.Session.{name}:{line}",
            "name": name,
            "qname": f"requests.Session.{name}",
            "file": "requests/sessions.py",
            "start_line": line,
            "end_line": line + 1,
            "signature": "def prepare_request(self, request)",
            "score": 1.0,
        }
        for line, name in ((1, "prepare_request"), (2, "request"), (3, "send"))
    ]
    client = DiscoveryClient([_repository()], rows)

    response = discover_symbols(
        "prepare request", repository="requests", limit=2, mode="graph", client=client
    )  # type: ignore[arg-type]

    assert response["status"] == "ok"
    assert response["repository"] == "requests"
    assert len(response["results"]) == 2
    first = response["results"][0]
    assert set(first) == {
        "symbol_id",
        "qualified_name",
        "file",
        "start_line",
        "end_line",
        "score",
        "matched_terms",
        "match_type",
        "scope",
        "signature",
    }
    assert first["qualified_name"] == "requests.Session.prepare_request"
    # B3 (codekg-ranking-presentation-spec.md): signature is emitted so the
    # model can distinguish same-named candidates without a get_definition
    # round trip.
    assert first["signature"] == "def prepare_request(self, request)"
    assert response["next_cursor"]
    payload = json.loads(
        base64.urlsafe_b64decode(
            response["next_cursor"] + "=" * (-len(response["next_cursor"]) % 4)
        )
    )
    assert payload["v"] == 2
    assert payload["s"] == "source"
    assert client.calls[1][1]["repo"] == "requests"
    assert client.calls[1][1]["commit"] == "abc"
    assert client.calls[1][1]["limit"] == 100

    next_page = discover_symbols(
        "prepare request",
        repository="requests",
        limit=2,
        mode="graph",
        cursor=response["next_cursor"],
        client=client,
    )  # type: ignore[arg-type]
    assert [row["qualified_name"] for row in next_page["results"]] == ["requests.Session.send"]
    assert client.calls[3][1]["limit"] == 100


def test_discover_symbols_truncates_long_signature() -> None:
    long_signature = "def f(" + ", ".join(f"arg{i}: int" for i in range(40)) + ") -> None"
    assert len(long_signature) > 200
    rows = [
        {
            "key": "pkg@abc:mod.py:pkg.f:1",
            "name": "f",
            "qname": "pkg.f",
            "file": "pkg/mod.py",
            "start_line": 1,
            "end_line": 2,
            "signature": long_signature,
            "score": 1.0,
        }
    ]
    client = DiscoveryClient([_repository()], rows)

    response = discover_symbols("f", repository="requests", mode="graph", client=client)  # type: ignore[arg-type]

    signature = response["results"][0]["signature"]
    assert len(signature) == 200
    assert signature.endswith("…")
    assert signature[:-1] == long_signature[:199]


def test_discover_symbols_signature_null_when_absent() -> None:
    rows = [
        {
            "key": "pkg@abc:mod.py:pkg.T:1",
            "name": "T",
            "qname": "pkg.T",
            "file": "pkg/mod.py",
            "start_line": 1,
            "end_line": 2,
            "score": 1.0,
        }
    ]
    client = DiscoveryClient([_repository()], rows)

    response = discover_symbols("T", repository="requests", mode="graph", client=client)  # type: ignore[arg-type]

    assert response["results"][0]["signature"] is None


def test_discover_backend_query_excludes_stopwords() -> None:
    client = DiscoveryClient([_repository()], [])

    discover_symbols(
        "request with auth",
        repository="requests",
        mode="graph",
        client=client,  # type: ignore[arg-type]
    )

    assert client.calls[1][1]["fulltext_query"] == "request AND auth"


def test_discover_lexical_backend_query_excludes_stopwords(monkeypatch) -> None:
    client = DiscoveryClient([_repository()], [])
    observed: list[str] = []

    def lexical(query, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(query)
        return []

    monkeypatch.setattr("codekg.queries.code._search_symbols_lexical", lexical)
    discover_symbols(
        "request with auth",
        repository="requests",
        mode="lexical",
        client=client,  # type: ignore[arg-type]
    )

    assert observed == ["request auth"]


def test_discover_one_token_prose_does_not_get_qname_suffix_tier() -> None:
    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "suffix",
                "name": "other",
                "qname": "pkg.Session.request",
                "file": "pkg.py",
                "start_line": 1,
                "end_line": 1,
                "score": 1,
            },
            {
                "key": "name",
                "name": "request",
                "qname": "pkg.request",
                "file": "pkg.py",
                "start_line": 2,
                "end_line": 2,
                "score": 1,
            },
        ],
    )

    response = discover_symbols("request", repository="requests", mode="graph", client=client)  # type: ignore[arg-type]

    assert response["results"][0]["symbol_id"] == "name"
    assert response["results"][0]["match_type"] == "exact_name"
    assert response["results"][1]["match_type"] == "terms"


def test_discover_prepared_request_prepare_exact_and_suffix_tiers() -> None:
    row = {
        "key": "prepare",
        "name": "prepare",
        "qname": "requests.models.PreparedRequest.prepare",
        "file": "requests/models.py",
        "start_line": 1,
        "end_line": 2,
        "score": 0,
    }
    client = DiscoveryClient([_repository()], [row])

    suffix = discover_symbols(
        "PreparedRequest.prepare", repository="requests", mode="graph", client=client
    )  # type: ignore[arg-type]
    exact = discover_symbols(
        "requests.models.PreparedRequest.prepare",
        repository="requests",
        mode="graph",
        client=client,
    )  # type: ignore[arg-type]

    assert suffix["results"][0]["match_type"] == "qualified_name_suffix"
    assert exact["results"][0]["match_type"] == "exact_qualified_name"


def test_discover_analyzer_rejects_stopword_only_query_without_backend_search() -> None:
    client = DiscoveryClient([_repository()])

    response = discover_symbols("the and with", repository="requests", client=client)  # type: ignore[arg-type]

    assert response["status"] == "invalid_query"
    assert response["diagnostics"]["query_terms"] == []
    assert response["diagnostics"]["ignored_terms"] == ["the", "and", "with"]
    assert client.calls == []


def test_discover_qname_suffix_and_exact_tiers_beat_prose() -> None:
    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "prose",
                "name": "helper",
                "qname": "requests.helper",
                "file": "requests/helper.py",
                "start_line": 1,
                "end_line": 2,
                "snippet": "Session request instructions",
                "score": 999,
            },
            {
                "key": "suffix",
                "name": "request",
                "qname": "requests.sessions.Session.request",
                "file": "requests/sessions.py",
                "start_line": 3,
                "end_line": 4,
                "score": 0,
            },
        ],
    )

    response = discover_symbols(
        "Session.request",
        repository="requests",
        mode="graph",
        client=client,  # type: ignore[arg-type]
    )

    assert response["results"][0]["symbol_id"] == "suffix"
    assert response["results"][0]["match_type"] == "qualified_name_suffix"
    assert response["results"][0]["matched_terms"] == ["session", "request"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pkg/tests/test_client.py", "tests"),
        ("pkg/testing/client.py", "tests"),
        ("pkg/fixtures/client.py", "tests"),
        ("pkg/conftest.py", "tests"),
        ("pkg/client_test.py", "tests"),
        ("benchmarks/search.py", "benchmarks"),
        ("pkg/benchmark_search.py", "benchmarks"),
        ("examples/demo.py", "examples"),
        ("samples/demo.py", "examples"),
        ("docs/usage.md", "docs"),
        ("pkg/module.py", "source"),
        ("strange/location/generated.py", "source"),
    ],
)
def test_discovery_path_scope_classifier(path: str, expected: str) -> None:
    from codekg.queries.code import _path_scope

    assert _path_scope(path) == expected


def test_discover_scope_filters_before_ranking_and_pagination() -> None:
    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "test",
                "name": "request",
                "qname": "tests.request",
                "file": "tests/test_api.py",
                "start_line": 1,
                "end_line": 2,
                "score": 999,
            },
            {
                "key": "source",
                "name": "request",
                "qname": "requests.request",
                "file": "requests/api.py",
                "start_line": 1,
                "end_line": 2,
                "score": 1,
            },
        ],
    )

    response = discover_symbols(
        "request", repository="requests", mode="graph", limit=1, client=client
    )  # type: ignore[arg-type]

    assert [row["symbol_id"] for row in response["results"]] == ["source"]
    assert response["next_cursor"] is None
    assert response["diagnostics"]["scoped_candidate_count"] == 1
    assert response["diagnostics"]["candidate_pool"] == 100


def test_discover_retrieval_scope_matrix_includes_fixtures_as_tests() -> None:
    rows = [
        {
            "key": "source",
            "name": "request",
            "qname": "pkg.request",
            "file": "pkg/request.py",
            "start_line": 1,
            "end_line": 1,
            "score": 1,
        },
        {
            "key": "tests",
            "name": "request",
            "qname": "pkg.tests.request",
            "file": "fixtures/request.py",
            "start_line": 1,
            "end_line": 1,
            "score": 1,
        },
        {
            "key": "docs",
            "name": "request",
            "qname": "pkg.docs.request",
            "file": "docs/request.py",
            "start_line": 1,
            "end_line": 1,
            "score": 1,
        },
        {
            "key": "examples",
            "name": "request",
            "qname": "pkg.examples.request",
            "file": "samples/request.py",
            "start_line": 1,
            "end_line": 1,
            "score": 1,
        },
        {
            "key": "benchmarks",
            "name": "request",
            "qname": "pkg.benchmarks.request",
            "file": "benchmark_request.py",
            "start_line": 1,
            "end_line": 1,
            "score": 1,
        },
    ]
    expected = {
        "source": {"source"},
        "tests": {"tests"},
        "docs": {"docs"},
        "examples": {"examples"},
        "benchmarks": {"benchmarks"},
        "all": {"source", "tests", "docs", "examples", "benchmarks"},
    }

    for scope, ids in expected.items():
        client = DiscoveryClient([_repository()], rows)
        response = discover_symbols(
            "request",
            repository="requests",
            mode="graph",
            scope=scope,
            client=client,  # type: ignore[arg-type]
        )
        assert {row["symbol_id"] for row in response["results"]} == ids


def test_discover_cursor_binds_scope_and_analyzer() -> None:
    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "one",
                "name": "request",
                "qname": "requests.request",
                "file": "requests/api.py",
                "start_line": 1,
                "end_line": 2,
                "score": 1,
            },
            {
                "key": "two",
                "name": "request",
                "qname": "requests.other_request",
                "file": "requests/api.py",
                "start_line": 3,
                "end_line": 4,
                "score": 1,
            },
        ],
    )
    first = discover_symbols("request", repository="requests", mode="graph", limit=1, client=client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="cursor"):
        discover_symbols(
            "request",
            repository="requests",
            mode="graph",
            scope="all",
            cursor=first["next_cursor"],
            client=client,
        )  # type: ignore[arg-type]


def test_merge_zvec_fields_supplements_graph_row() -> None:
    from codekg.queries.code import _merge_zvec_hit

    row = _merge_zvec_hit(
        {
            "key": "one",
            "score": 1.0,
            "fields": {
                "qname": "pkg.Session.request",
                "signature": "def request()",
                "path": "pkg/session.py",
                "text": "request docs",
            },
        },
        {"one": {"key": "one"}},
    )

    assert row["qname"] == "pkg.Session.request"
    assert row["signature"] == "def request()"
    assert row["file"] == "pkg/session.py"
    assert row["snippet"] == "request docs"


def test_merge_zvec_description_excludes_generated_identifier_header() -> None:
    from codekg.queries.code import _merge_zvec_hit

    row = _merge_zvec_hit(
        {
            "key": "one",
            "score": 1.0,
            "fields": {
                "qname": "pkg.Session.request",
                "signature": "def request()",
                "path": "pkg/session.py",
                "text": (
                    "request\nrequest\npkg.Session.request\ndef request()\n"
                    "Merge session state defaults."
                ),
            },
        },
        {"one": {"key": "one"}},
    )

    assert row["_description"] == "Merge session state defaults."
    assert row["snippet"] == "Merge session state defaults."


def test_prepared_request_behavioral_description_beats_generic_identifier_terms() -> None:
    from codekg.queries.code import _rank_discovery_rows

    ranked = _rank_discovery_rows(
        "PreparedRequest prepare with session state merge defaults request method",
        [
            {
                "key": "merge-environment-settings",
                "name": "merge_environment_settings",
                "qname": "requests.sessions.Session.merge_environment_settings",
                "signature": "def merge_environment_settings(self, request)",
                "file": "requests/sessions.py",
                "_description": "Check the environment and merge it with some settings.",
                "score": 10,
            },
            {
                "key": "merge-hooks",
                "name": "merge_hooks",
                "qname": "requests.sessions.merge_hooks",
                "signature": "def merge_hooks(request_hooks, session_hooks)",
                "file": "requests/sessions.py",
                "_description": "Properly merges both requests and session hooks.",
                "score": 10,
            },
            {
                "key": "merge-setting",
                "name": "merge_setting",
                "qname": "requests.sessions.merge_setting",
                "signature": "def merge_setting(request_setting, session_setting)",
                "file": "requests/sessions.py",
                "_description": "Determines appropriate setting for a given request and session.",
                "score": 1,
            },
            {
                "key": "session-prepare-request",
                "name": "prepare_request",
                "qname": "requests.sessions.Session.prepare_request",
                "signature": "def prepare_request(self, request)",
                "file": "requests/sessions.py",
                "_description": (
                    "Constructs a PreparedRequest for transmission. The PreparedRequest has "
                    "settings merged from the Request instance and those of the Session."
                ),
                "score": 1,
            },
        ],
    )

    assert ranked[0]["key"] == "session-prepare-request"


def test_qname_role_terms_splits_owner_module_package() -> None:
    from codekg.queries.code import _qname_role_terms

    roles = _qname_role_terms("src.click.core.Command.parse_args", "parse_args")
    assert roles["owner"] == ["command"]
    assert roles["module"] == ["click", "core"]
    assert roles["package"] == ["src"]


def test_qname_role_terms_plain_function_has_no_owner() -> None:
    from codekg.queries.code import _qname_role_terms

    roles = _qname_role_terms("base._finalize_fairy", "_finalize_fairy")
    assert roles["owner"] == []
    assert roles["module"] == ["base"]
    assert roles["package"] == []


def test_owner_class_match_outranks_same_named_method_in_another_class() -> None:
    """B2 (codekg-ranking-presentation-spec.md): the motivating task-04
    failure. Gold `Command.parse_args` must outrank `_OptionParser.parse_args`
    once the query names the owner class, even though both match "parse" and
    "args" identically."""
    from codekg.queries.code import _rank_discovery_rows

    ranked = _rank_discovery_rows(
        "parse_args command context leftover arguments",
        [
            {
                "key": "optionparser-parse-args",
                "name": "parse_args",
                "qname": "src.click.parser._OptionParser.parse_args",
                "signature": "def parse_args(self, args)",
                "file": "src/click/parser.py",
                "score": 10,
            },
            {
                "key": "command-parse-args",
                "name": "parse_args",
                "qname": "src.click.core.Command.parse_args",
                "signature": "def parse_args(self, ctx, args)",
                "file": "src/click/core.py",
                "score": 10,
            },
        ],
    )

    assert ranked[0]["key"] == "command-parse-args"


def test_owner_and_name_overlap_is_not_double_counted() -> None:
    """Regression test: a query term matching BOTH the owner class and the
    trailing method name (e.g. "context" matching owner `Context` and name
    `_make_sub_context`) must be credited once, not once per role. Without
    the fix, this candidate outranks a plainer, more relevant match purely on
    the coincidental vocabulary overlap."""
    from codekg.queries.code import _rank_discovery_rows

    ranked = _rank_discovery_rows(
        "make context command invoke",
        [
            {
                "key": "make-context",
                "name": "make_context",
                "qname": "src.click.core.Command.make_context",
                "signature": "def make_context(self, info_name, args, parent, **extra)",
                "file": "src/click/core.py",
                "score": 10,
            },
            {
                "key": "make-sub-context",
                "name": "_make_sub_context",
                "qname": "src.click.core.Context._make_sub_context",
                "signature": "def _make_sub_context(self, command, **kwargs)",
                "file": "src/click/core.py",
                "score": 10,
            },
        ],
    )

    assert ranked[0]["key"] == "make-context"


def test_specific_description_term_outweighs_generic_description_term() -> None:
    from codekg.queries.code import _rank_discovery_rows

    ranked = _rank_discovery_rows(
        "session class",
        [
            {
                "key": "generic",
                "name": "first",
                "qname": "pkg.first",
                "file": "pkg/first.py",
                "_description": "class",
                "score": 0,
            },
            {
                "key": "specific",
                "name": "second",
                "qname": "pkg.second",
                "file": "pkg/second.py",
                "_description": "session",
                "score": 0,
            },
        ],
    )

    assert [row["key"] for row in ranked] == ["specific", "generic"]
    assert [row["_discovery_score"] for row in ranked] == [120, 40]


def test_discover_symbols_default_limit_and_upper_bound() -> None:
    client = DiscoveryClient([_repository()])
    response = discover_symbols("session", mode="graph", client=client)  # type: ignore[arg-type]
    assert response["repository"] == "requests"
    with pytest.raises(ValueError, match="between 1 and 20"):
        discover_symbols("session", repository="requests", limit=21, client=client)  # type: ignore[arg-type]


def test_discover_symbols_name_matches_outrank_doc_only_generic_match() -> None:
    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "doc",
                "name": "other",
                "qname": "other",
                "score": 99.0,
                "file": "x",
                "start_line": 1,
                "end_line": 1,
            },
            {
                "key": "name",
                "name": "promote_primary",
                "qname": "x.promote_primary",
                "score": 1.0,
                "file": "x",
                "start_line": 2,
                "end_line": 2,
            },
        ],
    )
    response = discover_symbols(
        "promote primary", repository="requests", mode="graph", client=client
    )  # type: ignore[arg-type]
    assert [row["symbol_id"] for row in response["results"]] == ["name", "doc"]


def test_discover_hybrid_reranks_bounded_pool_for_exact_multi_term_match(monkeypatch) -> None:
    """A weak graph hit must beat high-scoring lexical one-term descriptions."""

    client = DiscoveryClient(
        [_repository()],
        [
            {
                "key": "exact",
                "name": "prepare_request",
                "qname": "requests.sessions.Session.prepare_request",
                "score": 0.01,
                "file": "requests/sessions.py",
                "start_line": 511,
                "end_line": 540,
            }
        ],
    )

    def lexical_hits(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["limit"] == 100
        return [
            {
                "key": f"generic-{index}",
                "name": "other",
                "qname": f"other.{index}",
                "snippet": "primary configuration only",
                "score": 999.0 - index,
                "file": "other.py",
                "start_line": index,
                "end_line": index,
            }
            for index in range(10)
        ]

    monkeypatch.setattr("codekg.queries.code._search_symbols_lexical", lexical_hits)

    response = discover_symbols(
        "prepare request session cookies auth",
        repository="requests",
        mode="hybrid",
        client=client,  # type: ignore[arg-type]
    )

    assert response["results"][0]["symbol_id"] == "exact"
    assert set(response["results"][0]["matched_terms"]) == {"prepare", "request", "session"}


def _symbol(key: str = "repo@abc:pkg.py:pkg.fn:1") -> dict[str, object]:
    return {
        "key": key,
        "labels": ["Function"],
        "name": "fn",
        "qname": "pkg.fn",
        "signature": "def fn()",
        "start_line": 1,
        "end_line": 2,
        "cyclomatic": 1,
        "file": "pkg.py",
        "repo": "repo",
        "commit": "abc",
    }


def test_qname_selector_requires_repo_and_exact_key_never_falls_back() -> None:
    client = SelectorClient(exact_rows=[_symbol()])

    with pytest.raises(SymbolResolutionError, match="does not belong"):
        get_definition("repo@abc:pkg.py:pkg.fn:1", repo="other", client=client)  # type: ignore[arg-type]

    assert len(client.calls) == 1
    assert "exact-symbol-selector" in client.calls[0][0]

    with pytest.raises(SymbolResolutionError, match="requires an explicit repository"):
        get_definition("pkg.fn", client=SelectorClient())  # type: ignore[arg-type]


def test_qname_selector_reports_candidate_keys_for_ambiguity() -> None:
    client = SelectorClient(
        qname_rows=[_symbol("repo@abc:a.py:pkg.fn:1"), _symbol("repo@abc:b.py:pkg.fn:4")]
    )

    with pytest.raises(SymbolResolutionError, match="use one of these exact keys") as exc_info:
        get_definition("pkg.fn", repo="repo", client=client)  # type: ignore[arg-type]

    assert "repo@abc:a.py:pkg.fn:1" in str(exc_info.value)
    assert "repo@abc:b.py:pkg.fn:4" in str(exc_info.value)


def test_call_queries_use_authoritative_callsites_at_depth_one_and_projection_afterwards() -> None:
    client = SelectorClient(exact_rows=[_symbol()])

    find_callers("repo@abc:pkg.py:pkg.fn:1", depth=1, client=client)  # type: ignore[arg-type]
    find_callees("repo@abc:pkg.py:pkg.fn:1", depth=2, client=client)  # type: ignore[arg-type]

    callers_query = client.calls[1][0]
    callees_query = client.calls[3][0]
    assert "RESOLVES_TO" in callers_query
    assert "HAS_CALLSITE" in callers_query
    assert "[:EXACT_CALLS*1..2]" in callees_query
    assert "RESOLVES_TO" not in callees_query
    assert "node.key STARTS WITH $snapshot_prefix" in callees_query


def test_trace_path_uses_keys_and_bounded_projection() -> None:
    client = SelectorClient(
        exact_rows={
            "repo@abc:pkg.py:pkg.fn:1": [_symbol()],
            "repo@abc:pkg.py:pkg.other:4": [_symbol("repo@abc:pkg.py:pkg.other:4")],
        }
    )

    trace_call_path(
        "repo@abc:pkg.py:pkg.fn:1",
        "repo@abc:pkg.py:pkg.other:4",
        max_depth=999,
        client=client,  # type: ignore[arg-type]
    )

    query, params, _ = client.calls[2]
    assert "shortestPath" in query
    assert "[:EXACT_CALLS*1..10]" in query
    assert "{key: node.key, qname: node.qname}" in query
    assert params["source_key"] == "repo@abc:pkg.py:pkg.fn:1"
    assert params["target_key"] == "repo@abc:pkg.py:pkg.other:4"
    assert params["snapshot_prefix"] == "repo@abc:"


def test_trace_path_rejects_cross_snapshot_endpoints() -> None:
    target = _symbol("other@def:pkg.py:pkg.other:4")
    target["repo"] = "other"
    target["commit"] = "def"
    client = SelectorClient(
        exact_rows={
            "repo@abc:pkg.py:pkg.fn:1": [_symbol()],
            "other@def:pkg.py:pkg.other:4": [target],
        }
    )

    with pytest.raises(SymbolResolutionError, match="same repository and commit"):
        trace_call_path(
            "repo@abc:pkg.py:pkg.fn:1",
            "other@def:pkg.py:pkg.other:4",
            client=client,  # type: ignore[arg-type]
        )


def test_hierarchy_is_selected_by_key_and_confined_to_its_snapshot() -> None:
    selected = _symbol("repo@abc:pkg.py:pkg.Child:4")
    selected["labels"] = ["Type"]
    client = SelectorClient(exact_rows=[selected])

    get_class_hierarchy("repo@abc:pkg.py:pkg.Child:4", client=client)  # type: ignore[arg-type]

    query, params, _ = client.calls[1]
    assert "(t:Type {key: $key})" in query
    assert "node.key STARTS WITH $snapshot_prefix" in query
    assert params["snapshot_prefix"] == "repo@abc:"


def test_find_dead_code_uses_authoritative_resolution_count() -> None:
    client = FakeClient()

    find_dead_code("sample", client=client)  # type: ignore[arg-type]

    query = client.calls[0][0]
    assert "RESOLVES_TO" in query
    assert "incoming_resolved_calls" in query
    assert "CALLS" not in query


def test_get_complexity_supports_top_n_mode() -> None:
    client = FakeClient()

    get_complexity(repo="patroni", top_n=5, client=client)  # type: ignore[arg-type]

    assert client.calls[0][1]["limit"] == 5
