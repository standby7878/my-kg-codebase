from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

import codekg.sqlite_cli as sqlite_cli_module
from codekg.sqlite_cli import SqliteCliImportError, SqliteCsvImporter, import_rows

pytestmark = pytest.mark.unit


@pytest.fixture
def sqlite_cli() -> str:
    executable = shutil.which("sqlite3")
    if executable is None:
        pytest.skip("SQLite CLI is required for streaming import tests")
    return executable


def test_stream_import_round_trips_json_csv_edge_cases(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "rows.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT, nullable TEXT)"
        )
    values = [
        (1, "", None),
        (2, '雪 ☃ "quoted"', "line\r\nnext\tfield"),
        (3, "before\x00after", ""),
    ]

    with SqliteCsvImporter(
        database, {"values_table": ("id", "value", "nullable")}, sqlite3=sqlite_cli
    ) as writer:
        for row in values:
            writer.write("values_table", row)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM values_table ORDER BY id").fetchall() == values


def test_stream_import_handles_row_larger_than_pipe_buffer(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "large.sqlite"
    value = "wide" * 100_000
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE payloads (value TEXT)")

    assert import_rows(database, "payloads", ("value",), [(value,)]) == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM payloads").fetchone() == (value,)


def test_target_named_like_temp_stage_writes_to_main_schema(
    tmp_path: Path, sqlite_cli: str
) -> None:
    database = tmp_path / "shadow.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE _codekg_stream_stage (value TEXT)")

    assert import_rows(database, "_codekg_stream_stage", ("value",), [("main target",)]) == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM main._codekg_stream_stage").fetchall() == [
            ("main target",)
        ]


def test_import_rows_conflict_replace_preserves_other_rows(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "replace.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO values_table VALUES (?,?)", (1, "old"))
        connection.execute("INSERT INTO values_table VALUES (?,?)", (2, "keep"))

    assert import_rows(database, "values_table", ("id", "value"), [(1, "new")], replace=True) == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM values_table ORDER BY id").fetchall() == [
            (1, "new"),
            (2, "keep"),
        ]


def test_stable_sort_preserves_duplicate_conflict_order(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "stable-order.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE replaced (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE ignored (id INTEGER PRIMARY KEY, value TEXT)")
    with SqliteCsvImporter(
        database,
        {"replaced": ("id", "value"), "ignored": ("id", "value")},
        replace_tables=("replaced",),
        ignore_tables=("ignored",),
        sqlite3=sqlite_cli,
    ) as importer:
        importer.write("replaced", (1, "first"))
        importer.write("ignored", (1, "first"))
        importer.write("replaced", (1, "last"))
        importer.write("ignored", (1, "last"))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM replaced").fetchall() == [(1, "last")]
        assert connection.execute("SELECT * FROM ignored").fetchall() == [(1, "first")]


