from __future__ import annotations

import csv
import json
from pathlib import Path

from codekg.bulk_export import export_repository_path
from codekg.bulk_identity import content_hash
from codekg.ingest import _iter_source_files, try_scan_file
from codekg.sql_config import SqlConfig


def test_explicit_sql_config_selects_templates_without_modifying_checkout(tmp_path: Path) -> None:
    (tmp_path / "extension.sql.in").write_text(
        "CREATE FUNCTION app.f() RETURNS int LANGUAGE sql AS 'SELECT 1';"
    )
    (tmp_path / "ignored.in").write_text("not SQL")
    config = SqlConfig(enabled=True, database="extensions", include=("**/*.sql.in",))
    assert not list(_iter_source_files(tmp_path))
    assert [p.name for p in _iter_source_files(tmp_path, sql_config=config)] == ["extension.sql.in"]
    file = try_scan_file(tmp_path, tmp_path / "extension.sql.in", sql_config=config)
    assert file is not None and file.language == "sql"
    assert file.sql_object_refs[0].database_name == "extensions"
    assert not (tmp_path / "codekg.toml").exists()


def test_streaming_export_has_snapshot_alias_and_worker_sql_override(tmp_path: Path) -> None:
    root = tmp_path / "arbitrary-worktree-name"
    root.mkdir()
    (root / "install.sql.in").write_text("CREATE TABLE public.facts(id int);")
    config = SqlConfig(enabled=True, database="extensions", include=("**/*.sql.in",))
    export = export_repository_path(
        root,
        tmp_path / "output",
        repo_name="postgis-pg18",
        commit_override="full-revision",
        sql_config=config,
        workers=2,
    )
    manifest = json.loads(export.manifest_path.read_text())
    assert manifest["repository"]["repo_name"] == "postgis-pg18"
    assert manifest["repository"]["commit"] == "full-revision"
    assert export.counts["nodes_SqlObject"] == 1
    group = export.node_groups["File"]
    with group[1].open(newline="") as stream:
        row = next(csv.reader(stream))
    assert row[0] == "postgis-pg18@full-revision:install.sql.in"


def test_sql_override_participates_in_nongit_content_identity(tmp_path: Path) -> None:
    source = tmp_path / "install.sql.in"
    source.write_text("SELECT 1;")
    config = SqlConfig(enabled=True, include=("**/*.sql.in",))
    legacy = content_hash(tmp_path)
    first = content_hash(tmp_path, sql_config=config)
    source.write_text("SELECT 2;")
    assert content_hash(tmp_path) == legacy
    assert content_hash(tmp_path, sql_config=config) != first
    assert (
        content_hash(
            tmp_path, sql_config=SqlConfig(enabled=True, database="other", include=("**/*.sql.in",))
        )
        != first
    )
