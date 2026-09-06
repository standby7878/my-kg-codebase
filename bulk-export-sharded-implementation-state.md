# Sharded bulk-export implementation state

## Frozen scope

Implement the single-root `bulk-export` path for one logical, potentially huge
monorepo.  The path must use bounded process-parallel extraction, lossless
SQLite extraction spools, a coordinator-built global SQLite resolver registry,
and bounded streaming CSV projection.  The registry is an internal build
artifact, never a Neo4j input.  Zvec lexical FTS is explicitly outside this
change.

## Non-negotiable invariants

* Repository identity is calculated once.  The Repository node key is exactly
  `repo_name`; all snapshot-scoped keys retain the existing
  `repo_name@commit:` prefix.  Partition IDs are never semantic identities.
* Extraction batches are deterministic and bounded by 128 files or 32 MiB of
  source bytes, with at most `2 * workers` submitted futures.
* A spool preserves every `FileIR` field and source ordering information; it
  must round-trip losslessly.  Duplicate qualified names remain ambiguous and
  preserve existing key ordering.
* Existing resolution rules are shared through a lookup abstraction; SQLite is
  a data backend, not a new resolver algorithm.
* Projection reconstructs, resolves, emits, and discards one file at a time.
  It cannot collect repository-wide FileIRs, rows, node-key sets, or
  relationship-key sets in Python memory.
* CSV v2 groups contain one header file followed by headerless, deterministically
  ordered shards.  Global entities are emitted by one global owner.  Both
  `CONSTRUCTS` edges retain distinct relationship identities despite their
  shared Neo4j `key` property.
* Exports use generation directories and atomically publish the top-level
  manifest last.  Failed builds leave an existing valid manifest intact.
* Python import and the Neo4j 5.26 Docker importer consume explicit manifest
  groups, never shard-discovery globs.

## Required validation gates

1. Legacy and sharded graph parity for cross-folder calls, inheritance,
   import aliases, module initializers, duplicate-qname ambiguity, factory
   annotations, and constructor edges.
2. Workers=1 and workers=N normalized graph parity and deterministic group
   ordering.
3. Lossless `FileIR` SQLite spool round trip and failure-without-publication.
4. Actual Neo4j 5.26 grouped CSV import.
5. A stress/memory measurement showing coordinator and worker memory do not
   grow with total repository graph cardinality.

## Integration sequence

1. Extract resolver lookup protocol without semantic change.
2. Add lossless SQLite spool and registry/index validation.
3. Add one-worker streaming sharded projection and v2 manifest/import groups.
4. Add bounded process extraction and parallel projection.
5. Exercise Docker/Neo4j and memory gates, then independent review.

## Live orchestration state

* Terra: active integration owner; no commits or staging permitted.
* Luna: no active Luna assignment. Terra attempted the required default-agent
  invocation with `gpt-5.6-luna` / medium, but the multi-agent spawn API is
  not exposed in this session. No substitute agent or model was used.
* Sol: reserved for a single high-effort independent gate after an integrated
  milestone.
* Astra: specification frozen by `gpt-6-astra`; it is resumed only for a
  genuine specification ambiguity.
* Landed: `ResolverIndex` abstraction with the existing in-memory backend;
  resolver parity tests passed (24 tests).  Lossless SQLite spool/registry
  foundation is formatted. Python importer supports ordered v1/v2 groups and
  the Neo4j 5.26 Docker image parses manifest groups through jq; focused
  importer/resolver tests passed (19 tests).
* Available routine sidecar scope for root-managed Luna: add focused v2
  manifest/export tests only in `tests/unit/test_bulk_export.py` and
  `tests/unit/test_cli.py` after the exporter public API below stabilizes.
* Active Luna (root-managed): `01a07208-b8d5-7103-bf68-8b3760e0d1e4`,
  `gpt-5.6-luna` medium; exclusive scope `tests/unit/test_bulk_spool.py`.
* Landed after Luna assignment: `SqliteResolverIndex` opens the finalized
  registry read-only with a bounded SQLite cache. Existing receiver and
  constructor resolver checks passed (13 tests, Ruff clean).
* Active Luna (root-managed): routine `gpt-5.6-luna` medium identity helper;
  exclusive files `src/codekg/bulk_identity.py` and
  `tests/unit/test_bulk_identity.py`. Terra must not edit either file.
* Active Luna high projection API contract for Terra integration:
  `project_repository(repo_name, commit, root_path, spool_paths, registry_path,
  output_dir, workers) -> BulkExport-compatible result`, with headerless
  shards and a v2 manifest written under `output_dir`.  Terra scheduler owns
  the disk-backed spool catalog and passes an iterator/tuple only if required
  by the completed API.
