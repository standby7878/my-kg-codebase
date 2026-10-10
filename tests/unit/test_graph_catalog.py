import json
import sqlite3

import pytest

from codekg.corpus_registry import create_native_registry
from codekg.graph_catalog import GraphCatalog


def _catalog(tmp_path):
    path = tmp_path / "corpus.sqlite"
    db = create_native_registry(path)
    db.execute("INSERT INTO metadata VALUES ('fixture','1')")
    for alias, role in (("app", "application"), ("pg", "postgres"), ("ext", "extension")):
        db.execute("INSERT INTO metadata VALUES (?,?)", (alias + ".role", role))
    evidence = [
        (
            "app",
            "src/a.py",
            0,
            {
                "origin": "python_execute",
                "schema_name": '"Odd.Schema"',
                "object_name": '"MixedCase"',
                "owner_qname": "Store.run",
                "dynamic": False,
                "text": 'SELECT "Odd.Schema"."MixedCase"()',
                "text_hash": "h1",
                "arity": 0,
            },
        ),
        (
            "app",
            "src/a.py",
            1,
            {
                "origin": "python_execute",
                "schema_name": None,
                "object_name": '"MixedCase"',
                "owner_qname": "Store.run",
                "dynamic": False,
                "condition": "if configured",
                "text_hash": "h2",
                "arity": 1,
            },
        ),
        (
            "app",
            "src/a.py",
            2,
            {
                "origin": "python_execute",
                "schema_name": None,
                "object_name": None,
                "owner_qname": "Store.run",
                "dynamic": True,
                "text_hash": None,
            },
        ),
    ]
    for alias, source, ordinal, fact in evidence:
        db.execute(
            "INSERT INTO evidence VALUES (?,?,?,?)", (alias, source, ordinal, json.dumps(fact))
        )
        key = f"{alias}:{ordinal}"
        db.execute(
            "INSERT INTO fact_keys VALUES (?,?,?,?,?)", (key, alias, source, "evidence", ordinal)
        )
    routines = [
        (
            "app",
            "models/local.sql",
            0,
            {
                "schema_name": "public",
                "name": "lookup",
                "arity": 2,
                "default_arg_count": 1,
                "variadic_arg_count": 0,
                "signature": "lookup(integer, integer)",
                "condition": None,
            },
        ),
        (
            "pg",
            "src/lookup.sql",
            0,
            {
                "schema_name": "public",
                "name": "lookup",
                "arity": 2,
                "default_arg_count": 0,
                "variadic_arg_count": 0,
                "signature": "lookup(integer, integer)",
                "condition": "#ifdef X",
            },
        ),
        (
            "ext",
            "sql/ext.sql",
            0,
            {
                "schema_name": "addons",
                "name": "lookup",
                "arity": 1,
                "default_arg_count": 0,
                "variadic_arg_count": 1,
                "signature": "lookup(VARIADIC text[])",
                "condition": None,
            },
        ),
    ]
    for alias, source, ordinal, fact in routines:
        db.execute(
            "INSERT INTO routines VALUES (?,?,?,?)", (alias, source, ordinal, json.dumps(fact))
        )
        db.execute(
            "INSERT INTO fact_keys VALUES (?,?,?,?,?)",
            (f"{alias}:routine:{ordinal}", alias, source, "routines", ordinal),
        )
    db.commit()
    db.close()
    return GraphCatalog(path, graph_id="app", generation_id="app:g1")


def test_catalog_reverse_quoted_name_and_owner_preserve_evidence_read_only(tmp_path):
    catalog = _catalog(tmp_path)
    reverse = catalog.reverse_intents(object_name='"MixedCase"', schema_name='"Odd.Schema"')
    assert len(reverse["items"]) == 2
    assert sum(item["schema_name"] == '"Odd.Schema"' for item in reverse["items"]) == 1
    assert reverse["items"][0]["text"] == 'SELECT "Odd.Schema"."MixedCase"()'
    assert reverse["items"][0]["text_hash"] == "h1"
    intents = catalog.list_intents(owner_qname="Store.run", limit=2)
    assert intents["truncated"] and intents["reason"] == "candidate_overflow"
    assert len(intents["items"]) == 2
    assert catalog.list_intents(local_key="app:2")["items"][0]["dynamic"] is True
    with pytest.raises(sqlite3.OperationalError), catalog._connection() as db:
        db.execute("DELETE FROM evidence")
    with catalog._connection() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT snapshot_alias,path,ordinal FROM evidence "
            "WHERE json_extract(fact,'$.object_name')=? "
            "AND json_extract(fact,'$.schema_name')=?",
            ('"MixedCase"', '"Odd.Schema"'),
        ).fetchall()
        owner_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT ordinal FROM evidence "
            "WHERE json_extract(fact,'$.owner_qname')=?",
            ("Store.run",),
        ).fetchall()
    assert any("idx_evidence_reverse_name_schema" in row[-1] for row in plan)
    assert any("idx_evidence_owner_qname" in row[-1] for row in owner_plan)


def test_routine_catalog_orders_local_before_search_path_and_keeps_arity_metadata(tmp_path):
    catalog = _catalog(tmp_path)
    result = catalog.routine_candidates(
        name="lookup",
        local_aliases=("app",),
        visible_aliases=("pg", "ext"),
        search_path=("public", "addons"),
    )
    assert [item["snapshot_alias"] for item in result["items"]] == ["app", "pg", "ext"]
    local = result["items"][0]
    assert local["default_arg_count"] == 1 and local["signature"] == "lookup(integer, integer)"
    assert result["generation_id"] == "app:g1"
    by_arity = catalog.routine_candidates(
        name="lookup",
        arity=1,
        local_aliases=("app",),
        visible_aliases=("pg", "ext"),
        search_path=("public", "addons"),
    )
    assert [item["snapshot_alias"] for item in by_arity["items"]] == ["app", "ext"]
    limited = catalog.routine_candidates(
        name="lookup", local_aliases=("app",), visible_aliases=("pg", "ext"), limit=2
    )
    assert limited["truncated"] and len(limited["items"]) == 2


def test_generation_catalog_uses_sqlite_progress_deadline(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        catalog.list_intents(owner_path="src/a.py", deadline_seconds=0)
