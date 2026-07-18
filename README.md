# CodeKG

CodeKG builds a local Neo4j knowledge graph from Python repositories and exposes
read-only code queries through an MCP server. It also creates a local lexical
index for function and method descriptions. The services run with Docker
Compose and are reachable only from the local machine.

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

## Index target repositories

To index every configured repository independently:

```bash
bash run-compose.sh dev-local index-sources
```

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
