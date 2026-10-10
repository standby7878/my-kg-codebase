# CodeKG model: detailed specification

This describes the registry-enabled, independent-graph implementation in `src/codekg`. It documents emitted/static facts and request-time algorithms, not runtime behavior. “Exact” below is the implementation's resolution state; it must not be read as proof of runtime execution, SQL type dispatch, ABI compatibility, or installation state.

## 1. Graph and generation identity

A registry contains graph entries of kind `application` or `database`, each pointing to one frozen generation manifest and one Neo4j Community backend. A database graph represents one PostgreSQL version snapshot plus selected extension/dependency snapshots. A context explicitly binds one application graph to one database graph and supplies `application_database`, `visible_extensions` (snapshot aliases), and `search_path`.

The serving `GraphHandle` is pinned to `(graph_id, generation_id)`. Its backend must contain exactly one matching `CodeKGGeneration` marker. `EntityRef` is exactly `{graph_id, generation_id, local_key}`; a reference from another graph or generation is invalid for that selected handle. A federation response carries application and database graph/generation metadata and, for context-bound operations, `context_id`; the context has no separate revision field in the current response model. Pagination cursors are similarly scoped to selector/context and generation(s). The registry can simultaneously register multiple application and database generations; each `GraphContext` selects exactly one pair. Registry content is loaded once per MCP process; activating a changed registry requires process restart. Discovery metadata alone does not verify a backend; first query/request-boundary verification does.

Corpus extraction stages facts and opaque occurrence keys in a generation-local SQLite `corpus.sqlite`; the exporter projects graph facts/relationships to CSV for offline Neo4j import. The catalog serves bounded, immutable read-only lookup from SQLite. Neo4j holds searchable graph facts and imported relationships. Neither store has a cross-database application-to-database relationship. The source is authoritative for what is emitted; see [registry](../src/codekg/graph_registry.py), [catalog](../src/codekg/graph_catalog.py), [export](../src/codekg/corpus_export.py), and [corpus registry/resolution](../src/codekg/corpus_registry.py).

### Ingestion and serving lifecycle

Within a selected source snapshot, extraction reads source files into ordinary repository/SQL facts and the supplemental native/routine/evidence staging tables in the same corpus SQLite workflow. The per-generation corpus catalog retains richer facts and resolution postings than the flattened graph columns. Offline composition resolves facts within that corpus, then emits Neo4j CSV nodes/relationships and a manifest with expected counts. Operators freeze the exported generation, import it into a fresh graph-specific Neo4j Community instance, apply schema, and verify imported counts plus the exact graph/generation marker. Only a validated registry is activated; an MCP restart is explicit and required to load that registry. App and database snapshots can be built/refreshed independently; ingestion does not eagerly compute a cross-KG dependency closure. Cross-KG resolution happens under a selected context at query time.

## 2. Stored entities and relationships

### Application graph

The ordinary application graph includes `Repository`, `File`, `Function`, `Method`, `ModuleInit`, `CallSite`, and, where relevant, `Type` and SQL-source labels such as `SqlArtifact`, `SqlStatement`, `Reference`, `Database`, and `SqlObject`. Repository/file and language-specific SQL edges are projected by the normal loaders. Important call relationships are:

| Stored relationship | Direction | Meaning |
| --- | --- | --- |
| `CONTAINS` | `Repository -> File`; `File -> Function/Method/ModuleInit/Type` | Repository/source structure. |
| `HAS_CALLSITE` | callable owner (`Function`, `Method`, `ModuleInit`) `-> CallSite` | Source-of-truth syntactic call occurrence. |
| `RESOLVES_TO` | `CallSite -> Function/Method` | Stored exact call-site resolution, with strategy/confidence metadata. |
| `CALLS` | caller `-> callee` | Compatibility projection of a resolved call; not the preferred traversal projection. |
| `EXACT_CALLS` | caller `-> callee` | Stored bounded-traversal projection emitted only for exact current-snapshot call resolution. |
| `CONSTRUCTS` | `CallSite -> Type` and compatibility projection callable owner `-> Type` | Resolved construction fact. |
| `HAS_DATABASE` | repository `-> Database` | SQL database ownership. |
| `HAS_OBJECT` | `Database -> SqlObject` | SQL object membership. |
| `CONTAINS_SQL` | file/artifact/statement according to SQL projection | SQL source containment. |
| `HAS_REFERENCE` | SQL artifact/statement `-> Reference` | SQL reference occurrence. |
| `REFERS_TO` | `Reference -> SqlObject` | Stored SQL-name resolution. |
| `DEFINES`, `READS_FROM`, `WRITES_TO`, `INVOKES_SQL`, `ALTERS`, `DROPS` | `SqlStatement -> SqlObject` | Role-specific SQL object use, with role/location metadata. |

