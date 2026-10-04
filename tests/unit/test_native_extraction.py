from __future__ import annotations

from codekg.native_evidence import parse_markdown_evidence, parse_python_sql
from codekg.native_parser import parse_native_source
from codekg.native_sql import parse_pg_proc_catalog, parse_routine_source
from codekg.sql_config import SqlConfig


def test_c_symbols_calls_conditions_utf8_and_ignored_text() -> None:
    source = """
/* π */
static int duplicate(int x);
int duplicate(int x) { // definition
    const char *s = "not_a_call()";
    /* also_not_a_call(); */
    return actual(x);
}
int (*callback)(int);
#ifdef USE_OPTION
void conditional(void) { callback(1); }
#endif
""".encode()
    facts = parse_native_source(source, "x.c")
    duplicate = [s for s in facts.symbols if s.name == "duplicate"]
    assert len(duplicate) == 2
    assert duplicate[0].declaration and not duplicate[1].declaration
    assert duplicate[0].static and duplicate[1].static
    assert duplicate[0].signature == duplicate[1].signature
    assert duplicate[1].body_hash
    assert [c.callee_name for c in facts.calls] == ["actual", None]
    assert facts.calls[1].dynamic
    assert facts.calls[1].condition == "ifdef USE_OPTION"
    assert facts.calls[0].owner_name == "duplicate"
    assert facts.calls[0].start_column > 0
    assert not any("not_a_call" in d.message for d in facts.diagnostics)


def test_c_static_symbol_and_external_declaration_are_distinct() -> None:
    facts = parse_native_source(
        b"extern int shared(int); static int hidden(void) { return shared(1); }", "a.c"
    )
    assert [(s.name, s.static, s.declaration) for s in facts.symbols] == [
        ("shared", False, True),
        ("hidden", True, False),
    ]
    assert facts.calls[0].callee_name == "shared"


def test_routine_bindings_default_entrypoint_defaults_variadic_and_other_pl() -> None:
    raw = b"""CREATE FUNCTION cron.schedule(a int DEFAULT 1, VARIADIC rest text[])
RETURNS int LANGUAGE C AS 'MODULE_PATHNAME', $$schedule_impl$$;
CREATE FUNCTION app.other(x int) RETURNS int LANGUAGE plpython3u AS $$return x$$;
"""
    facts = parse_routine_source(raw, "extension.sql", SqlConfig())
    routine, other = facts.routines
    assert (routine.schema_name, routine.name, routine.signature) == (
        "cron",
        "schedule",
        "schedule(int4,text[])",
    )
    assert (
        routine.library,
        routine.entrypoint,
        routine.default_arg_count,
        routine.variadic_arg_count,
    ) == ("MODULE_PATHNAME", "schedule_impl", 1, 1)
    assert other.language == "plpython3u"
    assert any(d.category == "unsupported_routine_language" for d in facts.diagnostics)


def test_routine_omitted_c_entrypoint_defaults_to_name_and_masks_templates() -> None:
    facts = parse_routine_source(
        b"#include 'config.h'\nCREATE FUNCTION f() RETURNS int LANGUAGE C AS 'lib';\n",
        "x.sql.in",
        SqlConfig(),
    )
    assert facts.routines[0].entrypoint == "f"
    assert facts.diagnostics[0].category == "sql_template_directive"


def test_routine_parser_recovers_after_an_invalid_statement() -> None:
    facts = parse_routine_source(
        b"CREATE FUNCTION broken() RETURNS int LANGUAGE C AS ;\n"
        b"CREATE FUNCTION good() RETURNS int LANGUAGE C AS 'lib';\n",
        "x.sql.in",
        SqlConfig(),
    )
    assert [item.name for item in facts.routines] == ["good"]
    assert any(item.category == "routine_sql_parse_error" for item in facts.diagnostics)


def test_sql_and_plpgsql_routine_bodies_reuse_static_sql_extraction() -> None:
    source = (
        b"CREATE FUNCTION app.sql_wrap(x int) RETURNS int LANGUAGE sql "
        b"AS $$ SELECT app.target(x); $$;\n"
        b"CREATE FUNCTION app.pl_wrap(x int) RETURNS int LANGUAGE plpgsql "
        b"AS $$ BEGIN PERFORM app.other_target(x); RETURN x; END $$;\n"
    )
    facts = parse_routine_source(source, "routines.sql", SqlConfig())
    assert {(item.object_name, item.owner_qname) for item in facts.evidence} == {
        ("target", "app.sql_wrap"),
        ("other_target", "app.pl_wrap"),
    }
    lines = source.splitlines()
    for item in facts.evidence:
        assert lines[item.start_line - 1][item.start_column : item.end_column].decode() == item.text


