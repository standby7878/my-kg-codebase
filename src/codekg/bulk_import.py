"""Run a Neo4j offline import from a bulk-export manifest."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BulkImportError(ValueError):
    """The bulk-import input or command execution is invalid."""


@dataclass(frozen=True)
class BulkImportResult:
    """The command and process output from a completed bulk import."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def build_import_command(
    export: Any,
    *,
    database: str = "neo4j",
    neo4j_admin: str = "neo4j-admin",
    overwrite: bool = True,
) -> list[str]:
    """Build the ``neo4j-admin database import full`` command for an export."""

    if not isinstance(database, str) or not database:
        raise BulkImportError("database must be a non-empty string")
    if not isinstance(neo4j_admin, str) or not neo4j_admin:
        raise BulkImportError("neo4j_admin must be a non-empty string")

    nodes = _file_arguments(export, "node_files", "node")
    relationships = _file_arguments(export, "relationship_files", "relationship")
    if not nodes and not relationships:
        raise BulkImportError("bulk export contains no node or relationship files")

    command = [
        neo4j_admin,
        "database",
        "import",
        "full",
        database,
        "--id-type=string",
        "--multiline-fields=true",
    ]
    if overwrite:
        command.append("--overwrite-destination=true")
    command.extend(f"--nodes={label}={path}" for label, path in nodes)
    command.extend(f"--relationships={kind}={path}" for kind, path in relationships)
    return command


def load_bulk_export(manifest_path: Path) -> Any:
    """Load the exporter-owned manifest lazily to keep this module testable."""

    try:
        from codekg.bulk_export import load_bulk_export as loader
    except ImportError as exc:  # pragma: no cover - only during partial installs
        raise BulkImportError("bulk export support is unavailable") from exc
    return loader(manifest_path)


def run_bulk_import(
    manifest_path: Path,
    *,
    database: str = "neo4j",
    neo4j_admin: str = "neo4j-admin",
    runner: Callable[..., Any] = subprocess.run,
) -> BulkImportResult:
    """Load, validate, and execute an offline Neo4j database import."""

    path = Path(manifest_path)
    if not path.exists() or not path.is_file():
        raise BulkImportError(f"bulk export manifest does not exist: {path}")
    try:
        export = load_bulk_export(path)
        command = build_import_command(export, database=database, neo4j_admin=neo4j_admin)
    except BulkImportError:
        raise
    except Exception as exc:
        raise BulkImportError(f"invalid bulk export manifest: {path}: {exc}") from exc

    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise BulkImportError(f"could not execute neo4j-admin: {exc}") from exc

    stdout = _output_text(getattr(completed, "stdout", ""))
    stderr = _output_text(getattr(completed, "stderr", ""))
    returncode = int(getattr(completed, "returncode", 0))
    if returncode:
        detail = stderr.strip() or "no stderr output"
        raise BulkImportError(f"neo4j-admin import failed with exit code {returncode}: {detail}")
    return BulkImportResult(command, returncode, stdout, stderr)


def _file_arguments(export: Any, attribute: str, kind: str) -> list[tuple[str, str]]:
    raw = getattr(export, attribute, None)
    if raw is None:
        raise BulkImportError(f"bulk export is missing {attribute}")
    entries = raw.items() if isinstance(raw, Mapping) else raw
    if not isinstance(raw, Mapping) and (
        isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence)
    ):
        raise BulkImportError(f"{attribute} must be a mapping or sequence")

    result: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(raw, Mapping):
            label, file_path = entry
        elif isinstance(entry, Mapping):
            label = entry.get("label", entry.get("type"))
            file_path = entry.get("path", entry.get("file"))
        else:
            try:
                label, file_path = entry
            except (TypeError, ValueError) as exc:
                raise BulkImportError(f"invalid {kind} file entry: {entry!r}") from exc
        if not isinstance(label, str) or not label or "=" in label:
            raise BulkImportError(f"invalid {kind} label: {label!r}")
        candidate = Path(file_path) if isinstance(file_path, (str, Path)) else None
        if candidate is None or not candidate.is_file():
            raise BulkImportError(f"missing {kind} file for {label!r}: {file_path!r}")
        result.append((label, str(candidate)))
    return result


def _output_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))
