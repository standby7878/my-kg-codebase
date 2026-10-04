# PostgreSQL and extension corpus support

## Goal and first acceptance scenario

Keep the existing Python/SQL graph, and add a durable, disk-backed corpus layer
that makes this chain inspectable with source evidence:

`Python literal SQL / runbook -> cron.schedule -> cron_schedule -> PG C API`.

Index PostgreSQL 18 and 19 beta4 concurrently, compare APIs and implementation
bodies, and identify extension callers affected by a selected API change. This
is an upgrade/security investigation aid, not a vulnerability scanner or proof
of ABI compatibility.

## Agreed choices

- PostgreSQL reference: `/media/alex/MYSSD/BACKUP/workspace/postgres`.
- Separate detached worktrees at `REL_18_0` and `REL_19_BETA4`; never switch the
  reference checkout (currently `20devel`). Resolve refs to immutable commits.
- Build-free C syntax parsing. Compiler enrichment is an optional later adapter,
  not a prerequisite or a claim of compiler-level resolution.
- SQL and the existing static PL/pgSQL extraction are analyzed. Other procedural
  language declarations retain language/body metadata and explicit body-analysis
  coverage diagnostics; arbitrary PL internals are not represented as resolved
  SQL calls.
- Application source was not separately supplied. Tests use a small Python/SQL/
  Markdown application; the corpus manifest accepts its real checkout later.

## Design

1. **Corpus manifest and identities.** Strict TOML configuration lists snapshots
   with unique aliases, logical repository, version, role, path, and explicit
   dependency aliases. Dependencies choose a PG variant: never automatically
   resolve one call against all indexed versions. Reject duplicate identities,
   missing roots/dependencies, cycles, and malformed options. Repository graph
   keys use aliases; every source key also includes the indexed commit.
2. **Reuse streaming bulk ingestion.** Extend the single-root exporter with an
   explicit alias and corpus-owned SQL selection/configuration. Each snapshot
   retains extraction spools, resolver registry, search stage, and CSV shards.
   Compose their immutable export groups without whole-repository FileIR lists.
   Corpus publication uses one atomic manifest pointer only after validation.
3. **C extraction.** Tree-sitter C parses `.c`/`.h`: definitions/declarations,
   signatures, linkage, locations, body hashes, direct calls, includes, and
   preprocessor condition evidence. File-static names remain file-local. Keep
   declarations separate from definitions. Indirect/function-pointer calls,
   macros, errors and unevaluated preprocessing receive explicit coverage/status
   rather than invented exact edges. No project build commands are executed.
4. **Routine bindings and core catalog.** Extract LANGUAGE, argument signatures,
   body hashes, external library and C entry point from CREATE FUNCTION/PROCEDURE.
   Recognize `MODULE_PATHNAME` through extension `.control` metadata; only bind
   within the selected extension snapshot. Read `pg_proc.dat` safely without
   executing Perl/Python; retain builtin SQL -> `prosrc` mappings. Select `.sql.in`
   and retain template/parse diagnostics rather than silently treating unexpanded
   macros as complete SQL. Index raw templates, never execute preprocessing.
5. **Cross-language evidence.** Parse constant Python DB execute/executemany SQL
   and fenced SQL in Markdown; retain source locations and distinguish runbook
   mentions from executable calls. Dynamic strings/EXECUTE remain unresolved.
   Resolve SQL routine references by declared dependency context, schema and
   available signature/arity evidence; retain all ambiguities. Unqualified names
   use a documented ordered search path, not fuzzy text matching.
6. **Supplemental graph.** NativeSymbol, Routine, SourceEvidence and coverage
   diagnostics are staged in indexed SQLite tables, then emitted as extra Neo4j
   CSV groups. Attach to existing Repository/File/SqlObject nodes where applicable;
   never replace existing Python/SQL semantics. Edges record source, location,
   resolution status and conditional context. Supplemental keys are namespaced
   to avoid collisions with existing nodes and relationship IDs.
7. **Queries and comparison.** CLI plus read-only MCP support snapshot discovery,
   native/routine search, dependency evidence, bounded cross-language traces,
   and snapshot comparison (added/removed/signature/body changed). Compare only
   matching logical repositories. Source hashes and conditional evidence are
   not assertions about binary ABI or CVE applicability. Expose bounded paging
   and reject ambiguous selectors; no unrestricted query interpolation.

