from __future__ import annotations

from pathlib import Path

import pytest

from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import FileIR
from codekg.sql_config import SqlConfig
from codekg.sql_ir import SqlArtifactIR, SqlObjectRefIR, SqlStatementIR
from codekg.sql_parser import parse_sql
from codekg.sql_resolver import SqliteSqlResolverIndex, sql_object_key

pytestmark = pytest.mark.unit


def _file(path: str, refs: tuple[SqlObjectRefIR, ...]) -> FileIR:
    return FileIR(
        path=path,
        language="sql",
        loc=1,
        module_qname=f"sql:{path}",
        sql_artifacts=(SqlArtifactIR(1, "sql_file", "postgres", "SQL", path, 1, 1, 1, 4),),
        sql_statements=(SqlStatementIR(1, 1, "sql", end_column=4),),
        sql_object_refs=refs,
    )


def _ref(
    ordinal: int,
    role: str,
    name: str,
    *,
    schema: str | None = None,
    kind: str = "table",
    signature: str | None = None,
    database: str | None = "db",
    search_path: tuple[str, ...] = ("public",),
    dynamic: bool = False,
) -> SqlObjectRefIR:
    return SqlObjectRefIR(
        1,
        1,
        ordinal,
        role,  # type: ignore[arg-type]
        name,
        database,
        schema,
        name,
        kind,
        signature,
        1,
        ordinal,
        1,
        ordinal + 1,
        dynamic,
        search_path,
    )


def _index(tmp_path: Path, files: tuple[FileIR, ...]) -> SqliteSqlResolverIndex:
    spools = []
    for ordinal, file in enumerate(files):
        spool = tmp_path / f"spool-{ordinal}.sqlite"
        create_spool(spool, [file])
        spools.append(spool)
    registry = tmp_path / "registry.sqlite"
    build_registry(registry, spools, repo_prefix="repo@commit")
    return SqliteSqlResolverIndex(registry)


def test_repeated_definitions_group_with_deterministic_owner(tmp_path: Path) -> None:
    definition = _ref(1, "define", "orders", schema="app")
    index = _index(
        tmp_path,
        (
            _file("z.sql", (definition,)),
            _file("a.sql", (definition,)),
        ),
    )
    try:
        objects = list(index.objects())
        assert len(objects) == 1
        assert objects[0].definition_count == 2
        assert objects[0].owner_path == "a.sql"
        assert list(index.objects_owned_by("a.sql")) == objects
        assert list(index.objects_owned_by("z.sql")) == []
    finally:
        index.close()


def test_search_path_relations_shadow_later_schema_and_include_views(tmp_path: Path) -> None:
    files = (
        _file("one.sql", (_ref(1, "define", "thing", schema="first"),)),
        _file("two.sql", (_ref(1, "define", "thing", schema="second", kind="view"),)),
    )
    index = _index(tmp_path, files)
    try:
        read = _ref(9, "read", "thing", search_path=("first", "second"))
        result = index.resolve(read)
        assert result.status == "exact"
        assert result.candidate_count == 1
        assert result.object_key == next(index.objects(schema_name="first")).key
    finally:
        index.close()


def test_routine_overloads_are_conservative_across_search_path(tmp_path: Path) -> None:
    files = (
        _file(
            "a.sql",
            (_ref(1, "define", "run", schema="one", kind="function", signature="run(int4)"),),
        ),
        _file(
            "b.sql",
            (_ref(1, "define", "run", schema="two", kind="function", signature="run(text)"),),
        ),
    )
    index = _index(tmp_path, files)
    try:
        unknown = _ref(4, "call", "run", kind="function", search_path=("one", "two"))
        assert index.resolve(unknown).status == "ambiguous"
        assert index.resolve(unknown).candidate_count == 2

        mismatched = _ref(
            5,
            "call",
            "run",
            kind="function",
            signature="run(unknown)",
            search_path=("one", "two"),
        )
        result = index.resolve(mismatched)
        assert result.status == "ambiguous"
        assert result.candidate_count == 2

        known = _ref(
            6,
            "call",
            "run",
            kind="function",
            signature="run(int4)",
            search_path=("one", "two"),
        )
        assert index.resolve(known).status == "exact"
    finally:
        index.close()


def test_cache_hit_preserves_second_reference_provenance(tmp_path: Path) -> None:
    definition = _ref(1, "define", "items", schema="public")
    index = _index(tmp_path, (_file("defs.sql", (definition,)),))
    try:
        first = _ref(2, "read", "items")
        second = _ref(3, "read", "items")
        assert index.resolve(first).ref == first
        assert index.resolve(second).ref == second
    finally:
        index.close()


def test_unknown_namespace_does_not_create_default_object(tmp_path: Path) -> None:
    definition = _ref(1, "define", "items", schema=None, search_path=())
    index = _index(tmp_path, (_file("unknown.sql", (definition,)),))
    try:
        assert list(index.objects()) == []
        assert index.resolve(definition).status == "unresolved"
    finally:
        index.close()


