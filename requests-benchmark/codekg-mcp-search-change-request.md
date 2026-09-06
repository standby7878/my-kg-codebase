# Task: tighten CodeKG MCP repository scoping and search-result efficiency

## Context

A Codex CLI benchmark asked CodeKG to investigate the Patroni repository. The MCP
transport worked, but `list_repositories` showed that Patroni was not indexed. The agent
was still able to call global `search_symbols`, which returned large result sets from
unrelated repositories such as `sql`, `engine`, and `click`.

The run returned 20, then 50, then 2 candidates. The responses contained the same full
records in both `content[].text` and `structured_content.result`. The CodeKG arm consumed
131,335 input tokens and took 43.820 seconds, compared with 42,702 input tokens and
25.831 seconds for the native arm. Neither arm produced verified source-line evidence.

This task is about correcting the MCP query contract and response shape. It is not about
tuning the benchmark prompt alone.

## Goals

1. Prevent cross-repository search when a task targets a specific repository.
2. Fail clearly when the requested repository is not indexed.
3. Make initial symbol discovery compact enough for iterative agent use.
4. Improve intent-search relevance so a single generic term cannot dominate ranking.
5. Preserve a separate expansion path for exact definitions and graph relationships.
6. Add measurements and tests that prevent response-size regressions.

## Required changes

### 1. Repository-scoped symbol search

Change `search_symbols` so that callers can provide an explicit repository identity.

Preferred contract:

```python
search_symbols(
    repository: str,
    query: str,
    mode: Literal["graph", "lexical", "hybrid"] = "hybrid",
    limit: int = 5,
    cursor: str | None = None,
) -> SymbolSearchResponse
```

Requirements:

- When more than one repository is indexed, `repository` must be required.
- Apply repository filtering inside the graph or lexical query, before ranking and limit.
- Do not collect global top results and filter them afterward.
- Do not silently fall back to global search.
- Return a typed `repository_not_found` result when the requested repository is absent.
- Include available repository names in that error, but do not perform a substitute search.
- Where practical, support repository name plus commit so callers can pin a revision.

### 2. Compact discovery response

The initial `search_symbols` response must contain only the fields needed to select a
candidate:

```json
{
  "symbol_id": "stable repository-qualified identifier",
  "qualified_name": "requests.sessions.Session.prepare_request",
  "file": "requests/sessions.py",
  "start_line": 450,
  "end_line": 500,
  "score": 12.34,
  "matched_terms": ["prepare", "session"]
}
```

Requirements:

- Default `limit` must be 5.
- Maximum accepted `limit` must be 20.
- Return pagination metadata when additional results exist.
- Do not include complete bodies or long docstrings.
- Any optional snippet must be capped at 160 characters.
- Avoid repeating repository and commit on every hit when they are already present in
  response-level metadata.

### 3. Definition and relationship expansion

Keep discovery separate from detail retrieval.

- `get_definition(symbol_id, ...)` returns the exact definition and bounded source text.
- `find_callers`, `find_callees`, and `trace_call_path` accept stable `symbol_id` values.
- Detail tools must enforce explicit output bounds such as maximum characters or maximum
  relationship count.
- The agent should be able to search for five candidates, inspect one or two definitions,
  and then traverse relationships without requesting another broad search.

### 4. Avoid full response duplication

Today, complete search results may appear both as serialized JSON in `content[].text` and
as objects in `structured_content.result`.

Requirements:

- Preserve `structured_content` as the canonical machine-readable result.
- If text compatibility is required, return a concise human-readable summary rather than
  a second full JSON serialization.
- Add a test that compares serialized byte sizes and fails when the text representation
  duplicates the complete structured payload.
- Verify the behavior with the Codex MCP client version used by the benchmark.

### 5. Relevance improvements

For lexical and hybrid modes:

- Weight exact symbol-name and qualified-name matches above documentation-only matches.
- Normalize identifier tokens such as snake_case and dotted qualified names.
- Prefer candidates matching multiple meaningful query terms.
- Prevent one generic term, such as `primary`, from dominating a query containing
  `standby leader promote primary`.
- Return `matched_terms` or equivalent lightweight diagnostics for benchmark analysis.
- Keep exact-name graph search available as the first stage of hybrid search.

### 6. Errors and observability

Return typed statuses rather than ambiguous empty lists where possible:

```json
{
  "status": "repository_not_found",
  "repository": "patroni",
  "available_repositories": ["click", "engine", "pool", "requests", "sql"]
}
```

Add structured logging or metrics for:

- repository;
- mode;
- requested and returned result count;
- query duration;
- serialized structured-result bytes;
- serialized text-result bytes;
- truncation or pagination;
- error status.

Do not log source bodies or sensitive local paths unnecessarily.

## Tests

Add or update tests covering at least:

1. A repository-scoped search returns results only from that repository.
2. A missing repository returns `repository_not_found`.
3. A missing repository never triggers a global fallback.
4. Repository filtering happens before ranking and limit.
5. Default limit is 5.
6. Limits greater than 20 are rejected or clamped according to the documented contract.
7. Pagination metadata is correct.
8. Search responses omit long bodies and cap snippets.
9. Text content does not duplicate the full structured payload.
10. Exact symbol-name matches outrank documentation-only generic-term matches.
11. Multi-term queries suppress candidates matching only one generic term.
12. Existing `get_definition`, caller, callee, and path behavior remains correct.
13. MCP integration tests verify both success and typed error responses.

Use the frozen synthetic corpus for deterministic edge cases and at least one indexed
real repository, such as `requests`, for integration coverage.

## Acceptance criteria

- A search explicitly scoped to `requests` returns zero hits from `click`, `engine`,
  `pool`, or `sql`.
- A search scoped to absent repository `patroni` returns `repository_not_found` and makes
  no lexical or graph search against another repository.
- The default search response contains at most 5 compact hits.
- The default response does not contain full source bodies.
- The text representation is not a full duplicate of `structured_content`.
- The serialized default response is substantially smaller than the current 20-result
  payload; record before-and-after bytes in the implementation summary.
- The query `prepare request session cookies auth` in repository `requests` places
  `Session.prepare_request` within the top 5, assuming that symbol exists in the pinned
  indexed commit.
- All unit and integration tests pass.
- Public MCP documentation and examples are updated.

## Non-goals

- Do not add semantic embedding infrastructure solely for this task.
- Do not redesign Neo4j or zvec storage.
- Do not change ingestion semantics except where required to support stable repository or
  symbol identifiers.
- Do not use prompt instructions as a substitute for enforcing repository scope in the
  MCP server.
- Do not optimize unsupported-language repositories as part of this change.
