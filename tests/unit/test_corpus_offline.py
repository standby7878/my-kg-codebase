from __future__ import annotations

import json
import sqlite3

import pytest

from codekg.corpus_queries import compare, search, snapshots


def _manifest(tmp_path):
    dbpath = tmp_path / "corpus.sqlite"
    db = sqlite3.connect(dbpath)
    db.executescript(
        "CREATE TABLE symbols(snapshot_alias,path,ordinal,fact); "
        "CREATE TABLE routines(snapshot_alias,path,ordinal,fact); "
        "CREATE TABLE fact_keys(key,snapshot_alias,path,table_name,ordinal); "
        "CREATE TABLE diagnostics(snapshot_alias,path,ordinal,fact);"
    )
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        (
            "v1",
            "f.sql",
            0,
            json.dumps({"name": "api", "signature": "api(integer)", "body_hash": "a"}),
        ),
    )
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        (
            "v1",
            "upgrade.sql",
            0,
            json.dumps({"name": "api", "signature": "api(integer)", "body_hash": "a"}),
        ),
    )
    db.executemany(
        "INSERT INTO fact_keys VALUES(?,?,?,?,?)",
        [
            ("fact:opaque-v1", "v1", "f.sql", "routines", 0),
            ("fact:opaque-v2", "v2", "f.sql", "routines", 0),
        ],
    )
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        (
            "v2",
            "f.sql",
            0,
            json.dumps({"name": "api", "signature": "api(bigint)", "body_hash": "a"}),
        ),
    )
    db.commit()
    db.close()
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "registry": "corpus.sqlite",
                "snapshots": [
                    {"alias": "v1", "logical_repo": "pg"},
                    {"alias": "v2", "logical_repo": "pg"},
                ],
            }
        )
    )
    return path


def test_offline_search_and_signature_diff(tmp_path):
    manifest = _manifest(tmp_path)
    assert len(snapshots(manifest)) == 2
    assert search(manifest, "api", snapshot="v1", kind="routine")[0]["name"] == "api"
    assert compare(manifest, "v1", "v2")[0]["change"] == "signature_changed"


def test_offline_rejects_registry_path_escape(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"registry": "../outside.sqlite", "snapshots": []}))
    with pytest.raises(ValueError, match="escapes"):
        snapshots(path)


def test_offline_diff_tracks_return_binding_kind_and_coverage(tmp_path):
    manifest = _manifest(tmp_path)
    db = sqlite3.connect(tmp_path / "corpus.sqlite")

    def add(alias, ordinal, **fact):
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            (alias, "facts.sql", ordinal, json.dumps(fact)),
        )

    add(
        "v1",
        10,
        name="api",
        schema_name="public",
        kind="function",
        language="sql",
        signature="api(integer)",
        return_type="integer",
        body_hash="a",
        definition_hash="d1",
    )
    add(
        "v1",
        11,
        name="binding",
        schema_name="public",
        kind="function",
        language="c",
        signature="binding(integer)",
        return_type="integer",
        body_hash="same",
        definition_hash="d",
        library="mod_a",
        entrypoint="fn_a",
    )
    add(
        "v1",
        12,
        name="collision",
        schema_name="public",
        kind="function",
        language="sql",
        signature="collision()",
        body_hash="a",
    )
    add(
        "v2",
        10,
        name="api",
        schema_name="public",
        kind="function",
        language="sql",
        signature="api(integer)",
        return_type="bigint",
        body_hash="a",
        definition_hash="d2",
    )
    add(
        "v2",
        11,
        name="binding",
        schema_name="public",
        kind="function",
        language="c",
        signature="binding(integer)",
        return_type="integer",
        body_hash="same",
        definition_hash="d",
        library="mod_b",
        entrypoint="fn_b",
    )
    add(
        "v2",
        12,
        name="collision",
        schema_name="public",
        kind="procedure",
        language="sql",
        signature="collision()",
        body_hash="a",
    )
    add(
        "v2",
        13,
        name="new_only",
        schema_name="public",
        kind="function",
        language="sql",
        signature="new_only()",
        body_hash="new",
    )
    db.commit()
    db.close()
    changes = compare(manifest, "v1", "v2", limit=100)
    collision_changes = [row for row in changes if "collision" in row["logical_id"]]
    assert {row["change"] for row in collision_changes} == {"added", "removed"}
    db = sqlite3.connect(tmp_path / "corpus.sqlite")
    db.execute(
        "INSERT INTO diagnostics VALUES(?,?,?,?)",
        ("v1", "huge.sql", 0, json.dumps({"category": "file_too_large"})),
    )
    db.commit()
    db.close()
    changes = compare(manifest, "v1", "v2", limit=100)
    by_name = {row["logical_id"].split(":")[-1].split(".")[-1]: row for row in changes}
    assert by_name["api"]["change"] == "signature_changed"
    assert by_name["binding"]["change"] == "definition_changed"
    assert by_name["new_only"]["change"] == "ambiguous"
    assert "diagnostics: v1=1" in by_name["new_only"]["caveat"]


