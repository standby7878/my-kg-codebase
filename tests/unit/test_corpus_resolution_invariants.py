from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from codekg.corpus_config import CorpusConfig, CorpusSnapshotConfig
from codekg.corpus_export import export_corpus
from codekg.corpus_registry import create_native_registry, resolve_corpus_facts
from codekg.sql_config import SqlConfig


def _export(tmp_path, *, c_source: str, sql_source: str = ""):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "native.c").write_text(c_source)
    if sql_source:
        (root / "routines.sql").write_text(sql_source)
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="repo"\nlogical_repo="repo"\nversion="1"\n'
        'role="postgres"\npath="repo"\n'
        "[snapshots.sql]\nenabled=true\n"
    )
    output = tmp_path / "out"
    manifest = export_corpus(config, output)
    db = sqlite3.connect(output / manifest["registry"])
    return output, manifest, db


def test_occurrence_keys_and_same_line_calls_remain_distinct(tmp_path):
    output, manifest, db = _export(
        tmp_path,
        c_source=(
            "int proto(int); int proto(int x) { return x; } "
            "int target(void) { return 1; } int caller(void) "
            "{ return target() + target(); }\n"
        ),
        sql_source=(
            "CREATE FUNCTION overload(integer) RETURNS integer LANGUAGE SQL AS 'SELECT 1'; "
            "CREATE FUNCTION overload(text) RETURNS text LANGUAGE SQL AS 'SELECT 2';\n"
        ),
    )
    try:
        target_facts = db.execute(
            "SELECT k.key,s.fact FROM fact_keys k JOIN symbols s "
            "ON k.snapshot_alias=s.snapshot_alias AND k.path=s.path AND k.ordinal=s.ordinal "
            "WHERE k.snapshot_alias='repo' "
            "AND json_extract(s.fact,'$.name') IN ('target','caller') "
            "AND k.table_name='symbols' "
            "ORDER BY k.ordinal"
        ).fetchall()
        assert len(target_facts) == 2
        keys = [row[0] for row in target_facts]
        assert len(set(keys)) == 2
        assert all(key.startswith("fact:") and len(key) == 69 for key in keys)
        call_edges = db.execute(
            "SELECT e.column_no,e.status FROM edges e JOIN fact_keys k "
            "ON e.source_key=k.key WHERE e.kind='CALLS_NATIVE' "
            "AND k.table_name='symbols' AND k.snapshot_alias='repo' "
            "AND json_extract((SELECT fact FROM symbols s WHERE s.snapshot_alias=k.snapshot_alias "
            "AND s.path=k.path AND s.ordinal=k.ordinal),'$.name')='caller' "
            "ORDER BY e.column_no"
        ).fetchall()
        assert len(call_edges) == 2
        assert call_edges[0][0] != call_edges[1][0]
        assert all(row[1] == "exact" for row in call_edges)
        csv_path = output / manifest["nodes"]["NativeSymbol"]["file"]
        with csv_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        same_line = [row for row in rows if row["start_line:int"] == "1"]
        assert len({row["key:ID(CodeKG)"] for row in same_line}) == len(same_line)
        routine_keys = db.execute(
            "SELECT k.key FROM fact_keys k JOIN routines r ON k.snapshot_alias=r.snapshot_alias "
            "AND k.path=r.path AND k.ordinal=r.ordinal WHERE k.snapshot_alias='repo' "
            "AND k.table_name='routines' AND json_extract(r.fact,'$.name')='overload'"
        ).fetchall()
        assert len(routine_keys) == 2 and len({row[0] for row in routine_keys}) == 2
    finally:
        db.close()


def test_native_evidence_links_to_the_same_exact_targets_without_sql_resolution(tmp_path):
    _, _, db = _export(
        tmp_path,
        c_source=("int same(void) { return 1; } int caller(void) { return same(); }\n"),
        sql_source=("CREATE FUNCTION same() RETURNS integer LANGUAGE SQL AS 'SELECT 1';\n"),
    )
    try:
        assert (
            db.execute(
                "SELECT 1 FROM edges WHERE kind='INVOKES_ROUTINE' "
                "AND source_key IN (SELECT k.key FROM fact_keys k JOIN evidence e "
                "ON k.snapshot_alias=e.snapshot_alias AND k.path=e.path AND k.ordinal=e.ordinal "
                "WHERE k.table_name='evidence' "
                "AND json_extract(e.fact,'$.origin')='native_call') LIMIT 1"
            ).fetchone()
            is None
        )
        assert db.execute(
            "SELECT 1 FROM edges WHERE kind='HAS_EVIDENCE' AND status='exact' LIMIT 1"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges e JOIN fact_keys k ON k.key=e.source_key "
            "JOIN evidence v ON v.snapshot_alias=k.snapshot_alias AND v.path=k.path "
            "AND v.ordinal=k.ordinal WHERE e.kind='CALLS_NATIVE' AND e.status='exact' "
            "AND json_extract(v.fact,'$.origin')='native_call' "
            "AND k.table_name='evidence' LIMIT 1"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges e JOIN fact_keys k ON k.key=e.source_key "
            "JOIN evidence v ON v.snapshot_alias=k.snapshot_alias AND v.path=k.path "
            "AND v.ordinal=k.ordinal WHERE e.kind='CALLS_NATIVE' AND e.status='exact' "
            "AND json_extract(v.fact,'$.origin')='native_call' "
            "AND k.table_name='evidence' "
            "AND e.target_key IN (SELECT key FROM fact_keys WHERE table_name='symbols') LIMIT 1"
        ).fetchone()
    finally:
        db.close()


