from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

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
    search_symbols as query_search_symbols,
)
from codekg.queries.code import (
    trace_call_path as query_trace_call_path,
)
from codekg.queries.repositories import list_repositories as query_list_repositories

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]
SearchMode = Literal["graph", "lexical"]

mcp = FastMCP(
    "codekg",
    instructions=(
        "Read-only tools for querying the offline CodeKG Neo4j graph. "
        "Use list_repositories first when the repository name is unknown. "
        "Use exact keys returned by earlier tools. Qualified-name selectors require repo, "
        "and ambiguous qualified names return their candidate exact keys."
    ),
)


@mcp.tool(
    description=(
        "List repositories currently indexed in the graph, including commit, root path, "
        "and file count. Use this before repository-scoped queries when the repo name "
        "is unknown."
    )
)
def list_repositories() -> list[dict[str, Any]]:
    return query_list_repositories()


@mcp.tool(
    description=(
        "Search indexed code symbols. mode='graph' searches Neo4j symbol names and "
        "qualified names. mode='lexical' searches zvec descriptions for indexed functions "
        "and methods, then resolves exact keys in Neo4j. Use this first when you do not "
        "know the exact symbol key. Results are capped by the limit argument and can be "
        "restricted to one indexed commit."
    )
)
def search_symbols(
    q: Annotated[str, Field(description="Case-insensitive substring to search for.")],
    kind: Annotated[SymbolKind | None, Field(description="Optional symbol kind filter.")] = None,
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    mode: Annotated[
        SearchMode,
        Field(description="graph for Neo4j name search, lexical for zvec description search."),
    ] = "graph",
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 25,
) -> list[dict[str, Any]]:
    return query_search_symbols(q, kind=kind, repo=repo, commit=commit, mode=mode, limit=limit)


@mcp.tool(
    description=(
        "Return the definition metadata for one indexed symbol, including repository, "
        "file path, line span, qualified name, signature, and symbol kind. Prefer an exact "
        "symbol key. A qualified-name fallback requires repo and fails on ambiguity."
    )
)
def get_definition(
    identifier: Annotated[str, Field(description="Symbol key or qualified name.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name lookup.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
) -> list[dict[str, Any]]:
    return query_get_definition(identifier, repo=repo, commit=commit)


@mcp.tool(
    description=(
        "Find symbols that call the selected function or method. Exact keys are preferred; "
        "qualified names require repo. Depth 1 reads authoritative CallSite resolutions; "
        "deeper traversal uses the dedicated EXACT_CALLS projection."
    )
)
def find_callers(
    identifier: Annotated[str, Field(description="Function or method key, or qualified name.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name lookup.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_find_callers(identifier, repo=repo, commit=commit, depth=depth, limit=limit)


@mcp.tool(
    description=(
        "Find symbols called by the selected function or method. Exact keys are preferred; "
        "qualified names require repo. Depth 1 reads authoritative CallSite resolutions; "
        "deeper traversal uses the dedicated EXACT_CALLS projection."
    )
)
def find_callees(
    identifier: Annotated[str, Field(description="Function or method key, or qualified name.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name lookup.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_find_callees(identifier, repo=repo, commit=commit, depth=depth, limit=limit)


@mcp.tool(
    description=(
        "Find a bounded call path between two functions or methods. Prefer exact keys; "
        "qualified-name endpoints require repo. The returned path contains exact key/qname "
        "pairs and uses only EXACT_CALLS projections."
    )
)
def trace_call_path(
    from_identifier: Annotated[str, Field(description="Source function or method key/qname.")],
    to_identifier: Annotated[str, Field(description="Target function or method key/qname.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name endpoints.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    max_depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS path depth.")] = 8,
    limit: Annotated[int, Field(ge=1, le=10, description="Maximum paths to return.")] = 5,
) -> list[dict[str, Any]]:
    return query_trace_call_path(
        from_identifier,
        to_identifier,
        repo=repo,
        commit=commit,
        max_depth=max_depth,
        limit=limit,
    )


@mcp.tool(
    description=(
        "List files that import the selected module. Module keys are exact; module qualified "
        "names require repo. Results are capped and grouped by repository and file path."
    )
)
def find_importers(
    module_identifier: Annotated[str, Field(description="Imported module key or qualified name.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name lookup.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> list[dict[str, Any]]:
    return query_find_importers(module_identifier, repo=repo, commit=commit, limit=limit)


@mcp.tool(
    description=(
        "Return ancestors or descendants of a selected type through inheritance and interface "
        "relationships. Exact keys are preferred; qualified names require repo. Direction "
        "must be explicit and results are bounded."
    )
)
def get_class_hierarchy(
    identifier: Annotated[str, Field(description="Type key or qualified name.")],
    repo: Annotated[str | None, Field(description="Required for qualified-name lookup.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    direction: Annotated[
        HierarchyDirection,
        Field(description="Use ancestors for base types or descendants for subtypes."),
    ] = "ancestors",
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum hierarchy depth.")] = 5,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_get_class_hierarchy(
        identifier,
        repo=repo,
        commit=commit,
        direction=direction,
        depth=depth,
        limit=limit,
    )


@mcp.tool(
    description=(
        "List callable symbols in a repository with no inbound authoritative CallSite "
        "resolution. Results include incoming_resolved_calls and are unreferenced candidates, "
        "not confirmed dead code."
    )
)
def find_dead_code(
    repo: Annotated[str, Field(description="Repository name.")],
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> list[dict[str, Any]]:
    return query_find_dead_code(repo, commit=commit, limit=limit)


@mcp.tool(
    description=(
        "Return cyclomatic complexity for one symbol, or the most complex symbols in a "
        "repository when a top-N request is provided. An identifier is exact-key-first; "
        "qualified-name lookup requires repo."
    )
)
def get_complexity(
    identifier: Annotated[
        str | None,
        Field(description="Optional symbol key or qualified name for a single symbol."),
    ] = None,
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    top_n: Annotated[
        int | None,
        Field(ge=1, le=500, description="Return the top N most complex callables."),
    ] = 25,
) -> list[dict[str, Any]]:
    return query_get_complexity(identifier, repo=repo, commit=commit, top_n=top_n)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
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
