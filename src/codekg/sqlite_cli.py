"""Sorted, set-based ingestion through the SQLite command-line client.

Rows are serialized to a private two-column CSV dump (table tag, JSON array),
stably sorted by tag, then imported by ``sqlite3`` without per-row bindings.
"""

from __future__ import annotations

import contextlib
import csv
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


class SqliteCliImportError(RuntimeError):
    """A SQLite CLI stream could not be safely imported."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OUTPUT_LIMIT = 64 * 1024
_TRAILER = "__codekg_stream_complete__"
_READY = "CODEKG_SQLITE_IMPORT_READY"
_SUCCESS = "CODEKG_SQLITE_IMPORT_OK"


class SqliteCsvImporter:
    """Stream tagged rows into one SQLite database using one CLI child.

    ``tables`` maps target table names to ordered target column names. Target
    writes occur only after a validated completion trailer, in one SQL
    transaction owned by the child process. ``replace_tables`` selects
    ``INSERT OR REPLACE`` conflict behavior per row; it does not clear a table.
    JSON serialization preserves strings exactly, but finite floating-point
    values are not guaranteed to retain their bit pattern through JSON/SQLite
    numeric conversion.
    """

    def __init__(
        self,
        database: str | Path,
        tables: Mapping[str, Sequence[str]],
        *,
        replace_tables: Iterable[str] = (),
        ignore_tables: Iterable[str] = (),
        sqlite3: str = "sqlite3",
        sort: str = "sort",
    ) -> None:
        self.database = str(Path(database).resolve())
        self.database_parent = Path(self.database).parent
        self.tables = {name: tuple(columns) for name, columns in tables.items()}
        self.replace_tables = frozenset(replace_tables)
        self.ignore_tables = frozenset(ignore_tables)
        self.sqlite3 = sqlite3
        self.sort = sort
        self._sqlite_executable = shutil.which(sqlite3)
        if self._sqlite_executable is None:
            raise SqliteCliImportError(
                f"SQLite CLI executable {sqlite3!r} was not found; no fallback is available"
            )
        self.counts = {name: 0 for name in self.tables}
        self._closed = False
        self._validate_spec()
        self.database_parent.mkdir(parents=True, exist_ok=True)
        self._temporary = None
        self._csv_file = None
        self._temporary = tempfile.TemporaryDirectory(
            prefix="codekg-sqlite-stream-", dir=self.database_parent
        )
        try:
            self._workdir = Path(self._temporary.name)
            self._raw_csv_path = self._workdir / "raw.csv"
            self._sorted_csv_path = self._workdir / "sorted.csv"
            # Builtin open avoids colliding with callers instrumenting Path.open.
            self._file_stack = contextlib.ExitStack()
            self._csv_file = self._file_stack.enter_context(
                open(self._raw_csv_path, "w", encoding="utf-8", newline="")  # noqa: SIM115
            )
            self._csv = csv.writer(self._csv_file, lineterminator="\n")
        except BaseException:
            with contextlib.suppress(BaseException):
                if self._csv_file is not None:
                    self._csv_file.close()
            with contextlib.suppress(BaseException):
                self._temporary.cleanup()
            raise
        self._process = None
        self._control = None
        self._monitor = None
        self._text_stdin = None
        self._ready_event = None
        self._monitor_failure = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._monitor_error = None
        self._ready_seen = False
        self._stdout_seen = 0
        self._ready_probe = bytearray()

    def _start_sqlite(self) -> None:
        executable = self._sqlite_executable
        control_read, control_write = os.pipe()
        self.command = [
            executable,
            "-batch",
            "-bail",
            self.database,
            self._setup_sql(),
            ".import --csv sorted.csv _codekg_stream_stage",
            "CREATE INDEX _codekg_stream_stage_tag_idx ON _codekg_stream_stage(tag);",
            f".print {_READY}",
            f".read /dev/fd/{control_read}",
        ]
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(control_read,),
                cwd=self._workdir,
            )
        except OSError as error:
            os.close(control_read)
            os.close(control_write)
            raise SqliteCliImportError(f"could not start SQLite CLI: {error}") from error
        os.close(control_read)
        self._control = os.fdopen(control_write, "wb")
        self._ready_event = threading.Event()
        self._stdout_seen = 0
        self._ready_probe = bytearray()
        self._monitor = threading.Thread(target=self._monitor_output, daemon=True)
        self._monitor.start()

    def _validate_spec(self) -> None:
        if not self.tables:
            raise ValueError("at least one target table is required")
        for table, columns in self.tables.items():
            if table == _TRAILER:
                raise ValueError(f"target table name is reserved: {table!r}")
            _check_identifier(table, "table")
            if not columns:
                raise ValueError(f"target table {table!r} needs at least one column")
            if len(columns) != len(set(columns)):
                raise ValueError(f"duplicate target columns for table {table!r}")
            for column in columns:
                _check_identifier(column, "column")
        unknown = self.replace_tables - self.tables.keys()
        if unknown:
            raise ValueError(f"replace_tables contains unknown tables: {sorted(unknown)!r}")
        unknown = self.ignore_tables - self.tables.keys()
        if unknown:
            raise ValueError(f"ignore_tables contains unknown tables: {sorted(unknown)!r}")
        if self.replace_tables & self.ignore_tables:
            raise ValueError("a table cannot use multiple conflict handling modes")

    def write(self, table: str, row: Sequence[Any]) -> None:
        if self._closed:
            raise ValueError("cannot write to a closed SQLite importer")
        try:
            if table not in self.tables:
                raise ValueError(f"unknown SQLite target table: {table!r}")
            if isinstance(row, (str, bytes, bytearray)) or len(row) != len(self.tables[table]):
                raise ValueError(
                    f"row for {table!r} has width {len(row) if hasattr(row, '__len__') else '?'}; "
                    f"expected {len(self.tables[table])}"
                )
            payload = json.dumps(
                [_normalize_scalar(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            self._write_record((table, payload))
            self.counts[table] += 1
        except BaseException as error:
            self.abort()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, ValueError) and not isinstance(error, (TypeError, OSError)):
                raise
            raise SqliteCliImportError(
                f"failed while serializing SQLite import row: {error}"
            ) from error

    def finish(self) -> dict[str, int]:
        if self._closed:
            raise ValueError("SQLite importer is already closed")
        try:
            trailer = json.dumps(self.counts, sort_keys=True, separators=(",", ":"))
            self._write_record((_TRAILER, trailer))
            self._csv_file.flush()
            self._csv_file.close()
            self._sort_dump()
            self._raw_csv_path.unlink(missing_ok=True)
            self._start_sqlite()
            # Import duration scales with the sorted dump; monitor EOF/error
            # paths release this barrier without an arbitrary file-size timeout.
            self._ready_event.wait()
            if self._monitor_failure is not None:
                raise SqliteCliImportError(self._monitor_failure)
            if not self._ready_seen:
                returncode = self._process.wait()
                self._monitor.join()
                stdout = bytes(self._stdout).decode("utf-8", "replace").strip()
                stderr = bytes(self._stderr).decode("utf-8", "replace").strip()
                if returncode:
                    detail = stderr or stdout or "no diagnostics"
                    raise SqliteCliImportError(
                        f"SQLite CLI exited before the import-ready barrier with "
                        f"exit code {returncode}: {detail}"
                    )
                if stderr:
                    raise SqliteCliImportError(
                        f"SQLite CLI emitted unexpected stderr before READY: {stderr}"
                    )
                if stdout:
                    raise SqliteCliImportError(
                        f"SQLite CLI emitted unexpected stdout before READY: {stdout}"
                    )
                raise SqliteCliImportError(self._monitor_error or "SQLite CLI exited before READY")
            if self._monitor_error is not None:
                raise SqliteCliImportError(self._monitor_error)
            self._control.write(self._finish_sql().encode("utf-8"))
            self._control.write(f"\n.print {_SUCCESS}\n".encode("ascii"))
            self._control.flush()
            self._control.close()
            returncode = self._process.wait()
            self._monitor.join()
            self._close_output_pipes()
            self._closed = True
            stdout = bytes(self._stdout).decode("utf-8", "replace")
            stderr = bytes(self._stderr).decode("utf-8", "replace")
            if returncode != 0:
                detail = stderr.strip() or stdout.strip() or "no diagnostics"
                raise SqliteCliImportError(
                    f"SQLite CLI import failed with exit code {returncode}: {detail}"
                )
            if stderr:
                raise SqliteCliImportError(
                    f"SQLite CLI emitted unexpected stderr: {stderr.strip()}"
                )
            if stdout.encode("utf-8") != f"{_READY}\n{_SUCCESS}\n".encode("ascii"):
                raise SqliteCliImportError(
                    f"SQLite CLI emitted unexpected stdout: {stdout.strip()}"
                )
            self._cleanup_temporary()
            return dict(self.counts)
        except BaseException:
            self._stop_child()
            self._closed = True
            self._cleanup_temporary()
            raise

    def abort(self) -> None:
        if self._closed:
            return
        self._stop_child()
        self._closed = True
        self._cleanup_temporary()

    def _stop_child(self) -> None:
        process = getattr(self, "_process", None)
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except OSError:
                with contextlib.suppress(OSError):
                    process.kill()
                process.wait()
        control = getattr(self, "_control", None)
        if control is not None and not control.closed:
            with contextlib.suppress(OSError, ValueError):
                control.close()
        monitor = getattr(self, "_monitor", None)
        if monitor is not None:
            monitor.join(timeout=2)
        self._close_output_pipes()

    def _cleanup_temporary(self) -> None:
        csv_file = getattr(self, "_csv_file", None)
        if csv_file is not None and not csv_file.closed:
            with contextlib.suppress(BaseException):
                csv_file.close()
        file_stack = getattr(self, "_file_stack", None)
        if file_stack is not None:
            with contextlib.suppress(BaseException):
                file_stack.close()
        temporary = getattr(self, "_temporary", None)
        if temporary is not None:
            with contextlib.suppress(BaseException):
                temporary.cleanup()

    def _sort_dump(self) -> None:
        executable = shutil.which(self.sort)
        if executable is None:
            raise SqliteCliImportError(
                f"GNU sort executable {self.sort!r} was not found; no fallback is available"
            )
        self.sort_command = [
            executable,
            "--stable",
            "-t",
            ",",
            "-k1,1",
            "-S",
            "32M",
            "--parallel=1",
            "-T",
            str(self._workdir),
            "-o",
            str(self._sorted_csv_path),
            "--",
            str(self._raw_csv_path),
        ]
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            with tempfile.TemporaryFile(dir=self._workdir) as diagnostics:
                result = subprocess.run(
                    self.sort_command,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=diagnostics,
                    env=environment,
                    cwd=self._workdir,
                )
                diagnostics.seek(0, os.SEEK_END)
                size = diagnostics.tell()
                diagnostics.seek(max(0, size - _OUTPUT_LIMIT))
                diagnostic_tail = diagnostics.read(_OUTPUT_LIMIT).decode("utf-8", "replace")
        except OSError as error:
            raise SqliteCliImportError(f"could not execute GNU sort: {error}") from error
        if result.returncode != 0 or diagnostic_tail:
            detail = diagnostic_tail.strip() or "no diagnostics"
            raise SqliteCliImportError(
                f"GNU sort failed with exit code {result.returncode}: {detail}"
            )

    def _close_output_pipes(self) -> None:
        for name in ("stdout", "stderr"):
            stream = getattr(getattr(self, "_process", None), name, None)
            if stream is not None and not stream.closed:
                stream.close()

    def _monitor_output(self) -> None:
        selector = selectors.DefaultSelector()
        streams = {
            self._process.stdout: self._stdout,
            self._process.stderr: self._stderr,
        }
        try:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select():
                    data = self._read_stream(selector, key.fileobj, streams[key.fileobj])
                    if data is None:
                        continue
                    if key.fileobj is self._process.stdout and not self._ready_seen:
                        self._maybe_accept_ready(selector, streams)
            if not self._ready_seen:
                self._monitor_error = "SQLite CLI exited before the import-ready barrier"
                self._ready_event.set()
        except BaseException as error:
            self._monitor_error = f"SQLite CLI output monitor failed: {error}"
            self._monitor_failure = self._monitor_error
            self._ready_event.set()
        finally:
            selector.close()

    def _read_stream(self, selector, stream, retained: bytearray) -> bytes | None:
        try:
            chunk = os.read(stream.fileno(), 8192)
        except BlockingIOError:
            return b""
        if not chunk:
            with contextlib.suppress(KeyError):
                selector.unregister(stream)
            return None
        remaining = _OUTPUT_LIMIT - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if retained is self._stdout:
            self._stdout_seen += len(chunk)
            if not self._ready_seen:
                self._ready_probe.extend(chunk)
                del self._ready_probe[:-64]
        return chunk

    def _maybe_accept_ready(self, selector, streams) -> None:
        marker = f"{_READY}\n".encode("ascii")
        if marker not in self._ready_probe:
            return
        # READY is printed only after .import completes. Drain every output
        # byte already available before the parent is allowed to send SQL.
        while True:
            ready = selector.select(timeout=0)
            if not ready:
                break
            for key, _ in ready:
                self._read_stream(selector, key.fileobj, streams[key.fileobj])
        stdout = bytes(self._stdout)
        if self._stdout_seen != len(marker) or stdout != marker:
            self._monitor_error = (
                "SQLite CLI emitted unexpected output before the import-ready barrier"
            )
        elif self._stderr:
            self._monitor_error = (
                "SQLite CLI emitted stderr before the import-ready barrier: "
                + bytes(self._stderr).decode("utf-8", "replace").strip()
            )
        self._ready_seen = True
        self._ready_event.set()

    def _setup_sql(self) -> str:
        return (
            "PRAGMA cache_size=-8192;"
            "PRAGMA temp.cache_size=-8192;"
            "PRAGMA temp_store=FILE;"
            "CREATE TEMP TABLE _codekg_stream_stage(tag TEXT NOT NULL, payload TEXT NOT NULL);"
        )

    def _write_record(self, record: Sequence[str]) -> None:
        self._csv.writerow(record)

    def _finish_sql(self) -> str:
        checks = [
            "(SELECT COUNT(*) FROM _codekg_stream_stage WHERE tag='" + _TRAILER + "')=1",
            "(SELECT json_valid(payload) FROM _codekg_stream_stage WHERE tag='" + _TRAILER + "')=1",
            "(SELECT json_type(payload) FROM _codekg_stream_stage WHERE tag='"
            + _TRAILER
            + "')='object'",
            "(SELECT COUNT(*) FROM json_each((SELECT payload FROM _codekg_stream_stage WHERE tag='"
            + _TRAILER
            + "')))="
            + str(len(self.tables)),
        ]
        for table, columns in self.tables.items():
            qtable = _quote(table)
            qtag = _literal(table)
            trailer_subquery = f"(SELECT payload FROM _codekg_stream_stage WHERE tag='{_TRAILER}')"
            trailer_count = f"json_extract({trailer_subquery}, '$.{table}')"
            checks += [
                f"NOT EXISTS(SELECT 1 FROM _codekg_stream_stage WHERE tag={qtag} AND "
                f"(json_valid(payload)!=1 OR json_type(payload)!='array' OR "
                f"json_array_length(payload)!={len(columns)}))",
                f"COALESCE((SELECT COUNT(*) FROM _codekg_stream_stage "
                f"WHERE tag={qtag}),0)={trailer_count}",
                f"json_type({trailer_subquery}, '$.{table}')='integer'",
            ]
        allowed = ",".join(_literal(name) for name in (*self.tables, _TRAILER))
        checks.append(
            f"NOT EXISTS(SELECT 1 FROM _codekg_stream_stage WHERE tag NOT IN ({allowed}))"
        )
        sql = [
            "BEGIN IMMEDIATE;",
            "CREATE TEMP TABLE _codekg_assert(ok INTEGER NOT NULL CHECK(ok=1));",
            "INSERT INTO _codekg_assert VALUES (" + " AND ".join(checks) + ");",
        ]
        for table, columns in self.tables.items():
            qtable = _quote(table)
            qcolumns = ",".join(_quote(column) for column in columns)
            qtag = _literal(table)
            json_values = ",".join(
                f"json_extract(s.payload, '$[{index}]')" for index in range(len(columns))
            )
            conflict = (
                " OR REPLACE"
                if table in self.replace_tables
                else (" OR IGNORE" if table in self.ignore_tables else "")
            )
            sql.append(
                f"INSERT{conflict} INTO main.{qtable} ({qcolumns}) SELECT {json_values} "
                f"FROM _codekg_stream_stage AS s WHERE s.tag={qtag} ORDER BY s.rowid;"
            )
        sql.append("COMMIT;")
        return "\n".join(sql)

    def __enter__(self) -> SqliteCsvImporter:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.abort()


def import_rows(
    database: str | Path,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    replace: bool = False,
) -> int:
    """Import one iterable into typed target columns and return its row count."""
    with SqliteCsvImporter(
        database,
        {table: columns},
        replace_tables=(table,) if replace else (),
    ) as importer:
        for row in rows:
            importer.write(table, row)
        count = importer.counts[table]
    return count


def _check_identifier(value: str, kind: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQLite {kind} identifier: {value!r}")


def _normalize_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError("integer value is outside SQLite's signed 64-bit range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not supported by SQLite bindings")
        return value
    raise TypeError(f"unsupported SQLite binding value type: {type(value).__name__}")


def _quote(identifier: str) -> str:
    _check_identifier(identifier, "identifier")
    return f'"{identifier}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
