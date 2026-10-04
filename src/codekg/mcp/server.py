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
from codekg.queries.corpus import (
    compare_corpus_snapshots as query_compare_corpus_snapshots,
)
from codekg.queries.corpus import (
    get_dependency_evidence as query_get_dependency_evidence,
)
from codekg.queries.corpus import (
    list_corpus_snapshots as query_list_corpus_snapshots,
)
from codekg.queries.corpus import (
    search_corpus_symbols as query_search_corpus_symbols,
)
from codekg.queries.corpus import (
    trace_corpus_path as query_trace_corpus_path,
)
from codekg.queries.repositories import list_repositories as query_list_repositories
from codekg.queries.sql import find_sql_usages as query_find_sql_usages
from codekg.queries.sql import get_sql_in_file as query_get_sql_in_file
from codekg.queries.sql import get_sql_object as query_get_sql_object
from codekg.queries.sql import search_sql_objects as query_search_sql_objects

SymbolKind = Literal["function", "method", "type"]
HierarchyDirection = Literal["ancestors", "descendants"]
SearchMode = Literal["graph", "lexical", "hybrid"]
SqlObjectKind = Literal[
    "table",
    "view",
    "materialized_view",
    "sequence",
    "index",
    "statistics",
    "function",
    "procedure",
    "schema",
    "extension",
    "type",
]
SqlUsageRole = Literal["read", "write", "call", "alter", "drop", "define", "all"]
SqlReferenceResolution = Literal["exact", "ambiguous", "unresolved", "dynamic", "all"]


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

_MCP_INSTRUCTIONS = """\
Read-only queries over commit-pinned CodeKG snapshots. Results describe the indexed commit,
not necessarily the working tree or HEAD — verify with list_repositories.

Python workflow:
1. list_repositories — pick repository and indexed commit.
2. search_symbols — discover candidates when the exact symbol is unknown.
3. get_definition / find_callers / find_callees / trace_call_path — pass symbol_id from step 2.

Callable/type identifiers (in order of preference):
- symbol_id: repo@commit:path/to/file.py:module.Class.method:812
- full qualified_name (requires repository; fails on ambiguity)
- unique dotted suffix, e.g. PGService._allocate_haproxy_frontend_ports (requires repository)
- NOT supported: bare method names like _allocate_haproxy_frontend_ports

Module identifiers (find_importers): exact module key or full module qualified name only.

SQL workflow: search_sql_objects → get_sql_object → find_sql_usages.
SQL scope: PostgreSQL .sql files selected by enabled [sql] configuration in codekg.toml.
The corpus workflow additionally indexes selected .sql.in templates, pg_proc.dat,
C/header syntax, literal Python DB SQL and Markdown/runbook evidence.
Live database catalog state is not indexed.

PostgreSQL corpus workflow: list_corpus_snapshots -> search_corpus_symbols ->
get_dependency_evidence / trace_corpus_path / compare_corpus_snapshots.
Snapshots are explicit aliases with dependency contexts and source fingerprints.
Build-free C links are source evidence, not compiler-verified ABI/runtime facts.
Candidate/conditional links are excluded from asserted paths. Documentation links
are DOCUMENTS_ROUTINE, not execution. Diffs aid upgrade/security investigations;
they are not vulnerability verdicts. Other PL bodies have explicit limited coverage.

Static call edges may be heuristic; resolution=heuristic is approximate. Zero callers/callees
or no trace path does not exclude callbacks, dynamic dispatch, or runtime wiring.
"""

mcp = FastMCP(
    "codekg",
    instructions=_MCP_INSTRUCTIONS,
)


