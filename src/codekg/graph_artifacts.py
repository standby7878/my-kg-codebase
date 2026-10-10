"""Bootstrap-only Neo4j generation marker for graph registry verification."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_generation_marker(client, graph_id: str, generation_id: str) -> None:
    """Create (or verify) the immutable marker after importing a generation.

    This is an import/bootstrap operation, never a query-time mutation. The
    dedicated Neo4j instance is expected to contain exactly one marker.
    """
    if not graph_id or not generation_id:
        raise ValueError("graph_id and generation_id must be non-empty")
    existing = client.execute_read(
        "MATCH (m:CodeKGGeneration) RETURN m.graph_id AS graph_id, "
        "m.generation_id AS generation_id LIMIT 2",
        max_rows=2,
        operation="inspect_generation_marker",
    )
    if any(row.get("graph_id") != graph_id for row in existing):
        raise RuntimeError("Neo4j instance already carries a marker for another graph")
    if existing and any(row.get("generation_id") != generation_id for row in existing):
        raise RuntimeError("Neo4j instance already carries a different generation marker")
    rows = client.execute_write(
        "MERGE (m:CodeKGGeneration {graph_id:$graph_id}) "
        "ON CREATE SET m.generation_id=$generation_id "
        "RETURN m.graph_id AS graph_id,m.generation_id AS generation_id",
        {"graph_id": graph_id, "generation_id": generation_id},
        operation="write_generation_marker",
    )
    if len(rows) != 1 or rows[0].get("generation_id") != generation_id:
        raise RuntimeError("Neo4j instance already carries a different generation marker")


def freeze_generation_manifest(source_manifest: str | Path, destination: str | Path) -> Path:
    """Copy an exported top-level manifest into a generation-pinned descriptor.

    Export paths are rebased from the mutable export root to the descriptor's
    directory. The corpus registry and all listed graph/search/bulk artifacts
    therefore continue to point at the exact exported generation after the
    export root's ``manifest.json`` is replaced by a later build.
    Existing destinations are immutable: identical content is idempotent,
    conflicting content is rejected.
    """
    source, target = Path(source_manifest).resolve(), Path(destination).resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read source corpus manifest: {exc}") from exc
    if not isinstance(document, dict) or document.get("kind") != "codekg-corpus":
        raise ValueError("source is not a codekg-corpus manifest")
    root = source.parent

    def rebase(value: str) -> str:
        path = Path(value)
        resolved = path if path.is_absolute() else (root / path).resolve()
        try:
            return resolved.relative_to(target.parent).as_posix()
        except ValueError:
            return os.path.relpath(resolved, target.parent)

    if isinstance(document.get("registry"), str):
        document["registry"] = rebase(document["registry"])
    for section in ("nodes", "relationships"):
        values = document.get(section, {})
        if isinstance(values, dict):
            for entry in values.values():
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("file"), str):
                    entry["file"] = rebase(entry["file"])
                if isinstance(entry.get("files"), list):
                    entry["files"] = [
                        rebase(path) if isinstance(path, str) else path for path in entry["files"]
                    ]
    search = document.get("search_stage")
    if isinstance(search, dict) and isinstance(search.get("file"), str):
        search["file"] = rebase(search["file"])
    for snapshot in document.get("snapshots", []):
        if isinstance(snapshot, dict) and isinstance(snapshot.get("bulk_manifest"), str):
            snapshot["bulk_manifest"] = rebase(snapshot["bulk_manifest"])
    document["output_dir"] = "."
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() == payload:
            return target
        raise FileExistsError(f"immutable generation manifest already exists: {target}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target
