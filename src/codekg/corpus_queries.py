"""Bounded read-only SQLite queries over an immutable corpus manifest."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_QUERY_BUDGET_SECONDS = 5.0


def _connect(registry: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    deadline = time.monotonic() + _QUERY_BUDGET_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return conn


def _raise_if_interrupted(error: sqlite3.OperationalError) -> None:
    if "interrupt" in str(error).lower():
        raise TimeoutError("corpus query exceeded its 5-second SQLite work budget") from error
    raise error


def _registry(manifest: str | Path) -> tuple[dict, Path]:
    path = Path(manifest).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    registry = Path(data["registry"])
    if not registry.is_absolute():
        registry = path.parent / registry
    registry = registry.resolve()
    try:
        registry.relative_to(path.parent.resolve())
    except ValueError as error:
        raise ValueError("corpus registry path escapes the manifest directory") from error
    if not registry.is_file():
        raise ValueError("corpus registry is missing")
    return data, registry


def snapshots(manifest: str | Path, *, limit: int = 100, offset: int = 0) -> list[dict]:
    _page(limit, offset)
    data, _ = _registry(manifest)
    return sorted(data["snapshots"], key=lambda row: row["alias"])[offset : offset + limit]


def search(
    manifest: str | Path,
    query: str,
    *,
    snapshot: str | None = None,
    kind: str = "all",
    limit: int = 20,
) -> list[dict]:
    _page(limit, 0)
    if not query.strip() or kind not in {"native", "routine", "all"}:
        raise ValueError("query must be non-empty and kind must be native, routine, or all")
    data, registry = _registry(manifest)
    aliases = {item["alias"] for item in data["snapshots"]}
    if snapshot and snapshot not in aliases:
        raise ValueError(f"unknown snapshot alias: {snapshot}")
    conn = _connect(registry)
    conn.row_factory = sqlite3.Row
    result = []
    tables = (
        ("symbols", "routines")
        if kind == "all"
        else (("symbols",) if kind == "native" else ("routines",))
    )
    try:
        for table in tables:
            sql = (
                f"SELECT k.key,s.snapshot_alias,s.path,s.fact FROM {table} s "
                "JOIN fact_keys k ON k.snapshot_alias=s.snapshot_alias "
                "AND k.path=s.path AND k.table_name=? AND k.ordinal=s.ordinal "
                "WHERE (instr(lower(coalesce(json_extract(s.fact,'$.name'),'')),lower(?))>0 "
                "OR instr(lower(coalesce(json_extract(s.fact,'$.signature'),'')),lower(?))>0)"
            )
            params: list[object] = [table, query, query]
            if snapshot:
                sql += " AND s.snapshot_alias=?"
                params.append(snapshot)
            sql += " ORDER BY s.snapshot_alias,s.path,s.ordinal LIMIT ?"
            params.append(limit - len(result))
            for row in conn.execute(sql, params):
                fact = json.loads(row["fact"])
                result.append(
                    {
                        "key": row["key"],
                        "kind": "native" if table == "symbols" else "routine",
                        "snapshot_alias": row["snapshot_alias"],
                        "path": row["path"],
                        **fact,
                    }
                )
                if len(result) >= limit:
                    return result
    except sqlite3.OperationalError as error:
        _raise_if_interrupted(error)
    finally:
        conn.close()
    return result


def evidence(
    manifest: str | Path, key: str, *, direction: str = "outgoing", limit: int = 50
) -> list[dict]:
    _page(limit, 0)
    if direction not in {"incoming", "outgoing"}:
        raise ValueError("direction must be incoming or outgoing")
    data, registry = _registry(manifest)
    conn = _connect(registry)
    try:
        column = "source_key" if direction == "outgoing" else "target_key"
        other = "target_key" if direction == "outgoing" else "source_key"
        rows = conn.execute(
            f"SELECT e.{column},e.{other},e.kind,e.status,e.path,e.line,e.column_no,e.condition "
            f"FROM edges e WHERE e.{column}=? ORDER BY e.kind,e.path,e.line LIMIT ?",
            (key, limit),
        ).fetchall()
        return [
            {
                "key": row[0],
                "related_key": row[1],
                "relationship": row[2],
                "status": row[3],
                "path": row[4],
                "line": row[5],
                "column": row[6],
                "condition": row[7],
            }
            for row in rows
        ]
    except sqlite3.OperationalError as error:
        _raise_if_interrupted(error)
    finally:
        conn.close()


def compare(
    manifest: str | Path, left: str, right: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    _page(limit, offset)
    data, registry = _registry(manifest)
    by_alias = {item["alias"]: item for item in data["snapshots"]}
    if left not in by_alias or right not in by_alias or left == right:
        raise ValueError("provide two distinct existing snapshot aliases")
    if by_alias[left]["logical_repo"] != by_alias[right]["logical_repo"]:
        raise ValueError("snapshots must share a logical_repo")
    conn = _connect(registry)
    try:
        coverage_counts = {left: 0, right: 0}
        has_diagnostics = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostics'"
        ).fetchone()
        if has_diagnostics:
            for alias in (left, right):
                coverage_counts[alias] = conn.execute(
                    "SELECT count(*) FROM diagnostics WHERE snapshot_alias=? AND "
                    "json_extract(fact,'$.category') IN "
                    "('file_too_large','unreadable_file','c_parse_error','c_missing_syntax',"
                    "'routine_sql_parse_error','unsupported_routine_language','catalog_record_unsupported')",
                    (alias,),
                ).fetchone()[0]
        # Disk-side grouping bounds Python state to one page.
        rows = conn.execute(
            """
            WITH l AS (
                 SELECT 'routine' kind,
                        coalesce(json_extract(fact,'$.language'),'') || ':' ||
                          coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
                          coalesce(json_extract(fact,'$.schema_name'),'') || '.' ||
                          json_extract(fact,'$.name') identity,
                        json_extract(fact,'$.signature') signature,
                        json_extract(fact,'$.return_type') return_type,
                        json_extract(fact,'$.body_hash') body_hash,
                        coalesce(json_extract(fact,'$.condition'),'') condition,
                        coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.variadic_arg_count'),0) definition_hash,
                        coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.language'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.return_type'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.variadic_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.kind'),'function') || char(31) ||
                          coalesce(json_extract(fact,'$.condition'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.definition_hash'),'') semantic
                 FROM routines WHERE snapshot_alias=?
                 UNION ALL
                 SELECT 'native',
                        'c:' || coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
                        CASE WHEN json_extract(fact,'$.static')
                          THEN path || ':' || json_extract(fact,'$.name')
                          ELSE json_extract(fact,'$.name') END,
                        json_extract(fact,'$.signature'),
                        NULL,
                        json_extract(fact,'$.body_hash'),
                        coalesce(json_extract(fact,'$.condition'),''),
                        NULL,
                        coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.condition'),'') semantic
                 FROM symbols s WHERE snapshot_alias=? AND (json_extract(fact,'$.declaration')=0
                   OR NOT EXISTS (SELECT 1 FROM symbols d WHERE d.snapshot_alias=s.snapshot_alias
                     AND json_extract(d.fact,'$.name')=json_extract(s.fact,'$.name')
                     AND json_extract(d.fact,'$.kind')=json_extract(s.fact,'$.kind')
                     AND json_extract(d.fact,'$.declaration')=0))),
                 r AS (
                 SELECT 'routine' kind,
                        coalesce(json_extract(fact,'$.language'),'') || ':' ||
                          coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
                          coalesce(json_extract(fact,'$.schema_name'),'') || '.' ||
                          json_extract(fact,'$.name') identity,
                        json_extract(fact,'$.signature') signature,
                        json_extract(fact,'$.return_type') return_type,
                        json_extract(fact,'$.body_hash') body_hash,
                        coalesce(json_extract(fact,'$.condition'),'') condition,
                        coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.variadic_arg_count'),0) definition_hash,
                        coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.language'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.return_type'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.variadic_arg_count'),0) || char(31) ||
                          coalesce(json_extract(fact,'$.library'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.kind'),'function') || char(31) ||
                          coalesce(json_extract(fact,'$.condition'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.definition_hash'),'') semantic
                 FROM routines WHERE snapshot_alias=?
                 UNION ALL
                 SELECT 'native',
                        'c:' || coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
                        CASE WHEN json_extract(fact,'$.static')
                          THEN path || ':' || json_extract(fact,'$.name')
                          ELSE json_extract(fact,'$.name') END,
                        json_extract(fact,'$.signature'),
                        NULL,
                        json_extract(fact,'$.body_hash'),
                        coalesce(json_extract(fact,'$.condition'),''),
                        NULL,
                        coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
                          coalesce(json_extract(fact,'$.condition'),'') semantic
                 FROM symbols s WHERE snapshot_alias=? AND (json_extract(fact,'$.declaration')=0
                   OR NOT EXISTS (SELECT 1 FROM symbols d WHERE d.snapshot_alias=s.snapshot_alias
                     AND json_extract(d.fact,'$.name')=json_extract(s.fact,'$.name')
                     AND json_extract(d.fact,'$.kind')=json_extract(s.fact,'$.kind')
                     AND json_extract(d.fact,'$.declaration')=0))),
            ls AS (
                 SELECT kind,identity,count(DISTINCT semantic) n,min(signature) signature,
                     min(return_type) return_type,min(body_hash) body_hash,
                     min(definition_hash) definition_hash,min(condition) condition
                 FROM l GROUP BY kind,identity),
            rs AS (
                 SELECT kind,identity,count(DISTINCT semantic) n,min(signature) signature,
                     min(return_type) return_type,min(body_hash) body_hash,
                     min(definition_hash) definition_hash,min(condition) condition
                 FROM r GROUP BY kind,identity),
            ids AS (SELECT kind,identity FROM ls UNION SELECT kind,identity FROM rs),
            changes AS (
              SELECT ids.kind,ids.identity,coalesce(ls.n,0) left_count,
                     coalesce(rs.n,0) right_count,ls.signature left_signature,
                     rs.signature right_signature,ls.return_type left_return,
                     rs.return_type right_return,ls.body_hash left_body,
                     rs.body_hash right_body,ls.definition_hash left_definition,
                       rs.definition_hash right_definition,
                       ls.condition left_condition,rs.condition right_condition,
                     CASE WHEN coalesce(ls.n,0)>1 OR coalesce(rs.n,0)>1 THEN 'ambiguous'
                       WHEN coalesce(ls.n,0)=0 THEN 'added'
                       WHEN coalesce(rs.n,0)=0 THEN 'removed'
                       WHEN ls.signature<>rs.signature OR coalesce(ls.return_type,'')<>
                         coalesce(rs.return_type,'') THEN 'signature_changed'
                       WHEN coalesce(ls.condition,'')<>coalesce(rs.condition,'')
                         THEN 'condition_changed'
                       WHEN coalesce(ls.body_hash,'')<>coalesce(rs.body_hash,'')
                         THEN 'body_changed'
                       WHEN coalesce(ls.definition_hash,'')<>coalesce(rs.definition_hash,'')
                         THEN 'definition_changed' ELSE 'unchanged' END change
              FROM ids LEFT JOIN ls USING(kind,identity)
                        LEFT JOIN rs USING(kind,identity))
            SELECT kind,identity,left_count,right_count,left_signature,
                   right_signature,left_body,right_body,change FROM changes
            WHERE change<>'unchanged' ORDER BY kind,identity LIMIT ? OFFSET ?
        """,
            (left, left, right, right, limit, offset),
        ).fetchall()
        output = []
        for kind, name, _lc, _rc, ls, rs, _lb, _rb, state in rows:
            if (coverage_counts[left] or coverage_counts[right]) and state in {"added", "removed"}:
                state = "ambiguous"
            output.append(
                {
                    "logical_id": name,
                    "kind": kind,
                    "change": state,
                    "left_signature": ls,
                    "right_signature": rs,
                    "caveat": (
                        "source-level comparison; incomplete coverage may hide facts "
                        f"(diagnostics: {left}={coverage_counts[left]}, "
                        f"{right}={coverage_counts[right]})"
                    ),
                }
            )
        return output
    except sqlite3.OperationalError as error:
        _raise_if_interrupted(error)
    finally:
        conn.close()


def trace(
    manifest: str | Path, from_key: str, to_key: str, *, max_depth: int = 6, limit: int = 5
) -> list[dict]:
    _page(limit, 10)
    if (
        not from_key
        or not to_key
        or isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or not 1 <= max_depth <= 8
    ):
        raise ValueError("provide keys and max_depth from 1 to 8")
    _, registry = _registry(manifest)
    conn = _connect(registry)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(origin,node,depth,visited,steps) AS (
              SELECT ?, ?, 0, json_array(?), json('[]')
              UNION ALL
              SELECT walk.origin,e.target_key,walk.depth+1,
                     json_insert(walk.visited,'$[#]',e.target_key),
                     json_insert(walk.steps,'$[#]',json_object(
                       'from',e.source_key,'to',e.target_key,'kind',e.kind,
                       'path',e.path,'line',e.line,'condition',e.condition))
              FROM walk JOIN edges e ON e.source_key=walk.node
              WHERE walk.depth < ? AND e.status='exact'
                AND coalesce(e.condition,'')=''
                AND NOT EXISTS (
                  SELECT 1 FROM json_each(walk.visited) visited
                  WHERE visited.value=e.target_key
                )
            )
              SELECT steps FROM walk WHERE node=? AND depth>0 LIMIT ?
            """,
            (from_key, from_key, from_key, max_depth, to_key, limit),
        ).fetchall()
        return [json.loads(row["steps"]) for row in rows]
    except sqlite3.OperationalError as error:
        _raise_if_interrupted(error)
    finally:
        conn.close()


def _page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
        raise ValueError("offset must be between 0 and 10000")