def test_quoted_dot_routine_owners_match_full_qname_on_shared_line(tmp_path):
    _, _, db = _export(
        tmp_path,
        c_source="int noop(void) { return 0; }\n",
        sql_source=(
            'CREATE FUNCTION app."x.y"() RETURNS integer LANGUAGE SQL AS '
            "$$ SELECT cron.schedule(1); $$; "
            'CREATE FUNCTION "other.schema"."x.y"() RETURNS integer LANGUAGE SQL AS '
            "$$ SELECT cron.schedule(1); $$;\n"
        ),
    )
    try:
        owners = db.execute(
            "SELECT json_extract(e.fact,'$.owner_qname'), "
            "json_extract(r.fact,'$.schema_name') || '.' || json_extract(r.fact,'$.name') "
            "FROM evidence e "
            "JOIN fact_keys ek ON ek.snapshot_alias=e.snapshot_alias AND ek.path=e.path "
            "AND ek.ordinal=e.ordinal AND ek.table_name='evidence' "
            "JOIN edges edge ON edge.target_key=ek.key AND edge.kind='HAS_EVIDENCE' "
            "JOIN fact_keys rk ON rk.key=edge.source_key AND rk.table_name='routines' "
            "JOIN routines r ON r.snapshot_alias=rk.snapshot_alias AND r.path=rk.path "
            "AND r.ordinal=rk.ordinal "
            "WHERE json_extract(e.fact,'$.origin')='routine_body' "
            "AND json_extract(e.fact,'$.object_name')='schedule' "
            "ORDER BY 1"
        ).fetchall()
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT ordinal FROM routines WHERE snapshot_alias=? "
            "AND path=? AND (json_extract(fact,'$.schema_name') || '.' || "
            "json_extract(fact,'$.name'))=? AND json_extract(fact,'$.start_line')=? LIMIT 2",
            ("repo", "routines.sql", "app.x.y", 1),
        ).fetchone()[3]
        assert owners == [
            ("app.x.y", "app.x.y"),
            ("other.schema.x.y", "other.schema.x.y"),
        ]
        assert "SEARCH routines USING INDEX idx_routines_owner" in plan
    finally:
        db.close()


def test_macro_invocation_does_not_become_exact_function_call(tmp_path):
    _, _, db = _export(
        tmp_path,
        c_source=(
            "#define dispatch(x) (x)\n"
            "int dispatch(int x) { return x; }\n"
            "int caller(void) { return dispatch(1); }\n"
        ),
    )
    try:
        exact_targets = db.execute(
            "SELECT e.target_key FROM edges e JOIN fact_keys sk ON sk.key=e.source_key "
            "JOIN evidence v ON v.snapshot_alias=sk.snapshot_alias AND v.path=sk.path "
            "AND v.ordinal=sk.ordinal JOIN fact_keys tk ON tk.key=e.target_key "
            "JOIN symbols s ON s.snapshot_alias=tk.snapshot_alias AND s.path=tk.path "
            "AND s.ordinal=tk.ordinal WHERE e.kind='CALLS_NATIVE' AND e.status='exact' "
            "AND json_extract(v.fact,'$.origin')='native_call' "
            "AND sk.table_name='evidence' "
            "AND json_extract(v.fact,'$.object_name')='dispatch' "
            "AND json_extract(s.fact,'$.name')='dispatch' "
            "AND json_extract(s.fact,'$.kind')='function'"
        ).fetchall()
        assert exact_targets == []
    finally:
        db.close()


