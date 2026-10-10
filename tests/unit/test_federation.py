import json
from types import SimpleNamespace

import pytest

from codekg.corpus_registry import create_native_registry
from codekg.federation import _bounded_paths, _resolve
from codekg.graph_catalog import GraphCatalog


def _catalog(tmp_path, filename, graph_id, generation_id, facts):
    path = tmp_path / filename
    db = create_native_registry(path)
    for alias, role in facts.get("roles", {}).items():
        db.execute("INSERT INTO metadata VALUES (?,?)", (f"{alias}.role", role))
    for table in ("evidence", "routines", "symbols"):
        for alias, source, ordinal, key, fact in facts.get(table, []):
            if table == "routines":
                fact = {"kind": "function", **fact}
            db.execute(
                f"INSERT INTO {table} VALUES (?,?,?,?)", (alias, source, ordinal, json.dumps(fact))
            )
            db.execute(
                "INSERT INTO fact_keys VALUES (?,?,?,?,?)", (key, alias, source, table, ordinal)
            )
    for edge in facts.get("edges", []):
        db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)", edge)
    db.commit()
    db.close()
    return GraphCatalog(path, graph_id=graph_id, generation_id=generation_id)


def test_literal_intent_resolves_and_walks_exact_routine_to_native(tmp_path, monkeypatch):
    app_catalog = _catalog(
        tmp_path,
        "app.sqlite",
        "app",
        "app:g1",
        {
            "roles": {"appsrc": "application"},
            "evidence": [
                (
                    "appsrc",
                    "src/client.py",
                    0,
                    "evidence-1",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": "schedule",
                        "schema_name": "cron",
                        "owner_qname": "Worker.run",
                        "dynamic": False,
                        "arity": 1,
                        "start_line": 10,
                        "end_line": 10,
                        "text": "cursor.execute('select cron.schedule(%s)')",
                    },
                )
            ],
        },
    )
    db_catalog = _catalog(
        tmp_path,
        "db.sqlite",
        "db",
        "db:g7",
        {
            "roles": {"pg": "postgres", "cron": "extension"},
            "routines": [
                (
                    "cron",
                    "sql/cron.sql",
                    0,
                    "routine-1",
                    {
                        "schema_name": "cron",
                        "name": "schedule",
                        "arity": 1,
                        "default_arg_count": 0,
                        "variadic_arg_count": 0,
                        "signature": "cron.schedule(text)",
                        "start_line": 4,
                    },
                )
            ],
            "symbols": [
                (
                    "pg",
                    "src/worker.c",
                    0,
                    "native-1",
                    {
                        "name": "BackgroundWorkerInitializeConnection",
                        "start_line": 88,
                    },
                )
            ],
            "edges": [
                ("routine-1", "native-1", "CALLS_NATIVE", "exact", "src/worker.c", 88, 3, None, 0)
            ],
        },
    )
    app = SimpleNamespace(
        graph_id="app",
        generation_id="app:g1",
        catalog=app_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "appsrc"},)),
    )
    db = SimpleNamespace(
        graph_id="db",
        generation_id="db:g7",
        catalog=db_catalog,
        generation=SimpleNamespace(
            snapshots=(
                {"alias": "pg", "role": "postgres"},
                {"alias": "cron", "role": "extension"},
            )
        ),
    )
    context = SimpleNamespace(
        id="app-pg", search_path=("cron", "public"), visible_extensions=("cron",)
    )
    evidence = app_catalog.get_intent("evidence-1")

    resolved = _resolve(evidence, context, app, db)

    assert resolved["status"] == "exact"
    assert resolved["candidates"][0]["local_key"] == "routine-1"
    paths, truncated = _bounded_paths(
        db, "routine-1", "native-1", 8, 10**20, {"visited": 0, "edges": 0}
    )
    assert not truncated
    assert paths[0]["edges"][0]["kind"] == "CALLS_NATIVE"
    assert paths[0]["edges"][0]["status"] == "exact"

    # Exercise the public orchestrator, including real ownership-sidecar reads.
    import sqlite3

    from codekg import federation

    with sqlite3.connect(app_catalog.path) as catalog_db:
        catalog_db.executemany(
            "INSERT INTO python_owners VALUES (?,?,?,?,?)",
            [
                ("appsrc", "entry", "src/client.py", "entry", 1),
                ("appsrc", "worker", "src/client.py", "Worker.run", 8),
                ("appsrc", "unrelated", "src/client.py", "other", 20),
            ],
        )
        catalog_db.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
            ("worker", "evidence-1", "HAS_EVIDENCE", "exact", "src/client.py", 10, 0, None, 0),
        )

    class PythonClient:
        def execute_read(self, query, params, **kwargs):
            assert "EXACT_CALLS" in query and "allShortestPaths" not in query
            return (
                [{"key": "worker", "path": "src/client.py", "line": 2}]
                if params["key"] == "entry"
                else []
            )

    app.client = PythonClient()
    context.application_database = "primary"
    monkeypatch.setattr(
        federation, "_context_handles", lambda *args, **kwargs: (None, context, app, db)
    )
    entry_ref = {"graph_id": "app", "generation_id": "app:g1", "local_key": "entry"}
    target_ref = {"graph_id": "db", "generation_id": "db:g7", "local_key": "native-1"}
    result = federation.trace_application_database_path(
        context_id="app-pg", entry_ref=entry_ref, target_ref=target_ref, max_depth=4
    )
    assert result["status"] == "ok"
    relations = [segment["relationship"] for segment in result["paths"][0]["segments"]]
    assert "EXACT_CALLS" in relations and "INVOKES" in relations and "CALLS_NATIVE" in relations
    shallow = federation.trace_application_database_path(
        context_id="app-pg", entry_ref=entry_ref, target_ref=target_ref, max_depth=3
    )
    assert not shallow["paths"]
    assert shallow["truncated"]
    unrelated = federation.trace_application_database_path(
        context_id="app-pg",
        entry_ref={**entry_ref, "local_key": "unrelated"},
        target_ref=target_ref,
    )
    assert not unrelated["paths"]
    stale = federation.trace_application_database_path(
        context_id="app-pg", entry_ref={**entry_ref, "generation_id": "old"}, target_ref=target_ref
    )
    assert stale["status"] == "invalid_reference"
    reverse = federation.find_application_database_usages(
        context_id="app-pg", target_ref=target_ref
    )
    assert reverse["usages"][0]["intent"]["local_key"] == "evidence-1"


