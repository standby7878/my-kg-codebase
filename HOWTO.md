# CodeKG HOWTO

This project builds a local Neo4j-backed code knowledge graph for an arbitrary
number of source repositories. Set `CODEKG_REPOS_ROOT` to a semicolon-separated
list of individual code-repository paths.

The current implementation supports schema bootstrap, source mounting, indexing,
repository listing, and a read-only FastMCP server with fourteen KG query tools.
CodeGraphContext is vendored under `third_party/CodeGraphContext` for local
study. The current extractor is Python-only and uses the standard library `ast`
module.

## Prerequisites

- Docker with Compose v2
- One or more local code repositories to index

The dev profile setting lives in `compose/dev-local/env` as
`CODEKG_REPOS_ROOT`. It can contain absolute paths or paths relative to
`compose/dev-local/`. Spaces are allowed in paths; semicolons separate entries
and are not valid inside a configured path. For example, this config names four
independent targets:

```dotenv
CODEKG_REPOS_ROOT=../../sources/repo-a;../../sources/repo-b;../../sources/repositories with spaces/repo-c;../../sources/repo-d
```

Each source is mounted read-only and indexed independently. The setting does
not identify a shared parent or search one for repositories.

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

This indexes every repository listed in `CODEKG_REPOS_ROOT`; there is no fixed
repository count. Each listed path must be an individual target code
repository.

To index one target repository without changing the profile configuration, use
a temporary environment override:

```bash
CODEKG_REPOS_ROOT=/absolute/path/to/repository bash run-compose.sh dev-local index-sources
```

The override applies only to that invocation; the profile setting in
`compose/dev-local/env` is unchanged.

`reindex` deletes the old graph for that repository name and writes a fresh
snapshot. The scanner reads `.git/HEAD` directly, so it records the real commit
short SHA even though the app image does not install the `git` binary.

Indexed source repositories are local input data for the KG, not part of the
CodeKG package. Keep them at the configured paths. Markdown
specifications, design notes, and API documentation must live inside the code
repository they describe; they enrich the zvec-backed lexical description index
(FTS only, with no embeddings or vector index). An independent
specifications-only repository is not indexed independently.

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
{'repo_name': '<repository-directory>', 'commit': '...', 'root_path': '/repos/<repository-directory>', 'files': 118}
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
codekg list
codekg delete <repository-directory>
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

## Symbol identifier formats

MCP tools accept symbols in three forms (in order of preference):

| Form | Example | Notes |
|---|---|---|
| `symbol_id` (exact key) | `aiven-core@abc123:aiven/logic/pkg.py:aiven.logic.pkg.fn:42` | Preferred; copy from `search_symbols` or any prior tool result |
| Full `qualified_name` | `aiven.logic.services.pg.service.PGService._allocate_haproxy_frontend_ports` | Requires `repository`; fails when ambiguous |
| Qualified-name suffix | `PGService._allocate_haproxy_frontend_ports` | Requires `repository`; unique suffix match only |
| **Not supported** | `_allocate_haproxy_frontend_ports` | Use `search_symbols` first |

SQL objects use `object_key` from `search_sql_objects`, or `schema.object_name` with
`repository` when unique.

### Worked example: Python call chain

```text
1. search_symbols(query="build_pg_component_infos", repository="aiven-core")
2. get_definition(identifier=<symbol_id from step 1>)
3. find_callers(identifier=<symbol_id>, repository="aiven-core")
```

### Worked example: SQL object

```text
1. search_sql_objects(query="services", repository="aiven-core", schema="public", kind="table")
2. get_sql_object(identifier=<object_key from step 1>)
3. find_sql_usages(identifier=<object_key>, repository="aiven-core")
```

When `find_callers` or `find_callees` returns zero rows, the symbol was resolved but
no static call edge exists in the graph. It may only be referenced dynamically (for
example, passed as a callback). Check `list_repositories` to confirm the indexed
commit matches the checkout you expect.

## Neo4j debug queries (operators)

Agents should use the MCP tools. For human debugging, Neo4j Browser is available at
`http://127.0.0.1:7474` (user `neo4j`, password `change-me-123`).