def test_repeated_macro_diagnostics_use_the_occurrence_primary_key(tmp_path, monkeypatch):
    import codekg.corpus_export as corpus_export
    from codekg.corpus_registry import create_native_registry as create_registry

    macro_count = 80
    root = tmp_path / "repo"
    root.mkdir()
    definitions = "\n".join(f"#define macro_{index:03d}() (0)" for index in range(macro_count))
    invocations = "\n".join(f"  macro_{index:03d}();" for index in range(macro_count))
    (root / "native.c").write_text(f"{definitions}\nvoid caller(void) {{\n{invocations}\n}}\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="repo"\nlogical_repo="repo"\nversion="1"\n'
        'role="postgres"\npath="repo"\n'
    )
    point_lookups = 0

    class GuardedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            nonlocal point_lookups
            normalized = " ".join(sql.lower().split())
            if "select ordinal from diagnostics" in normalized and "and fact=?" in normalized:
                raise AssertionError("diagnostic duplicate lookup must not scan facts")
            if (
                "select ordinal,fact from diagnostics" in normalized
                and "and ordinal=?" in normalized
            ):
                point_lookups += 1
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        corpus_export,
        "create_native_registry",
        lambda path: GuardedConnection(create_registry(path)),
    )
    output = tmp_path / "out"
    manifest = export_corpus(config, output)

    registry_path = output / manifest["registry"]
    db = sqlite3.connect(registry_path)
    try:
        diagnostics = db.execute(
            "SELECT count(*) FROM diagnostics WHERE snapshot_alias='repo'"
        ).fetchone()[0]
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT ordinal,fact FROM diagnostics "
            "WHERE snapshot_alias=? AND path=? AND ordinal=?",
            ("repo", "native.c", 1),
        ).fetchone()[3]
    finally:
        db.close()
    assert diagnostics == macro_count
    assert point_lookups == macro_count
    assert "SEARCH diagnostics" in plan and "snapshot_alias=? AND path=? AND ordinal=?" in plan


def test_sql_arity_filter_precedes_the_candidate_sentinel(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    snapshot = CorpusSnapshotConfig(
        "app", "application", "1", "application", root, (), SqlConfig(enabled=True)
    )
    corpus = CorpusConfig(tmp_path / "manifest.toml", (snapshot,))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.execute("INSERT INTO metadata VALUES('app.revision','revision')")
    for ordinal in range(34):
        routine = {
            "name": "api",
            "schema_name": "public",
            "signature": f"api(type_{ordinal}, type_{ordinal})",
            "arity": 2,
            "default_arg_count": 0,
            "variadic_arg_count": 0,
            "language": "sql",
            "kind": "function",
            "return_type": "integer",
            "body_hash": f"body-{ordinal}",
            "start_line": ordinal + 1,
        }
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            ("app", "routines.sql", ordinal, json.dumps(routine, sort_keys=True)),
        )
    valid = {
        "name": "api",
        "schema_name": "public",
        "signature": "api(integer)",
        "arity": 1,
        "default_arg_count": 0,
        "variadic_arg_count": 0,
        "language": "sql",
        "kind": "function",
        "return_type": "integer",
        "body_hash": "valid",
        "start_line": 100,
    }
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        ("app", "routines.sql", 34, json.dumps(valid, sort_keys=True)),
    )
    evidence = {
        "origin": "sql_source",
        "schema_name": "public",
        "object_name": "api",
        "arity": 1,
        "dynamic": False,
        "start_line": 1,
        "start_column": 1,
    }
    db.execute(
        "INSERT INTO evidence VALUES(?,?,?,?)",
        ("app", "call.sql", 0, json.dumps(evidence, sort_keys=True)),
    )
    resolve_corpus_facts(db, corpus)
    edge = db.execute("SELECT target_key,status FROM edges WHERE kind='INVOKES_ROUTINE'").fetchone()
    assert edge is not None and edge[1] == "exact"
    target_fact = db.execute(
        "SELECT r.fact FROM fact_keys k JOIN routines r ON k.snapshot_alias=r.snapshot_alias "
        "AND k.path=r.path AND k.ordinal=r.ordinal WHERE k.key=? AND k.table_name='routines'",
        (edge[0],),
    ).fetchone()[0]
    assert json.loads(target_fact)["signature"] == "api(integer)"
    db.close()