def test_local_routine_shadows_database_routine(tmp_path):
    app_catalog = _catalog(
        tmp_path,
        "app.sqlite",
        "app",
        "app:g1",
        {
            "roles": {"appsrc": "application"},
            "evidence": [
                (
                    "appsrc",
                    "src/a.py",
                    0,
                    "evidence",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": "lookup",
                        "schema_name": "public",
                        "dynamic": False,
                        "arity": 1,
                    },
                )
            ],
            "routines": [
                (
                    "appsrc",
                    "sql/local.sql",
                    0,
                    "local-routine",
                    {
                        "schema_name": "public",
                        "name": "lookup",
                        "arity": 1,
                        "signature": "lookup(integer)",
                        "start_line": 1,
                    },
                )
            ],
        },
    )
    db_catalog = _catalog(
        tmp_path,
        "db.sqlite",
        "db",
        "db:g1",
        {
            "roles": {"pg": "postgres"},
            "routines": [
                (
                    "pg",
                    "sql/lookup.sql",
                    0,
                    "db-routine",
                    {
                        "schema_name": "public",
                        "name": "lookup",
                        "arity": 1,
                        "signature": "lookup(text)",
                        "start_line": 1,
                    },
                )
            ],
        },
    )
    app = SimpleNamespace(
        graph_id="app",
        generation_id="app:g1",
        catalog=app_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "appsrc"},)),
    )
    db = SimpleNamespace(
        graph_id="db",
        generation_id="db:g1",
        catalog=db_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "pg", "role": "postgres"},)),
    )
    context = SimpleNamespace(id="ctx", search_path=("public",), visible_extensions=())

    result = _resolve(app_catalog.get_intent("evidence"), context, app, db)

    assert result["candidates"][0]["local_key"] == "local-routine"
    assert result["candidates"][0]["graph_id"] == "app"