SQL source containment is `File -> SqlArtifact -> SqlStatement`, with nested `SqlStatement -> SqlStatement` containment; `SqlStatement -> Reference` is `HAS_REFERENCE`. The role-specific derived edges are emitted only for exact resolutions. See [loader](../src/codekg/loader.py) and [SQL projection](../src/codekg/sql_graph.py).

### Database corpus graph

The supplemental/database projection uses these principal labels and fields:

| Label | Exported Neo4j properties (selected actual columns) |
| --- | --- |
| `CorpusSnapshot` | `key`, `alias`, `logical_repo`, `version`, `role`, `git_commit`, `revision`, `source_digest`, `fingerprint`, `root_path` |
| `File` | Ordinary file properties from the shared file schema; database-source file facts are represented by `File`. |
| `NativeSymbol` | `key`, `snapshot_alias`, `name`, `kind`, `logical_id`, `signature`, `language`, `path`, `start_line/end_line`, `start_column/end_column`, `static`, `declaration`, `condition`, `body_hash`, `return_type`, `definition_hash`, `coverage`, `comparison_primary` |
| `Routine` | `key`, `snapshot_alias`, `name`, `kind`, `logical_id`, `signature`, `language`, `path`, location fields, `arity`, `library`, `entrypoint`, `body_hash`, `return_type`, `definition_hash`, `coverage`, `condition`, `comparison_primary` |
| `SourceEvidence` | `key`, `snapshot_alias`, `name`, `path`, `origin`, location fields, `owner_key`, `status`, `dynamic`, `candidate_count`, `candidate_keys_json`, `condition` |
| `CorpusDiagnostic` | `key`, `snapshot_alias`, `path`, `category`, `severity`, `line`, `column`, `message` |

`CorpusDiagnostic` is an ingestion/extraction/resolution note, not a call or dependency. `HAS_CORPUS_DIAGNOSTIC` therefore means “this source file has a recorded diagnostic,” not “the file depends on the diagnostic.” Its `category`, `severity`, `message`, and optional `line`/`column` describe the note. A diagnostic can report incomplete coverage or a limit of available source metadata without indicating a bug in that source—for example, `MODULE_PATHNAME` may lack a concrete configured library identity, or a template/parser subset may not expose a body. Absence of a diagnostic is not itself a completeness guarantee.

For example, a read-only Neo4j Browser query for unresolved extension library placeholders is:

```cypher
MATCH (f:File)-[:HAS_CORPUS_DIAGNOSTIC]->(d:CorpusDiagnostic)
WHERE d.category = 'unresolved_module_pathname'
RETURN f.path AS source_file, d.category AS category,
       d.severity AS severity, d.line AS line, d.message AS explanation
ORDER BY source_file, line
LIMIT 100
```

The catalog's SQLite `routines.fact` also carries fields such as `schema_name`, `default_arg_count`, and `variadic_arg_count`; these are not exported `Routine` Neo4j columns. SQLite evidence facts carry richer fields (`object_name`, `schema_name`, `arity`, `owner_qname`, `text`/hash, `dynamic`, etc.) than the selected flattened Neo4j `SourceEvidence` columns. Do not infer missing Neo4j properties from the SQLite fact schema or vice versa.

Relationships emitted in the corpus graph include:

