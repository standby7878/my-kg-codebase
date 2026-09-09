# CodeKG

CodeKG builds a local Neo4j knowledge graph from Python repositories and exposes
read-only code queries through an MCP server. It also creates a local lexical
description index for functions and methods, implemented with zvec full-text
search (FTS). It does not create embeddings or a vector index. The services run
with Docker Compose and are reachable only from the local machine.

## Prerequisites

- Docker Engine with Docker Compose v2
- One or more local Python repositories to index

The development profile indexes an arbitrary number of repositories. Configure
`CODEKG_REPOS_ROOT` as a semicolon-separated list of individual repository
paths. The graph uses each checkout directory name as the repository name; it
does not discover repositories from a shared parent directory.

## Specify target repositories

Edit [`compose/dev-local/env`](compose/dev-local/env) and set
`CODEKG_REPOS_ROOT` to four individual repository paths separated by
semicolons. Absolute paths and paths relative to `compose/dev-local/` are
supported; spaces are allowed in a path. Semicolons are delimiters and
therefore cannot occur inside a configured path:

```dotenv
CODEKG_REPOS_ROOT=../../sources/repo-a;../../sources/repo-b;../../sources/repositories with spaces/repo-c;../../sources/repo-d
```

Each listed source is mounted read-only and indexed independently. Each path
must identify a code repository; a shared parent directory is not expanded or
searched for child repositories.

### Code and specifications

CodeKG currently scans Python source files. It also reads Markdown (`.md`)
files inside each target code repository to enrich callable descriptions for
lexical search. Put specifications, design notes, and API documentation beside
the code they describe, and use exact qualified callable names such as
`package.module.function_name` when referring to code.

An independent specifications-only repository is not supported: Markdown is
only used when it lives inside a target code repository. Copy or mount those
files into the corresponding code checkout before indexing.

### SQL ingestion

SQL ingestion is enabled per repository with `[sql] enabled = true` in
`codekg.toml`; the normal `index-sources` bulk or transactional commands need
no additional flags. See [SQL ingestion](docs/sql-ingestion.md) for selection,
PostgreSQL dialect, and supported/unsupported constructs. SQL source files are
parsed statically; Python strings, live catalog state, dynamic SQL names, and
complete procedural control-flow dependencies are not inferred.

## Start the project

From the project root, build the application image once:

```bash
bash run-compose.sh dev-local build
```

Start Neo4j, apply the schema, and launch the MCP HTTP server:

```bash
bash run-compose.sh dev-local start
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. Neo4j is available locally at
`http://127.0.0.1:7474` (Browser) and `bolt://127.0.0.1:7687` (Bolt).

### MCP symbol discovery

Use `list_repositories` when the indexed repository name or revision is
unknown, then call `search_symbols` with the exact indexed `repository` name.
For benchmark batches, perform that repository check once as an unmeasured
preflight instead of repeating it in every task. When more than one repository
is indexed, the repository argument is required and CodeKG will never
substitute results from another repository. An absent name returns a typed
`repository_not_found` response containing the available repository names.

`search_symbols` defaults to five compact candidates (maximum 20) and uses
`hybrid` ranking, which combines exact name matches with lexical descriptions.
Its default `scope` is `source`; use `tests`, `docs`, `examples`, `benchmarks`,
or `all` only when that category is relevant. Scope is applied before
graph/lexical fusion, ranking, and pagination within a fixed bounded backend
candidate pool. It keeps default source discovery narrow, but it is not a
guarantee of strict scope-recall beyond that pool; strict backend scope
filtering and re-indexing are deferred. Each candidate has a stable
`symbol_id`, qualified name, file, line span, score, matched terms, and match
type. Internal candidate-pool and ranking diagnostics are logged for
operations and evaluation but omitted from the normal agent-facing response.
For a plausible first result, search recommends `get_definition` with its
exact symbol ID. Use that definition call to verify indexed metadata and line
bounds, then use `find_callers` or `find_callees` to verify relationships;
relationship rows include repository-relative definition paths and complete
line bounds. Use `next_cursor` only when no candidate is plausible, with the
same repository, query, mode, and scope. The structured MCP result is
canonical; its text companion is only a short summary.

## Index target repositories

To index every configured repository independently:

```bash
bash run-compose.sh dev-local index-sources
```

`index-sources` defaults to a staged bulk build: CodeKG exports the complete
configured corpus to CSV, creates a fresh Neo4j store with `neo4j-admin`,
builds the matching zvec-backed lexical description index, validates their
exact callable keys, then briefly restarts Neo4j and MCP on the new generation.
The previous generation remains available if staging or validation fails.
Callable search documents are finalized into the published bulk manifest during
export. Search construction and validation consume that immutable stage and do
not rescan the mounted repositories. Per-callable Markdown enrichment is capped
at 256,000 characters so a frequently mentioned symbol cannot make staging use
repository-scale memory.

Use transactional writes only for a targeted update or operational debugging:

```bash
bash run-compose.sh dev-local index-sources --mode transactional
```

The default `--mode auto` resolves to bulk; `--mode bulk` requires the offline
import path to succeed and never silently falls back to transactional writes.
The active generation pointers live in the ignored
`compose/dev-local/runtime.env` file.

To index one target repository without changing the profile configuration, use
a temporary environment override:

```bash
CODEKG_REPOS_ROOT=/absolute/path/to/repository bash run-compose.sh dev-local index-sources
```

The override applies only to that invocation; the profile setting in
`compose/dev-local/env` is unchanged. Reindexing replaces that repository's
graph snapshot and its derived lexical index.

List the repositories currently in the graph:

```bash
docker compose \
  -f compose/dev-local/docker-compose.yml \
  --env-file compose/dev-local/env \
  run --rm ingestion codekg list
```

## Stop the project

Stop the containers while keeping Neo4j data and indexes:

```bash
bash run-compose.sh dev-local stop
```

To remove containers, volumes, and locally built images as well:

```bash
bash run-compose.sh dev-local clean
```

`clean` permanently removes the local Neo4j database and zvec index volumes.