def test_documentation_dynamic_hidden_extension_and_conditional_are_not_exact(tmp_path):
    app_catalog = _catalog(
        tmp_path,
        "app.sqlite",
        "app",
        "app:g1",
        {
            "roles": {"appsrc": "application"},
            "evidence": [
                (
                    "appsrc",
                    "docs/runbook.md",
                    0,
                    "docs",
                    {
                        "origin": "markdown_sql",
                        "object_name": "lookup",
                        "dynamic": False,
                    },
                ),
                (
                    "appsrc",
                    "src/a.py",
                    1,
                    "dynamic",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": None,
                        "dynamic": True,
                    },
                ),
                (
                    "appsrc",
                    "src/a.py",
                    2,
                    "conditional",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": "lookup",
                        "schema_name": "addons",
                        "dynamic": False,
                        "arity": 1,
                        "condition": "if enabled",
                    },
                ),
                (
                    "appsrc",
                    "src/a.py",
                    3,
                    "external",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": "hidden_api",
                        "schema_name": "addons",
                        "dynamic": False,
                        "arity": 1,
                    },
                ),
            ],
        },
    )
    db_catalog = _catalog(
        tmp_path,
        "db.sqlite",
        "db",
        "db:g1",
        {
            "roles": {"pg": "postgres", "hidden_ext": "extension"},
            "routines": [
                (
                    "hidden_ext",
                    "sql/addon.sql",
                    0,
                    "conditional-routine",
                    {
                        "schema_name": "addons",
                        "name": "lookup",
                        "arity": 1,
                        "signature": "lookup(text)",
                        "condition": "#ifdef ENABLED",
                    },
                ),
                (
                    "hidden_ext",
                    "sql/addon.sql",
                    1,
                    "hidden-routine",
                    {
                        "schema_name": "addons",
                        "name": "hidden_api",
                        "arity": 1,
                        "signature": "hidden_api(text)",
                    },
                ),
            ],
        },
    )
    app = SimpleNamespace(
        graph_id="app",
        generation_id="app:g1",
        catalog=app_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "appsrc"},)),
    )
    db = SimpleNamespace(
        graph_id="db",
        generation_id="db:g1",
        catalog=db_catalog,
        generation=SimpleNamespace(
            snapshots=(
                {"alias": "pg", "role": "postgres"},
                {"alias": "hidden_ext", "role": "extension"},
            )
        ),
    )
    context = SimpleNamespace(id="ctx", search_path=("addons",), visible_extensions=())

    assert _resolve(app_catalog.get_intent("docs"), context, app, db)["status"] == "documentation"
    assert _resolve(app_catalog.get_intent("dynamic"), context, app, db)["status"] == "dynamic"
    visible_context = SimpleNamespace(
        id="visible", search_path=("addons",), visible_extensions=("hidden_ext",)
    )
    assert (
        _resolve(app_catalog.get_intent("conditional"), visible_context, app, db)["status"]
        == "conditional"
    )
    assert _resolve(app_catalog.get_intent("external"), context, app, db)["status"] == "unresolved"