| Stored relationship | Direction | Meaning / relevant properties |
| --- | --- | --- |
| `SNAPSHOT_OF` | `CorpusSnapshot -> Repository` | Snapshot-to-logical repository link. |
| `DEPENDS_ON_SNAPSHOT` | `CorpusSnapshot -> CorpusSnapshot` | Explicit snapshot dependency. |
| `HAS_NATIVE_SYMBOL` | `File -> NativeSymbol` | File contains a native fact; status/location/condition can be retained. |
| `HAS_ROUTINE` | `File -> Routine` | File contains a routine definition/catalog fact. |
| `HAS_EVIDENCE` | `File -> SourceEvidence` and, when uniquely established, owning `Function`/`Method`/`Routine`/`NativeSymbol -> SourceEvidence` | Evidence ownership. A file owner is the `File` node (not a separate `CorpusFile`). Owner edges retain status, location and condition. |
| `HAS_CORPUS_DIAGNOSTIC` | `File -> CorpusDiagnostic` | Extractor/resolution diagnostic. |
| `INVOKES_ROUTINE` | `SourceEvidence -> Routine` | Stored, within-corpus routine invocation only when emitted by resolution. |
| `DOCUMENTS_ROUTINE` | `SourceEvidence -> Routine` | Documentation link, not executable invocation. |
| `ROUTINE_CANDIDATE` | `SourceEvidence -> Routine` | Potential routine matches whose identity is not exact. |
| `DESCRIBES_SQL_OBJECT` | `Routine -> SqlObject` | Exact kind/signature match between source routine and SQL object. |
| `BINDS_TO_NATIVE` | `Routine -> NativeSymbol` | Unique allowed C/internal binding. |
| `NATIVE_CANDIDATE` | `Routine -> NativeSymbol`, or native caller/evidence `-> NativeSymbol` | Candidate only; not an asserted path edge. |
| `CALLS_NATIVE` | `NativeSymbol -> NativeSymbol` and, where occurrence identity exists, `SourceEvidence -> NativeSymbol` | Exact statically resolved C call. |

Other graph relationship kinds can occur in the regular application/SQL model. The table above focuses on cross-language/native corpus facts. Stored relationship provenance/status/condition is important: candidate relationships do not become exact merely by appearing in the graph.

### Identity and revision rules

- Each graph has its own `graph_id` and immutable `generation_id`; a fact reference is additionally keyed by an opaque, generation-local `local_key`.
- Corpus snapshots have `alias`, repository/version/role metadata, source identity (`git_commit` when known), content/config `revision`, source digest and fingerprint. `revision` is not a Git commit.
- A graph `generation_id` hashes the exact generation-manifest bytes together with the sorted `(snapshot alias, revision)` vector; its graph ID scopes that identity. The selected context contributes a context ID/configuration to federation selection, not another emitted generation/revision scalar.
- Corpus fact keys derive from snapshot alias, revision, path, fact table and occurrence ordinal. Repeated same-name facts/calls remain distinct occurrences; logical IDs are comparison/grouping aids, not EntityRefs.
- SQL and native evidence retains source path/line/column and may include a conditional guard. Revisions/generations are not unified across versions: compare tools compare scoped catalogs, they do not merge the graph versions.

## 3. Evidence production and resolution

Current `SourceEvidenceIR` includes `origin`, optional schema/object name and arity, owner qname/line, source span, dynamic flag, optional text/hash, and condition. Implemented origins include:

- `python_execute`: literal/constant SQL passed to recognized Python database `execute` calls. Static string literals, safe module constants and locally tracked constants can be extracted; dynamic interpolation/runtime values are retained as dynamic evidence or omitted from exact-name resolution.
- `sql_source`: statically recognized function references in selected SQL source files outside extracted routine bodies.
- `routine_body`: SQL/PL/pgSQL function/procedure body call references found in selected `CREATE FUNCTION/PROCEDURE` source. SQL body evidence is emitted only where the established SQL parser can statically identify it.
- `native_call`: C function-call occurrence parsed from selected native source; paired to a uniquely owned C function when possible.
- `markdown_sql`: function calls from fenced SQL in Markdown; `markdown_mention`: explicitly qualified backticked routine mentions. These are documentation evidence; resolver reports `documentation` and does not assert invocation.

C `NativeSymbol` facts are parsed from selected C files. Routine facts are extracted from selected SQL/`.sql.in` declarations and safely parsed `pg_proc.dat` records. Those parsers do not execute source/build/configure code. See [native IR](../src/codekg/native_ir.py), [Python/Markdown evidence](../src/codekg/native_evidence.py), and [routine/catalog extraction](../src/codekg/native_sql.py).

### Contextual lookup order

For a concrete non-documentation intent, resolution checks schema/name and evidence arity in the application graph's routine facts first for each schema in context search-path order (or the explicitly named schema). An application-local routine shadows a database routine of the same selected name in that scope. If none exists locally, it checks the selected PostgreSQL snapshot plus only aliases in `visible_extensions`, with database schema/search-path ordering. It does not search every database graph or every registered extension implicitly.