@pytest.mark.parametrize("unrelated_count", [100, 200, 400])
def test_hot_resolution_lookups_use_selective_indexes_with_bounded_vm_growth(
    tmp_path, unrelated_count
):
    import json

    from codekg.corpus_registry import create_native_registry

    db = create_native_registry(tmp_path / f"registry-{unrelated_count}.sqlite")
    db.executemany(
        "INSERT INTO symbols VALUES(?,?,?,?)",
        (
            (
                "pg",
                f"target-{index:02d}.c",
                index,
                json.dumps(
                    {
                        "name": "target_entry",
                        "kind": "function",
                        "declaration": False,
                        "static": False,
                    }
                ),
            )
            for index in range(40)
        ),
    )
    db.executemany(
        "INSERT INTO symbols VALUES(?,?,?,?)",
        (
            (
                "pg",
                f"unrelated-{index:04d}.c",
                0,
                json.dumps(
                    {
                        "name": f"unrelated_{index}",
                        "kind": "function",
                        "declaration": False,
                        "static": False,
                    }
                ),
            )
            for index in range(unrelated_count)
        ),
    )
    db.executemany(
        "INSERT INTO sqlobjects VALUES(?,?,?,?,?,?,?)",
        (
            (
                "app",
                f"wrong-{index}",
                "public",
                "function",
                f"unrelated_{index}",
                f"unrelated_{index}()",
                "api.sql",
            )
            for index in range(unrelated_count)
        ),
    )
    db.execute(
        "INSERT INTO sqlobjects VALUES(?,?,?,?,?,?,?)",
        ("app", "right", "public", "function", "api", "api(integer)", "api.sql"),
    )

    symbol_sql = (
        "SELECT snapshot_alias,path,ordinal,fact FROM symbols INDEXED BY idx_symbols_scope "
        "WHERE snapshot_alias IN (?) AND json_extract(fact,'$.name')=? "
        "AND json_extract(fact,'$.kind')='function' "
        "AND json_extract(fact,'$.declaration')=0 AND json_extract(fact,'$.static')=0 "
        "ORDER BY path,ordinal LIMIT 33"
    )
    object_sql = (
        "SELECT key,signature FROM sqlobjects INDEXED BY idx_sqlobjects_identity "
        "WHERE snapshot_alias=? AND schema_name=? AND object_name=? AND kind=? "
        "AND signature=? ORDER BY key LIMIT 2"
    )
    symbol_plan = " ".join(
        row[3] for row in db.execute("EXPLAIN QUERY PLAN " + symbol_sql, ("pg", "target_entry"))
    )
    object_plan = " ".join(
        row[3]
        for row in db.execute(
            "EXPLAIN QUERY PLAN " + object_sql,
            ("app", "public", "api", "function", "api(integer)"),
        )
    )
    assert "idx_symbols_scope" in symbol_plan
    assert "idx_sqlobjects_identity" in object_plan

    def measured(sql, parameters):
        operations = 0

        def count_operation():
            nonlocal operations
            operations += 1
            return 0

        db.set_progress_handler(count_operation, 1)
        try:
            rows = db.execute(sql, parameters).fetchall()
        finally:
            db.set_progress_handler(None, 0)
        return rows, operations

    symbol_rows, symbol_operations = measured(symbol_sql, ("pg", "target_entry"))
    object_rows, object_operations = measured(
        object_sql, ("app", "public", "api", "function", "api(integer)")
    )
    assert len(symbol_rows) == 33
    assert object_rows == [("right", "api(integer)")]
    assert symbol_operations < 1_000
    assert object_operations < 200
    db.close()


def test_routine_guards_combine_and_sqlobject_signature_filter_uses_composite_index(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    snapshot = CorpusSnapshotConfig("app", "app", "1", "postgres", root)
    corpus = CorpusConfig(tmp_path / "manifest.toml", (snapshot,))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.execute("INSERT INTO metadata VALUES('app.revision','revision')")
    routine = {
        "name": "api",
        "schema_name": "public",
        "signature": "api(integer)",
        "arity": 1,
        "kind": "function",
        "language": "internal",
        "entrypoint": "api_impl",
        "condition": "routine guard",
        "start_line": 1,
        "start_column": 0,
    }
    db.execute(
        "INSERT INTO routines VALUES(?,?,?,?)",
        ("app", "api.sql", 0, json.dumps(routine, sort_keys=True)),
    )
    target = {
        "name": "api_impl",
        "kind": "function",
        "signature": "int api_impl(void)",
        "start_line": 1,
        "start_column": 0,
        "end_line": 1,
        "end_column": 20,
        "declaration": False,
        "static": False,
        "condition": "native guard",
        "body_hash": "body",
    }
    db.execute(
        "INSERT INTO symbols VALUES(?,?,?,?)",
        ("app", "api.c", 0, json.dumps(target, sort_keys=True)),
    )
    evidence = {
        "origin": "sql_source",
        "schema_name": "public",
        "object_name": "api",
        "arity": 1,
        "dynamic": False,
        "condition": "source guard",
        "start_line": 1,
        "start_column": 1,
    }
    db.execute(
        "INSERT INTO evidence VALUES(?,?,?,?)",
        ("app", "caller.sql", 0, json.dumps(evidence, sort_keys=True)),
    )
    for index in range(40):
        db.execute(
            "INSERT INTO sqlobjects VALUES(?,?,?,?,?,?,?)",
            (
                "app",
                f"wrong-{index}",
                "public",
                "function",
                "api",
                f"api(type_{index})",
                "api.sql",
            ),
        )
    db.execute(
        "INSERT INTO sqlobjects VALUES(?,?,?,?,?,?,?)",
        ("app", "right", "public", "function", "api", "api(integer)", "api.sql"),
    )
    query_plan = " ".join(
        row[3]
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT key FROM sqlobjects WHERE snapshot_alias=? "
            "AND schema_name=? AND object_name=? AND kind=? AND signature=?",
            ("app", "public", "api", "function", "api(integer)"),
        )
    )
    assert "idx_sqlobjects_identity" in query_plan
    resolve_corpus_facts(db, corpus)
    object_edge = db.execute(
        "SELECT target_key,status FROM edges WHERE kind='DESCRIBES_SQL_OBJECT'"
    ).fetchone()
    assert object_edge == ("right", "exact")
    bind_edge = db.execute(
        "SELECT status,condition FROM edges WHERE kind='BINDS_TO_NATIVE'"
    ).fetchone()
    assert bind_edge == ("conditional", "routine guard AND native guard")
    invoke_edge = db.execute(
        "SELECT status,condition FROM edges WHERE kind='INVOKES_ROUTINE'"
    ).fetchone()
    assert invoke_edge == ("conditional", "source guard AND routine guard")
    db.close()