def test_cross_graph_compare_preserves_overload_conflicts_and_conditions(tmp_path):
    from codekg.federation import _compare_catalogs

    left_catalog = _catalog(
        tmp_path,
        "left.sqlite",
        "left",
        "left:g1",
        {
            "roles": {"pg": "postgres"},
            "routines": [
                (
                    "pg",
                    "sql/api.sql",
                    0,
                    "left-a",
                    {
                        "schema_name": "public",
                        "name": "api",
                        "kind": "function",
                        "language": "sql",
                        "arity": 1,
                        "signature": "api(text)",
                        "condition": "#ifdef OLD",
                        "definition_hash": "d1",
                        "default_arg_count": 0,
                        "variadic_arg_count": 0,
                    },
                ),
                (
                    "pg",
                    "sql/api.sql",
                    1,
                    "left-b",
                    {
                        "schema_name": "public",
                        "name": "api",
                        "kind": "function",
                        "language": "sql",
                        "arity": 1,
                        "signature": "api(text)",
                        "condition": "#ifdef EXTRA",
                        "definition_hash": "d1",
                        "default_arg_count": 0,
                        "variadic_arg_count": 0,
                    },
                ),
                (
                    "pg",
                    "sql/other.sql",
                    2,
                    "left-c",
                    {
                        "schema_name": "public",
                        "name": "conditional",
                        "kind": "function",
                        "language": "sql",
                        "arity": 0,
                        "signature": "conditional()",
                        "condition": "#ifdef OLD",
                    },
                ),
            ],
        },
    )
    right_catalog = _catalog(
        tmp_path,
        "right.sqlite",
        "right",
        "right:g2",
        {
            "roles": {"pg": "postgres"},
            "routines": [
                (
                    "pg",
                    "sql/api.sql",
                    0,
                    "right-a",
                    {
                        "schema_name": "public",
                        "name": "conditional",
                        "kind": "function",
                        "language": "sql",
                        "arity": 0,
                        "signature": "conditional()",
                        "condition": "#ifdef NEW",
                    },
                )
            ],
        },
    )
    left = SimpleNamespace(
        corpus_path=left_catalog.path,
        generation=SimpleNamespace(
            manifest={
                "snapshots": [{"alias": "pg", "logical_repo": "postgres"}],
            }
        ),
    )
    right = SimpleNamespace(
        corpus_path=right_catalog.path,
        generation=SimpleNamespace(
            manifest={
                "snapshots": [{"alias": "pg", "logical_repo": "postgres"}],
            }
        ),
    )

    rows = _compare_catalogs(left, right, "pg", "pg", 100, 0, 5)

    assert {row["change"] for row in rows} == {"ambiguous", "condition_changed"}


def test_reverse_usage_paginates_past_false_postings_and_binds_continuation(monkeypatch):
    from types import SimpleNamespace

    from codekg import federation

    target = {"graph_id": "db", "generation_id": "db:g1", "local_key": "native"}
    context = SimpleNamespace(id="ctx", search_path=("public",), visible_extensions=())

    class DbCatalog:
        def fact(self, key, **_kwargs):
            return {"name": "native_fn", "fact_table": "symbols" if key == "native" else "routines"}

        def neighbors(self, key, **_kwargs):
            if key == "native":
                return {
                    "items": [
                        {"kind": "CALLS_NATIVE", "source_key": "routine", "target_key": "native"}
                    ],
                    "truncated": False,
                }
            return {"items": [], "truncated": False}

    postings = [
        {
            "snapshot_alias": "app",
            "path": f"docs/{index}.md",
            "ordinal": index,
            "local_key": f"false-{index}",
            "object_name": "native_fn",
            "valid": False,
        }
        for index in range(3)
    ] + [
        {
            "snapshot_alias": "app",
            "path": f"src/use_{index}.py",
            "ordinal": index + 10,
            "local_key": f"valid-{index}",
            "object_name": "native_fn",
            "valid": True,
        }
        for index in range(2)
    ]

    class AppCatalog:
        def reverse_intents(self, *, object_name, after=None, limit, **_kwargs):
            rows = postings if object_name == "native_fn" else []
            if after:
                rows = [
                    row
                    for row in rows
                    if (row["snapshot_alias"], row["path"], row["ordinal"]) > after
                ]
            items = rows[:limit]
            return {"items": items, "truncated": len(rows) > len(items)}

    app = SimpleNamespace(graph_id="app", generation_id="app:g1", catalog=AppCatalog())
    db = SimpleNamespace(graph_id="db", generation_id="db:g1", catalog=DbCatalog())
    monkeypatch.setattr(federation, "_context_handles", lambda *_a, **_kw: (None, context, app, db))

    def resolve(item, *_args, **_kwargs):
        keys = ["routine"] if item["valid"] else []
        return {
            "status": "exact" if keys else "unresolved",
            "candidates": [{"graph_id": "db", "local_key": key} for key in keys],
        }

    monkeypatch.setattr(federation, "_resolve", resolve)
    first = federation.find_application_database_usages(
        context_id="ctx", target_ref=target, limit=1
    )
    assert [row["intent"]["local_key"] for row in first["usages"]] == ["valid-0"]
    continuation = first["continuation"]
    assert continuation is not None and first["truncated"]

    second = federation.find_application_database_usages(
        context_id="ctx", target_ref=target, limit=1, continuation=continuation
    )
    assert [row["intent"]["local_key"] for row in second["usages"]] == ["valid-1"]
    assert second["continuation"] is None

    stale = dict(continuation, db_generation_id="db:old")
    rejected = federation.find_application_database_usages(
        context_id="ctx", target_ref=target, limit=1, continuation=stale
    )
    assert rejected["status"] == "invalid_cursor"

    # Same ID and graph generations, but a different effective DB context after restart.
    context.search_path = ("different_schema",)
    changed = federation.find_application_database_usages(
        context_id="ctx", target_ref=target, limit=1, continuation=continuation
    )
    assert changed["status"] == "invalid_cursor"
    context.search_path = ("public",)

    monkeypatch.setattr(federation, "MAX_REVERSE_POSTINGS", 2)
    capped = federation.find_application_database_usages(
        context_id="ctx", target_ref=target, limit=10
    )
    assert capped["truncated"] is True
    assert capped["continuation"] is not None
    assert not capped["usages"]


