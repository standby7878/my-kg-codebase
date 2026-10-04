from __future__ import annotations

import json
import subprocess

from codekg.corpus_config import CorpusSnapshotConfig
from codekg.corpus_registry import create_native_registry, extract_snapshot_facts, snapshot_identity
from codekg.sql_config import SqlConfig


def test_registry_extracts_facts_to_indexed_sqlite(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.c").write_text("int api(int x) { return x; }\n")
    (root / "calls.c").write_text("int call(void (*cb)(void)) { cb(); return 0; }\n")
    (root / "app.py").write_text('SQL = "select cron_schedule()"\ndb.execute(SQL)\n')
    snapshot = CorpusSnapshotConfig(
        "pg18", "postgres", "18", "postgres", root, (), SqlConfig(enabled=True)
    )
    db = create_native_registry(tmp_path / "registry.sqlite")
    counts = extract_snapshot_facts(db, snapshot, tmp_path / "output")
    assert counts["symbols"] >= 1 and counts["evidence"] >= 1
    assert (
        json.loads(db.execute("SELECT fact FROM symbols WHERE path='x.c'").fetchone()[0])["name"]
        == "api"
    )
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM symbols INDEXED BY idx_symbols_name "
        "WHERE snapshot_alias=? "
        "AND json_extract(fact,'$.name')=?",
        ("pg18", "api"),
    ).fetchall()
    assert any("PRIMARY KEY" in row[-1] or "idx_symbols_name" in row[-1] for row in plan)
    incoming_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM edges INDEXED BY idx_edges_target "
        "WHERE target_key=? AND kind=?",
        ("target", "CALLS_NATIVE"),
    ).fetchall()
    overload_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM routines INDEXED BY idx_routines_overload "
        "WHERE snapshot_alias=? AND json_extract(fact,'$.schema_name')=? "
        "AND json_extract(fact,'$.name')=? AND json_extract(fact,'$.arity')=?",
        ("pg18", "public", "api", 1),
    ).fetchall()
    assert any("idx_edges_target" in row[-1] for row in incoming_plan)
    assert any("idx_routines_overload" in row[-1] for row in overload_plan)
    callsite_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT ordinal FROM evidence INDEXED BY idx_evidence_callsite "
        "WHERE snapshot_alias=? AND path=? "
        "AND json_extract(fact,'$.origin')=? AND json_extract(fact,'$.start_line')=? "
        "AND json_extract(fact,'$.start_column')=? ORDER BY ordinal LIMIT 2",
        ("pg18", "calls.c", "native_call", 1, 0),
    ).fetchall()
    symbol_owner_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT ordinal FROM symbols WHERE snapshot_alias=? AND path=? "
        "AND json_extract(fact,'$.name')=? AND json_extract(fact,'$.start_line')=? LIMIT 2",
        ("pg18", "x.c", "api", 1),
    ).fetchall()
    python_owner_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT key FROM python_owners WHERE snapshot_alias=? AND path=? "
        "AND start_line=? AND (qname=? OR qname LIKE '%.' || ?) LIMIT 2",
        ("pg18", "app.py", 1, "api", "api"),
    ).fetchall()
    assert any("idx_evidence_callsite" in row[-1] for row in callsite_plan)
    assert any("idx_symbols_owner" in row[-1] for row in symbol_owner_plan)
    assert any("idx_python_owners_scope" in row[-1] for row in python_owner_plan)
    dynamic = db.execute("SELECT fact FROM evidence WHERE path='calls.c'").fetchone()
    assert json.loads(dynamic[0])["origin"] == "native_call"
    assert json.loads(dynamic[0])["dynamic"] is True
    db.close()


def test_identity_changes_when_selected_source_changes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "x.c"
    source.write_text("int api(void) { return 1; }\n")
    snapshot = CorpusSnapshotConfig("pg", "postgres", "18", "postgres", root)
    first = snapshot_identity(snapshot, tmp_path / "out")
    source.write_text("int api(void) { return 2; }\n")
    second = snapshot_identity(snapshot, tmp_path / "out")
    assert first.source_digest != second.source_digest
    assert first.revision != second.revision


