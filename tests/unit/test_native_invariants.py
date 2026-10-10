from __future__ import annotations

from codekg.native_evidence import parse_markdown_evidence, parse_python_sql
from codekg.native_parser import parse_native_source
from codekg.native_sql import parse_pg_proc_catalog, parse_routine_source
from codekg.source_locations import ByteLocations, Utf8Offsets
from codekg.sql_config import SqlConfig
from codekg.sql_parser import _protected_query_spans, _split_statements


def test_sparse_utf8_offsets_and_byte_locations_match_reference():
    source = "aé\n🦉z\n" * 100
    offsets = Utf8Offsets(source)
    raw = source.encode()
    locations = ByteLocations(raw)
    for i in range(len(source) + 1):
        byte = len(source[:i].encode())
        assert offsets.byte_offset(i) == byte
        prefix = raw[:byte]
        assert locations.position(byte) == (
            prefix.count(b"\n") + 1,
            len(prefix.rsplit(b"\n", 1)[-1]),
        )
    ascii_offsets = Utf8Offsets("x" * 100_000)
    assert len(ascii_offsets.positions) == 0


def test_sql_split_positions_do_not_rescan_prefix_per_boundary(monkeypatch):
    import codekg.sql_parser as module

    monkeypatch.setattr(
        module, "_char_offset", lambda *_: (_ for _ in ()).throw(AssertionError("prefix scan"))
    )
    source = "SELECT 'é';\n" * 2000
    assert len(_split_statements(source, source.encode())) == 2000
    assert not hasattr(_protected_query_spans, "cache_info")


def test_body_hash_includes_operators_but_ignores_format_and_comments():
    def body(text):
        return parse_native_source(text, "x.c").symbols[0].body_hash

    assert body(b"int f(int x) {return x+1;}") != body(b"int f(int x) {return x-1;}")
    assert body(b"int f(int x) {return x+1;}") == body(b"int f(int x) { /* note */ return x + 1; }")


def test_function_pointer_variables_are_not_native_functions():
    facts = parse_native_source(
        b"int (*callback)(int); int *actual(int); int (*factory(void))(int);", "x.h"
    )
    assert [s.name for s in facts.symbols] == ["actual", "factory"]


def test_directives_in_comments_do_not_end_real_conditional_scope():
    facts = parse_native_source(
        b"#ifdef OPTION\n/*\n#endif\n*/\nint f(void) { return target(); }\n#endif\n", "x.c"
    )
    assert facts.calls[0].condition == "ifdef OPTION"


def test_macros_remain_distinguishable_from_c_functions():
    facts = parse_native_source(b"#define ADD(x) ((x)+1)\nint f(void){return ADD(1);}", "x.c")
    assert [(s.name, s.kind) for s in facts.symbols] == [("ADD", "macro"), ("f", "function")]


def test_plain_args_do_not_count_as_default_args_and_other_pl_body_is_retained():
    facts = parse_routine_source(
        b"CREATE FUNCTION f(a int,b text DEFAULT '') RETURNS int LANGUAGE c AS 'lib';"
        b"CREATE FUNCTION py(a int) RETURNS int LANGUAGE plpython3u AS $$return a$$;",
        "x.sql",
        SqlConfig(),
    )
    assert facts.routines[0].default_arg_count == 1
    assert facts.routines[1].body_hash


def test_catalog_sql_language_does_not_invent_a_native_entrypoint():
    facts = parse_pg_proc_catalog(
        b"{proname => 'wrapper', prolang => 'sql', prosrc => 'SELECT 1', proargtypes => ''},",
        "pg_proc.dat",
    )
    assert facts.routines[0].language == "sql"
    assert facts.routines[0].entrypoint is None