def test_compare_identical_generation_alias_skips_sqlite_but_validates_arguments(
    tmp_path, monkeypatch
):
    import sqlite3
    from types import SimpleNamespace

    from codekg.federation import _compare_catalogs

    corpus = tmp_path / "large.sqlite"
    manifest = {
        "snapshots": [{"alias": "pg", "logical_repo": "postgres"}],
    }
    generation = SimpleNamespace(generation_id="pg18:g7", manifest=manifest)
    left = SimpleNamespace(corpus_path=corpus, generation=generation)
    right = SimpleNamespace(corpus_path=corpus, generation=generation)

    def fail_connect(*_args, **_kwargs):
        raise AssertionError("same-scope comparison must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    assert _compare_catalogs(left, right, "pg", "pg", 100, 0, 5) == []

    with pytest.raises(ValueError, match="limit"):
        _compare_catalogs(left, right, "pg", "pg", 0, 0, 5)
    with pytest.raises(ValueError, match="aliases"):
        _compare_catalogs(left, right, "missing", "missing", 100, 0, 5)

    newer = SimpleNamespace(
        corpus_path=corpus,
        generation=SimpleNamespace(generation_id="pg18:g8", manifest=manifest),
    )
    with pytest.raises(AssertionError, match="must not open SQLite"):
        _compare_catalogs(left, newer, "pg", "pg", 100, 0, 5)


def _sql_body_graphs(tmp_path, monkeypatch):
    from codekg import federation

    app_catalog = _catalog(
        tmp_path,
        "body-app.sqlite",
        "app",
        "app:g1",
        {
            "evidence": [
                (
                    "appsrc",
                    "client.py",
                    0,
                    "app-call",
                    {
                        "origin": "python_execute",
                        "receiver_status": "verified",
                        "routine_kind": "function",
                        "object_name": "wrapper",
                        "schema_name": "public",
                        "arity": 0,
                        "dynamic": False,
                    },
                )
            ],
        },
    )
    db_catalog = _catalog(
        tmp_path,
        "body-db.sqlite",
        "db",
        "db:g1",
        {
            "routines": [
                (
                    "pg",
                    "body.sql",
                    0,
                    "wrapper",
                    {
                        "name": "wrapper",
                        "schema_name": "public",
                        "arity": 0,
                        "language": "sql",
                        "start_line": 1,
                    },
                ),
                (
                    "pg",
                    "body.sql",
                    1,
                    "callee",
                    {
                        "name": "callee",
                        "schema_name": "public",
                        "arity": 0,
                        "language": "sql",
                        "start_line": 2,
                    },
                ),
                (
                    "pg",
                    "body.sql",
                    2,
                    "leaf",
                    {
                        "name": "leaf",
                        "schema_name": "public",
                        "arity": 0,
                        "language": "c",
                        "start_line": 3,
                    },
                ),
            ],
            "evidence": [
                (
                    "pg",
                    "body.sql",
                    0,
                    "body-call",
                    {
                        "origin": "routine_body",
                        "routine_kind": "function",
                        "object_name": "callee",
                        "schema_name": "public",
                        "arity": 0,
                        "owner_qname": "public.wrapper",
                        "owner_line": 1,
                        "dynamic": False,
                    },
                ),
                (
                    "pg",
                    "body.sql",
                    1,
                    "second-body-call",
                    {
                        "origin": "routine_body",
                        "routine_kind": "function",
                        "object_name": "leaf",
                        "schema_name": "public",
                        "arity": 0,
                        "owner_qname": "public.callee",
                        "owner_line": 2,
                        "dynamic": False,
                    },
                ),
            ],
            "symbols": [("pg", "native.c", 0, "native", {"name": "native", "kind": "function"})],
            "edges": [
                ("wrapper", "body-call", "HAS_EVIDENCE", "exact", "body.sql", 1, 0, None, 0),
                ("body-call", "callee", "INVOKES_ROUTINE", "exact", "body.sql", 1, 0, None, 0),
                ("callee", "second-body-call", "HAS_EVIDENCE", "exact", "body.sql", 2, 0, None, 0),
                ("second-body-call", "leaf", "INVOKES_ROUTINE", "exact", "body.sql", 2, 0, None, 0),
                ("leaf", "native", "BINDS_TO_NATIVE", "exact", "body.sql", 3, 0, None, 0),
            ],
        },
    )
    app = SimpleNamespace(
        graph_id="app",
        generation_id="app:g1",
        catalog=app_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "appsrc"},)),
    )
    db = SimpleNamespace(
        graph_id="db",
        generation_id="db:g1",
        catalog=db_catalog,
        generation=SimpleNamespace(snapshots=({"alias": "pg", "role": "postgres"},)),
    )
    context = SimpleNamespace(
        id="body",
        application_graph="app",
        database_graph="db",
        application_database="primary",
        visible_extensions=(),
        search_path=("public",),
    )
    monkeypatch.setattr(federation, "_context_handles", lambda *_a, **_kw: (None, context, app, db))
    return context, app, db