Verify a symbol exists by qualified-name suffix:

```cypher
MATCH (r:Repository {repo_name: $repo})-[:CONTAINS]->(f:File)-[:CONTAINS]->(s)
WHERE s.qname ENDS WITH $suffix
RETURN s.key AS symbol_id, s.qname, f.path AS file, s.start_line
ORDER BY size(s.qname), f.path, s.start_line
LIMIT 10;
```

Find static callers via CallSite resolution:

```cypher
MATCH (callee {key: $symbol_id})<-[:RESOLVES_TO]-(site:CallSite)<-[:HAS_CALLSITE]-(caller)
MATCH (caller_file:File)-[:CONTAINS]->(caller)
WHERE caller:Function OR caller:Method
RETURN caller.qname, caller_file.path AS file, site.line, site.strategy AS resolution
ORDER BY caller.qname
LIMIT 50;
```

List indexed repositories:

```cypher
MATCH (r:Repository)
RETURN r.repo_name, r.commit, r.root_path
ORDER BY r.repo_name;
```

## MCP Tool Prompts

In MCP, a tool's name, description, argument schema, and result schema are the
main prompt surface the agent sees. The descriptions below mirror the fourteen
read-only tools registered in `src/codekg/mcp/server.py`.

### `list_repositories`

List indexed repository snapshots (name, commit, normalized root, file count).
Use first to select the repository and verify the indexed commit.

### `search_symbols`

Discover compact code-symbol candidates in one indexed snapshot. Select a
plausible result, then call `get_definition` with its returned `symbol_id`.
Paginate with `cursor` only when needed.

### `get_definition`

Return indexed metadata and line bounds for one function, method, or type.
Prefer `symbol_id` from `search_symbols`; full qualified names and unique
dotted suffixes require `repository`. Bare member names are unsupported.

### `find_callers`

Find bounded static callers. Prefer `symbol_id`. Treat `resolution='heuristic'`
as approximate. Zero results do not exclude callback or dynamic references.

### `find_callees`

Find bounded static callees. Prefer `symbol_id`. Treat `resolution='heuristic'`
as approximate. Zero results do not exclude dynamic dispatch.

### `trace_call_path`

Find a shortest bounded exact static-call path in the same repository snapshot.
No result within `max_depth` does not exclude runtime callback or dynamic paths.

### `find_importers`

List files with indexed Python import edges to a module. Use an exact module key
or full module qualified name; suffix matching is not supported.

### `get_class_hierarchy`

Return bounded ancestors or descendants through indexed inheritance edges.
Prefer the type's `symbol_id`. Direction defaults to `ancestors`.

### `find_dead_code`

List callables with zero inbound indexed resolved call sites (exact and
heuristic). Results are unreferenced candidates—not confirmed dead code.

### `get_complexity`

With `identifier`, return complexity for one callable. Without `identifier`,
return the top `top_n` callables (optionally filtered by `repository`).

### `search_sql_objects`

Discover SQL objects from PostgreSQL `.sql` files selected by enabled `[sql]`
configuration in `codekg.toml`. Embedded Python strings are not indexed.

### `get_sql_object`

Return one SQL object and its definition sites. Prefer `object_key` from
`search_sql_objects`.

### `find_sql_usages`

Find bounded static SQL usages. Default `resolution='exact'` is not runtime
proof; ambiguous/unresolved/dynamic modes return candidate references.

### `get_sql_in_file`

Return parsed SQL artifacts, statements, and references for one indexed `.sql`
file. `limit` bounds each returned collection.

## MCP Prompting Rules

- Keep all MCP tools read-only.
- Include `repository`, `limit`, `depth`, or `max_depth` arguments wherever a
  query can grow.
- Prefer `symbol_id` / `object_key` from prior tool results over free-form names.
- Results describe the indexed commit snapshot, not necessarily HEAD.
- Surface confidence fields such as `resolution` in results.
- Tell the agent when a result is approximate or a candidate.
- Do not expose filesystem paths outside mounted repository paths.
- Do not expose `index`, `reindex`, `delete`, or `watch` as MCP tools unless the
  security model is changed deliberately.