def test_postgis_cost_template_attributes_do_not_hide_explicit_c_bindings():
    raw = (
        b"CREATE FUNCTION ST_Buffer(geometry,float8) RETURNS geometry "
        b"AS 'MODULE_PATHNAME','buffer' LANGUAGE c IMMUTABLE STRICT _COST_HIGH;"
    )
    facts = parse_routine_source(raw, "postgis.sql.in", SqlConfig())
    assert facts.routines[0].entrypoint == "buffer"
    assert any(d.category == "sql_template_attribute" for d in facts.diagnostics)


def test_routine_return_and_binding_changes_are_retained_for_comparison():
    def extract(return_type, entrypoint):
        return parse_routine_source(
            f"CREATE FUNCTION f(integer) RETURNS {return_type} LANGUAGE C "
            f"AS 'MODULE_PATHNAME', '{entrypoint}';".encode(),
            "extension.sql",
            SqlConfig(),
        ).routines[0]

    old = extract("integer", "impl")
    new_return = extract("bigint", "impl")
    new_binding = extract("integer", "new_impl")
    assert old.signature == new_return.signature == new_binding.signature
    assert old.return_type == "int4" and new_return.return_type == "int8"
    assert len({old.definition_hash, new_return.definition_hash, new_binding.definition_hash}) == 3


def test_macro_names_do_not_change_real_function_linkage():
    facts = parse_native_source(b"#define helper() 1\nint helper(void) {return 2;}", "x.c")
    function = next(symbol for symbol in facts.symbols if symbol.kind == "function")
    assert not function.static


def test_invalid_catalog_default_count_is_diagnostic_not_build_failure():
    facts = parse_pg_proc_catalog(
        b"[{ proname => 'f', prorettype => 'int4', proretset => 't', "
        b"pronargdefaults => 'bad', prosrc => 'f' }]",
        "pg_proc.dat",
    )
    assert facts.routines[0].return_type == "setof int4"
    assert facts.routines[0].body_hash is None
    assert any(item.category == "catalog_record_unsupported" for item in facts.diagnostics)


def test_standalone_sql_calls_and_python_coding_cookie():
    from codekg.native_evidence import parse_sql_source_evidence

    facts = parse_sql_source_evidence(b"SELECT cron.schedule(1,'x');", "calls.sql")
    assert [(item.origin, item.object_name, item.arity) for item in facts.evidence] == [
        ("sql_source", "schedule", 2),
    ]
    python = parse_python_sql(
        '# coding: latin-1\ndb.execute(\'SELECT "café"."f"(1)\')'.encode("latin-1"),
        "app.py",
    )
    assert python.evidence[0].schema_name == "café"


def test_other_procedural_languages_retain_bodies_without_fabricated_calls():
    for language in ("pltcl", "plperl", "plpython3u", "custom_language"):
        facts = parse_routine_source(
            f"CREATE FUNCTION f() RETURNS integer LANGUAGE {language} "
            "AS $$SELECT cron.schedule(1,'x');$$;".encode(),
            "language.sql",
            SqlConfig(),
        )
        routine = facts.routines[0]
        assert routine.language == language and routine.body_hash
        assert routine.coverage == "declaration_only"
        assert not facts.evidence
        assert any(d.category == "unsupported_routine_language" for d in facts.diagnostics)


def test_sql_reference_names_do_not_decode_source_suffix_per_reference():
    from codekg.sql_parser import parse_sql

    class CountedBytes(bytes):
        suffix_slices = 0

        def __getitem__(self, index):
            if isinstance(index, slice) and index.start and index.stop == len(self):
                self.suffix_slices += 1
            return super().__getitem__(index)

    raw = CountedBytes(("SELECT " + ",".join('"é".f(1)' for _ in range(1000)) + ";").encode())
    file = parse_sql(raw, context="many.sql", config=SqlConfig())
    assert len(file.sql_object_refs) == 1000
    assert {reference.raw_name for reference in file.sql_object_refs} == {'"é".f'}
    assert raw.suffix_slices < 5