def test_internal_bindings_are_scoped_to_selected_postgres_dependencies(tmp_path):
    roots = {name: tmp_path / name for name in ("pg_a", "pg_b", "ext", "orphan")}
    for root in roots.values():
        root.mkdir()
    snapshots = (
        CorpusSnapshotConfig("pg_a", "postgres", "18", "postgres", roots["pg_a"]),
        CorpusSnapshotConfig("pg_b", "postgres", "19", "postgres", roots["pg_b"]),
        CorpusSnapshotConfig("ext", "ext", "1", "extension", roots["ext"], dependencies=("pg_b",)),
        CorpusSnapshotConfig("orphan", "orphan", "1", "extension", roots["orphan"]),
    )
    corpus = CorpusConfig(tmp_path / "manifest.toml", snapshots)
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.executemany(
        "INSERT INTO metadata VALUES(?,?)",
        ((f"{item.alias}.revision", f"{item.alias}-r") for item in snapshots),
    )

    def symbol(alias, ordinal, name, condition=None):
        value = {
            "name": name,
            "kind": "function",
            "signature": f"int {name}(void)",
            "start_line": ordinal + 1,
            "start_column": 0,
            "end_line": ordinal + 1,
            "end_column": 10,
            "declaration": False,
            "static": False,
            "condition": condition,
        }
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?)",
            (alias, "builtins.c", ordinal, json.dumps(value, sort_keys=True)),
        )

    def routine(alias, path, ordinal, entrypoint, condition=None):
        value = {
            "name": entrypoint.removesuffix("_impl"),
            "schema_name": "pg_catalog",
            "signature": f"{entrypoint.removesuffix('_impl')}()",
            "arity": 0,
            "kind": "function",
            "language": "internal",
            "entrypoint": entrypoint,
            "condition": condition,
            "start_line": ordinal + 1,
            "start_column": 0,
        }
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            (alias, path, ordinal, json.dumps(value, sort_keys=True)),
        )

    symbol("pg_a", 0, "conditional_impl")
    symbol("pg_b", 0, "conditional_impl", "PG_FEATURE")
    symbol("pg_b", 1, "ambiguous_impl")
    symbol("pg_b", 2, "ambiguous_impl", "ALT_FEATURE")
    routine("ext", "ext.sql", 0, "conditional_impl", "EXT_FEATURE")
    routine("ext", "ext.sql", 1, "ambiguous_impl")
    routine("orphan", "orphan.sql", 0, "missing_impl")

    resolve_corpus_facts(db, corpus)
    conditional = db.execute(
        "SELECT e.status,e.condition,k.snapshot_alias,t.snapshot_alias "
        "FROM edges e JOIN fact_keys k ON k.key=e.source_key "
        "JOIN fact_keys t ON t.key=e.target_key WHERE e.kind='BINDS_TO_NATIVE'"
    ).fetchone()
    assert conditional == ("conditional", "EXT_FEATURE AND PG_FEATURE", "ext", "pg_b")
    candidates = db.execute(
        "SELECT e.status,count(*),count(DISTINCT t.snapshot_alias) "
        "FROM edges e JOIN fact_keys k ON k.key=e.source_key "
        "JOIN fact_keys t ON t.key=e.target_key WHERE e.kind='NATIVE_CANDIDATE' "
        "AND k.snapshot_alias='ext' GROUP BY e.status"
    ).fetchone()
    assert candidates == ("ambiguous", 2, 1)
    diagnostic = db.execute(
        "SELECT json_extract(fact,'$.category') FROM diagnostics WHERE snapshot_alias='orphan'"
    ).fetchone()
    assert diagnostic == ("internal_binding_unresolved",)
    db.close()


