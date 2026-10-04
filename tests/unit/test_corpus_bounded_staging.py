from __future__ import annotations

import csv
import io
import json
import subprocess
from pathlib import Path

import pytest

from codekg.corpus_export import export_corpus


def _manifest(tmp_path: Path, *, max_bytes: int = 1024) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("def api():\n    return 1\n")
    (source / "schema.sql").write_text("SELECT 1;\n")
    (source / "guide.md").write_text("# API\n`app.api()`\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="src"\n'
        f"max_file_bytes={max_bytes}\n"
        'sql={enabled=true,include=["**/*.sql"]}\n'
    )
    return source, config


def test_single_bounded_source_read_and_no_legacy_fallback(tmp_path, monkeypatch):
    import codekg.bulk_export
    import codekg.corpus_export
    import codekg.corpus_registry
    import codekg.ingest

    source, config = _manifest(tmp_path)
    read_counts: dict[Path, int] = {}
    extraction_active = False
    original_open = Path.open
    original_extract = codekg.corpus_registry.extract_snapshot_facts

    def tracked_open(path, *args, **kwargs):
        if extraction_active and path.is_relative_to(source):
            read_counts[path] = read_counts.get(path, 0) + 1
        return original_open(path, *args, **kwargs)

    def tracked_extract(*args, **kwargs):
        nonlocal extraction_active
        extraction_active = True
        try:
            return original_extract(*args, **kwargs)
        finally:
            extraction_active = False

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(codekg.corpus_registry, "extract_snapshot_facts", tracked_extract)
    monkeypatch.setattr(codekg.corpus_export, "extract_snapshot_facts", tracked_extract)
    monkeypatch.setattr(
        codekg.ingest,
        "scan_repository",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scan_repository fallback")),
    )
    monkeypatch.setattr(
        codekg.bulk_export,
        "export_repository_path",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("second extraction fallback")),
    )

    manifest = export_corpus(config, tmp_path / "out", workers=2)
    metrics = manifest["metrics"]
    stages = (
        "native_seconds",
        "bulk_export_seconds",
        "composition_seconds",
        "identity_seconds",
        "resolution_seconds",
        "supplemental_projection_seconds",
    )
    assert all(metrics[stage] >= 0 for stage in stages)
    assert metrics["elapsed_seconds"] + 0.01 >= sum(metrics[stage] for stage in stages)
    assert metrics["parent_peak_rss_kib"] > 0
    assert metrics["child_peak_rss_max_kib"] >= 0
    assert "not aggregate" in metrics["rss_scope"]
    assert read_counts == {path: 1 for path in sorted(source.iterdir())}
    assert manifest["metrics"]["bulk_extract_workers"] == 1
    bulk_manifest = manifest["snapshots"][0]["bulk_manifest"]
    schedule = tmp_path / "out" / Path(bulk_manifest).parent / ".building" / "schedule.sqlite"
    import sqlite3

    with sqlite3.connect(schedule) as catalog:
        spool_count = catalog.execute("SELECT count(*) FROM spools").fetchone()[0]
    assert spool_count == 1
    assert manifest["metrics"]["projection_workers"] == min(2, spool_count)


def test_many_files_roll_into_bounded_spools_and_report_real_workers(tmp_path):
    source, config = _manifest(tmp_path)
    for index in range(130):
        (source / f"module_{index:03d}.py").write_text(f"value = {index}\n")

    output = tmp_path / "out"
    manifest = export_corpus(config, output, workers=4)
    bulk_manifest = manifest["snapshots"][0]["bulk_manifest"]
    schedule = output / Path(bulk_manifest).parent / ".building" / "schedule.sqlite"
    import sqlite3

    with sqlite3.connect(schedule) as catalog:
        spool_paths = [
            row[0] for row in catalog.execute("SELECT path FROM spools ORDER BY ordinal")
        ]
    assert len(spool_paths) == 2
    file_counts = []
    for spool_path in spool_paths:
        with sqlite3.connect(spool_path) as spool:
            file_counts.append(spool.execute("SELECT count(*) FROM files").fetchone()[0])
    assert file_counts == [128, 4]
    assert max(file_counts) <= 128
    assert manifest["metrics"]["projection_workers"] == min(4, len(spool_paths)) == 2
    assert json.loads((output / bulk_manifest).read_text())["workers"] == 2