The current `_signature_known` check accepts a unique unguarded candidate when the evidence arity is compatible with candidate arity/default/variadic counts and no argument-type evidence is present. It explicitly does **not** prove PostgreSQL overload selection by argument types. Candidate truncation prevents exact status.

### Resolution states

- `exact`: one candidate selected by this static/contextual algorithm, with no relevant condition and no candidate overflow. Treat as a statically resolved source relation, not runtime proof.
- `ambiguous`: multiple candidates or insufficient static signature evidence to establish one.
- `conditional`: a unique candidate or source is guarded by an unevaluated condition/template/control context; not an unconditional path assertion.
- `candidate_overflow`: candidate list exceeded the bounded lookup cap; resolution is incomplete.
- `dynamic`: evidence has a dynamic name/value and no static target is asserted.
- `unresolved`: concrete name has no visible candidate or cannot be resolved.
- `documentation`: Markdown mention/snippet; descriptive evidence, not an invocation.

Corpus export can also summarize evidence's stored local status as `exact`, `conditional`, `ambiguous`, `dynamic`, or `unresolved` with candidate count/key list. This projection is not the same as a fresh context-specific federation result. Federation responses may additionally return API statuses including `ok`, `not_found`, `invalid_reference`, `invalid_arguments`, `context_required`, `deadline_exceeded`, and `invalid_cursor`.

## 4. Native binding and analysis limits

For SQL `LANGUAGE c`, extraction interprets `AS` values as library then C entrypoint (default entrypoint is the routine name when absent). For `LANGUAGE internal`, `AS` supplies the internal entrypoint. Selected PostgreSQL dependency snapshots provide possible native definitions. A `BINDS_TO_NATIVE` edge is emitted only when binding is allowed and exactly one selected non-static native function matches the entrypoint; otherwise matches are `NATIVE_CANDIDATE`, or an unresolved-binding diagnostic is recorded. Internal binding is explicitly limited to selected dependencies. A concrete extension library/control-file identity must be available to permit a definite C binding; a placeholder such as `MODULE_PATHNAME` is not magically resolved from a configure/build environment.

Native C calls are exact only when source parsing yields a concrete callee, owner resolves uniquely, one preferred target remains, and no visible macro/condition makes the target uncertain. Dynamic/indirect calls do not receive asserted target edges. Macro uncertainty and unresolved/ambiguous ownership are diagnostics/candidates rather than exact edges.

For `.sql.in`, preprocessor directives are masked, not evaluated. Template guards survive as conditions; unresolved tokens or unsupported attributes generate diagnostics and can reduce coverage. No preprocessor configuration matrix or configured output is synthesized.

## 5. SQL and PL body coverage

- `LANGUAGE sql` and `LANGUAGE plpgsql` declarations are recorded. The implementation reuses the established SQL parser for statically analyzable routine bodies and emits body evidence for concrete function references (including dynamic references as dynamic evidence). Parse diagnostics can downgrade body coverage to `body_partial`.
- A known body hash/coverage marker describes extracted source availability, not full semantic interpretation or execution. C/internal routines are declaration/binding facts, not parsed SQL bodies.
- Other languages are recorded as declaration-only with an unsupported-language diagnostic.
- PostgreSQL `pg_proc.dat` parsing handles a deliberately restricted quoted key/value record subset without evaluating Perl. Unsupported/malformed records are diagnostics; catalog mapping facts do not imply executable/runtime availability.
- External SQL evidence is not a complete SQL dependency model: current cross-language bridge evidence emphasizes function calls. Full SQL statement object semantics are represented separately by the regular SQL graph parser.

## 6. Forward and reverse federation

The public MCP federation surface is implemented in [federation.py](../src/codekg/federation.py) and registered in [server.py](../src/codekg/mcp/server.py):

