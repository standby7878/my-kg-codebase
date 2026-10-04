# PostgreSQL and extension corpus: implementation specification

**Status:** implemented and verified, 2026-10-04.

**Code commit:** `c55e3b2` — `Add bounded PostgreSQL extension corpus analysis`.

**Extractor fingerprint:** `corpus-extract-v6`.

This specification describes the committed implementation, not a proposal.
See [the original plan](postgresql-corpus-plan.md),
[the operating guide](postgresql-corpus.md), and
[the reference configuration](../corpus.postgresql.toml).

## 1. Purpose and boundaries

Connect application Python/SQL and Markdown/runbook evidence to PostgreSQL
extension routines, their C entrypoints, and APIs in an explicitly selected
PostgreSQL version. Support source-level upgrade and vulnerability investigation
by exposing definitions, dependency evidence, uncertainty, and version diffs.

The implementation does **not** compile projects, execute their SQL, expand a
complete C preprocessor environment, prove runtime reachability, certify ABI
compatibility, or determine whether a CVE is exploitable or fixed.

## 2. Language support

| Input | Implemented facts and analysis |
| --- | --- |
| C and headers | Build-free function definitions/declarations, signatures, body hashes, macros, calls, conditions, and positioned include evidence. Indirect calls, macro interference, and ambiguous targets are not promoted to exact calls. |
| SQL / `.sql.in` | Existing SQL IR plus routine declarations, overload/default/variadic metadata, language, return type, library/entrypoint, source evidence, and diagnostics. Templates are not preprocessed or executed. |
| PL/pgSQL | Partial static body analysis, including supported query statements, scalar `RETURN`, and assignment expressions. Unsupported control/default expressions, dynamic SQL, and parse failures remain explicit coverage limitations. |
| Other procedural languages | Routine language/body metadata and coverage diagnostics, not complete internal call graphs. |
| Python | Literal or conservatively known constant SQL passed to DB execute methods. Parameters, shadowed names, rebindings, unknown captures, and dynamic construction remain unresolved/dynamic. |
| Markdown / runbooks | SQL examples and callable mentions as descriptive evidence. Documentation relationships remain labeled as documentation, not execution. |

PostgreSQL's [procedural-language documentation](https://www.postgresql.org/docs/current/xplang.html)
describes PL/pgSQL, PL/Tcl, PL/Perl, PL/Python, and additional handler-based
languages. This implementation deliberately distinguishes declaration coverage
from body analysis for those languages.

PostgreSQL `pg_proc.dat` catalog records are also extracted so built-in routine
declarations and INTERNAL entrypoints can participate in the graph.

## 3. Snapshot and identity contract

Each configured snapshot supplies `alias`, `logical_repo`, `version`, `role`,
`path`, optional `dependencies`, SQL configuration, and `max_file_bytes`.
Roles are `application`, `postgres`, and `extension`.

- Aliases are unique; dependency references and cycles are validated.
- Dependency visibility is explicit and transitive. An extension must not bind
  against every PostgreSQL snapshot simply because they are all indexed.
- INTERNAL entrypoints use the owning PostgreSQL snapshot or its explicitly
  selected PostgreSQL dependency. C-library bindings remain in their own
  snapshot scope and retain unresolved module/library diagnostics.
- Identity includes the source Git commit where available, a selected-source
  digest, configuration/extractor fingerprint, and dependency revisions.
- A semantic extractor change invalidates prior extraction identity through
  the fingerprint; the accepted implementation uses v6.
- Detected source identity changes during extraction abort publication. A previously
  published manifest remains available.

The local reference configuration contains six contexts:

| Alias | Source/version context | Dependency |
| --- | --- | --- |
| `pg18` | PostgreSQL `REL_18_0` detached worktree | None |
| `pg19` | PostgreSQL `REL_19_BETA4` detached worktree | None |
| `cron18` / `cron19` | The local pg_cron checkout, indexed separately | `pg18` / `pg19` |
| `postgis18` / `postgis19` | The local PostGIS checkout, indexed separately | `pg18` / `pg19` |

