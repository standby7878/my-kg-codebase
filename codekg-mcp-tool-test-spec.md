# CodeKG MCP tool test requests: `requests`

## Target and conventions

Run these requests against `http://127.0.0.1:8765/mcp`. The current indexed
target is repository `requests` at commit `f361ead047be`. Qualified names in
this checkout include the source-root prefix, for example
`src.requests.sessions.Session.prepare_request`.

Use the `symbol_id` returned by `search_symbols` whenever a request below uses
an angle-bracket placeholder. That makes the expansion tests independent of
line-number or qualified-name convention changes.

`search_symbols` uses `repository`; all selector and relationship tools use
`repo`. Every request should preserve the returned repository and commit and
must not silently cross into another indexed repository.

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
   — expect no more than five compact hits and a `Session.prepare_request` hit.
2. `{"repository":"requests","query":"Session.prepare_request","mode":"graph","limit":5}`
   — expect an exact-name/qualified-name candidate with `symbol_id`, file, line
   bounds, score, and `matched_terms`; no full source body is returned.
3. `{"repository":"requests","query":"prepare request","mode":"lexical","limit":5}`
   — expect only `requests` candidates. If `next_cursor` is present, repeat the
   identical request with that cursor and assert no duplicate `symbol_id` from
   the first page.

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

## Paired Codex benchmark reproduction

The following commands capture one CodeKG arm and one native-search arm for
task `requests-intent-001`. Run them from the `my-kg-codebase` root after
setting `REQUESTS_DIR` to the frozen Requests checkout and `RUN_DIR` to a new
artifact directory.

The command line intentionally combines `--ignore-user-config` with explicit
model, reasoning, feature, MCP endpoint, and timeout values. This prevents
user-level tools or MCP servers from entering either arm while keeping the
effective benchmark contract identical. Do not add
`mcp_servers.codekg.default_tools_approval_mode`; the CodeKG arm uses an exact
four-tool allowlist instead.

```bash
BENCHMARK_EVAL_DIR="$PWD/evaluation"

mkdir -p "$RUN_DIR/codekg" "$RUN_DIR/native"
```

### CodeKG arm

```bash
time codex --ask-for-approval never exec \
  --profile benchmark \
  --ignore-user-config \
  --strict-config \
  --ephemeral \
  --json \
  --sandbox danger-full-access \
  --ignore-rules \
  --cd "$REQUESTS_DIR" \
  -c 'model="gpt-5.4-mini"' \
  -c 'model_reasoning_effort="low"' \
  -c 'features.multi_agent=false' \
  -c 'features.apps=false' \
  -c 'features.plugins=false' \
  -c 'web_search="disabled"' \
  -c 'mcp_servers.codekg.url="http://127.0.0.1:8765/mcp"' \
  -c 'mcp_servers.codekg.startup_timeout_sec=15' \
  -c 'mcp_servers.codekg.tool_timeout_sec=30' \
  -c 'mcp_servers.codekg.enabled=true' \
  -c 'mcp_servers.codekg.required=true' \
  -c 'mcp_servers.codekg.enabled_tools=["search_symbols","get_definition","find_callers","find_callees"]' \
  --output-schema "$BENCHMARK_EVAL_DIR/codex-answer.schema.json" \
  -o "$RUN_DIR/codekg/answer.json" \
  "$(< "$BENCHMARK_EVAL_DIR/prompts/requests-intent-001-codekg.txt")" \
  > "$RUN_DIR/codekg/events.jsonl" \
  2> "$RUN_DIR/codekg/stderr.log"
```

### Native arm

```bash
time codex --ask-for-approval never exec \
  --profile benchmark \
  --ignore-user-config \
  --strict-config \
  --ephemeral \
  --json \
  --sandbox danger-full-access \
  --ignore-rules \
  --cd "$REQUESTS_DIR" \
  -c 'model="gpt-5.4-mini"' \
  -c 'model_reasoning_effort="low"' \
  -c 'features.multi_agent=false' \
  -c 'features.apps=false' \
  -c 'features.plugins=false' \
  -c 'web_search="disabled"' \
  -c 'mcp_servers.codekg.url="http://127.0.0.1:8765/mcp"' \
  -c 'mcp_servers.codekg.startup_timeout_sec=15' \
  -c 'mcp_servers.codekg.tool_timeout_sec=30' \
  -c 'mcp_servers.codekg.enabled=false' \
  -c 'mcp_servers.codekg.required=false' \
  --output-schema "$BENCHMARK_EVAL_DIR/codex-answer.schema.json" \
  -o "$RUN_DIR/native/answer.json" \
  "$(< "$BENCHMARK_EVAL_DIR/prompts/requests-intent-001-native.txt")" \
  > "$RUN_DIR/native/events.jsonl" \
  2> "$RUN_DIR/native/stderr.log"
```

## Validated paired observation

The following single-pair observation was captured on 2026-07-27 against
Requests commit `f361ead047be5cb873174218582f7d8b9fcd9f49` with Codex CLI
`0.144.5`, model `gpt-5.4-mini`, and reasoning effort `low`.

| Measure | CodeKG | Native |
| --- | ---: | ---: |
| Validator result | valid | invalid |
| Primary symbol correct | yes | yes |
| Related-symbol result | two exact matches | no valid related match |
| Evidence compliant | yes | no |
| Unsupported claims | 0 | 1 |
| Tool calls | 4 | 5 |
| Input tokens | 42,173 | 48,681 |
| Cached input tokens | 31,232 | 32,768 |
| Uncached input tokens | 10,941 | 15,913 |
| Output tokens | 614 | 835 |
| Reasoning tokens | 120 | 200 |
| Cache-hit ratio | 74.1% | 67.3% |

Both arms correctly selected
`src.requests.sessions.Session.prepare_request` at
`src/requests/sessions.py:511-555`. The CodeKG arm also returned the two
gold-relevant relationship definitions with their exact indexed ranges:

```text
src.requests.sessions.Session.request
src/requests/sessions.py:557-653

src.requests.models.PreparedRequest.prepare
src/requests/models.py:424-451
```

The native arm reported `PreparedRequest.prepare` as `424-452`, including the
blank separator after the last syntax line. Its structured evidence therefore
failed with:

```text
native evidence does not match an AST definition:
PreparedRequest.prepare (424, 452)
```

It also described `Session.request` as `557-651` without inspecting or
reporting its complete definition; the actual final `return resp` is line 653.
Consequently the native trial was not evidence-compliant and had one
unsupported claim.

For this pair, CodeKG used 13.4% fewer total input tokens, 31.2% fewer
uncached input tokens, 26.5% fewer output tokens, 40% fewer reasoning tokens,
and one fewer tool call. The native arm's initial repository-wide `rg`
produced a large result spanning source, tests, documentation, and history,
then performed overlapping numbered inspections of `PreparedRequest.prepare`.
The CodeKG arm instead completed the required
`search_symbols` → `get_definition` → `find_callers` → `find_callees`
sequence with bounded structured results.

This observation establishes correctness and cost for one paired trial only.
Use the frozen warm-up plus ten paired repetitions in
`evaluation/run_benchmark.py` for aggregate conclusions and confidence
intervals. The JSONL events do not contain reliable per-tool timestamps, so
this observation does not compare MCP latency.
