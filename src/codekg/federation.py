"""Read-only, generation-pinned federation across independently served CodeKGs."""

from __future__ import annotations

import atexit
import time
from collections import OrderedDict, deque
from pathlib import Path
from threading import RLock
from typing import Any

from codekg.graph_catalog import CatalogDeadline
from codekg.graph_registry import EntityRef, GraphHandle, GraphRegistry, GraphRegistryError

_HANDLES: OrderedDict[tuple[str, str, str], GraphHandle] = OrderedDict()
_REGISTRY: GraphRegistry | None = None
_REGISTRY_PATH: str | None = None
_LOCK = RLock()
MAX_REVERSE_POSTINGS = 1000


def close_graph_handles() -> None:
    """Release cached drivers on process shutdown, never on in-flight eviction."""
    with _LOCK:
        handles = list(_HANDLES.values())
        _HANDLES.clear()
    for graph_handle in handles:
        graph_handle.close()


atexit.register(close_graph_handles)


def registry() -> GraphRegistry:
    """Load the process registry on first use; restart MCP to activate changes."""
    global _REGISTRY, _REGISTRY_PATH
    import os
    from pathlib import Path

    raw = os.environ.get("CODEKG_GRAPH_REGISTRY")
    if not raw:
        raise GraphRegistryError("graph registry is not configured")
    path = Path(raw).expanduser().resolve()
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = GraphRegistry.load(path)
            _REGISTRY_PATH = str(path)
        elif str(path) != _REGISTRY_PATH:
            raise GraphRegistryError(
                "registry is pinned for this process; restart MCP to activate changes"
            )
        return _REGISTRY


def handle(graph_id: str | None = None) -> GraphHandle:
    reg = registry()
    selected = graph_id or reg.default_application_graph
    if selected is None:
        raise GraphRegistryError("graph_id is required without a default application graph")
    try:
        spec = reg.graphs[selected]
    except KeyError as exc:
        raise GraphRegistryError(f"unknown graph id: {selected}") from exc
    key = (str(reg.path), selected, spec.generation.generation_id)
    with _LOCK:
        result = _HANDLES.get(key)
        if result is None:
            result = reg.handle(selected)
            _HANDLES[key] = result
        _HANDLES.move_to_end(key)
        return result


def list_knowledge_graphs() -> dict[str, Any]:
    reg = registry()
    return {
        "status": "ok",
        "default_application_graph": reg.default_application_graph,
        "graphs": reg.list_graphs(),
        "contexts": [
            {
                "id": c.id,
                "application_graph": c.application_graph,
                "application_generation": reg.graphs[c.application_graph].generation.generation_id,
                "database_graph": c.database_graph,
                "database_generation": reg.graphs[c.database_graph].generation.generation_id,
                "application_database": c.application_database,
                "visible_extensions": list(c.visible_extensions),
                "search_path": list(c.search_path),
            }
            for c in reg.contexts.values()
        ],
        "readiness": "metadata_only; backend generation verified on first query",
    }