def test_federated_forward_and_reverse_follow_owned_sql_body(tmp_path, monkeypatch):
    from codekg import federation

    _, _, db = _sql_body_graphs(tmp_path, monkeypatch)
    target = {"graph_id": "db", "generation_id": "db:g1", "local_key": "native"}
    forward = federation.trace_application_database_path(
        context_id="body",
        evidence_ref={"graph_id": "app", "generation_id": "app:g1", "local_key": "app-call"},
        target_ref=target,
    )
    assert forward["status"] == "ok" and forward["paths"]
    relationships = [segment["relationship"] for segment in forward["paths"][0]["segments"]]
    assert relationships[-3:] == ["HAS_EVIDENCE", "INVOKES_ROUTINE", "BINDS_TO_NATIVE"]
    reverse = federation.find_application_database_usages(context_id="body", target_ref=target)
    assert [hit["intent"]["local_key"] for hit in reverse["usages"]] == ["app-call"]
    assert reverse["coverage_complete"]
    paths, truncated = _bounded_paths(
        db, "wrapper", "native", 2, 10**20, {"visited": 0, "edges": 0}
    )
    assert not paths and truncated  # Ownership transitions still consume the depth budget.


@pytest.mark.parametrize(
    "origin,condition",
    [
        ("markdown_sql", None),
        ("python_execute", None),
        ("native_call", None),
        ("routine_body", "guard"),
    ],
)
def test_body_bridge_excludes_documentation_and_conditional_evidence(
    tmp_path,
    monkeypatch,
    origin,
    condition,
):
    import sqlite3

    _, _, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        row = connection.execute("SELECT fact FROM evidence WHERE ordinal=0").fetchone()
        fact = json.loads(row[0]) | {"origin": origin, "condition": condition}
        connection.execute("UPDATE evidence SET fact=? WHERE ordinal=0", (json.dumps(fact),))
    paths, _ = _bounded_paths(db, "wrapper", "native", 8, 10**20, {"visited": 0, "edges": 0})
    assert not paths


def test_unknown_python_receiver_cannot_be_an_exact_bridge(tmp_path, monkeypatch):

    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    evidence = app.catalog.get_intent("app-call")
    evidence["receiver_status"] = "unverified"
    result = _resolve(evidence, context, app, db)
    assert result["status"] == "receiver_unverified"
    assert result["candidates"][0]["local_key"] == "wrapper"
    evidence.pop("receiver_status")  # Old artifacts have no proof either.
    assert _resolve(evidence, context, app, db)["status"] == "receiver_unverified"


