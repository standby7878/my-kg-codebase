# Independent graph generations

CodeKG serves one application graph and one or more separately built database
graphs from independent Neo4j Community instances. Each graph has its own
frozen corpus generation, offline import, Neo4j data volume, endpoint, and
generation marker. Never point two graph IDs at the same Neo4j data volume.

## Prepare and activate a candidate

The following recipe applies independently to the application and database
corpora. The application export contains the app source; a database export
contains exactly one PostgreSQL snapshot and its selected extension dependency
view. Export each on its own cadence.

1. Export to a graph-specific artifact root:

   ```sh
   codekg bulk-export-corpus app-corpus.toml artifacts/app
   codekg bulk-export-corpus pg18-corpus.toml artifacts/pg18
   ```

2. The exporter atomically updates the root `manifest.json`. Freeze each
   generation beside its `corpus.sqlite` and graph CSV files. The destination
   uses the immutable generation directory identified by the root manifest's
   `registry` path:

   ```sh
   codekg graph freeze artifacts/app/manifest.json \
     artifacts/app/generations/<export-id>/manifest.json
   codekg graph freeze artifacts/pg18/manifest.json \
     artifacts/pg18/generations/<export-id>/manifest.json
   ```

   Freeze rebases both singular `file` artifacts and sharded `files` arrays.
   The frozen file refuses conflicting replacement. Keep the whole frozen
   generation directory, including CSV artifacts and `corpus.sqlite`, together.

3. Create a candidate registry beside the active registry, using the frozen
   manifests. `visible_extensions` contains explicit database snapshot aliases
   (not inferred from every registered graph). Endpoints and credentials are
   environment-variable names only. A database-only registry is valid while
   the application graph is still being built; omit
   `default_application_graph` until an application graph exists. For a
   combined app/database registry it is required:

   ```toml
   schema_version = 1
   default_application_graph = "app-main"

   [[graphs]]
   id = "app-main"
   kind = "application"
   generation_manifest = "artifacts/app/generations/<export-id>/manifest.json"
   endpoint_env = "CODEKG_APP_NEO4J_URI"
   credential_env_prefix = "CODEKG_APP_NEO4J"

   [[graphs]]
   id = "db-pg18"
   kind = "database"
   generation_manifest = "artifacts/pg18/generations/<export-id>/manifest.json"
   endpoint_env = "CODEKG_PG18_NEO4J_URI"
   credential_env_prefix = "CODEKG_PG18_NEO4J"

   [[contexts]]
   id = "app-main-on-pg18"
   application_graph = "app-main"
   database_graph = "db-pg18"
   application_database = "primary"
   visible_extensions = ["pg_cron18"]
   search_path = ["public", "pg_catalog"]
   ```

4. Store Docker runtime auth in an external, permission-restricted env file,
   separate from TOML. `graph prepare` imports the frozen CSV files offline
   into a newly created uniquely named volume *before* starting Neo4j. It
   creates a Community container on `codekg-graphs`, with no published host
   ports unless requested. It does not stop or replace existing containers,
   and it never removes a data volume. Optional ports bind only to localhost:

   ```sh
   codekg graph prepare app-main --registry candidate.toml \
     --env-file /secure/app-neo4j.env --http-port 17474 --bolt-port 17687
   codekg graph prepare db-pg18 --registry candidate.toml \
     --env-file /secure/pg18-neo4j.env --http-port 18474 --bolt-port 18687
   ```

   Default resource bounds are 2 GiB for offline import, 1 GiB heap, and 2 GiB
   page cache. Adjust with `--import-memory`, `--heap-max`, and `--pagecache` if
   the host budget permits. A prepared container is a candidate, not active.

5. Set the registry's endpoint and credential environment variables through
   the MCP deployment's secret/configuration mechanism. Bootstrap validates the
   imported per-label and per-relationship counts against the frozen manifest,
   applies schema, and writes the exact graph/generation marker:

   ```sh
   codekg graph bootstrap app-main --registry candidate.toml
   codekg graph bootstrap db-pg18 --registry candidate.toml
   codekg graph check --registry candidate.toml --backend
   ```

6. Activate only after backend checks pass. Candidate and active TOML must be
   siblings so relative generation paths retain their meaning. Activation
   atomically retains the old file at `<active>.previous`; it explicitly does
   **not** restart MCP. Restart the MCP process through its deployment manager
   to load the new registry. Rollback also validates the prior backends and
   requires an explicit MCP restart:

   ```sh
   codekg graph activate candidate.toml --active active.toml
   # Explicitly restart the MCP process using the deployment manager.
   codekg graph rollback --active active.toml
   # Explicitly restart MCP again after rollback.
   ```

`graph check` without `--backend` validates local registry, manifest, SQLite
revision, role, and context invariants without contacting Neo4j. `graph check
--backend` additionally checks every configured generation marker. The normal
registry activation command performs the backend checks before changing the
active file.

## Candidate cleanup and retention

Candidate resource names include graph ID, generation identity, and a random
token. Failed starts remove only a container whose Docker ownership token
matches this invocation; its data volume is retained for inspection. Retire
containers and volumes only through a separately reviewed retention procedure.
No command here runs `docker compose down`, `docker volume rm`, or automatically
switches a live MCP process.

The application `docker/app.Dockerfile` includes Git so source snapshot identity
can retain commit provenance when a mounted source checkout has Git metadata.