def test_offline_query_bounds_reject_bool_and_large_offset(tmp_path):
    manifest = _manifest(tmp_path)
    with pytest.raises(ValueError):
        snapshots(manifest, offset=10_001)
    with pytest.raises(ValueError):
        compare(manifest, "v1", "v2", offset=True)


def test_offline_diff_reports_guard_only_changes_but_coalesces_same_guard(tmp_path):
    manifest = _manifest(tmp_path)
    db = sqlite3.connect(tmp_path / "corpus.sqlite")
    for alias, guard in (("v1", "OLD_FEATURE"), ("v2", "NEW_FEATURE")):
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?)",
            (
                alias,
                "native.c",
                0,
                json.dumps(
                    {
                        "name": "guarded",
                        "kind": "function",
                        "signature": "int guarded(void)",
                        "body_hash": "same-body",
                        "condition": guard,
                        "declaration": False,
                    }
                ),
            ),
        )
    for alias in ("v1", "v2"):
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            (
                alias,
                "template.sql.in",
                0,
                json.dumps(
                    {
                        "name": "templated",
                        "schema_name": "public",
                        "kind": "function",
                        "language": "sql",
                        "signature": "templated()",
                        "body_hash": "template-body",
                        "condition": "SAME_TEMPLATE_GUARD",
                    }
                ),
            ),
        )
    db.commit()
    db.close()
    changes = compare(manifest, "v1", "v2", limit=100)
    guarded = [row for row in changes if row["logical_id"].endswith("guarded")]
    assert len(guarded) == 1 and guarded[0]["change"] == "condition_changed"
    assert not any(row["logical_id"].endswith("templated") for row in changes)


@pytest.mark.parametrize("variant", [{"condition": "FEATURE_X"}, {"definition_hash": "other"}])
def test_offline_does_not_coalesce_different_guards_or_attributes(tmp_path, variant):
    manifest = _manifest(tmp_path)
    db = sqlite3.connect(tmp_path / "corpus.sqlite")
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        (
            "v1",
            "variant.sql",
            0,
            json.dumps(
                {
                    "name": "api",
                    "signature": "api(integer)",
                    "body_hash": "a",
                    **variant,
                }
            ),
        ),
    )
    db.commit()
    db.close()
    assert compare(manifest, "v1", "v2")[0]["change"] == "ambiguous"


def test_trace_rejects_boolean_depth_and_sqlite_work_budget_interrupts(tmp_path, monkeypatch):
    import codekg.corpus_queries as queries

    manifest = _manifest(tmp_path)
    with pytest.raises(ValueError):
        queries.trace(manifest, "from", "to", max_depth=True)

    db = sqlite3.connect(tmp_path / "corpus.sqlite")
    facts = []
    keys = []
    for ordinal in range(2_000):
        facts.append(
            (
                "v1",
                "large.c",
                ordinal,
                json.dumps({"name": f"symbol_{ordinal}", "signature": "void(void)"}),
            )
        )
        keys.append((f"fact:large-{ordinal}", "v1", "large.c", "symbols", ordinal))
    db.executemany("INSERT INTO symbols VALUES(?,?,?,?)", facts)
    db.executemany("INSERT INTO fact_keys VALUES(?,?,?,?,?)", keys)
    db.commit()
    db.close()
    _, registry = queries._registry(manifest)
    monkeypatch.setattr(queries, "_QUERY_BUDGET_SECONDS", 0)
    with pytest.raises(TimeoutError, match="work budget"):
        queries.search(manifest, "no matching symbol", snapshot="v1", kind="native")
    conn = queries._connect(registry)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute(
                "WITH RECURSIVE count_up(value) AS (SELECT 1 UNION ALL "
                "SELECT value+1 FROM count_up WHERE value<1000000000) "
                "SELECT sum(value) FROM count_up"
            ).fetchone()
    finally:
        conn.close()
