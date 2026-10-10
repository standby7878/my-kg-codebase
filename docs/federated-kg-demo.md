# Independent KGs: implementation and live validation

Validated on 2026-10-10 with Python 3.12 and Neo4j 5.26 Community. The
[design](version-scoped-kg-refactoring-plan.md) was refined with Astra HIGH;
Luna HIGH implemented the changes, Terra reviewed and integrated them, and Sol
approved the final code review and subsequent narrow corrections.

## Delivered

- Separate application and database corpus exports, generation descriptors,
  Neo4j instances/volumes, and generation-verified clients. No application
  source is copied into the database KG.
- One MCP endpoint with **25 fixed tools**, including graph discovery, intent
  discovery/resolution, forward application/database paths, reverse usages,
  and database-generation comparison.
- Explicit deployment contexts choose a database graph, extension visibility,
  and search path. Entity references and continuation tokens carry graph and
  generation scope. Cross-version comparisons do not merge databases.
- Indexed, read-only SQLite boundary evidence and bounded on-demand path
  composition; eager intra-KG relationships remain available.
- Candidate offline import, count/schema/marker verification, atomic registry
  activation/rollback, and explicit MCP restart. Existing volumes are not
  overwritten. See the [operator recipe](independent-graph-operations.md).
- Disposable projection-validation SQLite tuning and failure cleanup. Durable
  serving catalogs and extraction spools retain their durability settings.

## Sources actually built

The database graph was built/imported first, then the Python graph. The Python
workload is a substantial public multi-repository demo, **not a benchmark of
the user's huge monorepo**. Django provides PostgreSQL/PostGIS integration;
Patroni provides real literal PostgreSQL calls. A separate `bridge_probe`
snapshot supplies small, explicitly labelled literal extension smoke tests.

| Graph | Snapshot | Pinned source commit |
| --- | --- | --- |
| `pg18` | PostgreSQL `REL_18_0` | `3d6a828938a5fa0444275d3d2f67b64ec3199eb7` |
| `pg18` | pg_cron | `5cedfa472ccc83567aa23ec645925ed8489a7797` |
| `pg18` | PostGIS | `8c02deb339a32ccf4a148ce2ada62480ce8d8c40` |
| `python` | Django `stable/5.2.x` | `cae7247962de54a94023d28d84f05fc9842e9646` |
| `python` | Patroni | `056e97bf32a4b0425156938231f9692ac2866fc9` |
| `python` | `bridge_probe` | Fixture below; content-addressed, not upstream project code |

`revision` fields in exported snapshots are content/config identities; they
are not interchangeable with these Git commits. The frozen manifests retain
both identities.

| Graph | Generation | Imported nodes | Imported relationships |
| --- | --- | ---: | ---: |
| `pg18` | `pg18:fdc8c1f3063716f45a037c7912c3bd21` | 809,300 | 1,630,844 |
| `python` | `python:47561fe51e0c26a45099bf990df0f933` | 249,906 | 413,669 |

Counts exclude the single bootstrap `CodeKGGeneration` marker per instance.
Bootstrap checked every imported label and relationship count against its
frozen manifest. The application lexical index contains 33,285 documents.

The smoke fixture's `demo_client.py` is:

```python
def inspect_core(cursor):
    cursor.execute("SELECT pg_catalog.pg_is_in_recovery()")

def inspect_postgis(cursor):
    cursor.execute("SELECT public.postgis_lib_version()")

def inspect_cron(cursor):
    cursor.execute("SELECT cron.schedule('* * * * *', 'SELECT 1')")

def run(cursor):
    inspect_core(cursor)
    inspect_postgis(cursor)
    inspect_cron(cursor)
```

This source was indexed, not executed against a PostgreSQL server. Results
below are static source evidence, not assertions of runtime execution or
installed extension compatibility.

## Live HTTP MCP evidence

Endpoint: **`http://127.0.0.1:18765/mcp`**. The process serves two graphs from
separate Community instances:

| Service | Container | Local endpoint |
| --- | --- | --- |
| MCP | `codekg-federated-mcp` | HTTP `18765/mcp` |
| Python KG | `codekg-python-47561fe51e0c-63cfaf36` | Bolt `17687`, HTTP `17474` |
| PG18 KG | `codekg-pg18-fdc8c1f30637-e908ab07` | Bolt `27687`, HTTP `27474` |

### Current local authentication mode

Both demo Neo4j containers now use `NEO4J_AUTH=none` and explicit
`NEO4J_dbms_security_auth__enabled=false`. The MCP environment sets
`CODEKG_APP_NEO4J_AUTH=none` and `CODEKG_PG18_NEO4J_AUTH=none`; all three
containers have no password environment variables. Select **No authentication**
when connecting in Neo4j Browser. Endpoints, volumes, generation markers, and
node/relationship counts are unchanged; neither KG was rebuilt.

