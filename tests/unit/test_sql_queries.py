from __future__ import annotations

import pytest

from codekg.queries.sql import (
    find_sql_usages,
    get_sql_in_file,
    get_sql_object,
    search_sql_objects,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def execute_read(self, query, params=None, **kwargs):
        self.calls.append((query, params, kwargs))
        return next(self.responses)


def test_search_scopes_and_parameterizes_values():
    client = FakeClient(
        [
            [{"repository": "demo", "commit": "abc", "repo_key": "demo"}],
            [{"object_key": "demo@abc:public.users", "object_name": "users"}],
        ]
    )
    result = search_sql_objects(
        "users' OR true",
        repository="demo",
        database="db",
        schema="public",
        kind="table",
        commit="abc",
        client=client,
    )
    assert result["results"][0]["object_name"] == "users"
    query, params, options = client.calls[1]
    assert "users' OR true" not in query
    assert params["query"] == "users' OR true"
    assert params["repository"] == "demo" and params["commit"] == "abc"
    assert options["max_rows"] == 5 and options["timeout_seconds"] <= 10


def test_missing_and_ambiguous_repository_are_clear():
    missing = FakeClient([[]])
    assert search_sql_objects("thing", repository="gone", client=missing)["status"] == "not_found"
    ambiguous = FakeClient(
        [[{"repository": "a", "commit": "1"}, {"repository": "b", "commit": "2"}]]
    )
    assert search_sql_objects("thing", client=ambiguous)["status"] == "ambiguous_repository"


def test_get_object_key_lookup_and_not_found():
    client = FakeClient([[{"repository": "demo", "commit": "abc"}], []])
    response = get_sql_object(
        "demo@abc:public.missing", repository="demo", commit="abc", client=client
    )
    assert response["status"] == "not_found"
    assert "o.key=$object_key" in client.calls[1][0]


@pytest.mark.parametrize("path", ["../secrets.sql", "/tmp/x.sql", "a\\..\\x.sql", ""])
def test_sql_file_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        get_sql_in_file(path, repository="demo", client=FakeClient([]))


def test_usage_validates_selectors_before_database_access():
    client = FakeClient([])
    with pytest.raises(ValueError):
        find_sql_usages("x", role="write-or-drop", client=client)
    assert client.calls == []
