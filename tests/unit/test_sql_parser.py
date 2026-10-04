from __future__ import annotations

import hashlib

import pytest

from codekg.sql_config import SqlConfig
from codekg.sql_parser import _find_query, _protected_query_spans, parse_sql

pytestmark = pytest.mark.unit


def refs(file, role=None):
    return [ref for ref in file.sql_object_refs if role is None or ref.role == role]


def test_plpgsql_query_search_uses_absolute_offsets_without_suffix_copies() -> None:
    decoys = "".join(f"/* SELECT app.fake_{index}(); */\n" for index in range(128))
    body = (
        "-- π SELECT app.comment_decoy();\n"
        + decoys
        + "pErFoRm app.first('π');\n"
        + "SELECT app.second('x');\n"
    )
    protected = _protected_query_spans(body)

    class NoSliceText(str):
        def __getitem__(self, index):
            if isinstance(index, slice):
                raise AssertionError("query search copied a source suffix")
            return super().__getitem__(index)

    source = NoSliceText(body)
    first = _find_query(source, "SELECT app.first('π')", 0, protected=protected)
    assert first == body.index("pErFoRm")
    cursor = first + len("PERFORM app.first('π')")
    second = _find_query(source, "SELECT app.second('x')", cursor, protected=protected)
    assert second == body.index("SELECT app.second")


def test_sql_parser_extracts_ddl_dml_view_and_preserves_artifact_text() -> None:
    source = (
        "CREATE TABLE app.users (id integer);\n"
        "CREATE VIEW app.active AS SELECT u.id FROM app.users AS u;\n"
        "INSERT INTO app.audit SELECT id FROM app.users;"
    )
    file = parse_sql(source, {"path": "database/schema.sql"})

    assert file.parse_status == "ok"
    assert [statement.kind for statement in file.sql_statements] == [
        "create_table",
        "create_view",
        "insert",
    ]
    assert file.sql_artifacts[0].text == source
    assert file.sql_artifacts[0].text_hash == hashlib.sha256(source.encode()).hexdigest()
    assert [(ref.role, ref.raw_name) for ref in refs(file)] == [
        ("define", "app.users"),
        ("define", "app.active"),
        ("read", "app.users"),
        ("write", "app.audit"),
        ("read", "app.users"),
    ]


def test_cte_and_aliases_are_not_global_object_refs() -> None:
    file = parse_sql(
        "WITH recent AS (SELECT id FROM app.users) "
        "SELECT recent.id FROM recent JOIN app.events AS e ON e.id = recent.id;"
    )
    assert [(ref.role, ref.object_name) for ref in refs(file)] == [
        ("read", "users"),
        ("read", "events"),
    ]


def test_cte_scope_applies_to_dml_but_not_schema_qualified_names() -> None:
    file = parse_sql(
        "WITH x AS (SELECT id FROM app.source) "
        "UPDATE x SET id = 1 FROM app.x WHERE x.id = app.x.id;"
    )
    assert [(ref.role, ref.raw_name) for ref in refs(file)] == [
        ("write", "x"),
        ("read", "x"),
        ("read", "app.source"),
        ("read", "app.x"),
    ]


def test_unqualified_references_keep_ordered_search_path_and_database() -> None:
    file = parse_sql(
        "SET search_path TO tenant, public; SELECT * FROM users; SELECT * FROM public.users;",
        {"path": "x.sql", "database": "management"},
    )
    user_refs = [ref for ref in refs(file, "read") if ref.object_name == "users"]
    assert user_refs[0].schema_name is None
    assert user_refs[0].search_path == ("tenant", "public")
    assert user_refs[0].database_name == "management"
    assert user_refs[1].schema_name == "public"


def test_default_schema_applies_to_initial_unqualified_definitions() -> None:
    file = parse_sql(
        "CREATE TABLE items(id int); CREATE FUNCTION refresh() RETURNS int "
        "LANGUAGE SQL AS $$SELECT 1$$;",
        config=SqlConfig(default_schema="tenant", search_path=("public",)),
    )

    definitions = refs(file, "define")
    assert [(ref.object_name, ref.schema_name) for ref in definitions] == [
        ("items", "tenant"),
        ("refresh", "tenant"),
    ]


def test_update_delete_merge_have_read_and_write_target_occurrences() -> None:
    file = parse_sql(
        "UPDATE app.t SET id = 1; DELETE FROM app.t; "
        "MERGE INTO app.t USING app.source ON true WHEN MATCHED THEN UPDATE SET id = 1;"
    )
    target_refs = [ref for ref in refs(file) if ref.object_name == "t"]
    assert [(ref.role, ref.statement_ordinal) for ref in target_refs] == [
        ("write", 1),
        ("read", 1),
        ("write", 2),
        ("read", 2),
        ("write", 3),
        ("read", 3),
    ]