def test_spool_batcher_rolls_at_source_byte_limit(tmp_path):
    import sqlite3

    from codekg.bulk_export import CorpusSpoolBatcher
    from codekg.ir import FileIR

    spool_dir = tmp_path / "spools"
    catalog = sqlite3.connect(tmp_path / "catalog.sqlite")
    catalog.execute("CREATE TABLE spools (ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL)")
    batcher = CorpusSpoolBatcher(spool_dir, catalog)
    for path in ("first.py", "second.py"):
        batcher.write(
            FileIR(path=path, language="python", loc=1, module_qname=path[:-3]),
            20 * 1024 * 1024,
        )
    batcher.finish()
    paths = [row[0] for row in catalog.execute("SELECT path FROM spools ORDER BY ordinal")]
    catalog.close()
    assert len(paths) == 2
    assert [Path(path).exists() for path in paths] == [True, True]


def test_dirty_git_mutation_after_projection_keeps_old_publication(tmp_path, monkeypatch):
    import codekg.bulk_export

    source, config = _manifest(tmp_path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    output = tmp_path / "out"
    output.mkdir()
    published = output / "manifest.json"
    published.write_text('{"generation":"previous"}')
    original = codekg.bulk_export.finalize_corpus_snapshot

    def mutate_after_projection(*args, **kwargs):
        result = original(*args, **kwargs)
        (source / "app.py").write_text("def api():\n    return 2\n")
        return result

    monkeypatch.setattr(codekg.bulk_export, "finalize_corpus_snapshot", mutate_after_projection)
    with pytest.raises(RuntimeError, match="source identity changed"):
        export_corpus(config, output)
    assert json.loads(published.read_text()) == {"generation": "previous"}
    assert not list((output / "generations").iterdir())


def test_oversized_py_sql_markdown_and_growth_race_are_never_parsed(tmp_path, monkeypatch):
    import codekg.corpus_export
    import codekg.corpus_registry
    import codekg.docs
    import codekg.ingest

    source, config = _manifest(tmp_path, max_bytes=8)
    parser_calls = []
    for target, name in (
        (codekg.ingest, "scan_file_bytes"),
        (codekg.corpus_registry, "parse_python_sql"),
        (codekg.corpus_registry, "parse_routine_source"),
        (codekg.corpus_registry, "parse_markdown_evidence"),
        (codekg.docs, "chunk_docs"),
    ):
        monkeypatch.setattr(target, name, lambda *a, _name=name, **k: parser_calls.append(_name))

    output = tmp_path / "out"
    manifest = export_corpus(config, output)
    registry = output / manifest["registry"]
    import sqlite3

    with sqlite3.connect(registry) as db:
        oversized = {
            row[0]
            for row in db.execute(
                "SELECT path FROM diagnostics "
                "WHERE json_extract(fact,'$.category')='file_too_large'"
            )
        }
    assert {"app.py", "schema.sql", "guide.md"} <= oversized
    assert parser_calls == []

    # Simulate growth after stat but before read; the capped read sees size+1.
    race_source, race_config = _manifest(tmp_path / "race", max_bytes=8)
    (race_source / "app.py").write_text("x")
    active = False
    original_open = Path.open

    class GrowingSource(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def grow_during_read(path, *args, **kwargs):
        if active and path == race_source / "app.py" and args and args[0] == "rb":
            return GrowingSource(b"0123456789")
        return original_open(path, *args, **kwargs)

    original_extract = codekg.corpus_registry.extract_snapshot_facts

    def race_extract(*args, **kwargs):
        nonlocal active
        active = True
        try:
            return original_extract(*args, **kwargs)
        finally:
            active = False

    monkeypatch.setattr(Path, "open", grow_during_read)
    monkeypatch.setattr(codekg.corpus_registry, "extract_snapshot_facts", race_extract)
    monkeypatch.setattr(codekg.corpus_export, "extract_snapshot_facts", race_extract)
    raced = export_corpus(race_config, tmp_path / "race-out")
    with sqlite3.connect(tmp_path / "race-out" / raced["registry"]) as db:
        assert db.execute(
            "SELECT 1 FROM diagnostics WHERE path='app.py' "
            "AND json_extract(fact,'$.category')='file_too_large'"
        ).fetchone()
    assert parser_calls == []


@pytest.mark.parametrize("workers", [1, 2])
def test_sql_failure_modules_keep_sql_namespace_and_unreadable_coverage(
    tmp_path, monkeypatch, workers
):
    import sqlite3

    root = tmp_path / "src"
    root.mkdir()
    (root / "schema.sql").write_text("SELECT 123456789;\n")
    (root / "schema.sql.py").write_text("x=1\n")
    (root / "blocked.sql").write_text("SELECT")
    (root / "blocked.sql.py").write_text("x=1\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="src"\nmax_file_bytes=8\n'
        'sql={enabled=true,include=["**/*.sql"]}\n'
    )
    blocked = root / "blocked.sql"
    original_open = Path.open

    def deny_blocked(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("fixture unreadable SQL")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_blocked)
    output = tmp_path / f"out-{workers}"
    manifest = export_corpus(config, output, workers=workers)
    with sqlite3.connect(output / manifest["registry"]) as db:
        categories = {
            (path, category)
            for path, category in db.execute(
                "SELECT path,json_extract(fact,'$.category') FROM diagnostics"
            )
        }
        assert ("schema.sql", "file_too_large") in categories
        assert ("blocked.sql", "unreadable_file") in categories

    module_csv = output / manifest["output_dir"] / manifest["nodes"]["Module"]["file"]
    with module_csv.open(encoding="utf-8", newline="") as source:
        module_rows = list(csv.DictReader(source))
    modules = {(row["language"], row["qname"]) for row in module_rows}
    assert ("sql", "sql:schema.sql") in modules
    assert ("python", "schema.sql") in modules
    assert ("sql", "sql:blocked.sql") in modules
    assert ("python", "blocked.sql") in modules
    assert manifest["nodes"]["Module"]["count"] == 4


def test_selected_paths_are_streamed_and_output_overlap_is_rejected(tmp_path):
    from codekg.corpus_config import CorpusSnapshotConfig
    from codekg.corpus_registry import selected_paths
    from codekg.sql_config import SqlConfig

    root, config = _manifest(tmp_path)
    (root / "extension.control.in").write_text("module_pathname = 'x'\n")
    (root / "misc.in").write_text("not SQL\n")
    outside_file = tmp_path / "escape.py"
    outside_file.write_text("outside = True\n")
    (root / "escape.py").symlink_to(outside_file)
    snapshot = CorpusSnapshotConfig(
        "app", "app", "1", "application", root, sql_config=SqlConfig(enabled=True)
    )
    paths = selected_paths(snapshot, tmp_path / "elsewhere")
    assert iter(paths) is paths
    assert next(paths).name == "app.py"
    names = {path.name for path in selected_paths(snapshot, tmp_path / "elsewhere")}
    assert "extension.control.in" in names
    assert "misc.in" not in names
    assert "escape.py" not in names
    with pytest.raises(ValueError, match="overlaps source root"):
        export_corpus(config, root / "output")


def test_late_composition_failure_preserves_previous_manifest_and_cleans_generation(
    tmp_path, monkeypatch
):
    import codekg.corpus_export

    _, config = _manifest(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    published = output / "manifest.json"
    published.write_text('{"generation":"old"}\n')
    before = set((output / "generations").iterdir()) if (output / "generations").exists() else set()
    monkeypatch.setattr(
        codekg.corpus_export,
        "_compose_graph",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late composition failure")),
    )
    with pytest.raises(RuntimeError, match="late composition failure"):
        export_corpus(config, output)
    assert json.loads(published.read_text()) == {"generation": "old"}
    after = set((output / "generations").iterdir())
    assert after == before
