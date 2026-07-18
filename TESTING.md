# Testing and verification

## Prerequisites

- Docker with Docker Compose (the `docker compose` plugin).
- Python 3.12.
- The vendored `zvec` dependency available in the checkout.

Create and activate a virtual environment, then install the project’s test
dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Local verification

Run Ruff and formatting checks:

```bash
ruff check .
ruff format --check .
```

Run the test suites individually or together:

```bash
.venv/bin/python -m pytest -q tests/unit
.venv/bin/python -m pytest -q     # full pytest suite
```

For a quick check of the bulk-ingestion implementation and its command-line
surface, run the focused tests:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_bulk_export.py \
  tests/unit/test_bulk_import.py \
  tests/unit/test_cli.py \
  tests/unit/test_run_compose.py
```

Validate Compose configuration before starting services, and build the image:

```bash
docker compose -f compose/dev-local/docker-compose.yml --env-file compose/dev-local/env config --quiet
bash run-compose.sh dev-local build
```

Run the targeted operational integration tests:

```bash
.venv/bin/python -m pytest -q tests/integration/test_operational_isolation.py tests/integration/test_mcp_transport_http.py
```

## Benchmark readiness

Before running benchmarks, confirm that the Compose configuration and image
build pass, services become ready, and the operational smoke tests pass. Read
the benchmark instructions in the benchmark README and use its documented
runner; do not substitute an ad-hoc command. Copilot trials are not run by the
test suites and require a signed-off corpus and ground truth before execution.

## End-to-end corpus, MCP, and Copilot runbook

This runbook has two deliberately separate layers:

1. **Repository corpus:** pinned Python checkouts that CodeKG indexes into
   Neo4j and zvec.
2. **Benchmark corpus:** frozen prompts, truth records, schedules, and result
   logs under [`benchmark/`](benchmark/README.md).

Do not use a dirty development checkout as a benchmark corpus. A benchmark
answer is only comparable when every run sees the same source commit.

### 1. Prepare repositories for processing

Create or reuse clean local clones. Record every repository name, absolute
path, commit, and typing-zone label (for example `strict_internal`,
`strict_test`, `generated_code`, or `untyped_external_boundary`). The
repository directory name becomes its CodeKG repository name, so names must be
unique.

```bash
git clone <repository-url> /work/codekg-corpus/service-a
git -C /work/codekg-corpus/service-a checkout <pinned-commit>
git -C /work/codekg-corpus/service-a status --porcelain

git clone <repository-url> /work/codekg-corpus/service-b
git -C /work/codekg-corpus/service-b checkout <pinned-commit>
git -C /work/codekg-corpus/service-b status --porcelain
```

Each `status --porcelain` command must produce no output. Avoid `reset --hard`
or `clean -fdx` in this guide: decide explicitly whether destructive cleanup is
appropriate for a particular checkout.

Place Markdown specifications, design notes, and API documentation inside the
code repository they describe. CodeKG currently enriches callable descriptions
from in-repository Markdown; it does not independently index a separate
specifications-only repository.

### 2. Configure and index the repository corpus

Set `CODEKG_REPOS_ROOT` in
[`compose/dev-local/env`](compose/dev-local/env) to semicolon-separated paths.
Paths may be absolute or relative to `compose/dev-local/`.

```dotenv
CODEKG_REPOS_ROOT=/work/codekg-corpus/service-a;/work/codekg-corpus/service-b
```

Build the local image, start Neo4j plus the HTTP MCP server, then index every
configured repository. Indexing is an operator action; MCP clients cannot
trigger it. `index-sources` defaults to the staged bulk path (`--mode auto`):
it exports the full configured corpus to CSV, creates a new Neo4j store with
`neo4j-admin database import full`, builds the matching zvec index, validates
the callable keys, and then publishes the new generation. The running graph is
left in place if export, import, schema setup, or validation fails.

```bash
bash run-compose.sh dev-local build
bash run-compose.sh dev-local start
bash run-compose.sh dev-local index-sources
```

Use the explicit transactional mode only for a targeted update or operational
debugging. It performs the existing batched Cypher writes and is not an
automatic fallback for a failed bulk import:

```bash
bash run-compose.sh dev-local index-sources --mode transactional
```

Use `--mode bulk` when a CI or operational run must require the offline import
path:

```bash
bash run-compose.sh dev-local index-sources --mode bulk
```

The active graph, zvec, log, and CSV-staging volume names are recorded in the
ignored `compose/dev-local/runtime.env` pointer file. This makes the bulk CSVs
available for diagnosis after a failed run without replacing the active
generation. Treat generation volumes as immutable; inspect or remove them only
after confirming that they are not the active generation.

Confirm that every intended repository was indexed:

```bash
docker compose \
  -f compose/dev-local/docker-compose.yml \
  --env-file compose/dev-local/env \
  run --rm ingestion codekg list