- `list_knowledge_graphs`: metadata-only graph/context inventory; backend verification is deferred until query.
- `list_database_intents`: bounded/paged app-side intent discovery by owner path, owner qname, or exact evidence key.
- `resolve_database_intent`: resolve one generation-scoped app evidence reference under a named context.
- `trace_application_database_path`: forward composition from exactly one evidence ref, owner path/qname, or selected entry ref to an optional database target. It follows app `EXACT_CALLS`, evidence ownership, context-bound intent-to-routine resolution and bounded stored database relationships. Application-local SQL routine body evidence is followed one level before switching to a database routine only when an explicit database target is provided. Without that target, a locally resolved path ends at the application-local routine. Only one app/database boundary is supported; callbacks and repeated crossings are outside coverage.
- `find_application_database_usages`: reverse from one database-generation target through stored database ancestors to bounded same-name app evidence postings, then re-resolves postings in context. Qualified and unqualified names are candidate postings, not proof; ambiguous/conditional hits are marked candidate-only.
- `compare_knowledge_graphs`: compare two selected database graph generations by logical repository with bounded catalog work; no graph merge.

Bounds are part of semantics. Trace clamps deadline to at most 10 seconds, depth to 1–32, output limit to at most 5 paths, Python reachable-owner walk and evidence collection are capped, and database traversal keeps visited/edge budgets. Reverse usage has a 10-second deadline, at most 1,000 returned usages, an incoming database walk capped at 1,000 visited facts/10,000 examined edges and a 1,000-posting work cap. Results expose truncation/coverage; incomplete work must not be represented as complete. Continuations bind context, both graph generations, target and depth; an incomplete ancestor walk cannot be resumed as if complete.

The response serializes virtual steps with relationship names such as `INVOKES` and `INVOKES_LOCAL_ROUTINE`. These are path-segment labels, not persisted graph relationship types. The persisted relation on the database side, if any, remains its actual type such as `INVOKES_ROUTINE`, `BINDS_TO_NATIVE`, or `CALLS_NATIVE`.

## 7. Demonstrated example (static only)

The live demo includes a fixture path:

```text
Python run
  --EXACT_CALLS--> inspect_cron
  --HAS_EVIDENCE--> Python SQL intent for cron.schedule
  --(virtual INVOKES under python-on-pg18 context)--> pg_cron Routine cron.schedule
  --BINDS_TO_NATIVE--> pg_cron NativeSymbol cron_schedule
  --CALLS_NATIVE--> PostgreSQL NativeSymbol errmsg
```

The PostgreSQL/pg_cron native implementation reached through `cron_schedule` contains a branch-specific error call to PostgreSQL `errmsg`; the path is extracted C source evidence and does not claim that the branch runs. The Python fixture itself only calls `cron.schedule`. More generally, trace tests establish source-to-source resolution against pinned snapshots, not that a live service executed the statement. The [demo report](federated-kg-demo.md) documents additional examples and explicitly distinguishes static evidence from runtime assertions.

## 8. Supported behavior versus future/generalized scope

| Implemented behavior | Not implied / not currently supported generally |
| --- | --- |
| Explicit Python literal/constant SQL evidence; selected SQL/PL routine-body evidence; selected C calls and PostgreSQL/extension routine-to-native bindings. | Arbitrary language plugins, arbitrary framework/database APIs, runtime string evaluation, reflective/dynamic dispatch, or universal multilingual interprocedural analysis. |
| Explicit context selects DB graph, visible extension aliases and search path; local routines shadow DB candidates. | Automatic discovery of installed extensions, dependency compatibility, extension build outputs or ambient configure flags. |
| Version-scoped generations and bounded forward/reverse source paths. | Persisted cross-database relationships, graph-wide union across versions, unbounded path completeness, repeated database crossings or callbacks. |
| SQL/PL and C/native extraction with diagnostics and coverage fields. | Full template/preprocessor evaluation, execution, complete control-flow/path conditions, ABI verification or proof of runtime behavior. |

## Source map

- [Registry and generation references](../src/codekg/graph_registry.py)
- [Federation and virtual path construction](../src/codekg/federation.py)
- [Generation catalog](../src/codekg/graph_catalog.py)
- [Offline corpus graph export](../src/codekg/corpus_export.py)
- [Fact extraction and in-corpus resolution](../src/codekg/corpus_registry.py)
- [Native/routine IR](../src/codekg/native_ir.py)
- [SQL routine and `pg_proc.dat` extraction](../src/codekg/native_sql.py)
- [Python SQL and Markdown evidence](../src/codekg/native_evidence.py)
- [Existing graph operations and demo](independent-graph-operations.md), [federated demo](federated-kg-demo.md)