@mcp.tool(
    description=(
        "List indexed repository snapshots with repository name, commit, normalized root, "
        "and file count. Use first to select the repository and verify the indexed commit "
        "before any repository-scoped query."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def list_repositories() -> ToolResult:
    rows = _normalize_public_rows(query_list_repositories())
    return _wrapped_list_result("list_repositories", rows)


@mcp.tool(
    description=(
        "Discover compact code-symbol candidates in one indexed snapshot. Call "
        "list_repositories first when the repository is unknown. With multiple indexed "
        "repositories, repository is required; searches never fall back to other "
        "repositories. mode='hybrid' combines exact graph-name matching with lexical "
        "ranking. Select a plausible result, then call get_definition with its returned "
        "symbol_id; paginate with cursor only when needed."
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
    kind: Annotated[
        SymbolKind | None,
        Field(description="Optional filter: function, method, or type."),
    ] = None,
    commit: Annotated[
        str | None, Field(description="Exact indexed commit from list_repositories.")
    ] = None,
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
        Field(
            description=(
                "Opaque next_cursor; reuse only with the same repository, commit, query, "
                "mode, kind, and scope."
            )
        ),
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


_PREVIEW_LIMIT = 5
_SUMMARY_TEXT_BUDGET_BYTES = 1200
_SUMMARY_HEADER_BUDGET_BYTES = 300

_EMPTY_CALL_EDGE_HINT = (
    "No static call edges found. The symbol may only be referenced dynamically "
    "(e.g. passed as a callback). Verify the indexed commit with list_repositories."
)


def _search_summary(response: dict[str, Any]) -> str:
    """Human-compatible text with compact previews for agents that read only content."""
    status = str(response.get("status", "ok"))
    repository = response.get("repository")
    if status != "ok":
        return (
            f"Symbol discovery status={status}; repository={repository!s}. See structured result."
        )
    results = response.get("results", [])
    count = len(results) if isinstance(results, list) else 0
    more = " More results are available." if response.get("next_cursor") else ""
    header = f"Found {count} symbol candidate(s) in repository={repository!s}.{more}"
    if not isinstance(results, list) or not results:
        return header
    return _bounded_summary(header, results, tool_name="search_symbols")


_SUMMARY_NOUNS = {
    "get_definition": "definition record",
    "find_callers": "caller",
    "find_callees": "callee",
    "trace_call_path": "call path",
    "find_importers": "importer",
    "get_class_hierarchy": "related type",
    "find_dead_code": "unreferenced candidate",
    "get_complexity": "complexity record",
    "find_sql_usages": "SQL usage",
    "list_corpus_snapshots": "corpus snapshot",
    "search_corpus_symbols": "corpus symbol",
    "get_dependency_evidence": "dependency evidence record",
    "trace_corpus_path": "corpus path",
    "compare_corpus_snapshots": "source change",
}


def _wrapped_list_result(
    tool_name: str,
    rows: list[dict[str, object]],
    *,
    subject: str | None = None,
    empty_hint: str | None = None,
) -> ToolResult:
    """Keep MCP list output structured while including compact previews in text."""
    public_rows = _with_symbol_ids(rows)
    count = len(public_rows)
    if public_rows and tool_name == "list_repositories":
        header = f"{count} {'repository' if count == 1 else 'repositories'}"
    elif public_rows and tool_name in {"get_definition", "find_callers", "find_callees"}:
        noun = _SUMMARY_NOUNS[tool_name].split(maxsplit=1)[0]
        subject_suffix = f" ({_safe_identity_label(subject)})" if count > 1 and subject else ""
        header = f"{count} {noun}{'' if count == 1 else 's'}{subject_suffix}"
    elif tool_name == "list_repositories":
        header = f"Found {count} indexed {'repository' if count == 1 else 'repositories'}."
    else:
        noun = _SUMMARY_NOUNS[tool_name]
        subject_suffix = f" for {_safe_identity_label(subject)}" if subject else ""
        header = f"Found {count} {noun}{'' if count == 1 else 's'}{subject_suffix}."
    text = _bounded_summary(
        header,
        public_rows,
        tool_name=tool_name,
        empty_hint=empty_hint if count == 0 else None,
    )
    structured_content = {"result": public_rows}
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


def _with_symbol_ids(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for row in rows:
        public_row = dict(row)
        key = public_row.get("key")
        if isinstance(key, str) and key and "symbol_id" not in public_row:
            public_row["symbol_id"] = key
        enriched.append(public_row)
    return enriched


def _bounded_summary(
    header: str,
    rows: list[dict[str, object]],
    *,
    tool_name: str,
    empty_hint: str | None = None,
) -> str:
    header = _truncate_utf8(header, _SUMMARY_HEADER_BUDGET_BYTES)
    remaining = _SUMMARY_TEXT_BUDGET_BYTES - len(header.encode("utf-8"))
    if not rows and empty_hint:
        hint = _truncate_utf8(" " + empty_hint, max(0, remaining))
        return header + hint
    previews = _preview_lines(rows, tool_name=tool_name, byte_budget=remaining - 1)
    return header if not previews else header + "\n" + "\n".join(previews)


def _truncate_utf8(value: str, byte_budget: int) -> str:
    if byte_budget <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_budget:
        return value
    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    if byte_budget < len(suffix_bytes):
        return encoded[:byte_budget].decode("utf-8", errors="ignore")
    prefix = encoded[: byte_budget - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return prefix + suffix


def _preview_lines(rows: list[dict[str, object]], *, tool_name: str, byte_budget: int) -> list[str]:
    if not rows:
        return []
    preview_rows = rows[:_PREVIEW_LIMIT]
    remaining = len(rows) - len(preview_rows)
    lines: list[str] = []
    used = 0
    for index, row in enumerate(preview_rows, start=1):
        line = _format_row_preview(index, row, tool_name=tool_name, numbered=len(rows) > 1)
        separator_bytes = 1 if lines else 0
        available = byte_budget - used - separator_bytes
        if available <= 0:
            break
        line = _truncate_utf8(line, available)
        lines.append(line)
        used += separator_bytes + len(line.encode("utf-8"))
    if remaining > 0 and len(lines) == len(preview_rows):
        note = f"  … and {remaining} more (see structured result)."
        separator_bytes = 1 if lines else 0
        available = byte_budget - used - separator_bytes
        if available > 0:
            lines.append(_truncate_utf8(note, available))
    return lines


def _safe_identity_label(value: str) -> str:
    """Render source identities as a human name without leaking full backend IDs."""
    repository, at, rest = value.partition("@")
    fields = rest.split(":")
    if at and repository and len(fields) >= 4 and fields[0] and fields[2]:
        commit = fields[0][:8]
        return f"{fields[2]} ({repository}@{commit})"
    if at and len(fields) >= 3:
        return "selected symbol"
    return _truncate_utf8(value, 120)


def _format_row_preview(
    index: int, row: dict[str, object], *, tool_name: str, numbered: bool = True
) -> str:
    prefix = f"  {index}. " if numbered else ""
    if tool_name == "list_corpus_snapshots":
        return f"{prefix}{row.get('alias', '?')} ({row.get('version', '?')})"
    if tool_name == "compare_corpus_snapshots":
        return f"{prefix}{row.get('identity', '?')} [{row.get('change', '?')}]"
    if tool_name == "trace_corpus_path" and isinstance(row.get("nodes"), list):
        labels = [
            f"{node.get('name') or node.get('qname') or 'indexed node'}"
            + (f" ({node['path']})" if isinstance(node.get("path"), str) else "")
            for node in row["nodes"]
            if isinstance(node, dict)
        ]
        return f"{prefix}{' -> '.join(labels)}"
    if tool_name == "list_repositories":
        repo_name = row.get("repo_name")
        commit = row.get("commit")
        files = row.get("files")
        label = repo_name if isinstance(repo_name, str) else "?"
        if isinstance(commit, str) and commit:
            label = f"{label}@{commit[:8]}"
        if isinstance(files, int):
            return f"{prefix}{label} {files} files"
        return f"{prefix}{label}"
    if tool_name == "trace_call_path":
        path = row.get("path")
        if isinstance(path, list):
            segments: list[str] = []
            for node in path:
                if not isinstance(node, dict):
                    continue
                label = node.get("qname") or node.get("name")
                if isinstance(label, str) and label:
                    segments.append(label)
            if segments:
                depth = row.get("depth")
                depth_suffix = f" (depth {depth})" if isinstance(depth, int) else ""
                return f"{prefix}{' -> '.join(segments)}{depth_suffix}"
    label = _format_symbol_label(row)
    location = _format_row_location(
        row,
        include_relative_path=tool_name
        in {
            "search_corpus_symbols",
            "get_dependency_evidence",
        },
    )
    resolution = row.get("resolution") or row.get("status")
    resolution_suffix = ""
    if isinstance(resolution, str) and resolution and resolution != "exact":
        resolution_suffix = f" [{resolution}]"
    if location:
        return f"{prefix}{label} {location}{resolution_suffix}"
    return f"{prefix}{label}{resolution_suffix}"


def _format_symbol_label(row: dict[str, object]) -> str:
    for field in ("qname", "qualified_name", "name", "module"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return "?"


def _format_row_location(
    row: dict[str, object], *, include_relative_path: bool = False
) -> str | None:
    file_path = row.get("file")
    if include_relative_path and not file_path:
        file_path = row.get("evidence_path") or row.get("path")
    if not isinstance(file_path, str) or not file_path:
        return None
    line_number = row.get("start_line")
    if not isinstance(line_number, int):
        line_number = row.get("line")
    if isinstance(line_number, int):
        return f"{file_path}:{line_number}"
    return file_path


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
    return _with_symbol_ids(normalized)


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
        "Return indexed metadata and line bounds for one function, method, or type. "
        "Prefer symbol_id from search_symbols; otherwise use a full qualified name or "
        "repository-scoped unique dotted suffix. Bare member names are unsupported."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_definition(
    identifier: Annotated[
        str,
        Field(
            description=(
                "symbol_id from a prior tool, full qualified name, or repository-scoped "
                "unique dotted suffix."
            )
        ),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for qualified-name or suffix lookup."),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
) -> ToolResult:
    return _wrapped_list_result(
        "get_definition",
        _normalize_public_rows(query_get_definition(identifier, repo=repository, commit=commit)),
    )


@mcp.tool(
    description=(
        "Find bounded static callers of a selected function or method. Prefer symbol_id. "
        "At depth 1, inspect indexed call-site resolutions and treat resolution='heuristic' "
        "as approximate; deeper traversal uses exact-only projected edges. Zero results do "
        "not exclude callback or dynamic references."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_callers(
    identifier: Annotated[
        str,
        Field(
            description=(
                "symbol_id from a prior tool, full qualified name, or repository-scoped "
                "unique dotted suffix."
            )
        ),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for qualified-name or suffix lookup."),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    repository_hint, commit_hint = _symbol_identity_hints(identifier, repository, commit)
    rows = _normalize_public_rows(
        query_find_callers(identifier, repo=repository, commit=commit, depth=depth, limit=limit),
        repository_hint=repository_hint,
        commit_hint=commit_hint,
    )
    return _wrapped_list_result(
        "find_callers",
        rows,
        subject=identifier,
        empty_hint=_EMPTY_CALL_EDGE_HINT if not rows else None,
    )


@mcp.tool(
    description=(
        "Find bounded static callees of a selected function or method. Prefer symbol_id. "
        "At depth 1, inspect indexed call-site resolutions and treat resolution='heuristic' "
        "as approximate; deeper traversal uses exact-only projected edges. Zero results do "
        "not exclude dynamic dispatch."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_callees(
    identifier: Annotated[
        str,
        Field(
            description=(
                "symbol_id from a prior tool, full qualified name, or repository-scoped "
                "unique dotted suffix."
            )
        ),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for qualified-name or suffix lookup."),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    depth: Annotated[int, Field(ge=1, le=10, description="Maximum CALLS traversal depth.")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    repository_hint, commit_hint = _symbol_identity_hints(identifier, repository, commit)
    rows = _normalize_public_rows(
        query_find_callees(identifier, repo=repository, commit=commit, depth=depth, limit=limit),
        repository_hint=repository_hint,
        commit_hint=commit_hint,
    )
    return _wrapped_list_result(
        "find_callees",
        rows,
        subject=identifier,
        empty_hint=_EMPTY_CALL_EDGE_HINT if not rows else None,
    )


@mcp.tool(
    description=(
        "Find a shortest bounded exact static-call path between two functions or methods "
        "in the same repository snapshot. Prefer symbol_id; qualified names or unique "
        "dotted suffixes require repository. No result means only that no indexed exact "
        "path exists within max_depth; runtime callback or dynamic paths may still exist."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def trace_call_path(
    from_identifier: Annotated[
        str,
        Field(
            description=(
                "Source symbol_id, full qualified name, or repository-scoped unique suffix."
            )
        ),
    ],
    to_identifier: Annotated[
        str,
        Field(
            description=(
                "Target symbol_id, full qualified name, or repository-scoped unique suffix."
            )
        ),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for qualified-name or suffix endpoints."),
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
        "List files with indexed Python import edges to a module. Use an exact module key "
        "or full module qualified name; module suffixes and bare final segments are not "
        "resolved. Results are capped and grouped by repository and file path."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_importers(
    module_identifier: Annotated[
        str,
        Field(description="Exact module key or full module qualified name (no suffix matching)."),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for module qualified-name lookup."),
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
        "Return bounded ancestors or descendants through indexed inheritance and implementation "
        "edges. Prefer the type's symbol_id; full qualified names and unique dotted suffixes "
        "require repository. Direction defaults to ancestors."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_class_hierarchy(
    identifier: Annotated[
        str,
        Field(
            description=(
                "symbol_id from a prior tool, full qualified name, or repository-scoped "
                "unique dotted suffix."
            )
        ),
    ],
    repository: Annotated[
        str | None,
        Field(description="Required for qualified-name or suffix lookup."),
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
        "List functions and methods with zero inbound indexed resolved call sites, including "
        "exact and heuristic resolutions. Results are unreferenced candidates—not confirmed "
        "dead code—and may include callbacks, framework hooks, decorators, or entry points."
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
        "Discover SQL objects parsed from PostgreSQL .sql files selected by enabled [sql] "
        "include/exclude configuration in codekg.toml. Call list_repositories first when "
        "the repository is unknown. Embedded Python strings and live database state are not "
        "indexed; dynamic names may remain unresolved. Call get_sql_object with the "
        "returned object_key."
    )
)
def search_sql_objects(
    query: Annotated[
        str, Field(description="Substring to match against schema-qualified SQL object names.")
    ],
    repository: Annotated[
        str | None,
        Field(
            description=(
                "Indexed repository name. Required when more than one repository is indexed."
            )
        ),
    ] = None,
    database: Annotated[
        str | None, Field(description="Optional logical database name filter.")
    ] = None,
    schema: Annotated[str | None, Field(description="Optional SQL schema filter.")] = None,
    kind: Annotated[SqlObjectKind | None, Field(description="Optional SQL object kind filter.")] = (
        None
    ),
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    limit: Annotated[
        int, Field(ge=1, le=20, description="Maximum compact SQL object candidates to return.")
    ] = 5,
) -> ToolResult:
    response = dict(
        query_search_sql_objects(
            query,
            repository=repository,
            database=database,
            schema=schema,
            kind=kind,
            commit=commit,
            limit=limit,
        )
    )
    results = response.get("results")
    if isinstance(results, list) and all(isinstance(row, dict) for row in results):
        response["results"] = _normalize_public_rows(
            results,
            repository_hint=response.get("repository")
            if isinstance(response.get("repository"), str)
            else None,
            commit_hint=response.get("commit") if isinstance(response.get("commit"), str) else None,
        )
    text = _sql_search_summary(response)
    return ToolResult(content=text, structured_content=response)


@mcp.tool(
    description=(
        "Return one indexed SQL object and its definition sites. Prefer object_key from "
        "search_sql_objects; otherwise use schema.object_name with repository, and provide "
        "database when needed to disambiguate."
    )
)
def get_sql_object(
    identifier: Annotated[str, Field(description="SQL object_key or schema.object_name.")],
    repository: Annotated[
        str | None, Field(description="Required for schema.object_name lookup.")
    ] = None,
    database: Annotated[
        str | None,
        Field(
            description=("Logical database filter; required when schema.object_name is ambiguous.")
        ),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
) -> ToolResult:
    response = dict(
        query_get_sql_object(
            identifier,
            repository=repository,
            database=database,
            commit=commit,
        )
    )
    definitions = response.get("definitions")
    if isinstance(definitions, list) and all(isinstance(row, dict) for row in definitions):
        response["definitions"] = _normalize_public_rows(
            definitions,
            repository_hint=response.get("repository")
            if isinstance(response.get("repository"), str)
            else None,
            commit_hint=response.get("commit") if isinstance(response.get("commit"), str) else None,
        )
    object_row = response.get("object")
    if isinstance(object_row, dict) and isinstance(object_row.get("owner_path"), str):
        owner_path = object_row["owner_path"]
        object_row["owner_path"] = _validate_relative_public_path(owner_path.replace("\\", "/"))
    text = _sql_object_summary(response)
    return ToolResult(content=text, structured_content=response)


@mcp.tool(
    description=(
        "Find bounded static usages of one SQL object. Default resolution='exact' returns "
        "statically resolved edges, not runtime proof. Ambiguous, unresolved, and dynamic "
        "modes return candidate references associated with the object. Prefer object_key."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def find_sql_usages(
    identifier: Annotated[str, Field(description="SQL object_key or schema.object_name.")],
    repository: Annotated[
        str | None, Field(description="Required for schema.object_name lookup.")
    ] = None,
    database: Annotated[
        str | None,
        Field(
            description=("Logical database filter; required when schema.object_name is ambiguous.")
        ),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    role: Annotated[
        SqlUsageRole,
        Field(
            description=(
                "Filter by read, write, call, alter, drop, or define; all disables role filtering."
            )
        ),
    ] = "all",
    resolution: Annotated[
        SqlReferenceResolution,
        Field(
            description=(
                "exact returns authoritative derived edges only; ambiguous/unresolved/dynamic "
                "include candidate Reference nodes; all returns both."
            )
        ),
    ] = "exact",
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum rows to return.")] = 50,
) -> ToolResult:
    rows = query_find_sql_usages(
        identifier,
        repository=repository,
        database=database,
        commit=commit,
        role=role,
        resolution=resolution,
        limit=limit,
    )
    repository_hint = rows[0].get("repo") if rows else repository
    commit_hint = rows[0].get("commit") if rows else commit
    return _wrapped_list_result(
        "find_sql_usages",
        _normalize_public_rows(
            rows,
            repository_hint=repository_hint if isinstance(repository_hint, str) else None,
            commit_hint=commit_hint if isinstance(commit_hint, str) else None,
        ),
    )


@mcp.tool(
    description=(
        "Return parsed SQL artifacts, statements, and references for one indexed "
        "repository-relative .sql file in a selected snapshot. Set include_text=true only "
        "when source text is needed. limit bounds each returned collection."
    )
)
def get_sql_in_file(
    file: Annotated[str, Field(description="Repository-relative path to an indexed .sql file.")],
    repository: Annotated[str, Field(description="Indexed repository name.")],
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    include_text: Annotated[
        bool, Field(description="Include SqlArtifact.text in the response.")
    ] = False,
    limit: Annotated[
        int,
        Field(ge=1, le=500, description="Maximum rows per returned collection (artifacts, etc.)."),
    ] = 100,
) -> ToolResult:
    response = dict(
        query_get_sql_in_file(
            file,
            repository=repository,
            commit=commit,
            include_text=include_text,
            limit=limit,
        )
    )
    text = _sql_file_summary(response)
    return ToolResult(content=text, structured_content=response)


def _sql_search_summary(response: dict[str, object]) -> str:
    status = str(response.get("status", "ok"))
    repository = response.get("repository")
    if status != "ok":
        return (
            f"SQL object discovery status={status}; repository={repository!s}. "
            "See structured result."
        )
    results = response.get("results", [])
    count = len(results) if isinstance(results, list) else 0
    return (
        f"Found {count} SQL object candidate(s) in repository={repository!s}. "
        "See structured result."
    )


def _sql_object_summary(response: dict[str, object]) -> str:
    status = str(response.get("status", "ok"))
    if status != "ok":
        return f"SQL object lookup status={status}. See structured result."
    object_row = response.get("object")
    name = None
    if isinstance(object_row, dict):
        schema_name = object_row.get("schema_name")
        object_name = object_row.get("object_name")
        if isinstance(schema_name, str) and isinstance(object_name, str):
            name = f"{schema_name}.{object_name}"
    definitions = response.get("definitions", [])
    definition_count = len(definitions) if isinstance(definitions, list) else 0
    return (
        f"Resolved SQL object {name!s} with {definition_count} definition site(s). "
        "See structured result."
    )


def _sql_file_summary(response: dict[str, object]) -> str:
    status = str(response.get("status", "ok"))
    file_path = response.get("file")
    if status != "ok":
        return f"SQL file lookup status={status}; file={file_path!s}. See structured result."
    statements = response.get("statements", [])
    references = response.get("references", [])
    statement_count = len(statements) if isinstance(statements, list) else 0
    reference_count = len(references) if isinstance(references, list) else 0
    return (
        f"Indexed SQL structure for file={file_path!s}: "
        f"{statement_count} statement(s), {reference_count} reference(s). "
        "See structured result."
    )


@mcp.tool(
    description=(
        "With identifier, return cyclomatic complexity for one callable; prefer symbol_id, "
        "while qualified names or unique dotted suffixes require repository. Without "
        "identifier, return the top top_n callables, optionally filtered by repository and "
        "commit; omitting repository ranks across all indexed repositories."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_complexity(
    identifier: Annotated[
        str | None,
        Field(
            description=(
                "Optional symbol_id, full qualified name, or repository-scoped unique suffix."
            )
        ),
    ] = None,
    repository: Annotated[
        str | None,
        Field(description="Repository filter; required for qualified-name or suffix lookup."),
    ] = None,
    commit: Annotated[str | None, Field(description="Optional indexed commit filter.")] = None,
    top_n: Annotated[
        int | None,
        Field(ge=1, le=500, description="Top-N ranking when identifier is omitted."),
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


@mcp.tool(
    description="List indexed PostgreSQL corpus snapshot aliases, pins and source fingerprints.",
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def list_corpus_snapshots(
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
) -> ToolResult:
    return _wrapped_list_result(
        "list_corpus_snapshots", query_list_corpus_snapshots(limit=limit, offset=offset)
    )


@mcp.tool(
    description=(
        "Search C/native symbols and SQL routine declarations in one corpus snapshot alias. "
        "Returns exact graph keys, signatures, source locations and coverage."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def search_corpus_symbols(
    query: str,
    snapshot_alias: str,
    kind: Literal["native", "routine", "all"] = "all",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
) -> ToolResult:
    return _wrapped_list_result(
        "search_corpus_symbols",
        query_search_corpus_symbols(
            query, snapshot_alias=snapshot_alias, kind=kind, limit=limit, offset=offset
        ),
    )


@mcp.tool(
    description=(
        "Inspect direct cross-language dependency evidence for an exact corpus graph key. "
        "Candidate links stay explicitly uncertain; documentation is not execution."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def get_dependency_evidence(
    key: str,
    direction: Literal["outgoing", "incoming"] = "incoming",
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
) -> ToolResult:
    return _wrapped_list_result(
        "get_dependency_evidence",
        query_get_dependency_evidence(key, direction=direction, limit=limit, offset=offset),
    )


@mcp.tool(
    description=(
        "Trace a bounded shortest source-evidence path from literal SQL/runbook/routine/native "
        "key to a native API key. Candidate and conditional links are excluded; DOCUMENTS "
        "links remain distinguishable. Not an execution or ABI compatibility proof."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def trace_corpus_path(
    from_key: str,
    to_key: str,
    max_depth: Annotated[int, Field(ge=1, le=8)] = 6,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> ToolResult:
    return _wrapped_list_result(
        "trace_corpus_path",
        query_trace_corpus_path(from_key, to_key, max_depth=max_depth, limit=limit),
    )


@mcp.tool(
    description=(
        "Compare two corpus snapshots of the same logical repository for added/removed APIs, "
        "signature/body/definition changes and ambiguous matches. Source-level findings only; "
        "limited extraction coverage must be considered before drawing "
        "upgrade/security conclusions."
    ),
    output_schema=_WRAPPED_LIST_OUTPUT_SCHEMA,
)
def compare_corpus_snapshots(
    left_alias: str,
    right_alias: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
    offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
) -> ToolResult:
    return _wrapped_list_result(
        "compare_corpus_snapshots",
        query_compare_corpus_snapshots(left_alias, right_alias, limit=limit, offset=offset),
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