## Implementation lanes

- Luna/high extraction: immutable native/routine/evidence contracts, C parser,
  routine/catalog/literal/Markdown extraction and focused tests.
- Luna/high integration: corpus manifest, disk registry/resolution/export,
  snapshot alias + SQL configuration hooks in the bulk path, CLI and focused tests.
- Terra integration: query/MCP/schema contracts, operator documentation and
  integration alignment in files outside the active Luna write set.
- Luna/medium verification: regression fixes (including missing SQL queries),
  complete unit suite, real-reference smoke checks, Docker integration tests and
  isolated Compose smoke where needed. Never replace existing Neo4j data merely
  to validate this feature.
- Sol final complete-diff review, then Luna fixes and rerun relevant checks.

## Required validation

- Exact PG18/PG19 identity isolation and explicit pg_cron variant dependencies.
- End-to-end literal/runbook -> routine -> C -> PG API with real source ranges.
- pg_cron C bindings, PostGIS SQL templates, PG pg_proc catalog on local sources.
- Overloads, ambiguous definitions, static-name collisions, indirect calls,
  quoted SQL identifiers, UTF-8 locations, unsupported PLs and template failures.
- Deterministic semantic outputs and indexes on lookup tables; per-file streaming
  memory with bounded query results, not corpus-sized Python sets/lists.
- Offline bulk manifest/import command, actual Neo4j import/query, MCP registration,
  existing regression suite and lint. Record skipped/unavailable external checks.
- Existing reference checkouts unchanged. Worktrees/artifacts are clearly located
  and recoverable; no secrets in configuration or agent prompts.

## Non-goals in this iteration

Building PostgreSQL/PostGIS, editing their vulnerabilities, runtime SQL recovery,
general PL/Tcl/PL/Perl interpretation, macro expansion/compile-command execution,
binary ABI analysis, automatic security verdicts, or automatic version switching.

## Astra review amendments (accepted)

- CSV composition rewrites rows to one canonical header per label/type, including
  Python-only versus SQL `DEFINES` layouts. Validate all keys and endpoints in
  disk-backed tables before publication. Merge lexical stages into the existing
  single-stage schema rather than changing the search consumer contract.
- Record full Git SHA (or non-Git content identity), plus a streaming selected-source
  digest and configuration/extractor fingerprint. Index revision keys include
  the fingerprint: changing configuration/content must not reuse an old identity.
  Verify source digest and Git state before/after extraction; fail publication
  on concurrent source change. Worktree creation never executes source code.
- Thread immutable SQL overrides through discovery and worker entry points;
  suffix selection explicitly admits `.sql.in`. Supplemental projection creates
  only missing File nodes for C/header/catalog/Markdown, with normal snapshot File
  keys, and attaches each fact to its source File.
- Dependency visibility is the explicit transitive closure of each alias. Own
  file-static C definitions take precedence, then own external definitions, then
  visible dependency definitions. Ambiguous or conditional targets remain
  candidates; they do not become asserted calls. Identical repeated SQL declarations
  can share a logical routine identity, but conflicting binding/signature metadata
  remains ambiguous and each occurrence keeps provenance.
- SQL arity narrows candidates but does not prove overload typing. Unknown argument
  types or multiple same-arity overloads stay ambiguous. Documentation mentions are
  `DOCUMENTS`, never executable calls. No case folding of quoted SQL identifiers.
- Diff matching uses language + logical scope/name (file scope for static C) first,
  then unique declaration/overload matching. A unique API whose signature changes
  is `signature_changed`, not remove/add. Multiple possible matches or incomplete
  extraction become `ambiguous`/coverage warnings, not definitive removals.

## Memory/performance review requirement

Compatibility-breaking changes to staging/CLI contracts are allowed. The new
corpus workflow must not retain legacy all-root materialization for compatibility.
Prefer one bounded extraction stream producing both standard spools and native
facts, reusing staging/projection APIs rather than rescanning repositories.
Review retained caches, indexed lookups, per-file size policies, pending worker
bytes, row merge complexity and temporary disk growth. Report stage timings and
actual concurrency; batching alone is not evidence of bounded process memory.
New correctness, negative, bounded-staging and Neo4j integration suites are part
of implementation, alongside the existing regressions.