@pytest.mark.parametrize(
    ("kind", "schema", "search_path", "expected_schema"),
    [
        ("sequence", "app", (), "app"),
        ("index", "app", (), "app"),
        ("statistics", "app", (), "app"),
        ("type", "app", (), "app"),
        ("schema", None, (), "<schema-less>"),
        ("extension", None, (), "<schema-less>"),
    ],
)
def test_direct_resolution_covers_nonrelation_kinds(
    tmp_path: Path,
    kind: str,
    schema: str | None,
    search_path: tuple[str, ...],
    expected_schema: str,
) -> None:
    definition = _ref(
        1,
        "define",
        f"{kind}_name",
        schema=schema,
        kind=kind,
        search_path=search_path,
    )
    index = _index(tmp_path, (_file(f"{kind}.sql", (definition,)),))
    try:
        assert index.resolve(definition).status == "exact"
        obj = next(index.objects(kind=kind))
        assert obj.schema_name == expected_schema
    finally:
        index.close()


def test_direct_resolution_uses_ordered_search_path(tmp_path: Path) -> None:
    second = _ref(1, "define", "seq_name", schema="second", kind="sequence")
    index = _index(tmp_path, (_file("defs.sql", (second,)),))
    try:
        ref = _ref(2, "read", "seq_name", kind="sequence", search_path=("first", "second"))
        result = index.resolve(ref)
        assert result.status == "exact"
        assert result.object_key == next(index.objects(schema_name="second", kind="sequence")).key
    finally:
        index.close()


def test_alter_table_resolves_as_table(tmp_path: Path) -> None:
    definition = _ref(1, "define", "users", schema="app")
    alter = _ref(2, "alter", "users", schema="app")
    index = _index(tmp_path, (_file("defs.sql", (definition, alter)),))
    try:
        assert index.resolve(alter).status == "exact"
        assert index.resolve(alter).object_key == index.resolve(definition).object_key
    finally:
        index.close()


def test_temporary_ctas_body_reads_persistent_before_shadowing(tmp_path: Path) -> None:
    persistent = _ref(1, "define", "temp_copy", schema="app", database="management")
    parsed = parse_sql(
        "CREATE TEMP TABLE temp_copy AS SELECT * FROM app.temp_copy;\nSELECT * FROM temp_copy;"
    )
    index = _index(tmp_path, (_file("defs.sql", (persistent,)), parsed))
    try:
        reads = [
            ref
            for ref in parsed.sql_object_refs
            if ref.role == "read" and ref.object_name == "temp_copy"
        ]
        body_read, later_read = reads[0], reads[1]
        assert body_read.schema_name == "app"
        assert index.resolve(body_read).status == "exact"
        assert later_read.object_kind_hint == "temporary_table"
        assert index.resolve(later_read).status == "unresolved"
    finally:
        index.close()


def test_schema_and_extension_definitions_use_schema_less_namespace(tmp_path: Path) -> None:
    parsed = parse_sql(
        "CREATE SCHEMA app;\nCREATE EXTENSION hstore;\n",
        context="defs.sql",
        config=SqlConfig(enabled=True),
    )
    index = _index(tmp_path, (parsed,))
    try:
        definitions = parsed.sql_object_refs
        assert [index.resolve(ref).status for ref in definitions] == ["exact", "exact"]
        assert {obj.kind for obj in index.objects()} == {"schema", "extension"}
        assert {obj.schema_name for obj in index.objects()} == {"<schema-less>"}
    finally:
        index.close()


def test_definition_signature_is_exact_even_when_signature_is_unsafe(tmp_path: Path) -> None:
    parsed = parse_sql(
        "CREATE FUNCTION f(x unknown_type) RETURNS int AS $$ SELECT 1 $$ LANGUAGE SQL;\n"
        "CREATE FUNCTION f(x text) RETURNS int AS $$ SELECT 1 $$ LANGUAGE SQL;\n",
        context="defs.sql",
        config=SqlConfig(enabled=True),
    )
    index = _index(tmp_path, (parsed,))
    try:
        definitions = parsed.sql_object_refs
        assert len(definitions) == 2
        first, second = (index.resolve(ref) for ref in definitions)
        assert first.status == second.status == "exact"
        assert first.object_key != second.object_key
        assert sorted(obj.signature for obj in index.objects(kind="function")) == [
            "f(text)",
            "f(unknown_type)",
        ]
    finally:
        index.close()


def test_sql_identity_tags_null_and_literal_null_differ() -> None:
    null_key = sql_object_key("repo@commit", "db", "public", "function", "f", None)
    literal_key = sql_object_key("repo@commit", "db", "public", "function", "f", "<null>")
    assert null_key != literal_key