def test_distinct_dependency_aliases_remain_ambiguous_after_candidate_grouping(tmp_path):
    roots = {name: tmp_path / name for name in ("app", "dep_a", "dep_b")}
    for root in roots.values():
        root.mkdir()
    app = CorpusSnapshotConfig("app", "app", "1", "application", roots["app"], ("dep_a", "dep_b"))
    dependencies = (
        CorpusSnapshotConfig("dep_a", "shared", "1", "extension", roots["dep_a"]),
        CorpusSnapshotConfig("dep_b", "shared", "1", "extension", roots["dep_b"]),
    )
    corpus = CorpusConfig(tmp_path / "manifest.toml", (*dependencies, app))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.executemany(
        "INSERT INTO metadata VALUES(?,?)",
        (("app.revision", "app-r"), ("dep_a.revision", "a-r"), ("dep_b.revision", "b-r")),
    )
    routine = {
        "name": "shared_api",
        "schema_name": "public",
        "signature": "shared_api(integer)",
        "arity": 1,
        "default_arg_count": 0,
        "variadic_arg_count": 0,
        "language": "sql",
        "kind": "function",
        "return_type": "integer",
        "body_hash": "same-body",
        "definition_hash": "same-definition",
        "condition": None,
    }
    for alias in ("dep_a", "dep_b"):
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            (alias, "api.sql", 0, json.dumps(routine, sort_keys=True)),
        )
    evidence = {
        "origin": "sql_source",
        "schema_name": "public",
        "object_name": "shared_api",
        "arity": 1,
        "dynamic": False,
        "start_line": 1,
        "start_column": 1,
    }
    db.execute(
        "INSERT INTO evidence VALUES(?,?,?,?)",
        ("app", "caller.sql", 0, json.dumps(evidence, sort_keys=True)),
    )
    resolve_corpus_facts(db, corpus)
    candidates = db.execute(
        "SELECT e.status,k.snapshot_alias FROM edges e JOIN fact_keys k "
        "ON k.key=e.target_key WHERE e.kind='ROUTINE_CANDIDATE' ORDER BY k.snapshot_alias"
    ).fetchall()
    assert candidates == [("ambiguous", "dep_a"), ("ambiguous", "dep_b")]
    db.close()