def test_global_rebinding_prevents_assuming_module_sql_constant_is_static():
    facts = parse_python_sql(
        b'QUERY="SELECT cron.schedule(1)"\n'
        b"def change(value):\n global QUERY\n QUERY=value\n"
        b"def run(db):\n db.execute(QUERY)\n",
        "app.py",
    )
    assert facts.evidence[0].dynamic


def test_template_guards_are_conditional_but_hash_comments_in_pl_bodies_are_preserved():
    source = (
        b"#ifdef OPTIONAL_API\nCREATE FUNCTION f() RETURNS int LANGUAGE C "
        b"AS 'MODULE_PATHNAME','f';\n#endif\n"
    )
    routine = parse_routine_source(source, "extension.sql.in", SqlConfig()).routines[0]
    assert routine.condition == "unevaluated SQL template guard"
    body = b"#ifdef NOT_A_TEMPLATE\nreturn 1\n#endif"
    raw = b"CREATE FUNCTION g() RETURNS int LANGUAGE pltcl AS $$\n" + body + b"\n$$;"
    normal = parse_routine_source(raw, "extension.sql", SqlConfig()).routines[0]
    template = parse_routine_source(raw, "extension.sql.in", SqlConfig()).routines[0]
    assert normal.condition is None and template.condition is None
    assert normal.body_hash == template.body_hash
    wrapper = parse_routine_source(
        b"#ifdef OPTIONAL_API\nCREATE FUNCTION wrapper() RETURNS int LANGUAGE sql "
        b"AS $$ SELECT public.target(); $$;\n#endif\n",
        "extension.sql.in",
        SqlConfig(),
    )
    assert wrapper.evidence and all(e.condition for e in wrapper.evidence)


def test_python_constant_resolution_does_not_fabricate_rebound_or_shadowed_queries():
    facts = parse_python_sql(
        b"""QUERY = "SELECT cron.schedule('a','b')"
def shadow(db, QUERY):
    db.execute(QUERY)
def rebound(db, user_query):
    q = "SELECT cron.schedule('a','b')"
    q = user_query
    db.execute(q)
def guarded(db, flag):
    q = "SELECT cron.schedule('a','b')"
    if flag:
        q = "SELECT cron.unschedule(1)"
    db.execute(q)
def static(db):
    db.execute(QUERY)
""",
        "app.py",
    )
    assert [e.dynamic for e in facts.evidence] == [True, True, True, False]
    assert facts.evidence[-1].object_name == "schedule"


def test_deferred_python_bodies_do_not_capture_temporary_sql_constants():
    facts = parse_python_sql(
        b"SQL = 'SELECT cron.before()'\n"
        b"f = lambda db: db.execute(SQL)\n"
        b"SQL = 'SELECT cron.after()'\n",
        "app.py",
    )
    assert len(facts.evidence) == 1 and facts.evidence[0].dynamic

    facts = parse_python_sql(
        b"SQL = 'SELECT cron.module_query()'\n"
        b"f = lambda SQL: db.execute(SQL)\n"
        b"g = lambda db: ((SQL := 'SELECT cron.before()'), "
        b"lambda: db.execute(SQL), (SQL := 'SELECT cron.after()'))\n",
        "app.py",
    )
    assert [item.dynamic for item in facts.evidence] == [True, True]

    facts = parse_python_sql(
        b"SQL = 'SELECT cron.before()'\n"
        b"g = (db.execute(SQL) for row in rows)\n"
        b"SQL = 'SELECT cron.after()'\n",
        "app.py",
    )
    assert len(facts.evidence) == 1 and facts.evidence[0].dynamic


def test_nested_lambda_respects_comprehension_bindings_but_stable_globals_remain_static():
    facts = parse_python_sql(
        b"SQL = 'SELECT cron.stable_query()'\n"
        b"callbacks = [lambda db: db.execute(SQL) for SQL in queries]\n"
        b"stable = lambda db: db.execute(SQL)\n",
        "app.py",
    )
    assert [item.dynamic for item in facts.evidence] == [True, False]
    assert facts.evidence[1].object_name == "stable_query"


