# Rebuild the local knowledge graph

`rebuild-kg.sh` replaces **only the dev-local Docker Compose stack** and its
graph, search, log, and staging volumes. It ingests local Git checkouts into one
corpus, then starts Neo4j and MCP. It does not fetch, switch, or modify the
original checkouts. A failed rebuild does **not** restore the old graph.

```bash
./rebuild-kg.sh \
  --application my-python-app /path/to/python-repo WORKTREE \
  --postgres pg18 /path/to/postgres REL_18_0 \
  --postgres pg19 /path/to/postgres REL_19_BETA4 \
  --extension pg_cron /path/to/pg_cron WORKTREE \
  --extension postgis /path/to/postgis WORKTREE \
  --workers 2 --dry-run
```

Inspect the plan, then repeat with `--yes` instead of `--dry-run`. Each option
is repeatable. `WORKTREE` includes uncommitted files; any other ref must resolve
locally to a Git commit. The script makes detached worktrees for those refs, so
both PostgreSQL versions can be ingested without changing your checkout.
Input files must be readable by the exporter container (UID 10001). The
generated manifest and worktrees are made readable for that container.

An application and each extension are represented separately in each PostgreSQL
context (for example, `my-python-app-pg18` and `my-python-app-pg19`). The script
enables SQL and SQL-template discovery for applications, PostgreSQL, and
extensions, alongside Python, C, and Markdown extraction.

Requirements: Git, Docker Engine with Compose, and the project's build context.
`--workers N` controls corpus projection workers; `--skip-build` reuses local
images. The script validates all input paths and refs before deleting the old
stack. It then builds images, runs `docker compose down --volumes`, exports the
corpus, builds the search index, imports Neo4j offline, starts Neo4j, bootstraps
the schema, and starts MCP. The generated manifest and detached worktrees are
retained in the printed `/var/tmp/codekg-rebuild.*` directory for provenance.
The manifest version records the chosen ref and resolved commit SHA; the app
image does not include Git, so its separate `git_commit` field may be empty.
