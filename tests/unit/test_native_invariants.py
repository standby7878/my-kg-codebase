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