def test_comprehension_clause_lambdas_respect_targets_in_filters_and_later_iterables():
    facts = parse_python_sql(
        b"SQL = 'SELECT cron.module_query()'\n"
        b"items = [row for SQL in queries if (lambda: db.execute(SQL))()]\n"
        b"later = [row for SQL in queries for row in (lambda: db.execute(SQL))()]\n"
        b"stable = lambda db: db.execute(SQL)\n",
        "app.py",
    )
    assert [item.dynamic for item in facts.evidence] == [True, True, False]
    assert facts.evidence[-1].object_name == "module_query"


def test_lambda_defaults_are_analyzed_in_the_immediate_enclosing_scope():
    facts = parse_python_sql(
        b"SQL = 'SELECT cron.default_query()'\nf = lambda query=db.execute(SQL): query\n",
        "app.py",
    )
    assert len(facts.evidence) == 1
    assert not facts.evidence[0].dynamic
    assert facts.evidence[0].object_name == "default_query"


def test_markdown_sql_positions_use_utf8_byte_offsets_and_sql_case_rules():
    raw = 'é `CRON.Schedule(a,b)`\n```sql\nSELECT \'🦉\', "Cron"."é"(1);\n```\n'.encode()
    facts = parse_markdown_evidence(raw, "book.md")
    fence, mention = facts.evidence
    assert (fence.schema_name, fence.object_name) == ("Cron", "é")
    assert fence.start_line == 3
    assert fence.start_column == len("SELECT '🦉', ".encode())
    assert (mention.schema_name, mention.object_name) == ("cron", "schedule")
    assert mention.start_column == len("é ".encode())


def test_many_sql_references_do_not_duplicate_large_body_in_staged_facts():
    import json
    from dataclasses import asdict

    snippet = "SELECT " + ",".join("cron.schedule('a','b')" for _ in range(1000)) + ";"
    facts = parse_markdown_evidence(f"```sql\n{snippet}\n```\n".encode(), "book.md")
    assert len(facts.evidence) == 1000
    assert all(e.text is None and e.text_hash for e in facts.evidence)
    assert len(json.dumps([asdict(e) for e in facts.evidence])) < len(snippet) * 50


def test_typedef_callback_shadowing_global_function_remains_dynamic():
    facts = parse_native_source(
        b"typedef int (*Callback)(int); "
        b"int victim(int x) { return x; } "
        b"int caller(Callback victim) { return victim(1); } "
        b"int direct(void) { return victim(2); }",
        "callback.c",
    )
    callback, direct = facts.calls
    assert callback.dynamic and callback.callee_name is None
    assert not direct.dynamic and direct.callee_name == "victim"


def test_local_callback_does_not_poison_direct_calls_outside_its_block():
    facts = parse_native_source(
        b"typedef int (*Callback)(int); int victim(int x) { return x; } "
        b"int caller(Callback cb) { { Callback victim=cb; victim(1); } return victim(2); }",
        "callback.c",
    )
    assert facts.calls[0].dynamic
    assert facts.calls[1].callee_name == "victim" and not facts.calls[1].dynamic


def test_explicit_pointer_local_does_not_poison_another_function():
    facts = parse_native_source(
        b"int victim(int x) { return x; } "
        b"int indirect(void) { int (*victim)(int); return victim(1); } "
        b"int direct(void) { return victim(2); }",
        "callback.c",
    )
    assert facts.calls[0].dynamic
    assert facts.calls[1].callee_name == "victim" and not facts.calls[1].dynamic


def test_routine_body_preserves_zero_and_nested_argument_counts():
    facts = parse_routine_source(
        b"CREATE FUNCTION wrapper() RETURNS int LANGUAGE sql "
        b"AS $$ SELECT target(inner_call(1), 2) + zero_call(); $$;",
        "body.sql",
        SqlConfig(),
    )
    assert {e.object_name: e.arity for e in facts.evidence} == {
        "target": 2,
        "inner_call": 1,
        "zero_call": 0,
    }