def test_mixed_case_guarded_perform_keeps_utf8_literal_and_ignores_decoys() -> None:
    source = (
        "CREATE FUNCTION app.wrapper() RETURNS text LANGUAGE plpgsql AS $body$\n"
        "BEGIN\n"
        "  -- PERFORM app.comment_decoy();\n"
        "  IF true THEN\n"
        "    pErFoRm app.target('π literal with SELECT app.string_decoy()');\n"
        "  END IF;\n"
        "  RETURN 'π';\n"
        "END\n"
        "$body$;\n"
    ).encode()
    facts = parse_routine_source(source, "routines.sql", SqlConfig())
    assert [(item.object_name, item.condition) for item in facts.evidence] == [
        ("target", "app.wrapper:conditional")
    ]
    item = facts.evidence[0]
    assert item.text == "app.target"
    line = source.splitlines()[item.start_line - 1]
    assert line[item.start_column : item.end_column].decode() == item.text


def test_plpgsql_return_and_assignment_calls_keep_exact_utf8_spans_and_guards() -> None:
    source = (
        "-- π before routine offsets\n"
        "CREATE FUNCTION app.from_return(x int) RETURNS int LANGUAGE plpgsql AS $body$\n"
        "BEGIN\n"
        "  RETURN cron.schedule(x);\n"
        "END\n"
        "$body$;\n"
        "CREATE FUNCTION app.from_assignment(x int) RETURNS int LANGUAGE plpgsql AS $body$\n"
        "DECLARE y int;\n"
        "BEGIN\n"
        "  IF x > 0 THEN\n"
        "    y := cron.schedule(x);\n"
        "  END IF;\n"
        "  RETURN y;\n"
        "END\n"
        "$body$;\n"
    ).encode()
    facts = parse_routine_source(source, "routines.sql", SqlConfig())
    assert [(item.owner_qname, item.object_name, item.condition) for item in facts.evidence] == [
        ("app.from_return", "schedule", None),
        ("app.from_assignment", "schedule", "app.from_assignment:conditional"),
    ]
    for item in facts.evidence:
        line = source.splitlines()[item.start_line - 1]
        assert line[item.start_column : item.end_column].decode() == item.text == "cron.schedule"
    guarded = facts.routines[1]
    assert guarded.coverage == "body_partial"
    assert any(
        item.category == "sql_unsupported_construct" and item.line == 7
        for item in facts.diagnostics
    )


def test_native_includes_are_positioned_guarded_occurrences() -> None:
    source = (
        "/* π */\n"
        "#ifdef USE_LOCAL\n"
        '#include "config.h"\n'
        "#endif\n"
        "#include <sys/types.h>\n"
        "#include HEADER_NAME\n"
    ).encode()
    facts = parse_native_source(source, "x.c")
    assert [(item.object_name, item.text, item.condition) for item in facts.evidence] == [
        ("config.h", '"config.h"', "ifdef USE_LOCAL"),
        ("sys/types.h", "<sys/types.h>", None),
        ("HEADER_NAME", "HEADER_NAME", None),
    ]
    assert len({(item.start_line, item.start_column) for item in facts.evidence}) == 3
    for item in facts.evidence:
        line = source.splitlines()[item.start_line - 1]
        assert line[item.start_column : item.end_column].decode() == item.text


def test_plpgsql_default_expression_is_diagnosed_without_misattributing_later_call() -> None:
    source = (
        b"CREATE FUNCTION app.defaulted(x int) RETURNS int LANGUAGE plpgsql AS $body$\n"
        b"DECLARE y int := cron.schedule(x);\n"
        b"BEGIN\n"
        b"  RETURN cron.schedule(x);\n"
        b"END\n"
        b"$body$;\n"
    )
    facts = parse_routine_source(source, "routines.sql", SqlConfig())
    assert len(facts.evidence) == 1
    item = facts.evidence[0]
    assert item.start_line == 4
    line = source.splitlines()[item.start_line - 1]
    assert line[item.start_column : item.end_column].decode() == "cron.schedule"
    assert facts.routines[0].coverage == "body_partial"
    assert any(
        item.category == "sql_unsupported_construct" and "default expressions" in item.message
        for item in facts.diagnostics
    )


def test_catalog_parses_escaped_records_without_evaluation() -> None:
    raw = b"""[
{ proname => 'quote_test', proargtypes => 'text', prosrc => 'say\\'hi', descr => 'x' },
{ proname => 'safe_only', prosrc => 'dangerous()' },
]"""
    facts = parse_pg_proc_catalog(raw, "pg_proc.dat")
    assert [(item.name, item.entrypoint, item.arity) for item in facts.routines] == [
        ("quote_test", "say'hi", 1),
        ("safe_only", "dangerous()", 0),
    ]
    assert not facts.diagnostics