def test_identity_tracks_unreadable_sources_and_readability_transitions(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "blocked.sql"
    source.write_text("SELECT 1;\n")
    snapshot = CorpusSnapshotConfig("app", "app", "1", "postgres", root)
    original_open = type(source).open
    unreadable = True

    def controlled_open(path, *args, **kwargs):
        if path == source and unreadable:
            raise PermissionError("fixture unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(source), "open", controlled_open)
    inaccessible = snapshot_identity(snapshot, tmp_path / "out")
    unreadable = False
    readable = snapshot_identity(snapshot, tmp_path / "out")
    assert inaccessible.source_digest != readable.source_digest
    assert inaccessible.revision != readable.revision


def test_registry_persists_each_positioned_native_include_occurrence(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "includes.c").write_text(
        '#ifdef USE_OPTION\n#include "api.h"\n#endif\n#include "api.h"\n'
    )
    snapshot = CorpusSnapshotConfig("pg", "postgres", "18", "postgres", root)
    db = create_native_registry(tmp_path / "registry.sqlite")
    counts = extract_snapshot_facts(db, snapshot, tmp_path / "output")
    evidence = [
        json.loads(row[0])
        for row in db.execute("SELECT fact FROM evidence WHERE path='includes.c' ORDER BY ordinal")
    ]
    db.close()
    assert counts["evidence"] == 2
    assert [(item["origin"], item["object_name"], item["condition"]) for item in evidence] == [
        ("native_include", "api.h", "ifdef USE_OPTION"),
        ("native_include", "api.h", None),
    ]
    assert evidence[0]["start_line"] == 2 and evidence[1]["start_line"] == 4


def test_selected_paths_classify_uppercase_sql_before_applying_include_policy(tmp_path):
    from codekg.corpus_registry import selected_paths

    root = tmp_path / "src"
    root.mkdir()
    (root / "leak.SQL").write_text("SELECT 1;\n")
    (root / "included.SQL").write_text(
        "CREATE FUNCTION included() RETURNS integer LANGUAGE SQL AS $$ SELECT 1 $$;\n"
    )
    (root / "template.SQL.IN").write_text(
        "CREATE FUNCTION templated() RETURNS integer LANGUAGE SQL AS $$ SELECT 1 $$;\n"
    )
    disabled = CorpusSnapshotConfig(
        "app", "app", "1", "application", root, sql_config=SqlConfig(enabled=False)
    )
    assert list(selected_paths(disabled, tmp_path / "out")) == []
    enabled = CorpusSnapshotConfig(
        "app",
        "app",
        "1",
        "application",
        root,
        sql_config=SqlConfig(enabled=True, include=("included.SQL", "template.SQL.IN")),
    )
    assert [path.name for path in selected_paths(enabled, tmp_path / "out")] == [
        "included.SQL",
        "template.SQL.IN",
    ]
    db = create_native_registry(tmp_path / "upper.sqlite")
    assert extract_snapshot_facts(db, enabled, tmp_path / "out")["routines"] == 2
    db.close()


def test_oversized_files_are_diagnosed_without_parsing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "huge.c").write_text("int oversized(void) { return 1; }\n")
    snapshot = CorpusSnapshotConfig("pg", "postgres", "18", "postgres", root, max_file_bytes=8)
    db = create_native_registry(tmp_path / "registry.sqlite")
    counts = extract_snapshot_facts(db, snapshot, tmp_path / "output")
    assert counts["oversized"] == 1
    assert counts["symbols"] == 0
    diagnostic = json.loads(db.execute("SELECT fact FROM diagnostics").fetchone()[0])
    assert diagnostic["category"] == "file_too_large"
    db.close()


def test_git_dirty_edits_at_same_head_change_revision(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "x.c"
    source.write_text("int api(void) { return 1; }\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "x.c"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    snapshot = CorpusSnapshotConfig("pg", "postgres", "18", "postgres", root)
    first = snapshot_identity(snapshot, tmp_path / "out")
    source.write_text("int api(void) { return 2; }\n")
    second = snapshot_identity(snapshot, tmp_path / "out")
    assert first.git_commit == second.git_commit
    assert first.source_digest != second.source_digest
    assert first.revision != second.revision
