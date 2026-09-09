from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codekg.bulk_spool import build_registry, create_spool, iter_spool_files
from codekg.ir import FileIR
from codekg.sql_ir import SqlArtifactIR, SqlObjectRefIR, SqlStatementIR

pytestmark = pytest.mark.unit


def _sql_file() -> FileIR:
    return FileIR(
        path="migrations/001.sql",
        language="sql",
        loc=12,
        module_qname="migrations.001",
        sql_artifacts=(
            SqlArtifactIR(4, "sql_file", "postgres", "CREATE TABLE x", "hash", 2, 1, 2, 15),
        ),
        sql_statements=(
            SqlStatementIR(
                4, 8, "create_table", control_context="transaction", end_line=2, end_column=15
            ),
        ),
        sql_object_refs=(
            SqlObjectRefIR(
                4,
                8,
                12,
                "define",
                "public.x",
                None,
                "public",
                "x",
                "table",
                None,
                2,
                1,
                2,
                15,
                search_path=("app", "public"),
            ),
        ),
    )


def test_v3_sql_ir_round_trip_is_lossless_and_normalized(tmp_path: Path) -> None:
    spool = tmp_path / "sql.sqlite"
    expected = _sql_file()

    create_spool(spool, [expected])

    assert list(iter_spool_files(spool)) == [expected]
    with sqlite3.connect(spool) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("3",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"sqlartifacts", "sqlstatements", "sqlrefs", "sqlref_search_path"} <= tables
        assert "payload" not in {row[1] for row in connection.execute("PRAGMA table_info(files)")}
        assert connection.execute("SELECT count(*) FROM sqlref_search_path").fetchone() == (2,)


def test_v3_registry_copy_keeps_sql_rows_without_reconstruction(tmp_path: Path) -> None:
    spool = tmp_path / "sql.sqlite"
    registry = tmp_path / "registry.sqlite"
    create_spool(spool, [_sql_file()])

    build_registry(registry, [spool], repo_prefix="repo@commit")

    with sqlite3.connect(registry) as connection:
        assert connection.execute("SELECT count(*) FROM sqlartifacts").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM sqlstatements").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM sqlrefs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM sqlref_search_path").fetchone() == (2,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'sqlrefs_lookup_idx'"
        ).fetchone() == ("sqlrefs_lookup_idx",)
