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

The active graph, zvec, and log volume names are recorded in the ignored
`compose/dev-local/runtime.env` pointer file. The matching CSV-staging volume
uses the same generation suffix. This makes the bulk CSVs available for
diagnosis without replacing the active generation. Treat generation volumes as
immutable; inspect or remove them only after confirming that they are not the
active generation.

#### Inspect or copy the active bulk CSV snapshot

Determine the CSV-staging volume for the active graph generation, then list
its `manifest.json`, `nodes_*.csv`, and `relationships_*.csv` files:

```bash
generation=$(sed -n 's/^CODEKG_NEO4J_DATA_VOLUME=codekg-dev-local_neo4j_data_//p' \
  compose/dev-local/runtime.env)
csv_volume="codekg-dev-local_bulk_staging_${generation}"

docker run --rm \
  -v "${csv_volume}:/csv:ro" \
  --entrypoint sh codekg-app:local \
  -c 'find /csv -maxdepth 1 -type f -printf "%f %s bytes\n" | sort'
```

To copy the snapshot to `./codekg-csv-export` without modifying the source
volume, run the container as root (the staged files may be unreadable to the
application user) and restore ownership to the current host user:

```bash
mkdir -p ./codekg-csv-export

docker run --rm \
  --user 0:0 \
  -v "${csv_volume}:/csv:ro" \
  -v "$PWD/codekg-csv-export":/output \
  --entrypoint sh codekg-app:local \
  -c "cp -R /csv/. /output/ && chown -R $(id -u):$(id -g) /output"
```

The `:ro` source mount ensures the retained CSV generation is never modified.

Confirm that every intended repository was indexed:

```bash
docker compose \
  -f compose/dev-local/docker-compose.yml \
  --env-file compose/dev-local/env \
  run --rm ingestion codekg list
```

#### Test the graph in Neo4j Browser

Open `http://127.0.0.1:7474`, connect to the `neo4j` database, and run the
following read-only Cypher queries individually. The examples use the pinned
`click` corpus; replace `click` with another repository returned by the first
query when testing a different corpus generation.

Repository inventory and file counts:

```cypher
MATCH (r:Repository)
OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
RETURN r.repo_name AS repository,
       r.commit AS commit,
       r.root_path AS root_path,
       count(f) AS files
ORDER BY repository;
```

Node counts by label:

```cypher
MATCH (n)
UNWIND labels(n) AS label
RETURN label, count(*) AS nodes
ORDER BY nodes DESC, label;
```

Relationship counts by type:

```cypher
MATCH ()-[rel]->()
RETURN type(rel) AS relationship, count(*) AS relationships
ORDER BY relationships DESC, relationship;
```

Sample indexed symbols and their source files:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
WHERE s:Function OR s:Method OR s:Type
RETURN labels(s) AS labels,
       s.key AS key,
       s.qname AS qualified_name,
       s.signature AS signature,
       f.path AS file,
       s.start_line AS line
ORDER BY file, line
LIMIT 50;
```

Full-text symbol search through the same Neo4j index used by
`search_symbols(mode="graph")`:

```cypher
CALL db.index.fulltext.queryNodes("code_symbol_search", "Group")
YIELD node AS symbol, score
MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:CONTAINS]->(symbol)
WHERE r.repo_name = "click"
RETURN labels(symbol) AS labels,
       symbol.key AS key,
       symbol.qname AS qualified_name,
       f.path AS file,
       score
ORDER BY score DESC
LIMIT 25;
```

Resolved call sites and their resolution strategies:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(f:File)-[:CONTAINS]->(caller)
MATCH (caller)-[:HAS_CALLSITE]->(site:CallSite)-[resolution:RESOLVES_TO]->(callee)
WHERE caller:Function OR caller:Method
RETURN caller.qname AS caller,
       site.raw_callee AS call_expression,
       resolution.strategy AS strategy,
       callee.qname AS callee,
       f.path AS file,
       site.start_line AS line
ORDER BY caller, line
LIMIT 50;
```

Visualize a bounded exact-call subgraph:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(:File)-[:CONTAINS]->(source)
MATCH path = (source)-[:EXACT_CALLS*1..2]->(target)
WHERE (source:Function OR source:Method)
  AND (target:Function OR target:Method)
  AND ALL(node IN nodes(path) WHERE node.key STARTS WITH r.repo_name + "@" + r.commit + ":")
RETURN path
LIMIT 50;
```

Find a concrete call path whose endpoint keys can be pasted into the MCP
Inspector `trace_call_path` tool:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(:File)-[:CONTAINS]->(source)
MATCH path = (source)-[:EXACT_CALLS*2..4]->(target)
WHERE ALL(node IN nodes(path) WHERE node.key STARTS WITH r.repo_name + "@" + r.commit + ":")
RETURN source.key AS from_key,
       source.qname AS source,
       target.key AS to_key,
       target.qname AS target,
       length(path) AS depth
LIMIT 20;
```

Inspect imports and their aliases:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(f:File)-[rel:IMPORTS]->(m:Module)
RETURN f.path AS importer,
       m.qname AS module,
       rel.name AS imported_name,
       rel.alias AS alias
