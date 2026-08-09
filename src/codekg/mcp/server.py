from __future__ import annotations

import json
import logging
import os
import time
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from pydantic import Field

from codekg.logging_config import configure_logging, debug_event
from codekg.queries.code import discover_symbols as query_discover_symbols
from codekg.queries.code import (
    find_callees as query_find_callees,
)
from codekg.queries.code import (
    find_callers as query_find_callers,
)
from codekg.queries.code import (
    find_dead_code as query_find_dead_code,
)
from codekg.queries.code import (
    find_importers as query_find_importers,
)
from codekg.queries.code import (
    get_class_hierarchy as query_get_class_hierarchy,
)
from codekg.queries.code import (
    get_complexity as query_get_complexity,
)
from codekg.queries.code import (
    get_definition as query_get_definition,
)
from codekg.queries.code import (
    trace_call_path as query_trace_call_path,
)
from codekg.queries.repositories import list_repositories as query_list_repositories

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]
SearchMode = Literal["graph", "lexical", "hybrid"]


class SearchScope(StrEnum):
    """Repository file categories available to compact symbol discovery."""

    SOURCE = "source"
    TESTS = "tests"
    DOCS = "docs"
    EXAMPLES = "examples"
    BENCHMARKS = "benchmarks"
    ALL = "all"


logger = logging.getLogger(__name__)

_WRAPPED_LIST_OUTPUT_SCHEMA = {
    "description": "Generic wrapper for non-object return types.",
    "properties": {
        "result": {"items": {"additionalProperties": True, "type": "object"}, "type": "array"}
    },
    "required": ["result"],
    "type": "object",
    "x-fastmcp-wrap-result": True,
}

mcp = FastMCP(
    "codekg",
    instructions=(
        "Read-only tools for querying the offline CodeKG Neo4j graph. "
        "Use list_repositories first when the repository name is unknown. "
        "Use exact keys returned by earlier tools. Qualified-name selectors require repository, "
        "and ambiguous qualified names return their candidate exact keys."
    ),
)


