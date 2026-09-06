# Requests benchmark run

> **Legacy/deprecated workflow.** This document is retained as historical
> material only. The sole canonical paired runner is
> `evaluation/run_benchmark.py`, documented in `evaluation/README.md`. Do not
> use the standalone commands below for new benchmark results: they do not
> implement the canonical frozen schedule, immutable state checks, validation,
> or aggregation contract.

## Environment

Run from the CodeKG repository root:

```bash
REQUESTS_DIR=/media/alex/MYSSD/BACKUP/workspace/codekg-corpus/requests
RUN_DIR=runs/requests-intent-001
mkdir -p "$RUN_DIR/codekg" "$RUN_DIR/native"
```

> **Warning:** this host cannot create the Bubblewrap loopback namespace used
> by Codex's `read-only` sandbox (`bwrap: loopback: Failed RTM_NEWADDR`).
> `read-only` is unsupported here. Both benchmark arms use no-approval,
> `danger-full-access`; run them only against a trusted, dedicated benchmark
> checkout. The prompts prohibit writes, but this configuration is unsafe for
> untrusted repositories.

## Preflight

Confirm that the MCP indexes `requests`:

```bash
codex --ask-for-approval never exec \
  --profile benchmark \
  --ephemeral \
  --json \
  --sandbox danger-full-access \
  --ignore-rules \
  --cd "$REQUESTS_DIR" \
  -c 'features.multi_agent=false' \
  -c 'mcp_servers.codekg.enabled=true' \
  'Call codekg.list_repositories exactly once. Return only whether requests is present.' \
  > "$RUN_DIR/preflight.jsonl" \
  2> "$RUN_DIR/preflight.stderr.log"
```

Do not proceed unless the completed `list_repositories` result includes `requests`.

## Native arm (canonical wrapper)

Use the wrapper as the canonical native path. It runs mandatory preflight
first, preserves JSONL/stderr output, and refuses to overwrite existing native
or preflight result files unless `BENCHMARK_OVERWRITE=1` is explicitly set. It
also rejects a full run without a completed successful command event, valid JSON
answer, and a non-`INFRA_ERROR:` answer. For every evidence entry it also
checks the repository-relative Python file and exact AST definition
`start_line`/`end_line` for completeness. The end may include only immediately
trailing blank separators after the AST definition; incomplete or approximate
ranges are rejected:

```bash
REQUESTS_DIR="$REQUESTS_DIR" RUN_DIR="$RUN_DIR" \
  bash scripts/run-requests-native-benchmark.sh
```

An `INFRA_ERROR` answer without an emitted actual `command_execution` event is
invalid. Do not run the full native task unless `native-preflight.jsonl`
contains a completed successful shell-command event and `NATIVE_PREFLIGHT_OK`;
the wrapper enforces this condition.

## CodeKG arm

```bash
mkdir -p runs/requests-intent-001/codekg

time codex --ask-for-approval never exec \
  --profile benchmark \
  --ephemeral \
  --json \
  --sandbox danger-full-access \
  --ignore-rules \
  --cd "$REQUESTS_DIR" \
  -c 'features.multi_agent=false' \
  -c 'mcp_servers.codekg.enabled=true' \
  --output-schema evaluation/codex-answer.schema.json \
  -o "$RUN_DIR/codekg/answer.json" \
  "$(cat evaluation/prompts/requests-intent-001-codekg.txt)" \
  > "$RUN_DIR/codekg/events.jsonl" \
  2> "$RUN_DIR/codekg/stderr.log"
```

## Validity checks

The CodeKG arm is valid only when:

- batch preflight (outside the measured task) succeeds, including its
  `list_repositories` repository/commit check;
- the measured task does not call `list_repositories`;
- at least one `search_symbols` call succeeds;
- no more than two `search_symbols` calls occur;
- one `get_definition` call succeeds for the selected exact `symbol_id`;
- `find_callers` and `find_callees` are attempted for that same ID, with at
  least one successful relationship result;
- the initial search uses `repository: "requests"`, `scope: "source"`, and a
  limit no greater than 5; after a plausible candidate it expands that ID rather
  than issuing another broad search;
- every reported symbol appears in MCP evidence;
- source line ranges are non-null.

The native arm is valid only when:

- the preflight and task contain completed shell-command events;
- at least one completed command searches the repository;
- the selected source files are read;
- source line ranges are non-null.

An `INFRA_ERROR` response is invalid without an emitted actual command attempt
and its explicit error event.

A guessed answer with no repository evidence is invalid even when the symbol happens to
be correct.