def test_temp_tables_are_marked_temporary_and_drop_and_call_are_single_refs() -> None:
    file = parse_sql(
        "CREATE TEMP TABLE t (id int); SELECT * FROM t; "
        "CALL app.refresh(); DROP TABLE app.t, app.u;"
    )
    table_refs = [ref for ref in refs(file) if ref.object_name in {"t", "u"}]
    assert [(ref.role, ref.object_name, ref.object_kind_hint) for ref in table_refs] == [
        ("define", "t", "temporary_table"),
        ("read", "t", "temporary_table"),
        ("drop", "t", "table"),
        ("drop", "u", "table"),
    ]
    calls = [ref for ref in refs(file, "call") if ref.object_name == "refresh"]
    assert len(calls) == 1
    assert calls[0].object_kind_hint == "procedure"


def test_alter_table_and_create_index_keep_table_schema() -> None:
    file = parse_sql(
        "ALTER TABLE app.users ADD COLUMN x int;\n"
        "CREATE INDEX idx_users ON app.users (id);\n"
        "CREATE INDEX idx_local ON users (id);"
    )
    definitions = refs(file, "define")
    alters = [ref for ref in refs(file, "alter") if ref.raw_name == "app.users"]
    indexes = [ref for ref in definitions if ref.object_kind_hint == "index"]
    assert alters[0].object_kind_hint == "table"
    assert indexes[0].schema_name == "app"
    assert [ref.raw_name for ref in indexes] == ["idx_users"]
    assert all(ref.raw_name != "idx_local" for ref in indexes)


def test_generic_function_call_remains_function_and_call_is_procedure() -> None:
    file = parse_sql("SELECT f(1); CALL app.refresh();")
    assert [(ref.role, ref.object_kind_hint) for ref in refs(file)] == [
        ("call", "function"),
        ("call", "procedure"),
    ]


def test_create_table_as_distinguishes_materialized_view() -> None:
    file = parse_sql(
        "CREATE TABLE app.copy AS SELECT 1;\n"
        "CREATE MATERIALIZED VIEW app.mv AS SELECT 1;\n"
        "CREATE TEMP TABLE temp_copy AS SELECT 1;"
    )
    definitions = refs(file, "define")
    assert [(ref.object_name, ref.object_kind_hint) for ref in definitions] == [
        ("copy", "table"),
        ("mv", "materialized_view"),
        ("temp_copy", "temporary_table"),
    ]


def test_temporary_ctas_shadows_later_references() -> None:
    file = parse_sql(
        "CREATE TABLE temp_copy (id int);\n"
        "CREATE TEMP TABLE temp_copy AS SELECT * FROM temp_copy;\n"
        "SELECT * FROM temp_copy;"
    )
    table_refs = [ref for ref in refs(file, "read") if ref.object_name == "temp_copy"]
    assert [(ref.role, ref.object_kind_hint) for ref in table_refs] == [
        ("read", "table"),
        ("read", "temporary_table"),
    ]


@pytest.mark.parametrize(
    ("sql", "expected_kind"),
    [
        ("ALTER TABLE app.t RENAME TO t2;", "table"),
        ("ALTER INDEX app.idx RENAME TO idx2;", "index"),
        ("ALTER VIEW app.v RENAME TO v2;", "view"),
    ],
)
def test_rename_statement_uses_rename_type_kind(sql: str, expected_kind: str) -> None:
    file = parse_sql(sql)
    assert refs(file, "alter")[0].object_kind_hint == expected_kind


def test_unimplemented_do_reports_unsupported_warning() -> None:
    file = parse_sql("DO $$ BEGIN PERFORM app.f(); END $$;")
    assert file.parse_status == "partial"
    assert any(d.category == "sql_unsupported_construct" for d in file.diagnostics)


def test_native_scanner_handles_nested_comments_and_escape_strings() -> None:
    source = r"/* outer /* nested; */ still */ SELECT E'it\'; still;' ; SELECT 2;"
    file = parse_sql(source)
    assert file.parse_status == "ok"
    assert [statement.kind for statement in file.sql_statements] == ["select", "select"]


def test_partial_native_scan_failure_keeps_prior_and_later_statements() -> None:
    file = parse_sql("SELECT * FROM first; SELECT FROM; SELECT * FROM last;")
    assert file.parse_status == "partial"
    assert [ref.object_name for ref in refs(file, "read")] == ["first", "last"]


