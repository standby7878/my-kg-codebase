"""Immutable, disk-backed lexical-search staging for bulk exports."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing
from pathlib import Path

from codekg.ir import RepositoryIR
from codekg.search_index import build_symbol_text, iter_callable_docs_from_repository
from codekg.zvec_store import SymbolDoc, fetch_symbol_docs

SEARCH_STAGE_VERSION = 1
VALIDATION_BATCH_SIZE = 1_000
VALIDATION_SAMPLE_LIMIT = 100
MAX_MARKDOWN_DESCRIPTION_CHARS = 256_000


def create_search_stage_from_registry(
    path: Path,
    registry_path: Path,
    root: Path,
    repo: RepositoryIR,
    *,
    markdown_paths: Iterable[Path] | None = None,
) -> int:
    """Create a search stage without reconstructing repository-wide IR."""

    partial, connection = _new_stage(path)
    registry = sqlite3.connect(
        f"file:{Path(registry_path).resolve()}?mode=ro&immutable=1", uri=True
    )
    try:
        connection.execute("INSERT INTO repositories VALUES (?, ?)", (repo.repo_name, repo.commit))
        connection.execute(
            "CREATE TABLE markdown ("
            "qname TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL)"
        )
        ordinal = 0
        from codekg.docs import chunk_docs
        from codekg.ingest import iter_markdown_files

        paths = iter_markdown_files(root) if markdown_paths is None else markdown_paths
        for markdown_path in paths:
            for chunk in chunk_docs([markdown_path], []):
                for qname in chunk.mentions:
                    known = registry.execute(
                        "SELECT 1 FROM symbols "
                        "WHERE qname = ? AND kind IN ('function', 'method') LIMIT 1",
                        (qname,),
                    ).fetchone()
                    if known is None:
                        continue
                    connection.execute(
                        "INSERT INTO markdown VALUES (?, ?, ?)",
                        (qname, ordinal, chunk.text),
                    )
                    ordinal += 1
        connection.execute("CREATE INDEX markdown_qname_idx ON markdown(qname, ordinal)")

        rows = registry.execute(
            "SELECT key, path, qname, kind, signature, start_line, end_line, "
            "name, docstring FROM symbols "
            "WHERE kind IN ('function', 'method') ORDER BY key"
        )
        count = 0
        for key, source_path, qname, kind, signature, start_line, end_line, name, docstring in rows:
            markdown = _bounded_markdown_descriptions(
                connection.execute(
                    "SELECT text FROM markdown WHERE qname = ? ORDER BY ordinal", (qname,)
                )
            )
            text = build_symbol_text(
                {
                    "name": name,
                    "qname": qname,
                    "signature": signature,
                    "docstring": docstring,
                },
                markdown,
            )
            _insert_doc(
                connection,
                SymbolDoc(
                    key=str(key),
                    repo=repo.repo_name,
                    commit=repo.commit,
                    path=str(source_path),
                    qname=str(qname),
                    kind=str(kind),
                    signature=str(signature),
                    start_line=int(start_line),
                    end_line=int(end_line),
                    text=text,
                ),
            )
            count += 1
        connection.execute("DROP TABLE markdown")
        _publish_stage(path, partial, connection, count)
        return count
    except Exception:
        _abort_stage(connection, partial)
        raise
    finally:
        registry.close()


def create_search_stage_from_repositories(path: Path, repos: Iterable[RepositoryIR]) -> int:
    """Create a combined stage from already-materialized legacy export inputs."""

    partial, connection = _new_stage(path)
    try:
        count = 0
        for repo in repos:
            connection.execute(
                "INSERT INTO repositories VALUES (?, ?)", (repo.repo_name, repo.commit)
            )
            for doc in iter_callable_docs_from_repository(repo):
                _insert_doc(connection, doc)
                count += 1
        _publish_stage(path, partial, connection, count)
        return count
    except Exception:
        _abort_stage(connection, partial)
        raise


def load_search_stage(manifest_path: Path) -> Path:
    """Resolve and validate the immutable search stage published by a manifest."""

    manifest_path = Path(manifest_path).resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = data.get("search_stage")
    if not isinstance(entry, dict):
        raise ValueError(f"bulk manifest has no search_stage: {manifest_path}")
    if entry.get("version") != SEARCH_STAGE_VERSION:
        raise ValueError(f"unsupported search stage version: {entry.get('version')!r}")
    file_value = entry.get("file")
    if not isinstance(file_value, str) or not file_value:
        raise ValueError("search_stage.file must be a non-empty relative path")
    if Path(file_value).is_absolute():
        raise ValueError("search_stage.file must be a relative path")
    stage_path = (manifest_path.parent / file_value).resolve()
    if not stage_path.is_relative_to(manifest_path.parent):
        raise ValueError("search stage must remain below the manifest directory")
    if not stage_path.is_file():
        raise ValueError(f"search stage does not exist: {stage_path}")
    with closing(_open_stage(stage_path)) as connection:
        version = _metadata_int(connection, "schema_version")
        metadata_count = _metadata_int(connection, "documents")
        count = int(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
    if version != SEARCH_STAGE_VERSION:
        raise ValueError(f"unsupported search stage schema: {version!r}")
    declared = entry.get("documents")
    if not isinstance(declared, int) or declared != count or metadata_count != count:
        raise ValueError(
            "search stage document count mismatch: "
            f"manifest={declared!r}, metadata={metadata_count}, actual={count}"
        )
    return stage_path


def validate_search_manifests(manifest_paths: Sequence[Path]) -> tuple[Path, ...]:
    """Validate every stage and reject cross-stage repository/key collisions."""

    stages = tuple(load_search_stage(path) for path in manifest_paths)
    check = sqlite3.connect("")
    check.execute("CREATE TABLE repositories (repo TEXT PRIMARY KEY, commit_value TEXT NOT NULL)")
    check.execute("CREATE TABLE keys (key TEXT PRIMARY KEY)")
    try:
        for stage in stages:
            with closing(_open_stage(stage)) as source:
                for repo, commit in source.execute(
                    "SELECT repo, commit_value FROM repositories ORDER BY repo"
                ):
                    try:
                        check.execute("INSERT INTO repositories VALUES (?, ?)", (repo, commit))
                    except sqlite3.IntegrityError as error:
                        raise ValueError(f"duplicate staged repository: {repo}") from error
                for (key,) in source.execute("SELECT key FROM documents ORDER BY key"):
                    try:
                        check.execute("INSERT INTO keys VALUES (?)", (key,))
                    except sqlite3.IntegrityError as error:
                        raise ValueError(f"duplicate staged callable key: {key}") from error
        return stages
    finally:
        check.close()


def iter_search_stage_docs(path: Path) -> Iterator[SymbolDoc]:
    """Yield staged documents in deterministic key order."""

    connection = _open_stage(path)
    try:
        rows = connection.execute(
            "SELECT key, repo, commit_value, path, qname, kind, signature, "
            "start_line, end_line, text FROM documents ORDER BY key"
        )
        for row in rows:
            yield SymbolDoc(*row)
    finally:
        connection.close()


def validate_staged_search(
    stages: Sequence[Path],
    *,
    collection,
    client,
) -> dict[str, object]:
    """Compare staged, graph, and zvec keys with bounded Python memory."""

    state = sqlite3.connect("")
    state.executescript(
        """
        CREATE TABLE expected (
            key TEXT PRIMARY KEY, repo TEXT NOT NULL, commit_value TEXT NOT NULL
        );
        CREATE TABLE graph (
            key TEXT PRIMARY KEY, repo TEXT NOT NULL, commit_value TEXT NOT NULL
        );
        CREATE TABLE expected_repositories (
            key TEXT PRIMARY KEY, commit_value TEXT NOT NULL
        );
        CREATE TABLE graph_repositories (
            key TEXT PRIMARY KEY, commit_value TEXT NOT NULL
        );
        """
    )
    try:
        for stage in stages:
            state.execute("ATTACH DATABASE ? AS staged", (str(Path(stage).resolve()),))
            state.execute(
                "INSERT INTO expected SELECT key, repo, commit_value FROM staged.documents"
            )
            state.execute(
                "INSERT INTO expected_repositories "
                "SELECT repo, commit_value FROM staged.repositories"
            )
            state.commit()
            state.execute("DETACH DATABASE staged")

        missing_in_zvec_count = 0
        missing_in_zvec: list[str] = []
        for batch in _iter_graph_repository_batches(client):
            state.executemany(
                "INSERT INTO graph_repositories VALUES (?, ?)",
                ((str(row["repo"]), str(row["commit"])) for row in batch),
            )
        for batch in _iter_graph_callable_batches(client):
            state.executemany(
                "INSERT INTO graph VALUES (?, ?, ?)",
                ((str(row["key"]), str(row["repo"]), str(row["commit"])) for row in batch),
            )
            keys = {str(row["key"]) for row in batch}
            fetched = fetch_symbol_docs(collection, keys)
            for key in sorted(keys):
                if fetched.get(key, {}).get("key") == key:
                    continue
                missing_in_zvec_count += 1
                if len(missing_in_zvec) < VALIDATION_SAMPLE_LIMIT:
                    missing_in_zvec.append(key)
        state.commit()

        missing_in_graph_count, missing_in_graph = _difference(state, "expected", "graph")
        unexpected_in_graph_count, unexpected_in_graph = _difference(state, "graph", "expected")
        missing_repository_count, missing_repositories = _difference(
            state, "expected_repositories", "graph_repositories"
        )
        unexpected_repository_count, unexpected_repositories = _difference(
            state, "graph_repositories", "expected_repositories"
        )
        commit_mismatch_count = int(
            state.execute(
                "SELECT count(*) FROM ("
                "SELECT e.key, e.commit_value, g.commit_value "
                "FROM expected_repositories e JOIN graph_repositories g ON g.key = e.key "
                "WHERE e.commit_value <> g.commit_value)"
            ).fetchone()[0]
        )
        commit_mismatches = [
            {"repo": row[0], "expected": row[1], "actual": row[2]}
            for row in state.execute(
                "SELECT e.key, e.commit_value, g.commit_value "
                "FROM expected_repositories e JOIN graph_repositories g ON g.key = e.key "
                "WHERE e.commit_value <> g.commit_value "
                "ORDER BY e.key LIMIT ?",
                (VALIDATION_SAMPLE_LIMIT,),
            )
        ]
        expected_count = int(state.execute("SELECT count(*) FROM expected").fetchone()[0])
        graph_count = int(state.execute("SELECT count(*) FROM graph").fetchone()[0])
        return {
            "ok": not (
                missing_in_graph_count
                or unexpected_in_graph_count
                or missing_in_zvec_count
                or missing_repository_count
                or unexpected_repository_count
                or commit_mismatch_count
            ),
            "expected_callables": expected_count,
            "live_graph_callables": graph_count,
            "verified_callables": graph_count - missing_in_zvec_count,
            "missing_in_graph_count": missing_in_graph_count,
            "missing_in_graph": missing_in_graph,
            "unexpected_in_graph_count": unexpected_in_graph_count,
            "unexpected_in_graph": unexpected_in_graph,
            "missing_in_zvec_count": missing_in_zvec_count,
            "missing_in_zvec": missing_in_zvec,
            "missing_repository_count": missing_repository_count,
            "missing_repositories": missing_repositories,
            "unexpected_repository_count": unexpected_repository_count,
            "unexpected_repositories": unexpected_repositories,
            "commit_mismatch_count": commit_mismatch_count,
            "commit_mismatches": commit_mismatches,
        }
    finally:
        state.close()


def _iter_graph_callable_batches(client) -> Iterator[list[dict[str, object]]]:
    after = ""
    while True:
        rows = client.execute_read(
            """
            MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
            WHERE (s:Function OR s:Method) AND s.key > $after
            RETURN s.key AS key, r.repo_name AS repo, r.commit AS commit
            ORDER BY s.key
            LIMIT $limit
            """,
            {"after": after, "limit": VALIDATION_BATCH_SIZE},
            max_rows=VALIDATION_BATCH_SIZE,
            operation="validate staged callable keys",
        )
        if not rows:
            return
        yield rows
        next_after = str(rows[-1]["key"])
        if next_after <= after:
            raise RuntimeError("graph callable pagination did not advance")
        after = next_after


def _iter_graph_repository_batches(client) -> Iterator[list[dict[str, object]]]:
    after = ""
    while True:
        rows = client.execute_read(
            """
            MATCH (r:Repository)
            WHERE r.repo_name > $after
            RETURN r.repo_name AS repo, r.commit AS commit
            ORDER BY r.repo_name
            LIMIT $limit
            """,
            {"after": after, "limit": VALIDATION_BATCH_SIZE},
            max_rows=VALIDATION_BATCH_SIZE,
            operation="validate staged repositories",
        )
        if not rows:
            return
        yield rows
        next_after = str(rows[-1]["repo"])
        if next_after <= after:
            raise RuntimeError("graph repository pagination did not advance")
        after = next_after


def _new_stage(path: Path) -> tuple[Path, sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, partial_value = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    os.close(descriptor)
    partial = Path(partial_value)
    connection = sqlite3.connect(partial)
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE repositories (
            repo TEXT PRIMARY KEY,
            commit_value TEXT NOT NULL
        );
        CREATE TABLE documents (
            key TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            commit_value TEXT NOT NULL,
            path TEXT NOT NULL,
            qname TEXT NOT NULL,
            kind TEXT NOT NULL,
            signature TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO metadata VALUES ('schema_version', ?)", (str(SEARCH_STAGE_VERSION),)
    )
    return partial, connection


def _insert_doc(connection: sqlite3.Connection, doc: SymbolDoc) -> None:
    connection.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc.key,
            doc.repo,
            doc.commit,
            doc.path,
            doc.qname,
            doc.kind,
            doc.signature,
            doc.start_line,
            doc.end_line,
            doc.text,
        ),
    )


def _bounded_markdown_descriptions(rows: Iterable[tuple[object]]) -> Iterator[str]:
    """Yield deterministic Markdown enrichment within a per-document memory bound."""

    remaining = MAX_MARKDOWN_DESCRIPTION_CHARS
    for row in rows:
        if remaining <= 0:
            return
        text = str(row[0])
        if len(text) > remaining:
            yield text[:remaining]
            return
        yield text
        remaining -= len(text)


def _publish_stage(path: Path, partial: Path, connection: sqlite3.Connection, count: int) -> None:
    connection.execute("INSERT INTO metadata VALUES ('documents', ?)", (str(count),))
    connection.commit()
    connection.close()
    os.replace(partial, path)


def _abort_stage(connection: sqlite3.Connection, partial: Path) -> None:
    connection.close()
    partial.unlink(missing_ok=True)


def _open_stage(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro&immutable=1", uri=True)


def _metadata_int(connection: sqlite3.Connection, key: str) -> int:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise ValueError(f"search stage is missing metadata: {key}")
    return int(row[0])


def _difference(connection: sqlite3.Connection, left: str, right: str) -> tuple[int, list[str]]:
    predicate = (
        f"SELECT l.key FROM {left} l LEFT JOIN {right} r ON r.key = l.key WHERE r.key IS NULL"
    )
    count = int(connection.execute(f"SELECT count(*) FROM ({predicate})").fetchone()[0])
    sample = [
        str(row[0])
        for row in connection.execute(
            f"{predicate} ORDER BY l.key LIMIT ?", (VALIDATION_SAMPLE_LIMIT,)
        )
    ]
    return count, sample