The switch was verified with unauthenticated Bolt clients, direct lifecycle
clients, `cypher-shell`, HTTP transaction requests without an Authorization
header, and all 19 live MCP assertions. The auth change's full unit suite passed
505 tests, and Sol approved its review. The legacy authenticated Compose path
also remains supported. See the [no-auth operator recipe](independent-graph-operations.md#local-no-auth-demo)
for the explicit per-graph mode and how to retain existing volumes.

Runtime manifests, registries, logs, and private env files live under the
ignored `.codekg-corpus/federated-demo/` directory. Credentials are not tracked.
The active registry is `config/active.toml`; manifests are frozen `serving.json`
files inside the graph-specific generation directories. These local services
and large generated artifacts are not distributed by a Git push.

An actual FastMCP HTTP client passed **19 assertions**, covering:

1. Two graph generations discovered through the same 25-tool endpoint.
2. Real Patroni evidence in `patroni/postgresql/rewind.py:99` resolves through
   `pg_proc.dat:6757` to PostgreSQL's `pg_is_in_recovery` native definition at
   `src/backend/access/transam/xlogfuncs.c:642`.
3. Fixture `run` follows actual Python `EXACT_CALLS`, owned SQL evidence,
   `cron.schedule` at `pg_cron.sql:31`, `BINDS_TO_NATIVE` to `cron_schedule`,
   then `CALLS_NATIVE` at `src/job_metadata.c:463` to PostgreSQL `errmsg` in
   `src/backend/utils/error/elog.c:1070`. The error branch is source evidence,
   not a guaranteed runtime branch. Results report traversal truncation when
   the depth/fan-out/path budget is reached.
4. Literal core, pg_cron, and PostGIS **SQL routine** resolution is exact under
   `python-on-pg18`. Under `python-on-pg18-no-postgis`, the same PostGIS intent
   becomes unresolved, with both graph generation IDs unchanged.
5. Django's templated SQL in `PostGISOperations._get_postgis_func` remains
   `dynamic`. No exact Django-to-native path is invented.
6. This unconfigured PostGIS source checkout has an unresolved concrete
   `MODULE_PATHNAME`/control-library identity. Its native definition is a
   `NATIVE_CANDIDATE`, not `BINDS_TO_NATIVE`; the forward native trace correctly
   returns no asserted path. Generating/pinning the concrete control metadata
   is a separate ingestion-context change.
7. Reverse native lookup returns fixture and real Patroni usages across pages;
   a continuation from another context is rejected. Intent and symbol pages
   also advance, and intent cursors reject changed selectors. Stale entity
   generations are rejected.
8. Identical generation/alias/corpus comparison returns no differences without
   scanning the large catalog. Different versions still use bounded semantic
   comparison; their overload/condition behavior is tested with synthetic
   fixtures. A second real PostgreSQL version was **not** built in this run.

Two further live scenarios passed:

- **Backend isolation:** stopped only the demo PG18 container. Python repository
  discovery and symbol search still succeeded. A bridge request returned a
  backend failure in approximately **0.056 seconds**. The PG18 service was
  restarted, and subsequent bridge queries succeeded.
- **Lifecycle:** activated a context-only candidate without changing generation
  IDs or rebuilding either KG; verified that the running MCP remained pinned
  until restart; rolled back to byte-identical registry content; verified an
  invalid candidate did not change the active registry. All four checks passed.

Detailed local results: `reports/mcp-demo.json`, `mcp-discover.json`,
`mcp-isolation.json`, and `lifecycle-demo.json`. The serving MCP was explicitly
restarted after final code changes; both Neo4j services remain running.

## Performance measurements and scope

These are single live builds, not paired cold-build benchmarks. PG18 export
started before scratch tuning; the Python workload was different and used the
tuned validator. Their timings **must not be compared as a speedup ratio**.

| Export stage | PG18 + extensions, seconds | Python workload, seconds |
| --- | ---: | ---: |
| Native/boundary extraction | 384.114 | 253.691 |
| Ordinary bulk export | 669.390 | 104.315 |
| Composition | 45.005 | 17.001 |
| Identity | 5.552 | 7.918 |
| Intra-corpus resolution | 47.397 | 0.057 |
| Supplemental projection | 43.479 | 0.073 |
| Total export | **1,205.915** | **396.386** |

The application lexical-index build took 8.984 seconds. Export resource metrics
are per-process peaks, **not total concurrent memory**: PG18 parent 225,964 KiB,
largest child 183,564 KiB; Python parent/largest child 122,936 KiB. Builds used
one native extraction worker and two projection workers. Each serving Neo4j
uses a 1 GiB heap and 512 MiB page cache in this demo.

A three-pair scratch merge replay used identical 20,000-node/20,000-edge input:

| Pair | Baseline, seconds | Tuned private scratch, seconds |
| --- | ---: | ---: |
| 1 | 2.6918 | 0.1667 |
| 2 | 2.7419 | 0.1756 |
| 3 | 36.0986 | 0.2226 |

All six outputs had identical counts and SHA-256
`61855050e92802f080733af2675fa6c5a9a8735522522627485d812722455849`.
The third baseline overlapped other disk activity; the replay was not isolated.
This establishes a microbenchmark improvement while preserving validated
graph content, **not an end-to-end ingestion speedup claim**.

The architectural improvement demonstrated here is independent refresh and
zero ingestion for bridge-context-only edits. File extraction caches, parallel
native workers, a shard-native composite writer, and clean huge-monorepo
before/after benchmarks remain the next phases of the design roadmap.

## Verification

- Initial federation unit suite: 495 tests passed; the subsequent no-auth
  update's unit suite passed 505 tests.
- Actual HTTP MCP protocol integration: 1 test passed.
- Neo4j corpus/offline-import integration: 3 tests passed, including a measured
  large-field CSV import and live cross-language corpus queries.
- Changed Python files: Ruff lint/format checks and `git diff --check` passed.
  Unrelated pre-existing repository-wide Ruff findings were left unchanged.
- Astra HIGH design validation, Terra self-review/fixes, and Sol final/narrow
  reviews completed. Regression coverage includes cursor scope, late valid
  reverse postings, bounded continuation, symlink-safe activation, and
  preserving the original projection failure while discarding scratch files.