@mcp.tool(
    description=(
        "List repositories currently indexed in the graph, including commit, root path, "
        "and file count. Use this before repository-scoped queries when the repo name "
        "is unknown."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def list_repositories() -> ToolResult:
    rows = _normalize_public_rows(query_list_repositories())
    return _wrapped_list_result("list_repositories", rows)


@mcp.tool(
    description=(
        "Discover compact, repository-scoped code-symbol candidates. Call "
        "list_repositories first when the repository is unknown. With multiple indexed "
        "repositories, repository is required; searches never fall back to other "
        "repositories. mode='hybrid' combines exact graph-name matching with lexical "
        "ranking. This is candidate discovery only: after a plausible candidate, call "
        "get_definition with its returned symbol_id instead of issuing another broad search."
    )
)
def search_symbols(
    query: Annotated[
        str, Field(description="Terms to search in indexed symbol names and descriptions.")
    ],
    repository: Annotated[
        str | None,
        Field(
            description=(
                "Indexed repository name. Required when more than one repository is indexed; "
                "omit only when exactly one repository is indexed."
            )
        ),
    ] = None,
    kind: Annotated[SymbolKind | None, Field(description="Optional symbol kind filter.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    mode: Annotated[
        SearchMode,
        Field(description="hybrid, graph, or lexical discovery ranking mode."),
    ] = "hybrid",
    scope: Annotated[
        SearchScope,
        Field(
            description=(
                "Repository file scope: source (default), tests, docs, examples, "
                "benchmarks, or all. Applied before candidate ranking."
            )
        ),
    ] = SearchScope.SOURCE,
    limit: Annotated[
        int, Field(ge=1, le=20, description="Maximum compact candidates to return.")
    ] = 5,
    cursor: Annotated[
        str | None,
        Field(description="Opaque cursor from a previous search with the same scope and query."),
    ] = None,
) -> ToolResult:
    """Return canonical structured discovery data plus a deliberately small text summary."""
    started = time.perf_counter()
    response = dict(
        query_discover_symbols(
            repository=repository,
            query=query,
            kind=kind,
            commit=commit,
            mode=mode,
            scope=scope.value,
            limit=limit,
            cursor=cursor,
        )
    )
    # Query diagnostics remain available to direct callers and internal logging,
    # but discovery's public MCP response is intentionally selection-focused.
    diagnostics = response.pop("diagnostics", {})
    results = response.get("results")
    if isinstance(results, list) and all(isinstance(row, dict) for row in results):
        for row in results:
            row.pop("scope", None)
        response["results"] = _normalize_public_rows(
            results,
            repository_hint=response.get("repository")
            if isinstance(response.get("repository"), str)
            else None,
            commit_hint=response.get("commit") if isinstance(response.get("commit"), str) else None,
        )
    _add_discovery_guidance(response, scope)
    structured_bytes = len(json.dumps(response, separators=(",", ":"), default=str).encode())
    text = _search_summary(response)
    text_bytes = len(text.encode())
    logger.info(
        "codekg_search_symbols %s",
        json.dumps(
            {
                "repository": response.get("repository", repository),
                "commit": response.get("commit", commit),
                "mode": mode,
                "scope": scope.value,
                "requested_count": limit,
                "returned_count": len(response.get("results", [])),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "structured_bytes": structured_bytes,
                "text_bytes": text_bytes,
                "has_next_cursor": bool(response.get("next_cursor")),
                "status": response.get("status", "ok"),
                "diagnostics": _safe_search_diagnostics(diagnostics, scope),
            },
            separators=(",", ":"),
        ),
    )
    return ToolResult(content=text, structured_content=response)


def _safe_search_diagnostics(diagnostics: object, scope: SearchScope) -> dict[str, object]:
    """Retain aggregate ranking observability without logging query text or paths."""
    if not isinstance(diagnostics, dict):
        return {"scope": scope.value}
    safe = {"scope": diagnostics.get("scope", scope.value)}
    for key in ("candidate_pool", "scoped_candidate_count", "ranked_count", "exact_match_count"):
        value = diagnostics.get(key)
        if isinstance(value, int):
            safe[key] = value
    for key in ("query_terms", "ignored_terms"):
        value = diagnostics.get(key)
        if isinstance(value, list):
            safe[f"{key}_count"] = len(value)
    return safe


# B4 (codekg-ranking-presentation-spec.md): recommended_symbol_id was removed
# rather than margin-gated. Calibration (evaluation/replay_b4.py, replayed
# offline against all recorded CodeKG search_symbols calls across three runs)
# found no ratio/absolute-difference threshold that separates correct from
# wrong rank-1 recommendations: a correct case (ratio=1.20, diff=220) and a
# wrong one (ratio=1.20, diff=262) share the same margin, and one wrong case
# -- a tier-boosted qualified-name-suffix false match -- carries ratio=9.69,
# diff=8080, an outlier margin higher than every correct case's, that would
# still be wrong. B4.5's ship gate (zero wrong recommendations emitted) is
# unreachable by any threshold on this signal, so per B4.4's escape hatch an
# absent field is shipped instead of a confidently wrong one.
def _add_discovery_guidance(response: dict[str, object], scope: SearchScope) -> None:
    """Keep scope and the discovery-to-evidence workflow visible to MCP clients."""
    response.setdefault("scope", scope.value)
    results = response.get("results")
    if response.get("status", "ok") != "ok" or not isinstance(results, list) or not results:
        return
    first = results[0]
    if not isinstance(first, dict):
        return
    # recommended_next_tool is unconditional: advising the client to fetch a
    # definition next is sound regardless of which candidate it picks.
    response.setdefault("recommended_next_tool", "get_definition")


def _search_summary(response: dict[str, Any]) -> str:
    """Human-compatible text that cannot duplicate the structured result payload."""
    status = str(response.get("status", "ok"))
    repository = response.get("repository")
    if status != "ok":
        return (
            f"Symbol discovery status={status}; repository={repository!s}. See structured result."
        )
    results = response.get("results", [])
    count = len(results) if isinstance(results, list) else 0
    more = " More results are available." if response.get("next_cursor") else ""
    return (
        f"Found {count} symbol candidate(s) in repository={repository!s}.{more} "
        "See structured result."
    )


_SUMMARY_NOUNS = {
    "get_definition": "definition record",
    "find_callers": "caller",
    "find_callees": "callee",
    "trace_call_path": "call path",
    "find_importers": "importer",
    "get_class_hierarchy": "related type",
    "find_dead_code": "unreferenced candidate",
    "get_complexity": "complexity record",
}


def _wrapped_list_result(tool_name: str, rows: list[dict[str, object]]) -> ToolResult:
    """Keep MCP list output structured while avoiding JSON duplication in text."""
    count = len(rows)
    if tool_name == "list_repositories":
        text = f"Found {count} indexed {'repository' if count == 1 else 'repositories'}."
    else:
        noun = _SUMMARY_NOUNS[tool_name]
        text = f"Found {count} {noun}{'' if count == 1 else 's'}."
    structured_content = {"result": rows}
    logger.info(
        "codekg_%s %s",
        tool_name,
        json.dumps(
            {
                "returned_count": count,
                "structured_bytes": len(
                    json.dumps(structured_content, separators=(",", ":"), default=str).encode()
                ),
                "text_bytes": len(text.encode()),
            },
            separators=(",", ":"),
        ),
    )
    return ToolResult(content=text, structured_content=structured_content)


def _normalize_public_rows(
    rows: list[dict[str, object]],
    *,
    repository_hint: str | None = None,
    commit_hint: str | None = None,
) -> list[dict[str, object]]:
    """Remove storage-root prefixes from public file paths without changing identities."""
    normalized = [dict(row) for row in rows]
    if any(
        isinstance(row.get("file"), str) and _is_absolute_public_path(row["file"])
        for row in normalized
    ):
        snapshots = [
            row
            for row in query_list_repositories()
            if isinstance(row.get("repo_name"), str) and isinstance(row.get("root_path"), str)
        ]
    else:
        snapshots = []
    for row in normalized:
        if "root_path" in row:
            row["root_path"] = "."
        file_path = row.get("file")
        if not isinstance(file_path, str):
            continue
        if not _is_absolute_public_path(file_path):
            row["file"] = _validate_relative_public_path(file_path)
            continue
        row_repository = row.get("repo")
        repository = (
            row_repository
            if isinstance(row_repository, str) and row_repository
            else repository_hint
        )
        row_commit = row.get("commit")
        commit = row_commit if isinstance(row_commit, str) and row_commit else commit_hint
        root_path = _resolve_indexed_root(snapshots, repository, commit)
        if not root_path or not _is_absolute_public_path(root_path):
            raise ValueError("Cannot safely normalize an indexed absolute file path.")
        try:
            public_path = _public_path(file_path)
            indexed_root = _public_path(root_path)
            if type(public_path) is not type(indexed_root):
                raise ValueError
            relative_path = str(public_path.relative_to(indexed_root))
        except ValueError as exc:
            raise ValueError("Cannot safely normalize an indexed absolute file path.") from exc
        row["file"] = _validate_relative_public_path(relative_path)
    return normalized


def _resolve_indexed_root(
    snapshots: list[dict[str, object]], repository: str | None, commit: str | None
) -> str | None:
    if not repository:
        return None
    matches = [row for row in snapshots if row["repo_name"] == repository]
    if commit is not None:
        matches = [row for row in matches if row.get("commit") == commit]
    roots = {str(row["root_path"]) for row in matches}
    return next(iter(roots)) if len(roots) == 1 else None


def _validate_relative_public_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("Cannot safely normalize an indexed file path containing traversal.")
    return normalized


def _is_absolute_public_path(value: str) -> bool:
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith("\\")
    )


def _public_path(value: str) -> PurePosixPath | PureWindowsPath:
    if PureWindowsPath(value).is_absolute() or value.startswith("\\"):
        return PureWindowsPath(value)
    return PurePosixPath(value)


def _symbol_identity_hints(
    identifier: str, repo: str | None, commit: str | None
) -> tuple[str | None, str | None]:
    """Use an exact stable symbol ID to normalize paths without exposing identity fields."""
    if repo is not None and commit is not None:
        return repo, commit
    repository_part, separator, remainder = identifier.partition("@")
    indexed_commit, commit_separator, _ = remainder.partition(":")
    if separator and commit_separator and repository_part and indexed_commit:
        return repo or repository_part, commit or indexed_commit
    return repo, commit


@mcp.tool(
    description=(
        "Verify exact indexed definition metadata and line bounds for one selected symbol. "
        "Pass the symbol_id returned by search_symbols; a qualified-name fallback requires "
        "repository and fails on ambiguity."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_definition(
    identifier: Annotated[str, Field(description="Symbol key or qualified name.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name lookup.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
) -> ToolResult:
    return _wrapped_list_result(
        "get_definition",
        _normalize_public_rows(query_get_definition(identifier, repo=repository, commit=commit)),
    )


@mcp.tool(
    description=(
        "Verify bounded incoming relationships for a selected function or method. Pass its "
        "exact symbol_id when available. Depth 1 reads authoritative CallSite resolutions; "
        "deeper traversal uses the dedicated EXACT_CALLS projection."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_callers(
    identifier: Annotated[str, Field(description="Function or method key, or qualified name.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name lookup.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    repository_hint, commit_hint = _symbol_identity_hints(identifier, repository, commit)
    return _wrapped_list_result(
        "find_callers",
        _normalize_public_rows(
            query_find_callers(identifier, repo=repository, commit=commit, depth=depth, limit=limit),
            repository_hint=repository_hint,
            commit_hint=commit_hint,
        ),
    )


@mcp.tool(
    description=(
        "Verify bounded outgoing relationships for a selected function or method. Pass its "
        "exact symbol_id when available. Depth 1 reads authoritative CallSite resolutions; "
        "deeper traversal uses the dedicated EXACT_CALLS projection."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_callees(
    identifier: Annotated[str, Field(description="Function or method key, or qualified name.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name lookup.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    repository_hint, commit_hint = _symbol_identity_hints(identifier, repository, commit)
    return _wrapped_list_result(
        "find_callees",
        _normalize_public_rows(
            query_find_callees(identifier, repo=repository, commit=commit, depth=depth, limit=limit),
            repository_hint=repository_hint,
            commit_hint=commit_hint,
        ),
    )


@mcp.tool(
    description=(
        "Find a bounded call path between two functions or methods. Prefer exact keys; "
        "qualified-name endpoints require repository. The returned path contains exact key/qname "
        "pairs and uses only EXACT_CALLS projections."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def trace_call_path(
    from_identifier: Annotated[str, Field(description="Source function or method key/qname.")],
    to_identifier: Annotated[str, Field(description="Target function or method key/qname.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name endpoints.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    max_depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS path depth.")] = 8,
    limit: Annotated[int, Field(ge=1, le=10, description="Maximum paths to return.")] = 5,
) -> ToolResult:
    return _wrapped_list_result(
        "trace_call_path",
        _normalize_public_rows(
            query_trace_call_path(
                from_identifier,
                to_identifier,
                repo=repository,
                commit=commit,
                max_depth=max_depth,
                limit=limit,
            )
        ),
    )


@mcp.tool(
    description=(
        "List files that import the selected module. Module keys are exact; module qualified "
        "names require repo. Results are capped and grouped by repository and file path."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_importers(
    module_identifier: Annotated[str, Field(description="Imported module key or qualified name.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name lookup.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> ToolResult:
    return _wrapped_list_result(
        "find_importers",
        _normalize_public_rows(
            query_find_importers(module_identifier, repo=repository, commit=commit, limit=limit)
        ),
    )


@mcp.tool(
    description=(
        "Return ancestors or descendants of a selected type through inheritance and interface "
        "relationships. Exact keys are preferred; qualified names require repository. Direction "
        "must be explicit and results are bounded."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_class_hierarchy(
    identifier: Annotated[str, Field(description="Type key or qualified name.")],
    repository: Annotated[
        str | None, Field(description="Required for qualified-name lookup.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    direction: Annotated[
        HierarchyDirection,
        Field(description="Use ancestors for base types or descendants for subtypes."),
    ] = "ancestors",
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum hierarchy depth.")] = 5,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    return _wrapped_list_result(
        "get_class_hierarchy",
        _normalize_public_rows(
            query_get_class_hierarchy(
                identifier,
                repo=repository,
                commit=commit,
                direction=direction,
                depth=depth,
                limit=limit,
            )
        ),
    )


@mcp.tool(
    description=(
        "List callable symbols in a repository with no inbound authoritative CallSite "
        "resolution. Results include incoming_resolved_calls and are unreferenced candidates, "
        "not confirmed dead code."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_dead_code(
    repository: Annotated[str, Field(description="Repository name.")],
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> ToolResult:
    return _wrapped_list_result(
        "find_dead_code",
        _normalize_public_rows(
            query_find_dead_code(repository, commit=commit, limit=limit),
            repository_hint=repository,
            commit_hint=commit,
        ),
    )


@mcp.tool(
    description=(
        "Return cyclomatic complexity for one symbol, or the most complex symbols in a "
        "repository when a top-N request is provided. An identifier is exact-key-first; "
        "qualified-name lookup requires repository."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_complexity(
    identifier: Annotated[
        str | None,
        Field(description="Optional symbol key or qualified name for a single symbol."),
    ] = None,
    repository: Annotated[
        str | None, Field(description="Optional repository name filter.")
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    top_n: Annotated[
        int | None,
        Field(ge=1, le=500, description="Return the top N most complex callables."),
    ] = 25,
) -> ToolResult:
    return _wrapped_list_result(
        "get_complexity",
        _normalize_public_rows(
            query_get_complexity(identifier, repo=repository, commit=commit, top_n=top_n),
            repository_hint=repository,
            commit_hint=commit,
        ),
    )


def main() -> None:
    configure_logging()
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    debug_event(logger, "mcp_started", transport=transport)
    if transport == "http":
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8765")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
