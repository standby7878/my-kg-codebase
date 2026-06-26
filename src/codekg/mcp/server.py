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

mcp = FastMCP(
    "codekg",
    instructions=(
        "Read-only tools for querying the offline CodeKG Neo4j graph. "
        "Use list_repositories first when the repository name is unknown. "
        "Prefer exact keys and qualified names returned by earlier tools."
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
        "Search indexed code symbols by name or qualified name substring. Use this first "
        "when you do not know the exact symbol key. Optionally filter by repository and "
        "symbol kind. Results are capped by the limit argument."
    )
)
def search_symbols(
    q: Annotated[str, Field(description="Case-insensitive substring to search for.")],
    kind: Annotated[SymbolKind | None, Field(description="Optional symbol kind filter.")] = None,
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 25,
) -> list[dict[str, Any]]:
    return query_search_symbols(q, kind=kind, repo=repo, limit=limit)


@mcp.tool(
    description=(
        "Return the definition metadata for one indexed symbol, including repository, "
        "file path, line span, qualified name, signature, and symbol kind. Use an exact "
        "symbol key or qualified name from search results."
    )
)
def get_definition(
    identifier: Annotated[str, Field(description="Symbol key or qualified name.")],
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
) -> list[dict[str, Any]]:
    return query_get_definition(identifier, repo=repo)


@mcp.tool(
    description=(
        "Find symbols that call the given function or method. Traversal depth is bounded "
        "and results are capped. Treat edges with resolution='heuristic' as approximate."
    )
)
def find_callers(
    qname: Annotated[str, Field(description="Function or method qualified name.")],
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_find_callers(qname, depth=depth, limit=limit)


@mcp.tool(
    description=(
        "Find symbols called by the given function or method. Traversal depth is bounded "
        "and results are capped. Treat edges with resolution='heuristic' as approximate."
    )
)
def find_callees(
    qname: Annotated[str, Field(description="Function or method qualified name.")],
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_find_callees(qname, depth=depth, limit=limit)


@mcp.tool(
    description=(
        "Find a bounded call path between two functions or methods. Use exact qualified "
        "names or keys. Returns no path when the graph cannot prove a connection within "
        "max_depth."
    )
)
def trace_call_path(
    from_qname: Annotated[str, Field(description="Source function or method qualified name.")],
    to_qname: Annotated[str, Field(description="Target function or method qualified name.")],
    max_depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS path depth.")] = 8,
    limit: Annotated[int, Field(ge=1, le=10, description="Maximum paths to return.")] = 5,
) -> list[dict[str, Any]]:
    return query_trace_call_path(from_qname, to_qname, max_depth=max_depth, limit=limit)


@mcp.tool(
    description=(
        "List files that import the requested module qualified name. Results are capped "
        "and grouped by repository and file path."
    )
)
def find_importers(
    module_qname: Annotated[str, Field(description="Imported module qualified name.")],
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> list[dict[str, Any]]:
    return query_find_importers(module_qname, repo=repo, limit=limit)


@mcp.tool(
    description=(
        "Return ancestors or descendants of a type through inheritance and interface "
        "relationships. Direction must be explicit. Results are bounded."
    )
)
def get_class_hierarchy(
    type_qname: Annotated[str, Field(description="Type qualified name.")],
    direction: Annotated[
        HierarchyDirection,
        Field(description="Use ancestors for base types or descendants for subtypes."),
    ] = "ancestors",
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum hierarchy depth.")] = 5,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> list[dict[str, Any]]:
    return query_get_class_hierarchy(type_qname, direction=direction, depth=depth, limit=limit)


@mcp.tool(
    description=(
        "List callable symbols in a repository with no inbound call edges. Excludes known "
        "entry points when entry-point metadata is available. Results are candidates, not "
        "confirmed dead code."
    )
)
def find_dead_code(
    repo: Annotated[str, Field(description="Repository name.")],
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 100,
) -> list[dict[str, Any]]:
    return query_find_dead_code(repo, limit=limit)


@mcp.tool(
    description=(
        "Return cyclomatic complexity for one symbol, or the most complex symbols in a "
        "repository when a top-N request is provided."
    )
)
def get_complexity(
    identifier: Annotated[
        str | None,
        Field(description="Optional symbol key or qualified name for a single symbol."),
    ] = None,
    repo: Annotated[str | None, Field(description="Optional repository name filter.")] = None,
    top_n: Annotated[
        int | None,
        Field(ge=1, le=500, description="Return the top N most complex callables."),
    ] = 25,
) -> list[dict[str, Any]]:
    return query_get_complexity(identifier, repo=repo, top_n=top_n)


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