ORDER BY importer, module
LIMIT 50;
```

Visualize the `click.Group` inheritance hierarchy:

```cypher
MATCH path = (selected:Type {qname: "src.click.core.Group"})-[:INHERITS*1..5]->(ancestor:Type)
WHERE selected.key STARTS WITH "click@"
RETURN path;
```

Show the most complex callables:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
WHERE s:Function OR s:Method
RETURN s.key AS key,
       s.qname AS qualified_name,
       s.cyclomatic AS cyclomatic,
       f.path AS file
ORDER BY cyclomatic DESC, qualified_name
LIMIT 25;
```

List unreferenced candidates. These are review candidates, not proof of dead
code, because dynamic calls may not produce a resolved call edge:

```cypher
MATCH (r:Repository {repo_name: "click"})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
WHERE s:Function OR s:Method
OPTIONAL MATCH (site:CallSite)-[:RESOLVES_TO]->(s)
WHERE site.key STARTS WITH r.repo_name + "@" + r.commit + ":"
WITH s, f, count(DISTINCT site) AS incoming_resolved_calls
WHERE incoming_resolved_calls = 0
RETURN s.key AS key,
       s.qname AS qualified_name,
       f.path AS file,
       s.start_line AS line
ORDER BY qualified_name
LIMIT 25;
```

Check for parse diagnostics before trusting completeness-sensitive results:

```cypher
MATCH (r:Repository)-[:CONTAINS]->(f:File)-[:HAS_DIAGNOSTIC]->(d:ParseDiagnostic)
RETURN r.repo_name AS repository,
       f.path AS file,
       d.line AS line,
       d.message AS diagnostic
ORDER BY repository, file, line;
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

#### MCP Inspector tool test prompts

In the Inspector's **Tools** tab, select a tool and enter one of the JSON
argument payloads below. Values such as `<FUNCTION_KEY>` are placeholders:
replace them with exact keys returned by `search_symbols` or by the Neo4j call
path query above. Exact keys are intentionally used for callables because a
qualified name can have multiple definitions at different source lines.

`list_repositories`

- Baseline inventory: `{}`
- Post-index consistency check: run `{}` again after `index-sources` and verify
  that repository commits and file counts match the ingestion `codekg list`
  output.

`search_symbols`

- Graph name search:
  `{"q":"Group","kind":"type","repo":"click","mode":"graph","limit":10}`
- Graph callable search:
  `{"q":"open_stream","kind":"function","repo":"click","mode":"graph","limit":10}`
- Descriptive lexical search:
  `{"q":"open a file stream","kind":"function","repo":"click","mode":"lexical","limit":10}`

`get_definition`

- Resolve a key copied from `search_symbols`:
  `{"identifier":"<SYMBOL_KEY>"}`
- Resolve a unique qualified type name within one repository:
  `{"identifier":"src.click.core.Group","repo":"click"}`

`find_callers`

- Direct callers of a copied callable key:
  `{"identifier":"<FUNCTION_KEY>","depth":1,"limit":25}`
- Transitive exact callers in the same snapshot:
  `{"identifier":"<FUNCTION_KEY>","depth":3,"limit":50}`

`find_callees`

- Direct callees of the `open_stream` key returned by `search_symbols`:
  `{"identifier":"<OPEN_STREAM_KEY>","depth":1,"limit":25}`
- Transitive exact callees:
  `{"identifier":"<OPEN_STREAM_KEY>","depth":4,"limit":50}`

`trace_call_path`

- Trace between the `from_key` and `to_key` returned by the Neo4j call-path
  query:
  `{"from_identifier":"<FROM_KEY>","to_identifier":"<TO_KEY>","max_depth":4,"limit":5}`
- Qualified-name test for the pinned `click` corpus:
  `{"from_identifier":"src.click.types.File.convert","to_identifier":"src.click._compat._find_binary_reader","repo":"click","max_depth":5,"limit":5}`

`find_importers`

- Requests compatibility imports:
  `{"module_identifier":"src.requests.compat","repo":"requests","limit":25}`
- Click core imports:
  `{"module_identifier":"src.click.core","repo":"click","limit":25}`

`get_class_hierarchy`

- Ancestors of `Group`:
  `{"identifier":"src.click.core.Group","repo":"click","direction":"ancestors","depth":5,"limit":25}`
- Descendants of `Command`:
  `{"identifier":"src.click.core.Command","repo":"click","direction":"descendants","depth":5,"limit":50}`

`find_dead_code`

- Click candidates: `{"repo":"click","limit":25}`
- Requests candidates: `{"repo":"requests","limit":25}`

Always describe these results as unreferenced candidates. Do not delete code
solely because this tool returned it.

`get_complexity`

- Top callables in Click: `{"repo":"click","top_n":10}`
- Complexity for a key copied from `search_symbols`:
  `{"identifier":"<FUNCTION_KEY>"}`

For every tool, also test one bounded empty-result case, such as a nonexistent
repository or symbol search, and verify that it returns an empty result or a
clear symbol-resolution error rather than unrelated data. Lexical-search
failures should be treated as a zvec publication or mount problem; they should
not silently fall back to graph search.

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