def test_resolution_uses_selected_ordinals_without_fact_json_lookups(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    snapshot = CorpusSnapshotConfig("app", "app", "1", "application", root)
    corpus = CorpusConfig(tmp_path / "manifest.toml", (snapshot,))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.execute("INSERT INTO metadata VALUES('app.revision','app-r')")
    db.executemany(
        "INSERT INTO symbols VALUES(?,?,?,?)",
        (
            (
                "app",
                "native.c",
                ordinal,
                json.dumps(
                    {
                        "name": name,
                        "kind": "function",
                        "signature": f"int {name}(void)",
                        "start_line": 1,
                        "start_column": ordinal,
                        "end_line": 2,
                        "end_column": 0,
                        "declaration": False,
                        "static": False,
                        "condition": None,
                        "body_hash": f"body-{name}",
                    },
                    sort_keys=True,
                ),
            )
            for ordinal, name in enumerate(("caller", "native_target"))
        ),
    )
    calls = []
    evidence = []
    for ordinal in range(80):
        calls.append(
            (
                "app",
                "native.c",
                ordinal,
                json.dumps(
                    {
                        "owner_name": "caller",
                        "owner_start_line": 1,
                        "callee_name": "native_target",
                        "start_line": 1,
                        "start_column": ordinal + 1,
                        "evidence_ordinal": ordinal,
                        "dynamic": False,
                        "condition": None,
                    },
                    sort_keys=True,
                ),
            )
        )
        evidence.append(
            (
                "app",
                "native.c",
                ordinal,
                json.dumps(
                    {
                        "origin": "native_call",
                        "object_name": "native_target",
                        "owner_qname": "caller",
                        "start_line": 1,
                        "start_column": ordinal + 1,
                        "dynamic": False,
                    },
                    sort_keys=True,
                ),
            )
        )
    db.executemany("INSERT INTO calls VALUES(?,?,?,?)", calls)
    db.executemany("INSERT INTO evidence VALUES(?,?,?,?)", evidence)
    for ordinal in range(40):
        routine = {
            "name": f"routine_{ordinal}",
            "schema_name": "public",
            "signature": f"routine_{ordinal}()",
            "arity": 0,
            "kind": "function",
            "language": "sql",
            "return_type": "integer",
            "body_hash": f"body-{ordinal}",
            "definition_hash": f"definition-{ordinal}",
        }
        db.execute(
            "INSERT INTO routines VALUES(?,?,?,?)",
            ("app", "routines.sql", ordinal, json.dumps(routine, sort_keys=True)),
        )
        sql_evidence = {
            "origin": "sql_source",
            "schema_name": "public",
            "object_name": f"routine_{ordinal}",
            "arity": 0,
            "dynamic": False,
            "start_line": ordinal + 1,
            "start_column": 1,
        }
        db.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (
                "app",
                "caller.sql",
                ordinal,
                json.dumps(sql_evidence, sort_keys=True),
            ),
        )

    class RejectFactLookup:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            lowered = " ".join(sql.lower().split())
            if "select ordinal from symbols" in lowered and "fact=?" in lowered:
                raise AssertionError("symbol fact JSON occurrence lookup returned to hot path")
            if "select ordinal from routines" in lowered and "fact=?" in lowered:
                raise AssertionError("routine fact JSON occurrence lookup returned to hot path")
            if (
                "select ordinal from evidence" in lowered
                and "json_extract(fact,'$.origin')='native_call'" in lowered
            ):
                raise AssertionError("native-call evidence occurrence lookup returned to hot path")
            return self.connection.execute(sql, parameters)

        def executemany(self, sql, parameters):
            return self.connection.executemany(sql, parameters)

        def commit(self):
            return self.connection.commit()

    resolve_corpus_facts(RejectFactLookup(db), corpus)
    call_edges = db.execute(
        "SELECT count(*) FROM edges WHERE kind='CALLS_NATIVE' AND status='exact'"
    ).fetchone()[0]
    assert call_edges == 160  # owner call edges and mirrored SourceEvidence edges
    same_line_calls = db.execute(
        "SELECT count(DISTINCT column_no) FROM edges WHERE kind='CALLS_NATIVE' AND path='native.c'"
    ).fetchone()[0]
    assert same_line_calls == 80
    routine_edges = db.execute(
        "SELECT count(*) FROM edges WHERE kind='INVOKES_ROUTINE' AND status='exact'"
    ).fetchone()[0]
    assert routine_edges == 40
    db.close()


def test_visible_header_macros_make_function_resolution_candidate_only(tmp_path):
    roots = {name: tmp_path / name for name in ("app", "dep_a", "dep_b")}
    for root in roots.values():
        root.mkdir()
    app = CorpusSnapshotConfig("app", "app", "1", "application", roots["app"], ("dep_a", "dep_b"))
    dependencies = (
        CorpusSnapshotConfig("dep_a", "dep_a", "1", "extension", roots["dep_a"]),
        CorpusSnapshotConfig("dep_b", "dep_b", "1", "extension", roots["dep_b"]),
    )
    corpus = CorpusConfig(tmp_path / "manifest.toml", (*dependencies, app))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.executemany(
        "INSERT INTO metadata VALUES(?,?)",
        (("app.revision", "app-r"), ("dep_a.revision", "a-r"), ("dep_b.revision", "b-r")),
    )

    def insert_symbol(alias, path, ordinal, name, kind, *, static=False):
        fact = {
            "name": name,
            "kind": kind,
            "signature": f"int {name}(void)",
            "start_line": 1,
            "start_column": 0,
            "end_line": 1,
            "end_column": 30,
            "declaration": False,
            "static": static,
            "condition": None,
            "body_hash": f"{alias}-{path}-{kind}",
        }
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?)",
            (alias, path, ordinal, json.dumps(fact, sort_keys=True)),
        )

    insert_symbol("dep_a", "api.c", 0, "foo", "function")
    insert_symbol("dep_a", "include/api.h", 0, "foo", "macro")
    insert_symbol("dep_b", "include/compat.h", 0, "foo", "macro")
    insert_symbol("app", "caller.c", 0, "caller", "function")
    db.execute(
        "INSERT INTO calls VALUES(?,?,?,?)",
        (
            "app",
            "caller.c",
            0,
            json.dumps(
                {
                    "owner_name": "caller",
                    "owner_start_line": 1,
                    "callee_name": "foo",
                    "start_line": 1,
                    "start_column": 20,
                    "dynamic": False,
                    "condition": None,
                },
                sort_keys=True,
            ),
        ),
    )
    resolve_corpus_facts(db, corpus)
    edges = db.execute(
        "SELECT e.kind,e.status,k.snapshot_alias,s.fact FROM edges e "
        "JOIN fact_keys k ON k.key=e.target_key JOIN symbols s "
        "ON s.snapshot_alias=k.snapshot_alias AND s.path=k.path AND s.ordinal=k.ordinal "
        "WHERE e.path='caller.c' AND e.kind IN ('CALLS_NATIVE','NATIVE_CANDIDATE')"
    ).fetchall()
    assert len(edges) == 1
    assert edges[0][0:3] == ("NATIVE_CANDIDATE", "ambiguous", "dep_a")
    assert json.loads(edges[0][3])["kind"] == "function"
    assert (
        db.execute("SELECT 1 FROM edges WHERE kind='CALLS_NATIVE' AND status='exact'").fetchone()
        is None
    )
    db.close()