def test_unknown_execute_receiver_is_not_verified_db_api():
    facts = parse_python_sql(
        b"class Logger:\n def execute(self, value): print(value)\n"
        b"def entry():\n Logger().execute('SELECT target()')\n",
        "logger.py",
    )
    assert len(facts.evidence) == 1  # Keep useful heuristic evidence, not a proven DB call.
    assert facts.evidence[0].receiver_status == "unverified"


def test_imported_db_api_cursor_chain_has_verified_receiver():
    facts = parse_python_sql(
        b"import sqlite3\nsqlite3.connect(':memory:').cursor().execute('SELECT target()')\n",
        "client.py",
    )
    assert facts.evidence[0].receiver_status == "verified"


def test_verified_driver_bindings_and_context_managers():
    facts = parse_python_sql(
        b"from sqlite3 import connect as open_db\n"
        b"def run():\n"
        b" connection=open_db(':memory:')\n"
        b" with connection.cursor() as cursor:\n"
        b"  cursor.executemany('SELECT target(1)', [])\n",
        "client.py",
    )
    assert facts.evidence[0].receiver_status == "verified"


def test_shadowed_or_rebound_driver_receivers_remain_unverified():
    sources = [
        b"import sqlite3\ndef run(sqlite3):\n sqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\nsqlite3=other\nsqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\nsqlite3.connect=other\n"
        b"sqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\ndef outer(sqlite3):\n def inner():\n"
        b"  sqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\ndef run():\n c=sqlite3.connect('x').cursor()\n c=other\n"
        b" c.execute('SELECT f()')",
        b"import sqlite3\ndef run(flag):\n if flag:\n  c=sqlite3.connect('x').cursor()\n"
        b" c.execute('SELECT f()')",
    ]
    for source in sources:
        facts = parse_python_sql(source, "client.py")
        assert facts.evidence[0].receiver_status == "unverified", source


def test_plpgsql_procedure_calls_preserve_argument_counts():
    facts = parse_routine_source(
        b"CREATE PROCEDURE wrapper() LANGUAGE plpgsql AS $$ BEGIN "
        b"CALL target(1, 'a,b'); PERFORM nested(2); END; $$;",
        "procedure.sql",
        SqlConfig(),
    )
    assert {e.object_name: e.arity for e in facts.evidence} == {"target": 2, "nested": 1}


def test_for_initializer_callback_scope_and_comma_declarations():
    facts = parse_native_source(
        b"typedef int (*Callback)(int); int victim(int x) { return x; } "
        b"void f(Callback cb) { for(Callback victim=cb, second=cb; 0;) victim(1); victim(2); }",
        "callback.c",
    )
    assert facts.calls[0].dynamic
    assert facts.calls[1].callee_name == "victim" and not facts.calls[1].dynamic


def test_implicit_scope_and_reflective_driver_mutation_are_unverified():
    for source in [
        b"import sqlite3\nf=lambda sqlite3: sqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\n"
        b"[sqlite3.connect('x').cursor().execute('SELECT f()') for sqlite3 in others]",
        b"import sqlite3\nsetattr(sqlite3, 'connect', other)\n"
        b"sqlite3.connect('x').cursor().execute('SELECT f()')",
    ]:
        assert parse_python_sql(source, "client.py").evidence[0].receiver_status == "unverified"


def test_custom_or_unpacked_factories_cannot_verify_receiver_identity():
    for source in [
        b"import sqlite3\nsqlite3.connect('x', factory=Logger).execute('SELECT f()')",
        b"import psycopg2\n"
        b"psycopg2.connect('x').cursor(cursor_factory=Logger).execute('SELECT f()')",
        b"import sqlite3\nsqlite3.connect('x', **options).execute('SELECT f()')",
    ]:
        assert parse_python_sql(source, "client.py").evidence[0].receiver_status == "unverified"