def test_python_sql_literal_and_constant_but_dynamic_is_not_fabricated() -> None:
    source = b"""QUERY = "select cron.schedule(1, 'hi')"
def run(db, value):
    db.execute(QUERY)
    db.executemany(f"select cron.unschedule({value})", [])
"""
    facts = parse_python_sql(source, "app.py")
    static, dynamic = facts.evidence
    assert (static.origin, static.schema_name, static.object_name, static.arity) == (
        "python_execute",
        "cron",
        "schedule",
        2,
    )
    assert static.owner_qname == "run" and static.owner_line == 2
    assert dynamic.dynamic and dynamic.object_name is None
    source_lines = source.splitlines()
    assert source_lines[static.start_line - 1][static.start_column :].startswith(b"db.execute")
    # Bodies are not duplicated into each occurrence's serialized fact.
    assert static.text is None and static.text_hash
    assert static.text_hash


def test_python_imports_and_scope_binding_forms_shadow_module_sql_constants() -> None:
    source = b"""QUERY = "SELECT cron.schedule(1)"
db.execute(QUERY)

def imported(db):
    import module as QUERY
    db.execute(QUERY)

def from_imported(db):
    from module import value as QUERY
    db.execute(QUERY)

def function_rebound(db):
    def QUERY():
        pass
    db.execute(QUERY)

def class_rebound(db):
    class QUERY:
        pass
    db.execute(QUERY)

def exception_bound(db):
    try:
        raise ValueError()
    except Exception as QUERY:
        pass
    db.execute(QUERY)

def match_bound(db, value):
    match value:
        case QUERY:
            pass
    db.execute(QUERY)

def deleted(db):
    del QUERY
    db.execute(QUERY)

def nested_scope(db):
    def inner():
        QUERY = "SELECT cron.inner_only()"
        return QUERY
    db.execute(QUERY)
"""
    facts = parse_python_sql(source, "app.py")
    assert len(facts.evidence) == 9
    nested = next(item for item in facts.evidence if item.owner_qname == "nested_scope")
    assert (nested.dynamic, nested.object_name) == (False, "schedule")
    module = next(item for item in facts.evidence if item.owner_qname is None)
    assert (module.dynamic, module.object_name) == (False, "schedule")
    assert all(
        item.dynamic and item.object_name is None
        for item in facts.evidence
        if item is not nested and item is not module
    )
    assert {item.owner_qname for item in facts.evidence} == {
        None,
        "imported",
        "from_imported",
        "function_rebound",
        "class_rebound",
        "exception_bound",
        "match_bound",
        "deleted",
        "nested_scope",
    }


def test_python_module_import_and_global_reassignment_invalidate_constants() -> None:
    module = parse_python_sql(
        b"""QUERY = "SELECT cron.schedule(1)"
import module as QUERY
db.execute(QUERY)
""",
        "module.py",
    )
    assert len(module.evidence) == 1 and module.evidence[0].dynamic

    global_rebinding = parse_python_sql(
        b"""QUERY = "SELECT cron.schedule(1)"
def change():
    global QUERY
    QUERY = get_query()
change()
db.execute(QUERY)
""",
        "global.py",
    )
    assert len(global_rebinding.evidence) == 1
    assert global_rebinding.evidence[0].dynamic


def test_python_nested_functions_do_not_treat_enclosing_bindings_as_globals() -> None:
    facts = parse_python_sql(
        b"""QUERY = "SELECT cron.global_query()"
def outer(SQL):
    def inner(db):
        db.execute(SQL)
    inner(None)
def assigned_outer():
    SQL = "SELECT cron.outer_query()"
    def inner(db):
        db.execute(SQL)
    inner(None)
def lambda_outer():
    SQL = "SELECT cron.lambda_query()"
    run = lambda db: db.execute(SQL)
    run(None)
def comprehension_outer():
    SQL = "SELECT cron.comprehension_query()"
    [db.execute(SQL) for db in databases]
def method_outer():
    SQL = "SELECT cron.method_query()"
    class Service:
        @classmethod
        def run(cls, db):
            db.execute(SQL)
def nonlocal_outer():
    SQL = "SELECT cron.nonlocal_query()"
    def inner(db):
        nonlocal SQL
        db.execute(SQL)
def global_outer():
    SQL = "SELECT cron.local_query()"
    def inner(db):
        global QUERY
        db.execute(QUERY)
def unshadowed_global(db):
    def inner(db):
        db.execute(QUERY)
""",
        "closures.py",
    )
    by_owner = {item.owner_qname: item for item in facts.evidence}
    assert by_owner["outer.inner"].dynamic
    assert by_owner["assigned_outer.inner"].dynamic
    assert by_owner["lambda_outer"].dynamic
    assert by_owner["comprehension_outer"].dynamic
    assert by_owner["method_outer.Service.run"].dynamic
    assert by_owner["nonlocal_outer.inner"].dynamic
    assert not by_owner["global_outer.inner"].dynamic
    assert by_owner["global_outer.inner"].object_name == "global_query"
    assert not by_owner["unshadowed_global.inner"].dynamic
    assert by_owner["unshadowed_global.inner"].object_name == "global_query"