def test_native_scope_precedence_filters_before_its_candidate_limit(tmp_path):
    app_root = tmp_path / "app"
    pg_root = tmp_path / "pg"
    app_root.mkdir()
    pg_root.mkdir()
    postgres = CorpusSnapshotConfig("pg", "postgres", "18", "postgres", pg_root)
    app = CorpusSnapshotConfig("app", "app", "1", "application", app_root, ("pg",))
    corpus = CorpusConfig(tmp_path / "manifest.toml", (postgres, app))
    db = create_native_registry(tmp_path / "registry.sqlite")
    db.executemany(
        "INSERT INTO metadata VALUES(?,?)",
        (("pg.revision", "pg-r"), ("app.revision", "app-r")),
    )

    def symbol(alias, path, ordinal, name, *, static=False):
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?)",
            (
                alias,
                path,
                ordinal,
                json.dumps(
                    {
                        "name": name,
                        "kind": "function",
                        "signature": f"int {name}(void)",
                        "start_line": 1,
                        "start_column": 0,
                        "end_line": 2,
                        "end_column": 1,
                        "declaration": False,
                        "static": static,
                        "condition": None,
                    },
                    sort_keys=True,
                ),
            ),
        )

    symbol("app", "main.c", 0, "caller")
    symbol("app", "lib.c", 0, "target")
    symbol("pg", "api.c", 0, "target")
    for index in range(40):
        symbol("app", f"zz_dupe/{index:02}.c", 0, "target")
    for index in range(40):
        symbol("app", f"unused/{index:02}.c", 0, "target", static=True)
    symbol("app", "local.c", 0, "local_caller")
    symbol("app", "local.c", 1, "local_target", static=True)
    symbol("app", "lib.c", 1, "local_target")
    symbol("app", "hidden.c", 0, "hidden_only", static=True)
    symbol("app", "hidden-call.c", 0, "hidden_caller")
    for ordinal, (path, owner, callee) in enumerate(
        (
            ("main.c", "caller", "target"),
            ("local.c", "local_caller", "local_target"),
            ("hidden-call.c", "hidden_caller", "hidden_only"),
        )
    ):
        call = {
            "owner_name": owner,
            "owner_start_line": 1,
            "callee_name": callee,
            "start_line": 1,
            "start_column": 20 + ordinal,
            "dynamic": False,
            "condition": None,
        }
        db.execute("INSERT INTO calls VALUES(?,?,?,?)", ("app", path, 0, json.dumps(call)))
        evidence = {
            "origin": "native_call",
            "object_name": callee,
            "owner_qname": owner,
            "owner_line": 1,
            "start_line": 1,
            "start_column": 20 + ordinal,
            "dynamic": False,
        }
        db.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            ("app", path, 0, json.dumps(evidence, sort_keys=True)),
        )
    resolve_corpus_facts(db, corpus)
    targets = db.execute(
        "SELECT e.path,t.snapshot_alias,t.path,t.fact FROM edges e "
        "JOIN fact_keys sk ON sk.key=e.source_key "
        "JOIN fact_keys tk ON tk.key=e.target_key "
        "JOIN symbols t ON t.snapshot_alias=tk.snapshot_alias AND t.path=tk.path "
        "AND t.ordinal=tk.ordinal WHERE e.kind='CALLS_NATIVE' AND e.status='exact' "
        "AND sk.snapshot_alias='app' AND sk.table_name='symbols' "
        "ORDER BY e.path"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in targets] == [
        ("local.c", "app", "local.c"),
        ("main.c", "app", "lib.c"),
    ]
    assert (
        db.execute(
            "SELECT 1 FROM edges WHERE kind IN ('CALLS_NATIVE','NATIVE_CANDIDATE') "
            "AND target_key IN (SELECT k.key FROM fact_keys k JOIN symbols s "
            "ON k.snapshot_alias=s.snapshot_alias AND k.path=s.path AND k.ordinal=s.ordinal "
            "WHERE k.snapshot_alias='app' AND s.path='hidden.c' "
            "AND json_extract(s.fact,'$.name')='hidden_only')"
        ).fetchone()
        is None
    )
    db.close()
