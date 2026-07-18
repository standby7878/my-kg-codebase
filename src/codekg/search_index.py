from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from codekg.ir import RepositoryIR, SymbolIR
from codekg.loader import symbol_key
from codekg.neo4j_client import Neo4jClient, get_client
from codekg.zvec_store import SymbolDoc, fetch_symbol_docs

NAME_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\W]+")


def callable_docs_from_repository(repo: RepositoryIR) -> list[SymbolDoc]:
    """Build the one-per-callable zvec descriptions from the immutable IR."""

    docs: list[SymbolDoc] = []
    for file in repo.files:
        for symbol in file.symbols:
            if not _is_searchable_callable(symbol):
                continue
            docs.append(
                SymbolDoc(
                    key=symbol_key(repo, file.path, symbol.qname, symbol.start_line),
                    repo=repo.repo_name,
                    commit=repo.commit,
                    path=file.path,
                    qname=symbol.qname,
                    kind=symbol.kind,
                    signature=symbol.signature,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    text=build_symbol_text(
                        symbol,
                        repo.markdown_descriptions.get(symbol.qname, ()),
                    ),
                )
            )
    return docs


def validate_search_index_consistency(
    expected_docs: list[SymbolDoc],
    *,
    live_graph_keys: set[str],
    replaced_keys: set[str] = frozenset(),
    collection,
) -> dict[str, object]:
    """Validate graph/IR agreement and known keys by deterministic safe-id fetch.

    This intentionally does not attempt to enumerate zvec.  The store cannot
    reliably query all records without an FTS query, so an exhaustive orphan
    scan is unsupported.  Replacement liveness is checked against the exact
    graph callable keys that existed immediately before deletion.
    """

    expected_keys = {doc.key for doc in expected_docs}
    missing_in_graph = sorted(expected_keys - live_graph_keys)
    unexpected_in_graph = sorted(live_graph_keys - expected_keys)
    fetched_current = fetch_symbol_docs(collection, live_graph_keys)
    missing_in_zvec = sorted(
        key for key in live_graph_keys if fetched_current.get(key, {}).get("key") != key
    )
    deleted_keys = set(replaced_keys) - live_graph_keys
    fetched_replaced = fetch_symbol_docs(collection, deleted_keys)
    stale_after_replace = sorted(
        key for key in deleted_keys if fetched_replaced.get(key, {}).get("key") == key
    )
    return {
        "ok": not (
            missing_in_graph or unexpected_in_graph or missing_in_zvec or stale_after_replace
        ),
        "expected_callables": len(expected_keys),
        "live_graph_callables": len(live_graph_keys),
        "verified_callables": len(live_graph_keys) - len(missing_in_zvec),
        "missing_in_graph": missing_in_graph,
        "unexpected_in_graph": unexpected_in_graph,
        "missing_in_zvec": missing_in_zvec,
        "stale_after_replace": stale_after_replace,
    }


def iter_callable_rows(
    *,
    repo: str | None = None,
    client: Neo4jClient | None = None,
) -> list[dict[str, Any]]:
    db = client or get_client()
    return db.execute_read(
        """
        MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
        WHERE (s:Function OR s:Method)
          AND ($repo IS NULL OR r.repo_name = $repo)
        RETURN s.key AS key
        ORDER BY s.key
        """,
        {"repo": repo},
        max_rows=1_000_000,
    )


def build_symbol_text(
    symbol: SymbolIR | Mapping[str, object],
    markdown_descriptions: tuple[str, ...] | list[str] = (),
) -> str:
    """Create lexical text without reading source files after extraction."""

    name = _value(symbol, "name")
    qname = _value(symbol, "qname")
    signature = _value(symbol, "signature")
    docstring = _value(symbol, "docstring")
    return "\n".join(
        part
        for part in [
            name,
            normalize_name(name or qname),
            qname,
            signature,
            docstring,
            *markdown_descriptions,
        ]
        if part
    ).strip()


def normalize_name(value: str) -> str:
    tail = value.rsplit(".", maxsplit=1)[-1]
    words = [word.lower() for word in NAME_BOUNDARY_RE.split(tail) if word]
    return " ".join(words)


def _is_searchable_callable(symbol: SymbolIR) -> bool:
    return symbol.kind in {"function", "method"}


def _value(symbol: SymbolIR | Mapping[str, object], name: str) -> str:
    value = symbol.get(name) if isinstance(symbol, Mapping) else getattr(symbol, name)
    return str(value or "")