def list_database_intents(
    *,
    graph_id: str | None = None,
    owner_path: str | None = None,
    owner_qname: str | None = None,
    evidence_key: str | None = None,
    after_key: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    app = _application_handle(graph_id)
    page = app.catalog.list_intents(
        owner_path=owner_path,
        owner_qname=owner_qname,
        local_key=evidence_key,
        after_key=after_key,
        limit=limit,
    )
    return {
        **page,
        "status": "ok",
        "graph_id": app.graph_id,
        "generation_id": app.generation_id,
        "coverage": "page is bounded; dynamic/unknown names are not reverse-indexed",
    }


def resolve_database_intent(
    evidence_ref: dict[str, str],
    *,
    context_id: str,
) -> dict[str, Any]:
    reg, context, app, db = _context_handles(context_id)
    try:
        ref = _entity_ref(evidence_ref)
        _validate_ref(ref, app)
        evidence = app.catalog.get_intent(ref.local_key)
        if evidence is None:
            return _status("not_found", app, db, context_id=context_id)
        return _resolve(evidence, context, app, db)
    except CatalogDeadline:
        return {"status": "deadline_exceeded"}
    except (ValueError, GraphRegistryError) as exc:
        return _status("invalid_reference", app, db, context_id=context_id, message=str(exc))


def trace_application_database_path(
    *,
    context_id: str,
    evidence_ref: dict[str, str] | None = None,
    owner_path: str | None = None,
    owner_qname: str | None = None,
    entry_ref: dict[str, str] | None = None,
    target_ref: dict[str, str] | None = None,
    database_target_key: str | None = None,
    max_depth: int = 8,
    limit: int = 5,
    deadline_seconds: float = 10.0,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + min(max(deadline_seconds, 0.05), 10.0)
    reg, context, app, db = _context_handles(context_id, deadline=deadline)
    if not 1 <= max_depth <= 32 or not 1 <= limit <= 5:
        return _status("invalid_arguments", app, db, context_id=context_id)
    if sum(bool(x) for x in (evidence_ref, owner_path, owner_qname, entry_ref)) != 1:
        return _status(
            "context_required",
            app,
            db,
            context_id=context_id,
            message="provide exactly one evidence_ref, owner_path, or owner_qname",
        )
    try:
        truncated = False
        budget = {"visited": 0, "edges": 0}
        if evidence_ref:
            ref = _entity_ref(evidence_ref)
            _validate_ref(ref, app)
            evidence = app.catalog.get_intent(ref.local_key, deadline_seconds=_remaining(deadline))
            intents = [evidence] if evidence else []
        else:
            intents = []
            if owner_path or owner_qname:
                page = app.catalog.list_intents(
                    owner_path=owner_path,
                    owner_qname=owner_qname,
                    limit=32,
                    deadline_seconds=_remaining(deadline),
                )
                intents = page["items"]
                truncated |= page["truncated"]
        owner = None
        reachable_owners = []
        if entry_ref:
            owner_ref = _entity_ref(entry_ref)
            _validate_ref(owner_ref, app)
            owner = app.catalog.python_owner(
                owner_ref.local_key, deadline_seconds=_remaining(deadline)
            )
            if owner is None:
                owner = app.catalog.fact(owner_ref.local_key, deadline_seconds=_remaining(deadline))
            if owner is None:
                return _status("not_found", app, db, context_id=context_id)
            reachable_owners, python_cut = _python_reachable(
                app, owner_ref.local_key, max_depth=max_depth, deadline=deadline, budget=budget
            )
            truncated |= python_cut
            for owner_path_info in reachable_owners:
                owner_fact = owner_path_info["owner"]
                page = app.catalog.intents_for_owner(
                    owner_fact["key"],
                    limit=32,
                    deadline_seconds=_remaining(deadline),
                )
                for item in page["items"]:
                    item["_call_chain"] = owner_path_info["call_chain"]
                    intents.append(item)
                truncated |= page["truncated"]
                if len(intents) >= 128:
                    intents = intents[:128]
                    truncated = True
                    break
            # Preserve deterministic unique evidence keys from overlapping call paths.
            intents = list({item["local_key"]: item for item in intents}.values())
        if target_ref:
            target_entity = _entity_ref(target_ref)
            _validate_ref(target_entity, db)
            if (
                db.catalog.fact(target_entity.local_key, deadline_seconds=_remaining(deadline))
                is None
            ):
                return _status("not_found", app, db, context_id=context_id)
            database_target_key = target_entity.local_key
        elif (
            database_target_key
            and db.catalog.fact(database_target_key, deadline_seconds=_remaining(deadline)) is None
        ):
            return _status("not_found", app, db, context_id=context_id)
        paths, unresolved = [], []
        for evidence in intents:
            if evidence is None or time.monotonic() >= deadline:
                truncated = True
                break
            resolved = _resolve(evidence, context, app, db, deadline_seconds=_remaining(deadline))
            if resolved["status"] != "exact":
                unresolved.append(resolved)
                continue
            for candidate in resolved["candidates"]:
                candidate_graph = db if candidate.get("graph_id") == db.graph_id else app
                if database_target_key and candidate_graph.graph_id != db.graph_id:
                    local_body = app.catalog.intents_for_owner(
                        candidate["local_key"],
                        limit=100,
                        deadline_seconds=_remaining(deadline),
                    )
                    truncated |= local_body["truncated"]
                    for body_evidence in local_body["items"]:
                        body_resolution = _resolve(
                            body_evidence,
                            context,
                            app,
                            db,
                            deadline_seconds=_remaining(deadline),
                        )
                        if body_resolution["status"] != "exact":
                            unresolved.append(body_resolution)
                            continue
                        for db_candidate in body_resolution["candidates"]:
                            if db_candidate["graph_id"] != db.graph_id:
                                continue
                            used_depth = (
                                len(evidence.get("_call_chain", [])) + int(owner is not None) + 3
                            )
                            remaining_depth = max_depth - used_depth
                            if remaining_depth < 0:
                                truncated = True
                                continue
                            dbpaths, cut = _bounded_paths(
                                db,
                                db_candidate["local_key"],
                                database_target_key,
                                remaining_depth,
                                deadline,
                                budget,
                            )
                            truncated |= cut
                            for dbpath in dbpaths:
                                path = _joined_path(
                                    body_evidence, db_candidate, dbpath, app, db, context
                                )
                                prefix = []
                                if owner:
                                    prefix.append(_source_segment(owner, app))
                                prefix.extend(
                                    {
                                        **edge,
                                        "graph_id": app.graph_id,
                                        "generation_id": app.generation_id,
                                        "relationship": "EXACT_CALLS",
                                    }
                                    for edge in evidence.get("_call_chain", [])
                                )
                                prefix.extend(
                                    [
                                        _source_segment(evidence, app),
                                        {
                                            **candidate,
                                            "relationship": "INVOKES_LOCAL_ROUTINE",
                                            "status": "exact",
                                        },
                                    ]
                                )
                                path["segments"] = prefix + path["segments"]
                                path["assumptions"].append(
                                    "app-local SQL routine body evidence was followed"
                                )
                                paths.append(path)
                    continue
                if database_target_key:
                    used_depth = len(evidence.get("_call_chain", [])) + int(owner is not None) + 1
                    remaining_depth = max_depth - used_depth
                    if remaining_depth < 0:
                        truncated = True
                        continue
                    dbpaths, cut = _bounded_paths(
                        candidate_graph,
                        candidate["local_key"],
                        database_target_key,
                        remaining_depth,
                        deadline,
                        budget,
                    )
                    truncated |= cut
                    for dbpath in dbpaths:
                        paths.append(
                            _joined_path(
                                evidence,
                                candidate,
                                dbpath,
                                app,
                                candidate_graph,
                                context,
                                owner=owner,
                                call_chain=evidence.get("_call_chain", []),
                            )
                        )
                else:
                    paths.append(
                        _joined_path(
                            evidence,
                            candidate,
                            {"nodes": [], "edges": []},
                            app,
                            candidate_graph,
                            context,
                            owner=owner,
                            call_chain=evidence.get("_call_chain", []),
                        )
                    )
                if len(paths) >= limit:
                    truncated = True
                    break
            if len(paths) >= limit:
                break
        if entry_ref and owner and paths:
            for path in paths:
                path["entry"] = _source_segment(owner, app)
                path["assumptions"].append("entry_ref is a caller-selected analysis entry")
        result = _status("ok" if paths else "unresolved", app, db, context_id=context_id)
        result.update(
            {
                "paths": paths[:limit],
                "unresolved": unresolved[:32],
                "truncated": truncated or len(intents) >= 32,
                "budgets": {
                    "max_depth": max_depth,
                    "visited": budget["visited"],
                    "edges": budget["edges"],
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                },
                "coverage": (
                    "one app/database boundary; callbacks and repeated crossings unsupported"
                ),
            }
        )
        return result
    except CatalogDeadline:
        return _status("deadline_exceeded", app, db, context_id=context_id)
    except (ValueError, GraphRegistryError) as exc:
        return _status("invalid_reference", app, db, context_id=context_id, message=str(exc))


def find_application_database_usages(
    *,
    context_id: str,
    target_ref: dict[str, str],
    limit: int = 100,
    max_depth: int = 8,
    continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + 10.0
    reg, context, app, db = _context_handles(context_id, deadline=deadline)
    try:
        if not 1 <= max_depth <= 32 or not 1 <= limit <= 1000:
            return _status("invalid_arguments", app, db, context_id=context_id)
        target = _entity_ref(target_ref)
        _validate_ref(target, db)
        if not db.catalog.fact(target.local_key, deadline_seconds=_remaining(deadline)):
            return _status("not_found", app, db, context_id=context_id)
        if continuation is not None:
            expected = {
                "context_id": context_id,
                "app_graph_id": app.graph_id,
                "app_generation_id": app.generation_id,
                "db_graph_id": db.graph_id,
                "db_generation_id": db.generation_id,
                "target_ref": {
                    "graph_id": target.graph_id,
                    "generation_id": target.generation_id,
                    "local_key": target.local_key,
                },
                "max_depth": max_depth,
            }
            if any(continuation.get(key) != value for key, value in expected.items()):
                return _status("invalid_cursor", app, db, context_id=context_id)
            if (
                not isinstance(continuation.get("name_index"), int)
                or continuation["name_index"] < 0
                or not isinstance(continuation.get("after"), list)
            ):
                return _status("invalid_cursor", app, db, context_id=context_id)
        queue = deque([(target.local_key, 0)])
        visited = {target.local_key}
        names = set()
        routine_keys = set()
        edges = 0
        truncated = False
        bfs_truncated = False
        while queue and len(visited) <= 1000 and edges < 10000 and time.monotonic() < deadline:
            key, depth = queue.popleft()
            fact = db.catalog.fact(key, deadline_seconds=_remaining(deadline))
            if fact and fact.get("name"):
                names.add(fact["name"])
            if depth >= max_depth:
                truncated = True
                bfs_truncated = True
                continue
            page = db.catalog.neighbors(
                key,
                direction="incoming",
                limit=100,
                deadline_seconds=_remaining(deadline),
            )
            truncated |= page["truncated"]
            bfs_truncated |= page["truncated"]
            for edge in page["items"]:
                edges += 1
                if edge["kind"] not in {"BINDS_TO_NATIVE", "CALLS_NATIVE", "INVOKES_ROUTINE"}:
                    continue
                if edge["source_key"] not in visited:
                    source_key = edge["source_key"]
                    visited.add(source_key)
                    routine_keys.add(source_key)
                    queue.append((source_key, depth + 1))
        if queue or time.monotonic() >= deadline:
            truncated = True
            bfs_truncated = True
        candidates, seen = [], set()
        ordered_names = sorted(names)
        name_index = continuation.get("name_index", 0) if continuation else 0
        after = tuple(continuation.get("after", ())) if continuation else ()
        next_state = None
        examined = 0
        work_cap = MAX_REVERSE_POSTINGS
        stopped = False
        while (
            name_index < len(ordered_names) and examined < work_cap and time.monotonic() < deadline
        ):
            name = ordered_names[name_index]
            posting_after = after if len(after) == 3 else None
            while examined < work_cap and time.monotonic() < deadline:
                page = app.catalog.reverse_intents(
                    object_name=name,
                    limit=min(100, work_cap - examined),
                    after=posting_after,
                    deadline_seconds=_remaining(deadline),
                )
                if not page["items"]:
                    break
                for item in page["items"]:
                    examined += 1
                    posting_after = (item["snapshot_alias"], item["path"], item["ordinal"])
                    if item["local_key"] in seen:
                        continue
                    seen.add(item["local_key"])
                    check = _resolve(item, context, app, db, deadline_seconds=_remaining(deadline))
                    resolved_keys = {
                        c["local_key"] for c in check["candidates"] if c["graph_id"] == db.graph_id
                    }
                    if check["status"] == "exact" and resolved_keys.intersection(
                        routine_keys | {target.local_key}
                    ):
                        candidates.append({"intent": item, "resolution": check})
                    elif check["status"] in {
                        "ambiguous",
                        "conditional",
                    } and resolved_keys.intersection(routine_keys | {target.local_key}):
                        candidates.append(
                            {"intent": item, "resolution": check, "candidate_only": True}
                        )
                    if len(candidates) >= limit:
                        more_in_page = item is not page["items"][-1]
                        more_after = bool(
                            page["truncated"] or more_in_page or name_index + 1 < len(ordered_names)
                        )
                        if more_after:
                            next_state = {"name_index": name_index, "after": list(posting_after)}
                        stopped = True
                        break
                if stopped:
                    break
                if not page["truncated"]:
                    break
                # Continue every page of this posting list before moving to the next name.
            if stopped:
                break
            if examined >= work_cap or time.monotonic() >= deadline:
                next_state = {"name_index": name_index, "after": list(posting_after or ())}
                break
            name_index += 1
            after = ()
        if name_index < len(ordered_names) and next_state is None and not stopped:
            next_state = {"name_index": name_index, "after": list(after)}
        truncated |= next_state is not None or bool(queue) or time.monotonic() >= deadline
        if bfs_truncated:
            # The posting cursor cannot resume an incomplete database ancestor walk.
            next_state = None
        if next_state is not None:
            next_state.update(
                {
                    "context_id": context_id,
                    "app_graph_id": app.graph_id,
                    "app_generation_id": app.generation_id,
                    "db_graph_id": db.graph_id,
                    "db_generation_id": db.generation_id,
                    "target_ref": {
                        "graph_id": target.graph_id,
                        "generation_id": target.generation_id,
                        "local_key": target.local_key,
                    },
                    "max_depth": max_depth,
                }
            )
        result = _status("ok", app, db, context_id=context_id)
        result.update(
            {
                "usages": candidates,
                "truncated": truncated,
                "continuation": next_state,
                "continuation_supported": not bfs_truncated,
                "coverage_complete": not bfs_truncated,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "coverage": (
                    "qualified/unqualified name postings re-resolved; "
                    "candidate postings are not proof"
                ),
            }
        )
        return result
    except CatalogDeadline:
        return _status("deadline_exceeded", app, db, context_id=context_id)
    except (ValueError, GraphRegistryError) as exc:
        return _status("invalid_reference", app, db, context_id=context_id, message=str(exc))


def compare_knowledge_graphs(
    *,
    left_graph_id: str,
    right_graph_id: str,
    left_alias: str,
    right_alias: str,
    limit: int = 100,
    offset: int = 0,
    deadline_seconds: float = 10.0,
) -> dict[str, Any]:
    try:
        left, right = handle(left_graph_id), handle(right_graph_id)
        if left.spec.kind != "database" or right.spec.kind != "database":
            raise ValueError("comparison requires two database graphs")
        left.verify_generation()
        right.verify_generation()
        rows = _compare_catalogs(
            left, right, left_alias, right_alias, limit, offset, deadline_seconds
        )
        result = _status("ok", left, right)
        result.update(
            {
                "left_graph_id": left.graph_id,
                "left_generation_id": left.generation_id,
                "right_graph_id": right.graph_id,
                "right_generation_id": right.generation_id,
                "changes": rows,
                "truncated": len(rows) == limit,
            }
        )
        return result
    except CatalogDeadline:
        return {"status": "deadline_exceeded"}
    except (ValueError, GraphRegistryError) as exc:
        return {"status": "invalid_graph_or_context", "message": str(exc)}


def _compare_catalogs(left, right, left_alias, right_alias, limit, offset, seconds):
    import sqlite3
    from urllib.parse import quote

    if (
        isinstance(limit, bool)
        or not 1 <= limit <= 100
        or isinstance(offset, bool)
        or not 0 <= offset <= 10000
    ):
        raise ValueError("limit must be 1..100 and offset 0..10000")
    manifests = [left.generation.manifest, right.generation.manifest]
    aliases = [{item["alias"]: item for item in manifest["snapshots"]} for manifest in manifests]
    if left_alias not in aliases[0] or right_alias not in aliases[1]:
        raise ValueError("aliases must identify snapshots in their selected graphs")
    if aliases[0][left_alias].get("logical_repo") != aliases[1][right_alias].get("logical_repo"):
        raise ValueError("snapshots must share a logical repository")
    left_generation_id = getattr(left.generation, "generation_id", None)
    right_generation_id = getattr(right.generation, "generation_id", None)
    if (
        left_alias == right_alias
        and left_generation_id is not None
        and left_generation_id == right_generation_id
        and Path(left.corpus_path).resolve() == Path(right.corpus_path).resolve()
    ):
        # Identical selected source scopes cannot differ. This is not ambiguity
        # analysis; cross-generation or cross-corpus comparisons still scan facts.
        return []
    conn = sqlite3.connect(f"file:{quote(str(left.corpus_path))}?mode=ro&immutable=1", uri=True)
    deadline = time.monotonic() + min(max(seconds, 0.05), 10.0)
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        conn.execute(
            "ATTACH DATABASE ? AS rhs",
            (f"file:{quote(str(right.corpus_path))}?mode=ro&immutable=1",),
        )
        query = """
        WITH facts(side,kind,identity,signature,return_type,body,definition,condition,semantic) AS (
          SELECT 0,'routine',coalesce(json_extract(fact,'$.language'),'') || ':' ||
            coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
            coalesce(json_extract(fact,'$.schema_name'),'') || '.' || json_extract(fact,'$.name'),
            json_extract(fact,'$.signature'),json_extract(fact,'$.return_type'),
            json_extract(fact,'$.body_hash'),
            coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.library'),'') || char(31) ||
              coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
              coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
              coalesce(json_extract(fact,'$.variadic_arg_count'),0),
            coalesce(json_extract(fact,'$.condition'),''),
            coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
              coalesce(json_extract(fact,'$.return_type'),'') || char(31) ||
              coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.condition'),'') || char(31) ||
              coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.library'),'') || char(31) ||
              coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
              coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
              coalesce(json_extract(fact,'$.variadic_arg_count'),0)
          FROM main.routines WHERE snapshot_alias=?
          UNION ALL
          SELECT 1,'routine',coalesce(json_extract(fact,'$.language'),'') || ':' ||
            coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
            coalesce(json_extract(fact,'$.schema_name'),'') || '.' || json_extract(fact,'$.name'),
            json_extract(fact,'$.signature'),json_extract(fact,'$.return_type'),
            json_extract(fact,'$.body_hash'),
            coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.library'),'') || char(31) ||
              coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
              coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
              coalesce(json_extract(fact,'$.variadic_arg_count'),0),
            coalesce(json_extract(fact,'$.condition'),''),
            coalesce(json_extract(fact,'$.signature'),'') || char(31) ||
              coalesce(json_extract(fact,'$.return_type'),'') || char(31) ||
              coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.condition'),'') || char(31) ||
              coalesce(json_extract(fact,'$.definition_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.library'),'') || char(31) ||
              coalesce(json_extract(fact,'$.entrypoint'),'') || char(31) ||
              coalesce(json_extract(fact,'$.default_arg_count'),0) || char(31) ||
              coalesce(json_extract(fact,'$.variadic_arg_count'),0)
          FROM rhs.routines WHERE snapshot_alias=?
          UNION ALL
          SELECT 0,'native','c:' || coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
            CASE WHEN json_extract(fact,'$.static') THEN path || ':' || json_extract(fact,'$.name')
                 ELSE json_extract(fact,'$.name') END,json_extract(fact,'$.signature'),NULL,
            json_extract(fact,'$.body_hash'),'',coalesce(json_extract(fact,'$.condition'),''),
            coalesce(json_extract(fact,'$.signature'),'') ||
              char(31) || coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.condition'), '')
          FROM main.symbols WHERE snapshot_alias=? AND (json_extract(fact,'$.declaration')=0 OR
            NOT EXISTS (SELECT 1 FROM main.symbols d
              WHERE d.snapshot_alias=main.symbols.snapshot_alias
              AND json_extract(d.fact,'$.name')=json_extract(main.symbols.fact,'$.name')
              AND json_extract(d.fact,'$.kind')=json_extract(main.symbols.fact,'$.kind')
              AND json_extract(d.fact,'$.declaration')=0))
          UNION ALL
          SELECT 1,'native','c:' || coalesce(json_extract(fact,'$.kind'),'function') || ':' ||
            CASE WHEN json_extract(fact,'$.static') THEN path || ':' || json_extract(fact,'$.name')
                 ELSE json_extract(fact,'$.name') END,json_extract(fact,'$.signature'),NULL,
            json_extract(fact,'$.body_hash'),'',coalesce(json_extract(fact,'$.condition'),''),
            coalesce(json_extract(fact,'$.signature'),'') ||
              char(31) || coalesce(json_extract(fact,'$.body_hash'),'') || char(31) ||
              coalesce(json_extract(fact,'$.condition'), '')
          FROM rhs.symbols WHERE snapshot_alias=? AND (json_extract(fact,'$.declaration')=0 OR
            NOT EXISTS (SELECT 1 FROM rhs.symbols d
              WHERE d.snapshot_alias=rhs.symbols.snapshot_alias
              AND json_extract(d.fact,'$.name')=json_extract(rhs.symbols.fact,'$.name')
              AND json_extract(d.fact,'$.kind')=json_extract(rhs.symbols.fact,'$.kind')
              AND json_extract(d.fact,'$.declaration')=0))
        ), grouped AS (
          SELECT side,kind,identity,count(DISTINCT semantic) n,min(signature) signature,
                 min(return_type) return_type,min(body) body,min(definition) definition,
                 min(condition) condition FROM facts GROUP BY side,kind,identity
        ), ids AS (SELECT kind,identity FROM grouped GROUP BY kind,identity), diff AS (
          SELECT ids.kind,ids.identity,l.n ln,r.n rn,l.signature ls,r.signature rs,
                 l.return_type lrt,r.return_type rrt,l.body lb,r.body rb,
                 l.condition lc,r.condition rc,
                 l.definition ld,r.definition rd,
            CASE WHEN coalesce(l.n,0)>1 OR coalesce(r.n,0)>1 THEN 'ambiguous'
                 WHEN l.n IS NULL THEN 'added' WHEN r.n IS NULL THEN 'removed'
                 WHEN coalesce(l.signature,'')<>coalesce(r.signature,'') OR
                      coalesce(l.return_type,'')<>coalesce(r.return_type,'')
                      THEN 'signature_changed'
                 WHEN coalesce(l.condition,'')<>coalesce(r.condition,'') THEN 'condition_changed'
                 WHEN coalesce(l.body,'')<>coalesce(r.body,'') THEN 'body_changed'
                 WHEN coalesce(l.definition,'')<>coalesce(r.definition,'') THEN 'definition_changed'
                 ELSE 'unchanged' END change
          FROM ids LEFT JOIN grouped l ON l.side=0 AND l.kind=ids.kind AND l.identity=ids.identity
                    LEFT JOIN grouped r ON r.side=1 AND r.kind=ids.kind AND r.identity=ids.identity
        ) SELECT kind,identity,ln,rn,ls,rs,change FROM diff WHERE change<>'unchanged'
          ORDER BY kind,identity LIMIT ? OFFSET ?
        """
        rows = conn.execute(
            query, (left_alias, right_alias, left_alias, right_alias, limit, offset)
        ).fetchall()
        return [
            {
                "kind": row[0],
                "logical_id": row[1],
                "left_count": row[2] or 0,
                "right_count": row[3] or 0,
                "left_signature": row[4],
                "right_signature": row[5],
                "change": row[6],
                "caveat": "source-level logical identity comparison",
            }
            for row in rows
        ]
    except sqlite3.OperationalError as exc:
        if time.monotonic() >= deadline:
            raise CatalogDeadline("cross-graph comparison deadline exceeded") from exc
        raise
    finally:
        conn.close()


def _application_handle(graph_id):
    selected = handle(graph_id)
    if selected.spec.kind != "application":
        raise GraphRegistryError("selected graph must be an application graph")
    selected.verify_generation()
    return selected


def _context_handles(context_id, *, deadline=None):
    reg = registry()
    try:
        context = reg.contexts[context_id]
    except KeyError as exc:
        raise GraphRegistryError(f"unknown context id: {context_id}") from exc
    app, db = handle(context.application_graph), handle(context.database_graph)
    app.verify_generation(timeout_seconds=_remaining(deadline) if deadline else 2.0)
    db.verify_generation(timeout_seconds=_remaining(deadline) if deadline else 2.0)
    return reg, context, app, db


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogDeadline("federation deadline exceeded")
    return remaining


def _resolve(evidence, context, app, db, *, deadline_seconds=2.0):
    deadline = time.monotonic() + min(max(deadline_seconds, 0.001), 10.0)
    result = {
        "status": "unresolved",
        "evidence": _source_segment(evidence, app),
        "context_id": context.id,
        "candidates": [],
        "assumptions": [],
    }
    if evidence.get("dynamic") or not evidence.get("object_name"):
        result["status"] = "dynamic" if evidence.get("dynamic") else "unresolved"
        return _metadata(result, app, db)
    origin = evidence.get("origin")
    if origin in {"markdown_sql", "markdown_mention"}:
        result["status"] = "documentation"
        result["evidence"]["relationship"] = "DOCUMENTS"
        return _metadata(result, app, db)
    name = evidence["object_name"]
    schema = evidence.get("schema_name")
    # Per search_path schema, application-local routines shadow DB declarations.
    schemas = [schema] if schema else list(context.search_path) or [None]
    for scope_schema in schemas:
        remaining = _remaining(deadline)
        own = app.catalog.routine_candidates(
            name=name,
            arity=evidence.get("arity"),
            schema_name=scope_schema,
            local_aliases=tuple(s["alias"] for s in app.generation.snapshots),
            limit=32,
            deadline_seconds=remaining,
        )
        candidates = own["items"]
        selected_graph = app
        if not candidates:
            pg_alias = next(s["alias"] for s in db.generation.snapshots if s["role"] == "postgres")
            aliases = (pg_alias, *context.visible_extensions)
            found = db.catalog.routine_candidates(
                name=name,
                arity=evidence.get("arity"),
                schema_name=scope_schema,
                local_aliases=(),
                visible_aliases=aliases,
                search_path=tuple([scope_schema]) if scope_schema else (),
                limit=32,
                deadline_seconds=_remaining(deadline),
            )
            candidates = found["items"]
            selected_graph = db
        else:
            found = own
        if candidates:
            if found.get("truncated"):
                result["status"] = "candidate_overflow"
                result["truncated"] = True
            elif evidence.get("condition") or any(c.get("condition") for c in candidates):
                result["status"] = "conditional"
            elif len(candidates) == 1 and _signature_known(evidence, candidates[0]):
                result["status"] = "exact"
            else:
                result["status"] = "ambiguous"
            result["candidates"] = [
                dict(
                    candidate,
                    graph_id=selected_graph.graph_id,
                    generation_id=selected_graph.generation_id,
                    ref={
                        "graph_id": selected_graph.graph_id,
                        "generation_id": selected_graph.generation_id,
                        "local_key": candidate.get("local_key"),
                    },
                )
                for candidate in candidates
            ]
            break
    if evidence.get("condition"):
        result["assumptions"].append("conditional evidence is not an asserted execution path")
    return _metadata(result, app, db)


def _signature_known(evidence, candidate):
    arity = evidence.get("arity")
    if arity is not None and candidate.get("arity") != arity:
        defaults = candidate.get("default_arg_count") or 0
        variadic = candidate.get("variadic_arg_count") or 0
        if not (
            candidate.get("arity", -1) - defaults <= arity
            and (variadic > 0 or arity <= candidate.get("arity", -1))
        ):
            return False
    # Similar arity/signature does not prove type-compatible overload selection.
    arg_types = evidence.get("argument_types") or evidence.get("arg_types")
    return not arg_types and not candidate.get("condition")


def _bounded_paths(db, start, target, max_depth, deadline, budget):
    paths = []
    queue = deque([(start, [start], [])])
    truncated = False
    while queue and len(paths) < 5:
        if time.monotonic() >= deadline or budget["visited"] >= 1000 or budget["edges"] >= 10000:
            return paths, True
        node, nodes, steps = queue.popleft()
        budget["visited"] += 1
        if node == target:
            paths.append({"nodes": nodes, "edges": steps})
            continue
        if len(steps) >= max_depth:
            truncated = True
            continue
        page = db.catalog.neighbors(node, limit=100, deadline_seconds=_remaining(deadline))
        truncated |= page["truncated"]
        for edge in page["items"]:
            budget["edges"] += 1
            if edge["kind"] not in {"BINDS_TO_NATIVE", "CALLS_NATIVE", "INVOKES_ROUTINE"}:
                continue
            nxt = edge["target_key"]
            if nxt in nodes:
                continue
            queue.append((nxt, [*nodes, nxt], [*steps, edge]))
    return paths, truncated or bool(queue)


def _python_reachable(app, start_key, *, max_depth, deadline, budget=None):
    """Bounded per-frontier EXACT_CALLS reads; no graph-wide path enumeration."""
    client = app.client
    budget = budget if budget is not None else {"visited": 0, "edges": 0}
    queue = deque([(start_key, 0, [])])
    seen = {start_key}
    owners = []
    truncated = False
    while queue:
        if time.monotonic() >= deadline or budget["visited"] >= 1000 or budget["edges"] >= 10000:
            return owners, True
        key, depth, call_chain = queue.popleft()
        budget["visited"] += 1
        owner = app.catalog.python_owner(key, deadline_seconds=_remaining(deadline))
        if owner:
            owners.append({"owner": owner, "call_chain": call_chain})
        if depth >= max_depth:
            truncated = True
            continue
        rows = client.execute_read(
            "CALL { MATCH (a:Function {key:$key})-[r:EXACT_CALLS]->(b) "
            "WHERE b:Function OR b:Method RETURN b.key AS key,r.path AS path,r.line AS line,"
            "r.column AS column UNION ALL "
            "MATCH (a:Method {key:$key})-[r:EXACT_CALLS]->(b) "
            "WHERE b:Function OR b:Method RETURN b.key AS key,r.path AS path,r.line AS line,"
            "r.column AS column } RETURN key,path,line,column ORDER BY key LIMIT 101",
            {"key": key},
            max_rows=101,
            timeout_seconds=max(0.001, min(2.0, deadline - time.monotonic())),
            operation="federation_python_frontier",
        )
        if len(rows) > 100:
            truncated = True
        for row in rows[:100]:
            if len(seen) >= 1000 or budget["edges"] >= 10000:
                truncated = True
                break
            budget["edges"] += 1
            child = row.get("key")
            if isinstance(child, str) and child not in seen:
                seen.add(child)
                edge = {
                    "from": key,
                    "to": child,
                    "kind": "EXACT_CALLS",
                    "path": row.get("path"),
                    "line": row.get("line"),
                    "column": row.get("column"),
                    "status": "exact",
                }
                queue.append((child, depth + 1, [*call_chain, edge]))
    return owners, truncated


def _joined_path(evidence, candidate, dbpath, app, db, context, owner=None, call_chain=()):
    segments = []
    if owner:
        segments.append(_source_segment(owner, app))
    for edge in call_chain:
        segments.append(
            {
                **edge,
                "graph_id": app.graph_id,
                "generation_id": app.generation_id,
                "relationship": "EXACT_CALLS",
            }
        )
    segments.append(_source_segment(evidence, app))
    segments.append(
        {
            "graph_id": candidate["graph_id"],
            "generation_id": candidate["generation_id"],
            "local_key": candidate.get("local_key"),
            "name": candidate.get("name"),
            "path": candidate.get("path"),
            "start_line": candidate.get("start_line"),
            "relationship": "INVOKES"
            if candidate["graph_id"] != app.graph_id
            else "INVOKES_LOCAL_ROUTINE",
            "status": "exact",
        }
    )
    for edge in dbpath.get("edges", []):
        segments.append(
            {
                **edge,
                "graph_id": db.graph_id,
                "generation_id": db.generation_id,
                "relationship": edge["kind"],
            }
        )
    return {
        "segments": segments,
        "assumptions": (
            [
                f"selected context {context.id}",
                "application database target "
                f"{context.application_database} selected by caller/context",
            ]
        ),
    }


def _source_segment(fact, graph):
    return {
        "graph_id": graph.graph_id,
        "generation_id": graph.generation_id,
        "local_key": fact.get("local_key") or fact.get("key"),
        "name": fact.get("object_name") or fact.get("name") or fact.get("qname"),
        "path": fact.get("path"),
        "start_line": fact.get("start_line"),
        "end_line": fact.get("end_line"),
        "start_column": fact.get("start_column"),
        "end_column": fact.get("end_column"),
        "origin": fact.get("origin"),
        "relationship": "HAS_EVIDENCE",
    }


def _entity_ref(value):
    if not isinstance(value, dict) or set(value) != {"graph_id", "generation_id", "local_key"}:
        raise ValueError("EntityRef must contain exactly graph_id, generation_id, local_key")
    return EntityRef(**value)


def _validate_ref(ref, graph):
    if ref.graph_id != graph.graph_id or ref.generation_id != graph.generation_id:
        raise ValueError("EntityRef graph or generation does not match selected graph")


def _status(status, left, right, **more):
    return {
        "status": status,
        "graph_id": left.graph_id,
        "generation_id": left.generation_id,
        "related_graph_id": right.graph_id,
        "related_generation_id": right.generation_id,
        **more,
    }


def _metadata(result, app, db):
    result.update(
        {
            "graph_id": app.graph_id,
            "generation_id": app.generation_id,
            "database_graph_id": db.graph_id,
            "database_generation_id": db.generation_id,
        }
    )
    return result
