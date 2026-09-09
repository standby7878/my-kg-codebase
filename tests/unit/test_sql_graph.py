from __future__ import annotations

import json
from pathlib import Path

import pytest

from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import FileIR
from codekg.sql_graph import (
    SQL_NODE_COLUMNS,
    SQL_REL_COLUMNS,
    iter_all_sql_global_nodes,
    iter_sql_file_nodes,
    iter_sql_file_relationships,
    iter_sql_global_nodes,
    iter_sql_global_relationships,
)
from codekg.sql_ir import SqlArtifactIR, SqlObjectRefIR, SqlStatementIR
from codekg.sql_resolver import SqliteSqlResolverIndex

pytestmark = pytest.mark.unit


def _ref(ordinal: int, role: str, name: str, *, schema: str | None = "public") -> SqlObjectRefIR:
    return SqlObjectRefIR(
        1,
        1,
        ordinal,
        role,  # type: ignore[arg-type]
        name,
        "db",
        schema,
        name,
        "table",
        None,
        1,
        ordinal,
        1,
        ordinal + 1,
        False,
        ("public",),
    )


def _file(path: str, refs: tuple[SqlObjectRefIR, ...]) -> FileIR:
    return FileIR(
        path=path,
        language="sql",
        loc=2,
        module_qname=f"sql:{path}",
        sql_artifacts=(SqlArtifactIR(1, "sql_file", "postgres", "SELECT", "hash", 1, 1, 2, 1),),
        sql_statements=(SqlStatementIR(1, 1, "select", end_line=2, end_column=1),),
        sql_object_refs=refs,
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


def test_sql_graph_columns_and_global_relationships_are_public(tmp_path: Path) -> None:
    file = _file("migrations/001.sql", (_ref(1, "define", "items"),))
    index = _index(tmp_path, (file,))
    try:
        assert {"Database", "SqlObject", "SqlArtifact", "SqlStatement", "Reference"} <= set(
            SQL_NODE_COLUMNS
        )
        assert {"HAS_DATABASE", "HAS_OBJECT", "CONTAINS_SQL", "REFERS_TO"} <= set(SQL_REL_COLUMNS)
        global_nodes = list(iter_all_sql_global_nodes(index))
        assert {label for label, _ in global_nodes} == {"Database", "SqlObject"}
        relationships = list(
            iter_sql_global_relationships("repo@commit", "repo@commit", index, all_global=True)
        )
        assert {row[0] for row in relationships} == {"HAS_DATABASE", "HAS_OBJECT"}
        assert list(iter_sql_global_nodes(index, owner_path="migrations/001.sql")) == global_nodes
    finally:
        index.close()


def test_file_projection_keeps_source_occurrences_and_deduplicates_derived_edges(
    tmp_path: Path,
) -> None:
    file = _file(
        "migrations/001.sql",
        (
            _ref(1, "define", "items"),
            _ref(2, "read", "items"),
            _ref(3, "read", "items"),
        ),
    )
    index = _index(tmp_path, (file,))
    try:
        nodes = list(iter_sql_file_nodes("repo@commit", file, index))
        references = [row for label, row in nodes if label == "Reference"]
        assert len(references) == 3
        assert len({row["key"] for row in references}) == 3
        assert all(row["status"] == "exact" for row in references)

        relationships = list(iter_sql_file_relationships("repo@commit", file, index))
        kinds = [row[0] for row in relationships]
        assert kinds.count("HAS_REFERENCE") == 3
        assert kinds.count("REFERS_TO") == 3
        assert kinds.count("DEFINES") == 1
        assert kinds.count("READS_FROM") == 1
        assert all(
            ":sql-reference:1:1:" in row[4]
            for row in relationships
            if row[0] in {"HAS_REFERENCE", "REFERS_TO"}
        )
    finally:
        index.close()


def test_reference_candidate_keys_json_preserves_semicolon_identifier(tmp_path: Path) -> None:
    file = _file("quoted.sql", (_ref(1, "define", "a;b"), _ref(2, "read", "a;b")))
    index = _index(tmp_path, (file,))
    try:
        references = [
            row
            for label, row in iter_sql_file_nodes("repo@commit", file, index)
            if label == "Reference"
        ]
        assert json.loads(references[1]["candidate_keys_json"])[0].endswith(":v3:a;b:n")
    finally:
        index.close()