def test_prototype_parameters_do_not_shadow_unrelated_direct_calls():
    facts = parse_native_source(
        b"typedef int (*Callback)(int); int victim(int); "
        b"void register_callback(Callback victim); "
        b"int direct(void) { return victim(2); }",
        "prototype.c",
    )
    assert facts.calls[0].callee_name == "victim" and not facts.calls[0].dynamic


def test_mutated_aliases_cannot_verify_original_driver_or_connection():
    for source in [
        b"import sqlite3\nother=sqlite3\nother.connect=custom\n"
        b"sqlite3.connect('x').cursor().execute('SELECT f()')",
        b"import sqlite3\nc=sqlite3.connect('x')\nother=c\nother.cursor=custom\n"
        b"c.cursor().execute('SELECT f()')",
        b"import sqlite3 as db\nimport sqlite3 as other\nother.connect=custom\n"
        b"db.connect('x').cursor().execute('SELECT f()')",
    ]:
        assert parse_python_sql(source, "client.py").evidence[0].receiver_status == "unverified"


def test_python_embedded_call_keeps_procedure_and_nested_function_kinds():
    facts = parse_python_sql(
        b"import psycopg\npsycopg.connect('x').execute('CALL proc(nested(1))')",
        "client.py",
    )
    assert [
        (e.object_name, e.arity, e.routine_kind, e.receiver_status) for e in facts.evidence
    ] == [
        ("proc", 1, "procedure", "verified"),
        ("nested", 1, "function", "verified"),
    ]


def test_callback_parameter_shadowing_survives_function_returning_callable():
    facts = parse_native_source(
        b"typedef int (*Callback)(int); int victim(int); "
        b"int (*caller(Callback victim))(int) { victim(1); return 0; }",
        "returns_callback.c",
    )
    assert len(facts.calls) == 1
    assert facts.calls[0].callee_name is None and facts.calls[0].dynamic


def test_receiver_mutation_through_control_flow_aliases_invalidates_source():
    for mutation in [
        b"for alias in [c]:\n    alias.cursor=custom\n",
        b"with c as alias:\n    alias.cursor=custom\n",
        b"[setattr(alias, 'cursor', custom) for alias in [c]]\n",
        b"(alias := c)\nalias.cursor=custom\n",
        b"bag: list = [c]\nfor alias in bag:\n    alias.cursor=custom\n",
        b"(bag := [c])\nfor alias in bag:\n    alias.cursor=custom\n",
        b"bag=[c]\nbag[0].cursor=custom\n",
        b"bag=[c]\nsetattr(bag[0], 'cursor', custom)\n",
    ]:
        facts = parse_python_sql(
            b"import psycopg\nc=psycopg.connect('db')\n"
            + mutation
            + b"c.cursor().execute('SELECT target()')",
            "client.py",
        )
        assert facts.evidence[0].receiver_status == "unverified"


def test_pg_proc_procedure_kind_and_out_count_are_preserved():
    facts = parse_pg_proc_catalog(
        b"{ proname => 'p', prokind => 'p', proargtypes => 'int4', "
        b"proargmodes => '{i,o}', prosrc => 'p_native' }",
        "pg_proc.dat",
    )
    assert [(r.kind, r.arity, r.out_arg_count) for r in facts.routines] == [("procedure", 1, 1)]


def test_large_receiver_alias_group_has_bounded_extraction_memory():
    import tracemalloc

    # A generated source can legitimately mention thousands of names in one
    # container. Alias taint must preserve reachability without an O(names²) clique.
    names = ",".join(f"v{i}" for i in range(2000))
    source = (
        "import psycopg\nc=psycopg.connect('db')\n"
        f"bag=[c,{names}]\nfor alias in bag:\n    alias.cursor=custom\n"
        "c.cursor().execute('SELECT target()')"
    ).encode()
    tracemalloc.start()
    try:
        facts = parse_python_sql(source, "generated.py")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert facts.evidence[0].receiver_status == "unverified"
    assert peak < 32 * 1024 * 1024