def test_quoted_names_and_overload_signature_fold_correctly() -> None:
    file = parse_sql(
        'CREATE FUNCTION "App"."DoWork"(integer, OUT result text) '
        "RETURNS text LANGUAGE sql AS $$SELECT 1$$;\n"
        "CREATE FUNCTION app.dowork(text) RETURNS text LANGUAGE sql AS $$SELECT 1$$;"
    )
    definitions = refs(file, "define")
    assert [
        (ref.raw_name, ref.schema_name, ref.object_name, ref.signature_hint) for ref in definitions
    ] == [
        ('"App"."DoWork"', "App", "DoWork", "DoWork(int4)"),
        ("app.dowork", "app", "dowork", "dowork(text)"),
    ]
    assert [statement.parent_ordinal for statement in file.sql_statements] == [None, 1, None, 3]


def test_routine_signature_keeps_arrays_and_quoted_type_case() -> None:
    file = parse_sql(
        'CREATE FUNCTION f(a int[], b "MyType", OUT result text) '
        "RETURNS text LANGUAGE sql AS $$SELECT 1$$;"
    )
    ref = refs(file, "define")[0]
    assert ref.schema_name == "public"
    assert ref.signature_hint == 'f(int4[],"MyType")'


def test_quoted_identifier_raw_names_preserve_spaces() -> None:
    file = parse_sql(
        'CREATE TABLE "my table" (id int); '
        'CREATE FUNCTION "f name"() RETURNS int LANGUAGE SQL AS $$SELECT 1$$;'
    )
    definitions = refs(file, "define")
    assert [ref.raw_name for ref in definitions] == ['"my table"', '"f name"']
    assert definitions[0].end_column == definitions[0].start_column + len('"my table"')


def test_function_body_anchor_ignores_body_text_in_quoted_function_name() -> None:
    file = parse_sql(
        'CREATE FUNCTION "SELECT id FROM public.t"() RETURNS int '
        "LANGUAGE sql AS $$SELECT id FROM public.t$$;"
    )
    body_reads = refs(file, "read")
    assert [(ref.raw_name, ref.start_column) for ref in body_reads] == [("public.t", 90)]


def test_function_body_anchor_skips_comment_after_as() -> None:
    file = parse_sql(
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS "
        "/* SELECT id FROM t */ $$SELECT id FROM t$$;"
    )
    body_reads = refs(file, "read")
    assert [(ref.raw_name, ref.start_column) for ref in body_reads] == [("t", 89)]


def test_plpgsql_query_anchor_skips_comment_text() -> None:
    file = parse_sql(
        "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$"
        "BEGIN /* DELETE FROM public.t */ DELETE FROM public.t; END$$;"
    )
    table_refs = [ref for ref in refs(file) if ref.object_name == "t"]
    assert [(ref.role, ref.raw_name, ref.start_column) for ref in table_refs] == [
        ("write", "public.t", 101),
        ("read", "public.t", 101),
    ]


def test_call_signature_is_unknown_without_safe_argument_types() -> None:
    file = parse_sql("SELECT f(1), f(1::integer), f();")
    calls = refs(file, "call")
    assert [ref.signature_hint for ref in calls] == [None, "f(int4)", "f()"]


def test_dynamic_and_unsupported_constructs_are_explicit() -> None:
    file = parse_sql(
        "CREATE FUNCTION app.run() RETURNS void LANGUAGE plpgsql AS $$"
        "BEGIN EXECUTE 'SELECT * FROM ' || name; END; $$;"
    )
    assert file.parse_status == "partial"
    assert any(d.category == "sql_dynamic_reference" for d in file.diagnostics)
    assert any(ref.dynamic and ref.object_name is None for ref in file.sql_object_refs)


def test_partial_parse_keeps_successful_statements_and_reports_error() -> None:
    file = parse_sql("CREATE TABLE ok (id int); SELECT FROM; CREATE TABLE never (id int);")
    assert file.parse_status == "partial"
    assert [statement.kind for statement in file.sql_statements] == ["create_table", "create_table"]
    assert any(d.category == "sql_parse_error" for d in file.diagnostics)


def test_unicode_locations_are_utf8_byte_columns() -> None:
    file = parse_sql("-- café\nSELECT * FROM public.таблица;")
    ref = refs(file, "read")[0]
    assert ref.start_line == 2
    assert ref.start_column == len(b"SELECT * FROM ") + 1
    assert ref.raw_name == "public.таблица"
    assert ref.end_column == ref.start_column + len(ref.raw_name.encode("utf-8"))