The original PostgreSQL, pg_cron, and PostGIS checkouts were not modified.
No real application checkout was supplied: application-to-extension chains are
covered by dedicated fixtures. Add the actual application path and dependencies
to the manifest before claiming coverage of that application.

## 4. Extraction, projection, and publication

```text
Configured snapshots + selected-source identity checks
    -> file-at-a-time native facts and ordinary IR staging
    -> SQLite native registry, extraction spools, search stages
    -> ordinary graph projection + context-scoped native/routine resolution
    -> supplemental nodes and typed evidence relationships
    -> composed CSV + disk-backed ID/endpoint validation
    -> atomic manifest publication
    -> offline queries, isolated Neo4j import, and MCP queries
```

Native and ordinary staging consume the selected source through the extraction
workflow, without a separate whole-repository `RepositoryIR` materialization.
Search-stage construction consumes disk-backed staging rather than reparsing
all Python and SQL into a second repository IR.

Important graph additions include `CorpusSnapshot`, `NativeSymbol`, `Routine`,
`SourceEvidence`, and `CorpusDiagnostic`. Relationships include snapshot
dependencies, file/lexical evidence ownership, routine-to-native bindings,
native calls, routine invocation, documentation, and explicitly uncertain
candidate links.

Each evidence occurrence has one File owner; a uniquely identified lexical
owner is added where available. Ownership retains path, location, condition,
and provenance. Native call evidence carries its occurrence ordinal instead
of rediscovering identity through repeated JSON equality searches.

Routine ownership uses the full schema/name identity, including quoted names
containing dots. Python package initializers and ordinary modules retain their
intended logical-module coalescing across worker spools.

## 5. Resolution and comparison semantics

- Exact means a uniquely resolved source-level target under the represented
  context; it is not proof that execution occurs.
- Conditional, ambiguous, dynamic, and unresolved facts remain distinguishable.
  Guards from source occurrences and target declarations are retained.
- Python analysis respects enclosing function/lambda/comprehension bindings.
  Deferred lambda/generator bodies do not inherit transient values that may
  have changed before invocation; first iterable/default expressions are
  analyzed in their enclosing evaluation scope.
- C include occurrences are descriptive source evidence, not compiler-resolved
  include expansion or executable calls.
- Traces are bounded and exclude candidate/conditional edges. They can include
  explicitly labeled documentation relationships; such paths remain source
  evidence paths, not execution traces.
- Comparisons require snapshots of the same logical repository and distinguish
  added/removed, signature, body, definition, condition, and ambiguous changes.
  Coverage limitations accompany source-level findings.
- SQL scalar-expression parser prefixes are removed in character coordinates
  before conversion to source UTF-8 byte offsets.

## 6. Resource and performance contract

The scalable path is the disk-backed bulk/corpus exporter, not the legacy direct
`scan_repository`/`load_repository` path, which still materializes repository IR
and derived rows. No claim is made that every legacy Python resolver loop has
been redesigned or that all stages are parallel.

Implemented improvements:

1. Native/ordinary corpus staging is file-at-a-time, with `max_file_bytes` capped
   at 64 MiB. Oversized/unreadable files produce explicit coverage state rather
   than silently entering the ordinary parser unbounded.
2. Cross-file facts, deduplication, ownership, graph validation, and search
   staging use SQLite/disk-backed state rather than whole-corpus Python sets.
3. SQL location hot paths use reusable UTF-8/newline indexes; fallback statement
   splitting no longer copies the entire remaining source at every boundary.
4. Disabled DEBUG logging no longer unconditionally JSON-serializes event fields.
5. Import ownership has an `(module, path)` index. Native/C entrypoint and SQL
   object lookups explicitly use selective indexes, avoiding snapshot-wide
   traversal caused by the ordering planner.
6. Diagnostic occurrence lookups use primary-key ordinals instead of repeated
   JSON equality scans. Evidence and owner lookups use direct occurrence keys
   and selective composite indexes.
7. Neo4j owner/callee loading uses typed, indexed label branches rather than
   label-less node matches; EXPLAIN regressions reject AllNodesScan plans.