def test_unknown_call_arity_cannot_be_exact(tmp_path, monkeypatch):
    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    evidence = app.catalog.get_intent("app-call")
    evidence["arity"] = None
    assert _resolve(evidence, context, app, db)["status"] != "exact"


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_qname": "public.other"},
        {"owner_line": 99},
        {"dynamic": True},
    ],
)
def test_owned_body_bridge_rejects_incorrect_owner_and_dynamic_evidence(
    tmp_path,
    monkeypatch,
    overrides,
):
    import sqlite3

    _, _, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        raw = connection.execute("SELECT fact FROM evidence WHERE ordinal=0").fetchone()[0]
        connection.execute(
            "UPDATE evidence SET fact=? WHERE ordinal=0", (json.dumps(json.loads(raw) | overrides),)
        )
    paths, _ = _bounded_paths(db, "wrapper", "native", 8, 10**20, {"visited": 0, "edges": 0})
    assert not paths


def test_context_fingerprint_captures_all_effective_fields():
    from codekg.federation import _context_fingerprint

    fields = dict(
        id="ctx",
        application_graph="app",
        database_graph="db",
        application_database="primary",
        visible_extensions=("ext",),
        search_path=("public", "pg_catalog"),
    )
    baseline = _context_fingerprint(SimpleNamespace(**fields))
    assert baseline == _context_fingerprint(SimpleNamespace(**fields))
    changes = dict(
        application_graph="other-app",
        database_graph="other-db",
        application_database="other-database",
        visible_extensions=(),
        search_path=("pg_catalog", "public"),
    )
    for key, value in changes.items():
        assert baseline != _context_fingerprint(SimpleNamespace(**(fields | {key: value})))


def test_variadic_and_default_argument_bounds_are_consistent(tmp_path, monkeypatch):
    from codekg.federation import _signature_known

    assert _signature_known(
        {"routine_kind": "function", "arity": 0},
        {"kind": "function", "arity": 1, "variadic_arg_count": 1},
    )
    assert _signature_known(
        {"routine_kind": "function", "arity": 1},
        {"kind": "function", "arity": 2, "default_arg_count": 1},
    )
    assert not _signature_known(
        {"routine_kind": "function", "arity": None}, {"kind": "function", "arity": 0}
    )
    assert not _signature_known(
        {"routine_kind": "function", "arity": 2}, {"kind": "function", "arity": 0}
    )


def test_reverse_returns_unverified_receiver_only_as_candidate(tmp_path, monkeypatch):
    import sqlite3

    from codekg import federation

    _, app, _ = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(app.catalog.path) as connection:
        raw = connection.execute("SELECT fact FROM evidence").fetchone()[0]
        connection.execute(
            "UPDATE evidence SET fact=?",
            (json.dumps(json.loads(raw) | {"receiver_status": "unverified"}),),
        )
    result = federation.find_application_database_usages(
        context_id="body",
        target_ref={"graph_id": "db", "generation_id": "db:g1", "local_key": "native"},
    )
    assert result["usages"][0]["candidate_only"]
    assert result["usages"][0]["resolution"]["status"] == "receiver_unverified"


def test_file_ownership_is_not_a_database_execution_bridge(tmp_path, monkeypatch):
    import sqlite3

    _, _, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        connection.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
            ("file-key", "body-call", "HAS_EVIDENCE", "exact", "body.sql", 1, 0, None, 0),
        )
    paths, _ = _bounded_paths(db, "file-key", "native", 8, 10**20, {"visited": 0, "edges": 0})
    assert not paths


def test_owned_body_cycle_and_nonexact_edges_do_not_fabricate_paths(tmp_path, monkeypatch):
    import sqlite3

    _, _, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        connection.execute(
            "UPDATE edges SET target_key='wrapper' WHERE source_key='second-body-call' "
            "AND kind='INVOKES_ROUTINE'"
        )
        connection.execute(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
            ("second-body-call", "leaf", "INVOKES_ROUTINE", "ambiguous", "body.sql", 2, 0, None, 1),
        )
    paths, truncated = _bounded_paths(
        db, "wrapper", "native", 32, 10**20, {"visited": 0, "edges": 0}
    )
    assert not paths and not truncated  # Cycles terminate; candidate edge is not executable.