def test_plpgsql_expression_prefix_preserves_unicode_identifier_locations() -> None:
    source = (
        "CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "  RETURN foo('λ') || bar();\n"
        "END $$;\n"
        "CREATE FUNCTION g() RETURNS void LANGUAGE plpgsql AS $$\n"
        "DECLARE result text;\n"
        "BEGIN\n"
        "  result := early('λ') || late();\n"
        "END $$;"
    )
    file = parse_sql(source)
    calls = [
        ref for ref in refs(file, "call") if ref.object_name in {"foo", "bar", "early", "late"}
    ]
    assert [(ref.raw_name, ref.start_line) for ref in calls] == [
        ("foo", 3),
        ("bar", 3),
        ("early", 8),
        ("late", 8),
    ]
    lines = source.splitlines()
    for ref in calls:
        line = lines[ref.start_line - 1].encode("utf-8")
        start = ref.start_column - 1
        end = ref.end_column - 1
        assert line[start:end].decode("utf-8") == ref.raw_name

    normal = refs(parse_sql("SELECT normal('λ')"), "call")[0]
    assert normal.start_column == len(b"SELECT ") + 1
    assert normal.end_column == normal.start_column + len(b"normal")


def test_invalid_source_bytes_are_not_replaced() -> None:
    file = parse_sql(b"SELECT \xff;")
    assert file.parse_status == "error"
    assert file.sql_artifacts[0].text == ""
    assert file.diagnostics[0].category == "source_decode_error"


@pytest.mark.parametrize("type_name", ["x,y", "x.y", "x[]", "X", 'x"y'])
def test_signature_type_components_cannot_collide(type_name: str) -> None:
    quoted = '"' + type_name.replace('"', '""') + '"'
    file = parse_sql(
        f"CREATE FUNCTION f(a {quoted}) RETURNS int LANGUAGE SQL AS $$SELECT 1$$;"
        "CREATE FUNCTION f(a x, b y) RETURNS int LANGUAGE SQL AS $$SELECT 1$$;"
        "CREATE FUNCTION f(a x[]) RETURNS int LANGUAGE SQL AS $$SELECT 1$$;"
    )
    signatures = [r.signature_hint for r in refs(file, "define")]
    assert signatures == [f"f({quoted})", "f(x,y)", "f(x[])"]
    assert len(set(signatures)) == 3


@pytest.mark.parametrize(
    ("setting", "path", "schema"),
    [
        ("''", (), None),
        ("'app', public", ("app", "public"), "app"),
        ('"$user", public', (), None),
        ("DEFAULT", (), None),
    ],
)
def test_creation_uses_only_known_search_path(setting, path, schema) -> None:
    file = parse_sql(f"SET search_path TO {setting}; CREATE TABLE foo(id int);")
    definition = refs(file, "define")[0]
    assert definition.schema_name == schema
    assert definition.search_path == path


@pytest.mark.parametrize(
    "setting, expected",
    [
        ("secret,public", ("secret", "public")),
        ("''", ()),
        ('"$user",public', ()),
    ],
)
def test_routine_path_is_local_and_body_name_stops_at_delimiter(setting, expected) -> None:
    file = parse_sql(
        "SET search_path TO outer_schema,public;"
        f"CREATE FUNCTION public.f() RETURNS int LANGUAGE SQL SET search_path={setting} "
        "AS $$SELECT id FROM t$$; SELECT * FROM t;"
    )
    body, outer = refs(file, "read")
    assert body.raw_name == outer.raw_name == "t"
    assert body.schema_name is outer.schema_name is None
    assert body.search_path == expected
    assert outer.search_path == ("outer_schema", "public")
    assert refs(file, "define")[0].schema_name == "public"


@pytest.mark.parametrize(
    ("query", "roles"),
    [
        ("DELETE FROM public.foo RETURNING *", ["write", "read"]),
        ("UPDATE public.foo SET id=1 RETURNING *", ["write", "read"]),
        ("INSERT INTO public.foo VALUES (1) RETURNING *", ["write"]),
    ],
)
def test_data_modifying_ctes_keep_target_roles(query, roles) -> None:
    file = parse_sql(f"WITH a AS ({query}) SELECT * FROM a;")
    assert file.parse_status == "ok"
    assert [r.role for r in refs(file)] == roles
    assert all(r.raw_name == "public.foo" for r in refs(file))


def test_begin_atomic_is_explicitly_incomplete() -> None:
    file = parse_sql("CREATE FUNCTION f() RETURNS int LANGUAGE SQL BEGIN ATOMIC SELECT 1; END;")
    assert file.parse_status != "ok"
    assert file.diagnostics
    assert not refs(file, "define")
