from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ZVEC_PATH = "/data/zvec/codekg"
COLLECTION_NAME = "codekg"


class ZvecUnavailableError(RuntimeError):
    """Raised when zvec is not installed or a collection cannot be opened."""


@dataclass(frozen=True)
class SymbolDoc:
    id: str
    source: str
    repo: str
    commit: str
    path: str
    qname: str
    kind: str
    signature: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class DocChunkDoc:
    id: str
    source: str
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


def ensure_collection(path: str | None = None):
    zvec = _zvec()
    collection_path = path or default_path()
    try:
        return zvec.open(
            collection_path,
            option=zvec.CollectionOption(read_only=False, enable_mmap=True),
        )
    except Exception:
        Path(collection_path).parent.mkdir(parents=True, exist_ok=True)
        return zvec.create_and_open(
            path=collection_path,
            schema=_schema(zvec),
            option=zvec.CollectionOption(read_only=False, enable_mmap=True),
        )


def open_write(path: str | None = None):
    return ensure_collection(path)


def open_read(path: str | None = None):
    zvec = _zvec()
    try:
        return zvec.open(
            path or default_path(),
            option=zvec.CollectionOption(read_only=True, enable_mmap=True),
        )
    except Exception as exc:
        raise ZvecUnavailableError(str(exc)) from exc


def delete_repo(collection, repo: str) -> None:
    collection.delete_by_filter(f"repo = '{_filter_string(repo)}'")


def delete_ids(collection, ids: set[str]) -> None:
    for batch in _batches(sorted(ids), 100):
        quoted = ", ".join(f"'{_filter_string(item)}'" for item in batch)
        collection.delete_by_filter(f"key in ({quoted})")


def delete_source(collection, source: str) -> None:
    collection.delete_by_filter(f"source = '{_filter_string(source)}'")


def upsert_symbol_docs(collection, docs: list[SymbolDoc]) -> int:
    return _upsert_docs(collection, docs)


def upsert_doc_chunks(collection, docs: list[DocChunkDoc]) -> int:
    return _upsert_docs(collection, docs)


def _upsert_docs(collection, docs: list[SymbolDoc] | list[DocChunkDoc]) -> int:
    zvec = _zvec()
    rows = [
        zvec.Doc(
            id=doc.id,
            fields={
                "key": doc.id,
                "source": doc.source,
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
        for doc in docs
    ]
    if not rows:
        return 0
    statuses = collection.upsert(rows)
    if not isinstance(statuses, list):
        statuses = [statuses]
    failed = [status for status in statuses if not status.ok()]
    if failed:
        raise ZvecUnavailableError(f"zvec upsert failed for {len(failed)} document(s)")
    return len(rows)


def search_symbols(
    collection,
    query: str,
    *,
    repo: str | None = None,
    kind: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    _zvec()
    from zvec.model.param.query import Fts, Query

    filters = ["source = 'symbol'"]
    if repo:
        filters.append(f"repo = '{_filter_string(repo)}'")
    if kind:
        filters.append(f"kind = '{_filter_string(kind)}'")
    docs = collection.query(
        queries=Query(field_name="text", fts=Fts(match_string=query)),
        topk=max(1, min(int(limit), 500)),
        filter=" and ".join(filters),
        output_fields=[
            "source",
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
        {
            "id": doc.id,
            "score": doc.score,
            "fields": _doc_fields(doc),
        }
        for doc in docs
    ]


def search_docs(
    collection,
    query: str,
    *,
    repo: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    _zvec()
    from zvec.model.param.query import Fts, Query

    filters = ["source = 'doc'"]
    if repo:
        filters.append(f"repo = '{_filter_string(repo)}'")
    docs = collection.query(
        queries=Query(field_name="text", fts=Fts(match_string=query)),
        topk=max(1, min(int(limit), 500)),
        filter=" and ".join(filters),
        output_fields=[
            "source",
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
        {
            "id": doc.id,
            "score": doc.score,
            "fields": _doc_fields(doc),
        }
        for doc in docs
    ]


def list_symbol_ids(collection, *, repo: str | None = None, limit: int = 100000) -> set[str]:
    return _list_source_ids(collection, source="symbol", repo=repo, limit=limit)


def list_doc_ids(collection, *, repo: str | None = None, limit: int = 100000) -> set[str]:
    return _list_source_ids(collection, source="doc", repo=repo, limit=limit)


def _list_source_ids(
    collection,
    *,
    source: str,
    repo: str | None,
    limit: int,
) -> set[str]:
    filters = [f"source = '{_filter_string(source)}'"]
    if repo:
        filters.append(f"repo = '{_filter_string(repo)}'")
    docs = collection.query(
        topk=max(1, min(int(limit), 100000)),
        filter=" and ".join(filters),
        output_fields=["repo"],
        include_vector=False,
    )
    return {doc.id for doc in docs}


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
                "source",
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


def _doc_fields(doc) -> dict[str, Any]:
    if hasattr(doc, "field_names") and hasattr(doc, "field"):
        return {name: doc.field(name) for name in doc.field_names()}
    fields = getattr(doc, "fields", None)
    if isinstance(fields, dict):
        return dict(fields)
    if isinstance(doc, dict):
        return dict(doc)
    return {}