def test_reverse_cursor_without_context_fingerprint_is_rejected(tmp_path, monkeypatch):
    from codekg import federation

    _, _, _ = _sql_body_graphs(tmp_path, monkeypatch)
    target = {"graph_id": "db", "generation_id": "db:g1", "local_key": "native"}
    legacy = {
        "context_id": "body",
        "app_graph_id": "app",
        "app_generation_id": "app:g1",
        "db_graph_id": "db",
        "db_generation_id": "db:g1",
        "target_ref": target,
        "max_depth": 8,
        "name_index": 0,
        "after": [],
    }
    assert (
        federation.find_application_database_usages(
            context_id="body",
            target_ref=target,
            continuation=legacy,
        )["status"]
        == "invalid_cursor"
    )


@pytest.mark.parametrize("expected_kind", ["procedure", None])
def test_forward_bridge_never_asserts_wrong_or_unknown_routine_kind(
    tmp_path, monkeypatch, expected_kind
):
    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    evidence = app.catalog.fact("app-call")
    evidence["routine_kind"] = expected_kind
    result = _resolve(evidence, context, app, db)
    assert result["status"] == ("unresolved" if expected_kind else "ambiguous")


def test_database_body_edge_cannot_invoke_wrong_routine_kind(tmp_path, monkeypatch):
    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    import sqlite3

    with sqlite3.connect(db.catalog.path) as connection:
        raw = connection.execute("SELECT fact FROM evidence WHERE ordinal=0").fetchone()[0]
        connection.execute(
            "UPDATE evidence SET fact=? WHERE ordinal=0",
            (json.dumps(json.loads(raw) | {"routine_kind": "procedure"}),),
        )
    from time import monotonic

    from codekg.federation import _database_edge_allowed

    assert not _database_edge_allowed(
        db,
        {"kind": "INVOKES_ROUTINE", "source_key": "body-call", "target_key": "callee"},
        monotonic() + 2,
        {},
    )


@pytest.mark.parametrize(
    ("arity", "out_count", "status"),
    [(1, 1, "unresolved"), (2, 1, "exact"), (1, None, "ambiguous")],
)
def test_federated_call_arity_includes_out_arguments(
    tmp_path, monkeypatch, arity, out_count, status
):
    import sqlite3

    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        raw = connection.execute("SELECT fact FROM routines WHERE ordinal=0").fetchone()[0]
        connection.execute(
            "UPDATE routines SET fact=? WHERE ordinal=0",
            (
                json.dumps(
                    json.loads(raw) | {"kind": "procedure", "arity": 1, "out_arg_count": out_count}
                ),
            ),
        )
    evidence = app.catalog.fact("app-call") | {"routine_kind": "procedure", "arity": arity}
    assert _resolve(evidence, context, app, db)["status"] == status


@pytest.mark.parametrize("catalog_kind", ["aggregate", "window"])
def test_function_syntax_resolves_catalog_callable_kinds(tmp_path, monkeypatch, catalog_kind):
    import sqlite3

    context, app, db = _sql_body_graphs(tmp_path, monkeypatch)
    with sqlite3.connect(db.catalog.path) as connection:
        raw = connection.execute("SELECT fact FROM routines WHERE ordinal=0").fetchone()[0]
        connection.execute(
            "UPDATE routines SET fact=? WHERE ordinal=0",
            (json.dumps(json.loads(raw) | {"kind": catalog_kind}),),
        )
    assert _resolve(app.catalog.fact("app-call"), context, app, db)["status"] == "exact"


def test_call_with_out_and_defaults_never_claims_exact_positional_arity():
    from codekg.federation import _signature_known

    candidate = {"kind": "procedure", "arity": 1, "out_arg_count": 1, "default_arg_count": 1}
    assert not _signature_known({"routine_kind": "procedure", "arity": 1}, candidate)
    assert not _signature_known({"routine_kind": "procedure", "arity": 2}, candidate)