def test_sort_argv_groups_tags_and_uses_private_temp_paths(
    tmp_path: Path, sqlite_cli: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "parent path" / "sort check.sqlite"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
    original_run = sqlite_cli_module.subprocess.run
    observed = {}

    def checked_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return original_run(command, **kwargs)

    monkeypatch.setattr(sqlite_cli_module.subprocess, "run", checked_run)
    importer = SqliteCsvImporter(database, {"items": ("value",)}, sqlite3=sqlite_cli)
    workdir = importer._workdir
    importer.write("items", ("value",))
    importer.finish()

    command = observed["command"]
    assert command[1:] == [
        "--stable",
        "-t",
        ",",
        "-k1,1",
        "-S",
        "32M",
        "--parallel=1",
        "-T",
        str(workdir),
        "-o",
        str(workdir / "sorted.csv"),
        "--",
        str(workdir / "raw.csv"),
    ]
    assert observed["kwargs"]["env"]["LC_ALL"] == "C"
    assert observed["kwargs"]["cwd"] == workdir
    assert observed["kwargs"]["stdout"] == sqlite_cli_module.subprocess.DEVNULL
    assert not workdir.exists()


def test_zero_rows_sorts_trailer_and_finishes(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "empty.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
    importer = SqliteCsvImporter(database, {"items": ("value",)}, sqlite3=sqlite_cli)
    workdir = importer._workdir
    assert importer.finish() == {"items": 0}
    assert not workdir.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM items").fetchall() == []


def test_missing_sort_fails_before_sqlite_child_and_cleans_dump(
    tmp_path: Path, sqlite_cli: str
) -> None:
    database = tmp_path / "missing-sort.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('original')")
    importer = SqliteCsvImporter(
        database, {"items": ("value",)}, sqlite3=sqlite_cli, sort="missing-sort"
    )
    workdir = importer._workdir
    importer.write("items", ("candidate",))
    with pytest.raises(SqliteCliImportError, match="GNU sort executable.*no fallback"):
        importer.finish()
    assert importer._process is None
    assert not workdir.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM items").fetchall() == [("original",)]


def test_sort_failure_does_not_start_sqlite_or_publish_rows(
    tmp_path: Path, sqlite_cli: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "sort-fails"
    executable.write_text("#!/bin/sh\necho sort-error >&2\nexit 9\n")
    executable.chmod(executable.stat().st_mode | 0o111)
    database = tmp_path / "sort-failure.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('original')")
    importer = SqliteCsvImporter(
        database, {"items": ("value",)}, sqlite3=sqlite_cli, sort=str(executable)
    )
    workdir = importer._workdir
    importer.write("items", ("candidate",))
    with pytest.raises(SqliteCliImportError, match="GNU sort failed.*sort-error"):
        importer.finish()
    assert importer._process is None
    assert not workdir.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM items").fetchall() == [("original",)]


def test_ready_wait_has_no_import_size_timeout(
    tmp_path: Path, sqlite_cli: str, monkeypatch
) -> None:
    event = sqlite_cli_module.threading.Event
    waits = []

    class RecordingEvent(event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return super().wait(timeout)

    monkeypatch.setattr(sqlite_cli_module.threading, "Event", RecordingEvent)
    database = tmp_path / "wait.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
    assert import_rows(database, "items", ("value",), [("x",)]) == 1
    assert waits and all(timeout is None for timeout in waits)


def test_constraint_failure_does_not_commit_partial_target_rows(
    tmp_path: Path, sqlite_cli: str
) -> None:
    database = tmp_path / "constraint.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO values_table VALUES (1, 'original')")

    writer = SqliteCsvImporter(database, {"values_table": ("id", "value")}, sqlite3=sqlite_cli)
    writer.write("values_table", (2, "valid"))
    writer.write("values_table", (3, None))
    with pytest.raises(SqliteCliImportError, match="failed"):
        writer.finish()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM values_table").fetchall() == [(1, "original")]


def test_producer_failure_aborts_and_keeps_target_unchanged(
    tmp_path: Path, sqlite_cli: str
) -> None:
    database = tmp_path / "producer.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO values_table VALUES (1, 'original')")

    with (
        pytest.raises(RuntimeError, match="producer failed"),
        SqliteCsvImporter(
            database, {"values_table": ("id", "value")}, sqlite3=sqlite_cli
        ) as writer,
    ):
        writer.write("values_table", (2, "partial"))
        raise RuntimeError("producer failed")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM values_table").fetchall() == [(1, "original")]


def test_abort_closes_control_and_process_pipe_descriptors(tmp_path: Path, sqlite_cli: str) -> None:
    fd_root = Path("/proc/self/fd")
    if not fd_root.exists():
        pytest.skip("descriptor count assertion requires /proc")
    database = tmp_path / "fds.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table (value TEXT)")
    before = len(list(fd_root.iterdir()))
    writer = SqliteCsvImporter(database, {"values_table": ("value",)}, sqlite3=sqlite_cli)
    writer.write("values_table", ("x" * 300_000,))
    writer.abort()

    assert len(list(fd_root.iterdir())) <= before + 1


def test_missing_cli_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SqliteCliImportError, match="no fallback"):
        SqliteCsvImporter(tmp_path / "missing.sqlite", {"items": ("value",)}, sqlite3="not-sqlite3")