8. CSV composition recognizes exact schema layouts and carries sidecar layouts
   through their headerless shards. Valid identifiers beginning with `key` are
   not mistaken for headers.
9. CSV readers have a finite bound. Generated manifests record the measured
   maximum serialized UTF-8 field size, including CSV quote escaping. Metadata
   is validated against a 128 MiB + 4 KiB bound. For fields above Neo4j's 4 MiB
   default, import uses the measured size plus 64 KiB; small exports keep the
   default instead of receiving a large global buffer.
10. MCP human-readable previews are bounded to 1,200 UTF-8 bytes and five rows
    while structured results retain their identifiers and provenance.

`--workers` bounds projection concurrency; native/shared extraction remains
serial. Disk-backed staging and serial validation/composition still cost time
and temporary disk space. A file-size bound is not a bound on AST expansion or
aggregate concurrent memory. Metrics report phase timings and per-process RSS
maxima, explicitly **not** aggregate simultaneous memory consumption.

Deterministic lookup regressions with 100/200/400 unrelated rows measured:

| Lookup | Previous SQLite VM operations | Selective-index operations |
| --- | --- | --- |
| Native entrypoint | 1,726 / 2,921 / 5,321 | 898 / 884 / 884 |
| SQL object identity | 740 / 2,135 / 4,935 | 51 / 34 / 34 |

These are isolated operation counts, not an end-to-end speedup benchmark.

## 7. Interfaces

CLI commands: `bulk-export-corpus`, `corpus-snapshots`, `corpus-search`,
`corpus-compare`, `corpus-trace`, and `corpus-evidence`.

The five corpus MCP tools are `list_corpus_snapshots`, `search_corpus_symbols`,
`get_dependency_evidence`, `trace_corpus_path`, and `compare_corpus_snapshots`.
They join the existing tools for 19 total tools. Queries retain bounded limits
and source provenance; tracing has a maximum depth of eight.

The combined manifest is consumed by the existing bulk import/search-stage
interfaces. Neo4j-admin import is an offline operation: use an isolated data
directory/database, then bootstrap schema indexes before serving live queries.

## 8. Verification of the committed code

| Check | Result |
| --- | --- |
| Complete project suite | **480 passed**, 72 dependency/deprecation warnings, 712.56 s |
| Evaluation suite | **37 passed**, 0.18 s |
| Ruff | Source/tests check and formatting of all 50 changed/new Python files passed |
| Dependencies / syntax / whitespace | `pip check`, source compilation, and Git whitespace checks passed |
| Review | Astra High reviewed the feature and approved the corrected material findings |
| Neo4j / Docker | Full suite included actual CSV imports, live queries, a 5+ MiB SQL field import, typed-index plan checks, and fresh isolated Compose MCP checks |
| MCP transport gates | Original thresholds retained; 80.30% text and 28.22% wire reduction passed |

New suites cover configuration/identity, bounded staging, extraction, Unicode
positions, overloads and guards, context isolation, ownership, Python implicit
scopes, query plans/operation counts, CSV field sizing and exact round trips,
offline/live queries, diffs, traces, and MCP contracts.

Final v6 export against all six real reference contexts passed endpoint
validation and published **1,655,127 nodes and 3,318,772 relationships**.
Observed elapsed time was 816.389 s while the test suite was also running;
parent peak RSS was 235,296 KiB and maximum reported child peak was 197,628 KiB.
These are run-specific per-process observations, not production memory bounds.
The maximum serialized field was 7,632,868 bytes, producing an import read buffer
of 7,698,404 bytes.

Offline smoke checks passed snapshot discovery, pg_cron/PostGIS symbol search,
PG18-to-PG19 source comparison, and a three-edge exact source path from
`cron.schedule(text,text)` through extension C code to the selected PG18 API.
The full real-reference dataset was not loaded into a production Neo4j service;
actual importer/live-query behavior was exercised with isolated integration
fixtures. Source-level findings must still be interpreted with their coverage
diagnostics and build/runtime limitations.
