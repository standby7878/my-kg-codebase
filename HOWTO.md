# CodeKG HOWTO

This project builds a local Neo4j-backed code knowledge graph for four source
repositories:

- `pghoard`
- `pgbackrest`
- `pglookout`
- `patroni`

The current implementation supports schema bootstrap, source mounting, indexing,
repository listing, and a read-only FastMCP server with ten KG query tools.
CodeGraphContext is vendored under `third_party/CodeGraphContext` for local
study. The current extractor is Python-only and uses the standard library `ast`
module.

## Prerequisites

- Docker with Compose v2
- The four source repositories checked out next to this project directory:
  - `../pghoard`
  - `../pgbackrest`
  - `../pglookout`
  - `../patroni`

The dev profile paths live in `compose/dev-local/env`. They are relative to the
Compose file and point outside this project with `../../../<repo>`.

## Build the App Image

From this repository root:

```bash
bash run-compose.sh dev-local build
```

This builds `codekg-app:local`, the Python image used by schema bootstrap,
ingestion, and MCP.

## Start Neo4j and Apply the Schema

```bash
bash run-compose.sh dev-local bootstrap
```

This starts Neo4j if needed, waits for it to become healthy, and applies the
schema constraints and indexes. It is safe to run repeatedly.

Neo4j is exposed locally for development:

- Browser: `http://127.0.0.1:7474`
- Bolt: `bolt://127.0.0.1:7687`
- User: `neo4j`
- Password: `change-me-123`

To start Neo4j, apply the schema, and launch the MCP HTTP server:

```bash
bash run-compose.sh dev-local start
```

## Index the Source Repositories

```bash
bash run-compose.sh dev-local index-sources
```

This reindexes all four mounted repositories:

- `/repos/pghoard`
- `/repos/pgbackrest`
- `/repos/pglookout`
- `/repos/patroni`

`reindex` deletes the old graph for that repository name and writes a fresh
snapshot. The scanner reads `.git/HEAD` directly, so it records the real commit
short SHA even though the app image does not install the `git` binary.

Indexed source repositories are local input data for the KG, not part of the
CodeKG package. Keep them as sibling directories of this project.

Current extraction level:

- Python files: files, modules, imports, classes, inheritance, functions,
  methods, module-level pseudo-callables, source spans, signatures, simple
  cyclomatic complexity, and heuristic call edges.
- Non-Python source files are not indexed in the current implementation.

## List Indexed Repositories

```bash
docker compose \
  -f compose/dev-local/docker-compose.yml \
  --env-file compose/dev-local/env \
  run --rm ingestion codekg list
```

Expected shape:

```text
{'repo_name': 'patroni', 'commit': '...', 'root_path': '/repos/patroni', 'files': 118}
{'repo_name': 'pgbackrest', 'commit': '...', 'root_path': '/repos/pgbackrest', 'files': 569}
{'repo_name': 'pghoard', 'commit': '...', 'root_path': '/repos/pghoard', 'files': 70}
{'repo_name': 'pglookout', 'commit': '...', 'root_path': '/repos/pglookout', 'files': 20}
```

## Stop or Clean

Stop containers but keep Neo4j data:

```bash
bash run-compose.sh dev-local stop
```

Remove containers, volumes, and local images:

```bash
bash run-compose.sh dev-local clean
```

## Python CLI Commands

Inside the app image, the CLI is `codekg`.

```bash
codekg bootstrap
codekg reindex /repos/pghoard
codekg list
codekg delete pghoard
```

The host helper script wraps the common Compose invocations.

## Development Checks

If you do not have local Python tooling installed, use a throwaway Python
container:

```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -e '.[dev]' >/tmp/codekg-dev-install.log && \
         ruff check . && \
         ruff format --check . && \
         pytest -m unit"
```

## MCP Server

The Compose profile runs the MCP server with:

```yaml
command: ["python", "-m", "codekg.mcp.server"]
```

The HTTP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

Quick client smoke test:

```bash
.venv/bin/python - <<'PY'
import anyio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8765/mcp") as client:
        tools = await client.list_tools()
        print([tool.name for tool in tools])
        result = await client.call_tool("list_repositories", {})
        print(result.structured_content)

anyio.run(main)
PY
```

The MCP server is read-only. Indexing, deleting, and watching repositories stay
operator-only CLI actions, not MCP tools.

## MCP Tool Prompts

In MCP, a tool's name, description, argument schema, and result schema are the
main prompt surface the agent sees. Keep descriptions short, imperative, and
bounded. The descriptions below are the intended prompts for the ten read-only
tools.

### `search_symbols`

Prompt:

```text
Search indexed code symbols by name or qualified name substring. Use this first
when you do not know the exact symbol key. Optionally filter by repository and
symbol kind. Results are capped by the limit argument.
```

Use when the agent needs to locate functions, methods, classes, structs, or
interfaces before asking for details.

### `get_definition`

Prompt:

```text
Return the definition metadata for one indexed symbol, including repository,
file path, line span, qualified name, signature, and symbol kind. Use an exact
symbol key or qualified name from search results.
```

Use when the agent already has a symbol candidate and needs source location or
signature context.

### `find_callers`

Prompt:

```text
Find symbols that call the given function or method. Traversal depth is bounded
and results are capped. Treat edges with resolution='heuristic' as approximate.
```

Use for impact analysis: "who depends on this?" The prompt must remind the agent
that call edges can be approximate.

### `find_callees`

Prompt:

```text
Find symbols called by the given function or method. Traversal depth is bounded
and results are capped. Treat edges with resolution='heuristic' as approximate.
```

Use for understanding a function's downstream behavior.

### `trace_call_path`

Prompt:

```text
Find a bounded call path between two functions or methods. Use exact qualified
names or keys. Returns no path when the graph cannot prove a connection within
max_depth.
```

Use for "how can A reach B?" questions. The result should be treated as graph
evidence, not proof that no runtime path exists.

### `find_importers`

Prompt:

```text
List files that import the requested module qualified name. Results are capped
and grouped by repository and file path.
```

Use for module-level dependency questions, especially in Python packages.

### `get_class_hierarchy`

Prompt:

```text
Return ancestors or descendants of a type through inheritance and interface
relationships. Direction must be explicit. Results are bounded.
```

Use for class/type hierarchy exploration once inheritance extraction exists.

### `find_dead_code`

Prompt:

```text
List callable symbols in a repository with no inbound call edges. Excludes known
entry points when entry-point metadata is available. Results are candidates, not
confirmed dead code.
```

Use for cleanup candidates. The prompt must discourage deleting code solely from
this result.

### `get_complexity`

Prompt:

```text
Return cyclomatic complexity for one symbol, or the most complex symbols in a
repository when a top-N request is provided.
```

Use for maintainability triage and test-focus decisions.

### `list_repositories`

Prompt:

```text
List repositories currently indexed in the graph, including commit, root path,
and file count. Use this before repository-scoped queries when the repo name is
unknown.
```

Use as the discovery tool at the start of a session.

## MCP Prompting Rules

- Keep all MCP tools read-only.
- Include `repo`, `limit`, `depth`, or `max_depth` arguments wherever a query
  can grow.
- Prefer exact keys returned by previous tools over free-form names.
- Surface confidence fields such as `resolution` in results.
- Tell the agent when a result is approximate or a candidate.
- Do not expose filesystem paths outside mounted repository paths.
- Do not expose `index`, `reindex`, `delete`, or `watch` as MCP tools unless the
  security model is changed deliberately.