def test_validation_failure_aborts_child_and_partial_batch(tmp_path: Path, sqlite_cli: str) -> None:
    database = tmp_path / "validation.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)")

    writer = SqliteCsvImporter(database, {"values_table": ("id", "value")}, sqlite3=sqlite_cli)
    writer.write("values_table", (1, "good"))
    with pytest.raises(ValueError, match="width"):
        writer.write("values_table", (2,))
    with pytest.raises(ValueError, match="closed"):
        writer.write("values_table", (3, "late"))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM values_table").fetchall() == []


@pytest.mark.parametrize("corruption", ["missing_trailer", "bad_count", "unknown_tag", "bad_width"])
def test_sql_rejects_incomplete_or_invalid_stream(
    tmp_path: Path, sqlite_cli: str, corruption: str
) -> None:
    database = tmp_path / f"{corruption}.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO items VALUES (1, 'original')")
    writer = SqliteCsvImporter(database, {"items": ("id", "value")}, sqlite3=sqlite_cli)
    writer.write("items", (2, "candidate"))
    if corruption == "missing_trailer":
        original = writer._write_record
        writer._write_record = lambda record: (
            None if record[0] == "__codekg_stream_complete__" else original(record)
        )
    elif corruption == "bad_count":
        writer.counts["items"] = 0
    elif corruption == "unknown_tag":
        writer._write_record(("not_a_target", "[]"))
    else:
        writer._write_record(("items", "[3]"))

    with pytest.raises(SqliteCliImportError):
        writer.finish()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM items ORDER BY id").fetchall() == [(1, "original")]


def test_import_warning_prevents_merge_even_when_counts_match(
    tmp_path: Path, sqlite_cli: str
) -> None:
    database = tmp_path / "extra-field.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('original')")
    writer = SqliteCsvImporter(database, {"items": ("value",)}, sqlite3=sqlite_cli)
    original = writer._write_record

    def extra_csv_field(record):
        if record[0] == "items":
            writer._csv.writerow((*record, "unexpected-third-column"))
        else:
            original(record)

    writer._write_record = extra_csv_field
    writer.write("items", ("candidate",))
    with pytest.raises(SqliteCliImportError, match="stderr before the import-ready barrier"):
        writer.finish()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM items").fetchall() == [("original",)]


def test_monitor_failure_terminates_blocked_child_and_cleans_dump(
    tmp_path: Path, sqlite_cli: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "monitor-failure.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('original')")
    writer = SqliteCsvImporter(database, {"items": ("value",)}, sqlite3=sqlite_cli)
    writer.write("items", ("candidate",))
    workdir = writer._workdir

    def fail_read(*args, **kwargs):
        raise OSError("injected output read failure")

    monkeypatch.setattr(writer, "_read_stream", fail_read)
    results = []

    def finish_import():
        try:
            writer.finish()
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=finish_import)
    thread.start()
    try:
        thread.join(timeout=2)
        if thread.is_alive():
            process = writer._process
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            thread.join(timeout=2)
        assert not thread.is_alive(), "finish hung after monitor I/O failure"
        assert len(results) == 1
        assert isinstance(results[0], SqliteCliImportError)
        assert "injected output read failure" in str(results[0])
        assert writer._process is not None and writer._process.poll() is not None
        assert not workdir.exists()
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM items").fetchall() == [("original",)]
    finally:
        if thread.is_alive():
            process = writer._process
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            thread.join(timeout=2)


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("cat >/dev/null; echo diagnostic >&2; exit 7", "exit code 7"),
        ("cat >/dev/null; echo unexpected", "unexpected stdout"),
        ("cat >/dev/null; echo diagnostic >&2", "unexpected stderr"),
    ],
)
def test_process_failures_are_reported_and_do_not_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, message: str
) -> None:
    executable = tmp_path / "sqlite3-fake"
    executable.write_text(f"#!/bin/sh\n{script}\n")
    executable.chmod(executable.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    database = tmp_path / "fake.sqlite"
    writer = SqliteCsvImporter(database, {"items": ("value",)}, sqlite3=executable.name)
    writer.write("items", ("value",))
    with pytest.raises(SqliteCliImportError, match=message):
        writer.finish()