def test_python_implicit_scope_targets_shadow_outer_constants_without_leaking() -> None:
    source = b"""QUERY = "SELECT cron.schedule(1)"
[db.execute(QUERY) for QUERY in sqls]
db.execute(QUERY)
f = lambda QUERY: (db.execute(QUERY), db.execute("SELECT cron.fixed()"))
db.execute(QUERY)
g = lambda *QUERY: db.execute(QUERY)
h = lambda **QUERY: db.execute(QUERY)
f_default = lambda QUERY=db.execute(QUERY): None
"""
    evidence = parse_python_sql(source, "implicit.py").evidence

    assert [(item.dynamic, item.object_name) for item in evidence] == [
        (True, None),
        (False, "schedule"),
        (True, None),
        (False, "fixed"),
        (False, "schedule"),
        (True, None),
        (True, None),
        (False, "schedule"),
    ]


def test_python_definition_defaults_are_evaluated_in_enclosing_scope() -> None:
    source = b"""QUERY = "SELECT cron.schedule(1)"
def module_default(QUERY=db.execute(QUERY)):
    pass
def outer():
    QUERY = "SELECT cron.outer_query()"
    def nested_default(QUERY=db.execute(QUERY)):
        pass
g = lambda QUERY=db.execute(QUERY): None
@db.execute(QUERY)
def decorated():
    pass
class Based(db.execute(QUERY)):
    pass
"""
    evidence = parse_python_sql(source, "defaults.py").evidence

    assert [(item.dynamic, item.object_name, item.owner_qname) for item in evidence] == [
        (False, "schedule", None),
        (False, "outer_query", "outer"),
        (False, "schedule", None),
        (False, "schedule", None),
        (False, "schedule", None),
    ]


def test_python_comprehension_scopes_cover_shapes_clauses_and_nested_scopes() -> None:
    source = b"""QUERY = "SELECT cron.schedule(1)"
[db.execute(QUERY) for QUERY in values]
{db.execute(QUERY) for QUERY in values}
{db.execute(QUERY): db.execute("SELECT cron.fixed()") for QUERY in values}
(db.execute(QUERY) for QUERY in values)
[(db.execute(QUERY), db.execute("SELECT cron.fixed()"))
 for (QUERY, other) in pairs
 for QUERY in nested
 if QUERY]
[item for item in db.execute(QUERY) for QUERY in values]
[[db.execute(QUERY) for QUERY in inner] for QUERY in outer]
[db.execute(QUERY) for item in values if (QUERY := item)]
db.execute(QUERY)
"""
    evidence = parse_python_sql(source, "comprehensions.py").evidence

    assert [(item.dynamic, item.object_name) for item in evidence] == [
        (True, None),
        (True, None),
        (True, None),
        (False, "fixed"),
        (True, None),
        (True, None),
        (False, "fixed"),
        (False, "schedule"),  # first iterable is evaluated in the outer scope
        (True, None),
        (True, None),
        (True, None),
    ]


def test_comprehension_clause_lambdas_shadow_targets_and_first_iterable() -> None:
    source = b"""SQL = "SELECT cron.module_query()"
list_filter = [row for SQL in queries if (lambda: db.execute(SQL))()]
set_filter = {row for SQL in queries if (lambda: db.execute(SQL))()}
dict_filter = {row: row for SQL in queries if (lambda: db.execute(SQL))()}
gen_filter = (row for SQL in queries if (lambda: db.execute(SQL))())
list_later = [row for SQL in queries for row in (lambda: db.execute(SQL))()]
set_later = {row for SQL in queries for row in (lambda: db.execute(SQL))()}
dict_later = {row: row for SQL in queries for row in (lambda: db.execute(SQL))()}
gen_later = (row for SQL in queries for row in (lambda: db.execute(SQL))())
first_iter = [row for SQL in (lambda: db.execute(SQL))()]
stable = lambda db: db.execute(SQL)
"""
    evidence = parse_python_sql(source, "comprehension_clauses.py").evidence

    assert [item.dynamic for item in evidence] == [
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert [item.object_name for item in evidence[-2:]] == [
        "module_query",
        "module_query",
    ]


def test_markdown_fenced_sql_and_backticked_mentions_are_documentation() -> None:
    facts = parse_markdown_evidence(
        b"Run `cron.schedule(job_id, command)` first.\n\n```sql\nSELECT cron.unschedule(1);\n```\n",
        "runbook.md",
    )
    assert [(e.origin, e.object_name) for e in facts.evidence] == [
        ("markdown_sql", "unschedule"),
        ("markdown_mention", "schedule"),
    ]
    assert all(not item.dynamic for item in facts.evidence)