```

Re-run `index-sources` whenever a pinned checkout intentionally changes. It
replaces that repository's Neo4j snapshot and derived zvec records.

### 3. Inspect the live MCP server

CodeKG serves streamable HTTP MCP at:

```text
http://127.0.0.1:8765/mcp
```

For an interactive protocol inspection, install Node.js 22.7.5 or later and
start the official MCP Inspector locally:

```bash
npx -y @modelcontextprotocol/inspector
```

Open `http://127.0.0.1:6274`, choose the HTTP/streamable HTTP transport, and
connect it to `http://127.0.0.1:8765/mcp`. In the Inspector:

1. Initialize the session and run `tools/list`.
2. Call `list_repositories` and compare the result with the ingestion `list`
   command above.
3. Call `search_symbols` in `graph` mode with a repository filter.
4. Call `search_symbols` in `lexical` mode with a docstring, normalized-name,
   or Markdown phrase, then use its exact returned key with `get_definition`.
5. For one known function, call `find_callers` and `find_callees`; treat
   heuristic/dynamic outcomes as evidence bounds rather than runtime proof.

The Inspector is a local development tool. Do not expose its UI or the CodeKG
MCP endpoint beyond loopback.

### 4. Attach CodeKG to Copilot and perform a smoke check

Use a dedicated Copilot configuration directory so the experiment does not
alter normal CLI settings. Confirm separately that the account is entitled to
the exact GPT-4.1 model identifier before recording benchmark data.

```bash
copilot --config-dir .benchmark-copilot-smoke mcp add \
  --transport http \
  codekg http://127.0.0.1:8765/mcp
```

Start a one-off smoke session from a disposable checkout:

```bash
cd /work/codekg-corpus/service-a
copilot \
  --config-dir /absolute/path/to/.benchmark-copilot-smoke \
  --model gpt-4.1 \
  --disable-builtin-mcps
```

Ask for a concrete, read-only task such as:

```text
Use the CodeKG MCP to list indexed repositories, then find one function in
<repository-name> by qualified name. Return the repository, exact key, path,
and source lines. Do not modify files.
```

Verify that Copilot can see the `codekg` tools, calls `list_repositories`, and
returns an exact symbol key that Inspector and `get_definition` can resolve.
Do not count this smoke run as benchmark data.

### 5. Freeze and run the formal test corpus

Before trials, create a signed-off corpus record for every repository and a
truth record for every prompt. Use the templates in:

- [`benchmark/prompts/`](benchmark/prompts/README.md)
- [`benchmark/truth/`](benchmark/truth/README.md)
- [`benchmark/schedules/`](benchmark/schedules/README.md)

For each prompt, prepare independent source-reviewed truth: expected keys,
qnames, file paths, edges, required points, and forbidden claims. Do not create
truth solely from CodeKG output.

Run the frozen schedule with fresh sessions and separate conditions:

```text
B  = normal Copilot repository tools, no CodeKG MCP
M  = normal tools plus CodeKG MCP
MF = M with the explicit "use CodeKG first" diagnostic instruction
```

The benchmark runner writes JSONL logs and creates a disposable detached clone
of the supplied clean checkout for each trial. It never authorizes execution by
itself. After corpus and truth approval, invoke it with a signed-off prompt and
a log directory outside the source checkout:

```bash
benchmark/scripts/run-copilot.sh \
  M \
  /work/codekg-corpus/service-a \
  benchmark/prompts/<prompt-id>.txt \
  /work/codekg-results/<run-id>
```

Score blinded responses with
[`benchmark/results/scoring.csv`](benchmark/results/scoring.csv), retain raw
JSONL logs, then compare B/M/MF only after all scores are locked. Stop services
when the run is complete:

```bash
bash run-compose.sh dev-local stop
```
