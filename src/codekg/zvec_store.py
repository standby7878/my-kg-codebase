from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from codekg.logging_config import debug_event

DEFAULT_ZVEC_PATH = "/data/zvec/codekg"
COLLECTION_NAME = "codekg"
ZVEC_MAX_WRITE_BATCH_SIZE = 1024
logger = logging.getLogger(__name__)


class ZvecUnavailableError(RuntimeError):
    """Raised when the optional zvec package is not installed."""


@dataclass(frozen=True)
class SymbolDoc:
    """One lexical-search document for one Neo4j callable node."""

    key: str
    repo: str
    commit: str
    path: str
    qname: str
    kind: str
    signature: str
    start_line: int
    end_line: int
    text: str


def default_path() -> str:
    return os.getenv("CODEKG_ZVEC_PATH", DEFAULT_ZVEC_PATH)


def zvec_available() -> bool:
    try:
        _zvec()
    except ZvecUnavailableError:
        return False
    return True


def doc_id_for_key(key: str) -> str:
    """Derive a fixed-width zvec-safe ID from a Neo4j key.

    zvec limits document-id length, so the exact key is stored separately in
    the mandatory scalar ``key`` field and is always used for graph joins.
    """

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def ensure_collection(path: str | None = None):
    """Open a collection, creating it only when its path is absent.

    In particular, a corrupt or incompatible existing collection must not be
    replaced by an empty one.  Those failures deliberately propagate to the
    caller so an ingest cannot claim success with no searchable descriptions.
    """

    zvec = _zvec()
    collection_path = Path(path or default_path())
    option = zvec.CollectionOption(read_only=False, enable_mmap=True)
    if collection_path.exists():
        debug_event(logger, "zvec_collection_opening", exists=True)
        return zvec.open(str(collection_path), option=option)

    collection_path.parent.mkdir(parents=True, exist_ok=True)
    debug_event(logger, "zvec_collection_creating")
    return zvec.create_and_open(path=str(collection_path), schema=_schema(zvec), option=option)


def open_write(path: str | None = None):
    return ensure_collection(path)


def open_read(path: str | None = None):
    zvec = _zvec()
    debug_event(logger, "zvec_collection_opening", read_only=True)
    return zvec.open(
        path or default_path(),
        option=zvec.CollectionOption(read_only=True, enable_mmap=True),
    )


def delete_repo(collection, repo: str) -> None:
    debug_event(logger, "zvec_repository_delete_started")
    collection.delete_by_filter(f"repo = '{_filter_string(repo)}'")
    debug_event(logger, "zvec_repository_delete_completed")


def delete_repo_records(repo: str, *, zvec_path: str | None = None) -> None:
    """Delete a repository's descriptions before deleting its graph nodes."""

    collection = open_write(zvec_path)
    delete_repo(collection, repo)
    optimize_and_flush(collection)


def optimize_and_flush(collection) -> None:
    """Publish scalar and FTS mutations for a separate read-only process."""

    debug_event(logger, "zvec_flush_started")
    collection.optimize()
    collection.flush()
    debug_event(logger, "zvec_flush_completed")


def delete_keys(collection, keys: set[str]) -> None:
    for batch in _batches(sorted(keys), 100):
        quoted = ", ".join(f"'{_filter_string(key)}'" for key in batch)
        collection.delete_by_filter(f"key in ({quoted})")


