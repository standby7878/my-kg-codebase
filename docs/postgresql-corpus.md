# PostgreSQL and extension investigation

The corpus exporter joins Python/SQL/Markdown application evidence to extension
routine declarations and build-free C facts. It supports concurrent PostgreSQL
snapshots without resolving an extension against every indexed PG version.
It does not build source projects, execute SQL, or certify ABI/CVE compatibility.

## Configure explicit version contexts

Create a manifest in the project root. Input paths may be absolute or relative
to the manifest; output must not overlap a source root. These detached PG
worktrees already exist locally:

`corpus.postgresql.toml` is ready for the local PostgreSQL checkouts plus pg_cron
and PostGIS in both version contexts. Add the application checkout separately;
it is not assumed to be this KG implementation repository.

- `.codekg-worktrees/postgres18`, `REL_18_0`
- `.codekg-worktrees/postgres19beta4`, `REL_19_BETA4`

The original `/media/alex/MYSSD/BACKUP/workspace/postgres` checkout is unchanged.
For a new environment, create separate worktrees from that reference repository;
do not switch a checkout that somebody else is using.

```toml
[[snapshots]]
alias = "pg18"
logical_repo = "postgres"
version = "18"
role = "postgres"
path = ".codekg-worktrees/postgres18"

[[snapshots]]
alias = "pg19"
logical_repo = "postgres"
version = "19beta4"
role = "postgres"
path = ".codekg-worktrees/postgres19beta4"

[[snapshots]]
alias = "cron18"
logical_repo = "pg_cron"
version = "working-source"
role = "extension"
path = "../pg_cron"
dependencies = ["pg18"]

[[snapshots]]
alias = "cron19"
logical_repo = "pg_cron"
version = "working-source"
role = "extension"
path = "../pg_cron"
dependencies = ["pg19"]

[[snapshots]]
alias = "postgis18"
logical_repo = "postgis"
version = "working-source"
role = "extension"
path = "../postgis"
dependencies = ["pg18"]

# Add the real application checkout and its explicit extension dependencies.
# Separate app18/app19 aliases can index the same checkout in each context.
# [[snapshots]]
# alias = "app18"
# logical_repo = "application"
# version = "working-source"
# role = "application"
# path = "/path/to/application"
# dependencies = ["cron18", "postgis18"]
# sql = { enabled = true, include = ["**/*.sql"], search_path = ["public", "pg_catalog"] }
```

PostgreSQL/extension roles select `.sql` and `.sql.in` by default. Application
standalone SQL selection is explicit. Other procedural languages retain their
language/body metadata and coverage diagnostics; their internals are not claimed
as analyzed SQL calls. Templates are indexed without executing preprocessors.
In particular, PL/Tcl, PL/Perl and PL/Python bodies are declaration/coverage
facts, not complete call graphs. See PostgreSQL's
[procedural-language overview](https://www.postgresql.org/docs/current/xplang.html).

## Export and investigate offline

```sh
CODEKG_LOG_LEVEL=INFO .venv/bin/codekg bulk-export-corpus corpus.toml .codekg-corpus --workers 2
.venv/bin/codekg corpus-snapshots .codekg-corpus/manifest.json
.venv/bin/codekg corpus-search .codekg-corpus/manifest.json cron_schedule --snapshot cron18 --kind native
.venv/bin/codekg corpus-search .codekg-corpus/manifest.json st_buffer --snapshot postgis18 --kind routine
.venv/bin/codekg corpus-compare .codekg-corpus/manifest.json pg18 pg19 --limit 100
```

Use exact returned keys with `corpus-evidence` (`--direction incoming` for callers)
and `corpus-trace MANIFEST FROM_KEY TO_KEY --max-depth 8`. Treat keys as opaque.
Documentation links are not executable calls; candidate/conditional links are
visible evidence but excluded from asserted traces. A missing path is not proof
that no runtime dependency exists. Static literal DB calls are a limited subset
of application SQL; dynamic query construction remains unresolved.

The combined manifest is compatible with the existing bulk-import/search-stage
consumers. Import into a **new, isolated Neo4j database/data directory**; offline
Neo4j-admin import is not an incremental update of a running database. After
import, bootstrap schema/indexes before serving MCP. The five corpus MCP tools
provide snapshot discovery, symbol search, dependency evidence, traces and diffs.

## Resource and reproducibility contract

Corpus extraction stages file-at-a-time facts and ordinary IR on disk, with an
explicit per-snapshot `max_file_bytes` limit (at most 64 MiB). Oversized files
produce coverage diagnostics rather than bypassing limits through ordinary
ingestion. A bounded file is not a bound on its AST expansion: lower the limit
for constrained machines. Shared corpus extraction is serial; `--workers` caps
parallel graph projection (actual concurrency also depends on staged batches).
Read worker/concurrency metrics as reported; worker count alone does not describe
peak memory.

Full Git commits, selected-source digests, extractor/configuration fingerprints
and explicit dependency revisions identify snapshots. Changes during extraction
abort publication; the prior manifest remains published. Source hashing performs
separate streaming passes for consistency checks, not retained source copies.

SQLite staging, per-snapshot exports and composed CSV consume temporary disk
space. Generations are retained for recovery/reproducibility. Remove only an
identified, unused generation after confirming no published manifest references
it; do not delete the output root or original source checkouts. Build-free parsing
and incomplete/template diagnostics limit the certainty of comparisons.
