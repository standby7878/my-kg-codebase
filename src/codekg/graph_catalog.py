"""Bounded read-only catalog over the rich facts in an exported corpus.sqlite."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from codekg.native_ir import routine_target_kinds


class CatalogDeadline(TimeoutError):
    """A catalog query exceeded its shared operation deadline."""


class GraphCatalog:
    """Read-only fact access pinned to the corpus registry of one generation.

    Public contracts for federation: ``list_intents`` supports owner/key paging,
    ``reverse_intents`` finds qualified and unqualified schema/name postings,
    and ``routine_candidates`` returns raw routine facts in deterministic local,
    then ordered search-path precedence. Results include ``truncated`` and an
    overflow sentinel; callers must treat postings/candidates as candidates.
    """

    def __init__(self, handle_or_path, *, graph_id: str = "", generation_id: str = ""):
        if hasattr(handle_or_path, "corpus_path"):
            handle = handle_or_path
            self.path = Path(handle.corpus_path)
            self.graph_id = handle.graph_id
            self.generation_id = handle.generation_id
        else:
            self.path = Path(handle_or_path)
            self.graph_id = graph_id
            self.generation_id = generation_id

    @contextmanager
    def _connection(self, deadline: float | None = None):
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro&immutable=1"
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            yield db
        except sqlite3.OperationalError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise CatalogDeadline("catalog deadline exceeded") from exc
            raise
        finally:
            db.close()

    def list_intents(
        self,
        *,
        owner_path: str | None = None,
        owner_qname: str | None = None,
        local_key: str | None = None,
        after_key: str | None = None,
        limit: int = 100,
        deadline_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Page external/dynamic evidence owned by a file/function or exact fact key."""
        _limit(limit)
        if not any((owner_path, owner_qname, local_key)):
            raise ValueError("list_intents requires owner_path, owner_qname, or local_key")
        deadline = _deadline(deadline_seconds)
        with self._connection(deadline) as db:
            clauses, params = [], []
            if owner_path:
                clauses.append("e.path=?")
                params.append(owner_path)
            if owner_qname:
                clauses.append("json_extract(e.fact,'$.owner_qname')=?")
                params.append(owner_qname)
            if local_key:
                clauses.append(
                    "EXISTS (SELECT 1 FROM fact_keys fk WHERE fk.snapshot_alias=e.snapshot_alias "
                    "AND fk.path=e.path AND fk.table_name='evidence' AND fk.ordinal=e.ordinal "
                    "AND fk.key=?)"
                )
                params.append(local_key)
            if after_key:
                clauses.append("fk.key>?")
                params.append(after_key)
            rows, overflow = _read_rows(
                db,
                "SELECT e.snapshot_alias,e.path,e.ordinal,e.fact,fk.key AS local_key "
                "FROM evidence e JOIN fact_keys fk ON fk.snapshot_alias=e.snapshot_alias "
                "AND fk.path=e.path AND fk.table_name='evidence' AND fk.ordinal=e.ordinal "
                + (f"WHERE {' AND '.join(clauses)} " if clauses else "")
                + "ORDER BY fk.key LIMIT ?",
                (*params, limit + 1),
                limit,
            )
            facts = [_evidence_row(row) for row in rows]
            return _page(
                facts,
                overflow,
                facts[-1].get("local_key") if facts else None,
                self.graph_id,
                self.generation_id,
            )

    def get_intent(self, local_key: str, *, deadline_seconds: float = 2.0) -> dict[str, Any] | None:
        """Read one exact evidence fact by its generation-scoped fact key."""
        if not isinstance(local_key, str) or not local_key:
            raise ValueError("local_key must be a non-empty string")
        deadline = _deadline(deadline_seconds)
        with self._connection(deadline) as db:
            row = db.execute(
                "SELECT e.snapshot_alias,e.path,e.ordinal,e.fact,fk.key AS local_key "
                "FROM fact_keys fk JOIN evidence e ON e.snapshot_alias=fk.snapshot_alias "
                "AND e.path=fk.path AND e.ordinal=fk.ordinal "
                "WHERE fk.table_name='evidence' AND fk.key=? LIMIT 1",
                (local_key,),
            ).fetchone()
            return _evidence_row(dict(row)) if row else None

    def fact(self, local_key: str, *, deadline_seconds: float = 2.0) -> dict[str, Any] | None:
        """Return a routine/native/evidence fact only when its opaque key matches."""
        if not isinstance(local_key, str) or not local_key:
            raise ValueError("local_key must be a non-empty string")
        deadline = _deadline(deadline_seconds)
        with self._connection(deadline) as db:
            row = db.execute(
                "SELECT table_name,snapshot_alias,path,ordinal FROM fact_keys WHERE key=? LIMIT 1",
                (local_key,),
            ).fetchone()
            if row is None:
                return None
            table = row["table_name"]
            if table not in {"symbols", "routines", "evidence", "calls", "diagnostics"}:
                return None
            fact_row = db.execute(
                f"SELECT fact FROM {table} WHERE snapshot_alias=? AND path=? AND ordinal=? LIMIT 1",
                (row["snapshot_alias"], row["path"], row["ordinal"]),
            ).fetchone()
            if fact_row is None:
                return None
            value = json.loads(fact_row["fact"])
            value.update(
                {
                    "local_key": local_key,
                    "snapshot_alias": row["snapshot_alias"],
                    "path": row["path"],
                    "ordinal": row["ordinal"],
                    "fact_table": table,
                }
            )
            return value

    def python_owner(self, local_key: str, *, deadline_seconds: float = 2.0) -> dict | None:
        """Find an ordinary Python symbol using its exact generation-local key."""
        with self._connection(_deadline(deadline_seconds)) as db:
            row = db.execute(
                "SELECT snapshot_alias,key,path,qname,start_line FROM python_owners "
                "WHERE key=? LIMIT 1",
                (local_key,),
            ).fetchone()
            return dict(row) if row else None

    def intents_for_owner(
        self, local_key: str, *, limit: int = 100, deadline_seconds: float = 2.0
    ) -> dict:
        """Follow indexed ownership edges, not same-name evidence in unrelated scopes."""
        _limit(limit)
        with self._connection(_deadline(deadline_seconds)) as db:
            rows, overflow = _read_rows(
                db,
                "SELECT e.snapshot_alias,e.path,e.ordinal,e.fact,fk.key AS local_key "
                "FROM edges r JOIN fact_keys fk ON fk.key=r.target_key "
                "JOIN evidence e ON e.snapshot_alias=fk.snapshot_alias "
                "AND e.path=fk.path AND e.ordinal=fk.ordinal "
                "WHERE r.source_key=? AND r.kind='HAS_EVIDENCE' "
                "AND fk.table_name='evidence' ORDER BY fk.key LIMIT ?",
                (local_key, limit + 1),
                limit,
            )
            return _page(
                [_evidence_row(row) for row in rows],
                overflow,
                None,
                self.graph_id,
                self.generation_id,
            )

    def neighbors(
        self,
        local_key: str,
        *,
        direction: str = "outgoing",
        limit: int = 100,
        exact_only: bool = True,
        deadline_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Read capped indexed adjacency, preserving relation/status and source provenance."""
        _limit(limit)
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("direction must be outgoing or incoming")
        key_column = "source_key" if direction == "outgoing" else "target_key"
        deadline = _deadline(deadline_seconds)
        with self._connection(deadline) as db:
            clauses = [f"{key_column}=?"]
            params: list[Any] = [local_key]
            if exact_only:
                clauses.extend(("status='exact'", "coalesce(condition,'')=''"))
            rows, overflow = _read_rows(
                db,
                "SELECT source_key,target_key,kind,status,path,line,column_no,condition "
                f"FROM edges WHERE {' AND '.join(clauses)} "
                "ORDER BY kind,path,line,column_no,target_key LIMIT ?",
                (*params, limit + 1),
                limit,
            )
            return _page(rows, overflow, None, self.graph_id, self.generation_id)

    def reverse_intents(
        self,
        *,
        object_name: str,
        schema_name: str | None = None,
        after: tuple[str, str, int] | None = None,
        limit: int = 100,
        deadline_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Find exact qualified postings or all same-name unqualified postings."""
        _limit(limit)
        if not object_name:
            raise ValueError("object_name must be non-empty")
        deadline = _deadline(deadline_seconds)
        with self._connection(deadline) as db:
            clauses = [
                "json_extract(e.fact,'$.object_name')=?",
                "json_extract(e.fact,'$.dynamic')=0",
            ]
            params: list[Any] = [object_name]
            if schema_name is not None:
                clauses.append(
                    "(json_extract(e.fact,'$.schema_name')=? OR "
                    "json_extract(e.fact,'$.schema_name') IS NULL)"
                )
                params.append(schema_name)
            if after:
                clauses.append("(e.snapshot_alias,e.path,e.ordinal)>(?,?,?)")
                params.extend(after)
            rows, overflow = _read_rows(
                db,
                "SELECT e.snapshot_alias,e.path,e.ordinal,e.fact,fk.key AS local_key "
                "FROM evidence e LEFT JOIN fact_keys fk ON fk.snapshot_alias=e.snapshot_alias "
                "AND fk.path=e.path AND fk.table_name='evidence' AND fk.ordinal=e.ordinal "
                f"WHERE {' AND '.join(clauses)} ORDER BY e.snapshot_alias,e.path,e.ordinal LIMIT ?",
                (*params, limit + 1),
                limit,
            )
            facts = [_evidence_row(row) for row in rows]
            return _page(
                facts,
                overflow,
                tuple(facts[-1][k] for k in ("snapshot_alias", "path", "ordinal"))
                if facts
                else None,
                self.graph_id,
                self.generation_id,
            )

    def routine_candidates(
        self,
        *,
        name: str,
        arity: int | None = None,
        routine_kind: str | None = None,
        schema_name: str | None = None,
        local_aliases: tuple[str, ...] = (),
        search_path: tuple[str, ...] = (),
        visible_aliases: tuple[str, ...] = (),
        limit: int = 32,
        deadline_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Return raw routine metadata with own/local aliases before DB search_path.

        ``local_aliases`` and ``visible_aliases`` are explicit snapshot visibility
        inputs. No implicit all-extension resolution is performed.
        """
        _limit(limit)
        if not name:
            raise ValueError("name must be non-empty")
        alias_order = tuple(dict.fromkeys((*local_aliases, *visible_aliases)))
        if not alias_order:
            return _page([], False, None, self.graph_id, self.generation_id)
        deadline = _deadline(deadline_seconds)
        schema_rank = {schema: i for i, schema in enumerate(search_path)}
        with self._connection(deadline) as db:
            clauses = [
                "snapshot_alias IN (" + ",".join("?" for _ in alias_order) + ")",
                "json_extract(fact,'$.name')=?",
            ]
            params: list[Any] = [*alias_order, name]
            if schema_name is not None:
                clauses.append("json_extract(fact,'$.schema_name')=?")
                params.append(schema_name)
            elif search_path:
                clauses.append(
                    "(json_extract(fact,'$.schema_name') IN ("
                    + ",".join("?" for _ in search_path)
                    + ") OR json_extract(fact,'$.schema_name') IS NULL)"
                )
                params.extend(search_path)
            if routine_kind is not None:
                target_kinds = routine_target_kinds(routine_kind)
                if not target_kinds:
                    return _page([], False, None, self.graph_id, self.generation_id)
                clauses.append(
                    "json_extract(fact,'$.kind') IN (" + ",".join("?" for _ in target_kinds) + ")"
                )
                params.extend(target_kinds)
            if arity is not None:
                invocation_arity = (
                    "(json_extract(fact,'$.arity') + CASE WHEN "
                    "json_extract(fact,'$.kind')='procedure' THEN "
                    "coalesce(json_extract(fact,'$.out_arg_count'),0) ELSE 0 END)"
                )
                clauses.append(
                    f"({invocation_arity}=? OR "
                    "(json_extract(fact,'$.default_arg_count')>0 AND "
                    f"? BETWEEN {invocation_arity}-"
                    "json_extract(fact,'$.default_arg_count') "
                    f"AND {invocation_arity}) OR "
                    "(json_extract(fact,'$.variadic_arg_count')>0 AND "
                    f"? >= {invocation_arity}-json_extract(fact,'$.default_arg_count')-"
                    "json_extract(fact,'$.variadic_arg_count')))"
                )
                params.extend((arity, arity, arity))
            local_case = (
                "CASE snapshot_alias "
                + " ".join("WHEN ? THEN 0" for _ in local_aliases)
                + " ELSE 1 END"
                if local_aliases
                else "CAST(0 AS INTEGER)"
            )
            local_params = list(local_aliases)
            alias_case = (
                "CASE snapshot_alias "
                + " ".join("WHEN ? THEN ?" for _ in alias_order)
                + f" ELSE {len(alias_order)} END"
            )
            alias_params = [part for i, alias in enumerate(alias_order) for part in (alias, i)]
            if search_path:
                schema_case = (
                    "CASE json_extract(fact,'$.schema_name') "
                    + " ".join("WHEN ? THEN ?" for _ in search_path)
                    + f" ELSE {len(search_path)} END"
                )
                schema_params = [
                    part for schema in search_path for part in (schema, schema_rank[schema])
                ]
            else:
                schema_case, schema_params = "CAST(0 AS INTEGER)", []
            query = (
                "SELECT snapshot_alias,path,ordinal,fact FROM routines WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY {schema_case},{local_case},{alias_case},snapshot_alias,path,ordinal"
            )
            cursor = db.execute(query, (*params, *schema_params, *local_params, *alias_params))
            results, seen, overflow = [], set(), False
            examined = 0
            for row in cursor:
                examined += 1
                if examined > 10000:
                    overflow = True
                    break
                fact = json.loads(row["fact"])
                alias = row["snapshot_alias"]
                semantic = tuple(
                    fact.get(field)
                    for field in (
                        "schema_name",
                        "name",
                        "kind",
                        "signature",
                        "language",
                        "arity",
                        "library",
                        "default_arg_count",
                        "variadic_arg_count",
                        "condition",
                        "definition_hash",
                        "body_hash",
                        "entrypoint",
                        "return_type",
                    )
                ) + (alias,)
                if semantic in seen:
                    continue
                seen.add(semantic)
                if len(results) == limit:
                    overflow = True
                    break
                fact.update(
                    {
                        "snapshot_alias": alias,
                        "path": row["path"],
                        "ordinal": row["ordinal"],
                        "local_key": _fact_key(db, alias, row["path"], row["ordinal"], "routines"),
                    }
                )
                results.append(fact)
            return _page(results, overflow, None, self.graph_id, self.generation_id)


def _read_rows(db: sqlite3.Connection, sql: str, params: tuple, limit: int):
    cursor = db.execute(sql, params)
    output = []
    for row in cursor:
        if len(output) == limit:
            return output, True
        output.append(dict(row))
    return output, False


def _evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    fact = json.loads(row["fact"])
    fact.update(
        {
            "snapshot_alias": row["snapshot_alias"],
            "path": row["path"],
            "ordinal": row["ordinal"],
            "local_key": row.get("local_key"),
        }
    )
    return fact


def _fact_key(db: sqlite3.Connection, alias: str, path: str, ordinal: int, table: str):
    row = db.execute(
        "SELECT key FROM fact_keys WHERE snapshot_alias=? AND path=? "
        "AND table_name=? AND ordinal=? LIMIT 1",
        (alias, path, table, ordinal),
    ).fetchone()
    return row[0] if row else None


def _page(items: list, truncated: bool, continuation: Any, graph_id: str, generation_id: str):
    return {
        "items": items,
        "truncated": truncated,
        "reason": "candidate_overflow" if truncated else None,
        "continuation": continuation if truncated else None,
        "overflow_sentinel": {"kind": "overflow_sentinel"} if truncated else None,
        "graph_id": graph_id,
        "generation_id": generation_id,
    }


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1000:
        raise ValueError("limit must be between 1 and 1000")


def _deadline(seconds: float) -> float:
    if seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    return time.monotonic() + seconds