def upsert_symbol_docs(collection, docs: Iterable[SymbolDoc]) -> int:
    zvec = _zvec()
    total = 0
    for docs_batch in _iter_batches(docs, ZVEC_MAX_WRITE_BATCH_SIZE):
        debug_event(logger, "zvec_upsert_batch_started", documents=len(docs_batch))
        rows = [
            zvec.Doc(
                id=doc_id_for_key(doc.key),
                fields={
                    "key": doc.key,
                    "repo": doc.repo,
                    "commit": doc.commit,
                    "path": doc.path,
                    "qname": doc.qname,
                    "kind": doc.kind,
                    "signature": doc.signature,
                    "start_line": int(doc.start_line),
                    "end_line": int(doc.end_line),
                    "text": doc.text,
                },
            )
            for doc in docs_batch
        ]
        statuses = collection.upsert(rows)
        if not isinstance(statuses, list):
            statuses = [statuses]
        failed = [status for status in statuses if not status.ok()]
        if failed:
            debug_event(logger, "zvec_upsert_batch_failed", documents=len(rows), failed=len(failed))
            raise RuntimeError(f"zvec upsert failed for {len(failed)} description record(s)")
        total += len(rows)
        debug_event(logger, "zvec_upsert_batch_completed", documents=len(rows), total=total)
    debug_event(logger, "zvec_upsert_completed", documents=total)
    return total


def search_symbols(
    collection,
    query: str,
    *,
    repo: str | None = None,
    commit: str | None = None,
    kind: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    _zvec()
    from zvec.model.param.query import Fts, Query

    filters = []
    if repo:
        filters.append(f"repo = '{_filter_string(repo)}'")
    if commit:
        filters.append(f"commit = '{_filter_string(commit)}'")
    if kind:
        filters.append(f"kind = '{_filter_string(kind)}'")
    docs = collection.query(
        queries=Query(field_name="text", fts=Fts(match_string=query)),
        topk=max(1, min(int(limit), 500)),
        filter=" and ".join(filters) or None,
        output_fields=[
            "key",
            "repo",
            "commit",
            "path",
            "qname",
            "kind",
            "signature",
            "start_line",
            "end_line",
            "text",
        ],
        include_vector=False,
    )
    return [
        {"key": str(_doc_fields(doc)["key"]), "score": doc.score, "fields": _doc_fields(doc)}
        for doc in docs
    ]


def fetch_symbol_docs(collection, keys: set[str]) -> dict[str, dict[str, Any]]:
    """Fetch requested descriptions by safe id, keyed by their Neo4j key.

    zvec 0.5.1 cannot enumerate all documents with an empty query.  Liveness
    therefore uses only deterministic ``fetch`` calls for known graph keys.
    """

    ids_by_key = {key: doc_id_for_key(key) for key in keys}
    if not ids_by_key:
        return {}
    fetched = collection.fetch(
        list(ids_by_key.values()),
        output_fields=["key"],
        include_vector=False,
    )
    return {
        key: _doc_fields(fetched[doc_id]) for key, doc_id in ids_by_key.items() if doc_id in fetched
    }


def _schema(zvec):
    return zvec.CollectionSchema(
        name=COLLECTION_NAME,
        fields=[
            zvec.FieldSchema(
                "key",
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                "repo",
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("commit", zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema("path", zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema("qname", zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(
                "kind",
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema("signature", zvec.DataType.STRING, nullable=True),
            zvec.FieldSchema("start_line", zvec.DataType.INT64, nullable=False),
            zvec.FieldSchema("end_line", zvec.DataType.INT64, nullable=False),
            zvec.FieldSchema(
                "text",
                zvec.DataType.STRING,
                nullable=False,
                index_param=zvec.FtsIndexParam(
                    tokenizer_name="standard",
                    filters=["lowercase"],
                ),
            ),
        ],
    )


def _zvec():
    try:
        import zvec
    except ImportError as exc:
        raise ZvecUnavailableError("zvec is not installed") from exc
    return zvec


def _filter_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _batches(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _iter_batches(values: Iterable[SymbolDoc], size: int) -> Iterator[list[SymbolDoc]]:
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def _doc_fields(doc) -> dict[str, Any]:
    if hasattr(doc, "field_names") and hasattr(doc, "field"):
        return {name: doc.field(name) for name in doc.field_names()}
    fields = getattr(doc, "fields", None)
    if isinstance(fields, dict):
        return dict(fields)
    if isinstance(doc, dict):
        return dict(doc)
    return {}
