# CodeKG MCP tool test requests: `requests`

## Target and conventions

Run these requests against `http://127.0.0.1:8765/mcp`. The current indexed
target is repository `requests` at commit `f361ead047be`. Qualified names in
this checkout include the source-root prefix, for example
`src.requests.sessions.Session.prepare_request`.

Use the `symbol_id` returned by `search_symbols` whenever a request below uses
an angle-bracket placeholder. That makes the expansion tests independent of
line-number or qualified-name convention changes.

`search_symbols` uses `repository` and defaults to `scope: "source"`; all
selector and relationship tools use `repo`. Every request should preserve the
returned repository and commit and must not silently cross into another indexed
repository. Scope diagnostics describe filtering of the bounded candidate pool,
not a strict backend-scope-recall guarantee.

## Fixture discovery

Run these once before the placeholder-based requests:

```json
{"repository":"requests","query":"prepare request session cookies auth","mode":"hybrid","limit":5}
```

Record the `symbol_id` for `Session.prepare_request` as
`<prepare_request_id>`, and find `Session.request`, `PreparedRequest.prepare`,
and `HTTPAdapter` in the same way when needed.

## `list_repositories`

1. `{}` — expect an entry named `requests`, commit `f361ead047be`, and a
   non-zero file count.
2. `{}` after a scoped search — expect the same `requests` entry; this verifies
   discovery does not mutate repository metadata.

## `search_symbols`

1. `{"repository":"requests","query":"prepare request session cookies auth","mode":"hybrid","limit":5}`
   — omit `scope` to exercise the `source` default; expect no more than five
   compact hits, `scope: "source"` diagnostics, and a `Session.prepare_request`
   hit. When a candidate is returned, expect `recommended_next_tool:
   "get_definition"` and its `recommended_symbol_id`.
2. `{"repository":"requests","query":"Session.prepare_request","mode":"graph","limit":5}`
   — expect an exact-name/qualified-name candidate with `symbol_id`, file, line
   bounds, score, and `matched_terms`; no full source body is returned.
3. `{"repository":"requests","query":"prepare request","mode":"lexical","limit":5}`
   — expect only `requests` candidates. If `next_cursor` is present, repeat the
   identical request with that cursor and assert no duplicate `symbol_id` from
   the first page.
4. Repeat a compact query with each explicit scope:
   `{"repository":"requests","query":"prepare request","scope":"tests","limit":5}`,
   `{"repository":"requests","query":"prepare request","scope":"docs","limit":5}`,
   `{"repository":"requests","query":"prepare request","scope":"examples","limit":5}`,
   `{"repository":"requests","query":"prepare request","scope":"benchmarks","limit":5}`,
   and `{"repository":"requests","query":"prepare request","scope":"all","limit":5}`.
   — for each, assert the response scope and diagnostics report the requested
   scope. Empty results are valid; do not infer strict scope recall beyond the
   bounded backend candidate pool.

## `get_definition`

1. `{"identifier":"<prepare_request_id>"}` — expect the exact method
   definition metadata and non-null source line bounds.
2. `{"identifier":"src.requests.sessions.Session.prepare_request","repo":"requests","commit":"f361ead047be"}`
   — expect the same selected symbol as the exact-ID request.
3. `{"identifier":"src.requests.sessions.Session.prepare_request","repo":"click"}`
   — expect a selector error; it must not return a `requests` definition.

## `find_callers`

1. `{"identifier":"<prepare_request_id>","depth":1,"limit":20}` — inspect
   direct resolved callers of the preparation method.
2. `{"identifier":"src.requests.sessions.Session.prepare_request","repo":"requests","depth":2,"limit":20}`
   — expect only `requests` snapshot-local callers and depths no greater than 2.
3. `{"identifier":"<prepare_request_id>","depth":1,"limit":1}` — expect at
   most one row, proving the relationship bound is honored.

## `find_callees`

1. `{"identifier":"<prepare_request_id>","depth":1,"limit":20}` — inspect
   direct resolved helpers used to construct the prepared request.
2. `{"identifier":"<session_request_id>","depth":2,"limit":20}` — inspect
   the orchestration path below `Session.request`; results remain within the
   selected snapshot.
3. `{"identifier":"<prepare_request_id>","depth":1,"limit":1}` — expect at
   most one row.

## `trace_call_path`

1. `{"from_identifier":"<session_request_id>","to_identifier":"<prepare_request_id>","repo":"requests","max_depth":3,"limit":3}`
   — if the static edge is represented, expect paths whose endpoints match the
   supplied IDs; an empty list is acceptable for unresolved dynamic dispatch.
2. `{"from_identifier":"<session_request_id>","to_identifier":"<prepared_request_prepare_id>","repo":"requests","max_depth":5,"limit":1}`
   — expect no more than one bounded path.
3. Repeat request 1 with `repo:"click"` — expect an identity/snapshot error,
   never a cross-repository path.

## `find_importers`

1. `{"module_identifier":"src.requests.sessions","repo":"requests","limit":20}`
   — list files importing the sessions module, if imports exist in this
   snapshot.
2. `{"module_identifier":"src.requests.models","repo":"requests","limit":20}`
   — inspect imports of the module containing `PreparedRequest`.
3. Repeat request 2 with `limit:1` — expect at most one row.

## `get_class_hierarchy`

1. `{"identifier":"src.requests.adapters.HTTPAdapter","repo":"requests","direction":"ancestors","depth":3,"limit":20}`
   — inspect declared base types, if any.
2. `{"identifier":"src.requests.adapters.HTTPAdapter","repo":"requests","direction":"descendants","depth":3,"limit":20}`
   — inspect local subclasses; an empty list is valid.
3. Repeat request 1 with `limit:1` — expect at most one related type.

## `find_dead_code`

1. `{"repo":"requests","commit":"f361ead047be","limit":20}` — return
   unreferenced callable candidates only from `requests`; treat them as leads,
   not confirmed dead code.
2. `{"repo":"requests","limit":1}` — expect at most one candidate.
3. `{"repo":"click","limit":20}` — negative scope control: no returned row
   may identify `requests`.

## `get_complexity`

1. `{"repo":"requests","commit":"f361ead047be","top_n":10}` — list the
   ten highest-complexity `requests` callables.
2. `{"identifier":"<prepare_request_id>"}` — return complexity for the exact
   preparation method.
3. `{"repo":"requests","top_n":1}` — expect exactly zero or one row.

## Result checks

For every successful result, record tool name, arguments, elapsed time, result
count, repository/commit, and returned `symbol_id` values. Reject a run if a
scoped `requests` request yields another repository, line ranges are missing
for a reported definition, or a text payload repeats the full structured
payload.
