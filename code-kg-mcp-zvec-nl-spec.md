# Code-KG MCP — Descriptive Code Search

## Decision

Keep Neo4j as the **code graph** and zvec as a derived, local **description index**.
There are no `Document` or `DocChunk` nodes, no `MENTIONS` relationships, and no standalone
documentation search results. Markdown is an input to description construction only.

Each zvec record represents exactly one live Neo4j `Function` or `Method` node. zvec document IDs
must use its safe identifier alphabet and length limit, so `id` is the deterministic 64-character
lowercase SHA-256 hex digest of the exact Neo4j `key`; the exact key is also stored in the required
`key` scalar field. A
descriptive search therefore has one simple join:

```text
human-language query
  -> zvec ranks code descriptions
  -> zvec record key field (Neo4j symbol key)
  -> Neo4j fetches and returns the code-graph node
  -> existing graph tools traverse callers, callees, imports, hierarchy, and so on
```

zvec is a ranking sidecar, not a second graph and not a source of structural truth.

## Goal

Let an agent find a function or method from a human-language description, such as “the function
that promotes a standby”, then continue with the existing Neo4j code-graph tools. Identifier and
qualified-name search must continue to work.

## Non-goals

- No document graph in Neo4j: no `Document`, `DocChunk`, or documentation-to-code edges.
- No prose blobs in Neo4j. Docstrings and Markdown prose are indexed in zvec only.
- No standalone documentation search surface or additional MCP tool.
- No runtime network access, model download, or agent-exposed indexing.
- No semantic embeddings or comments in the first delivery. Those are later, optional phases.
- No additional source languages. Phase A reads Python and optional Markdown enrichment only.

## Fixed constraints

| # | Constraint | Final design |
|---|---|---|
| 6 | In-process search engine | zvec `==0.5.1`, installed from a vendored wheel under `third_party/wheels/`. |
| 7 | Offline runtime | Phase A is FTS-only and has no embedding model. A later semantic phase must vendor its model and runtime. |
| 8 | MCP budget | Keep the existing ten tools. Descriptive search is a `search_symbols` mode. |
| 9 | Idempotency | zvec is a derived per-repository index. On replacement or deletion, remove its records before changing the graph snapshot, then rebuild from the new snapshot. |
| 10 | Concurrency | Ingestion is the only zvec writer. MCP opens the collection read-only from the `zvec_data` volume. |

## Data design

### Neo4j

The existing code graph remains the authority:

```text
Repository -> File -> Function | Method | Type
Function | Method -[:CALLS]-> Function | Method
File -[:IMPORTS]-> Module
Type -[:INHERITS]-> Type
Type -[:HAS_METHOD]-> Method
```

No schema additions are needed for Markdown. The existing symbol key is the cross-store anchor:

```text
{repo}@{commit}:{path}:{qname}:{start_line}
```

`SymbolIR` gains an in-memory `docstring: str | None`, taken with `ast.get_docstring`. It is used
to create the zvec description and is not persisted as a Neo4j property.

### zvec

There is one FTS collection named `codekg` and one record for every indexed `Function` and
`Method`. Types continue to be found through graph identifier search; they are not descriptive
search records in Phase A.

```text
id          SHA-256 hex digest of key; zvec-internal identifier
key         exact Neo4j Function/Method key; unique cross-store anchor and join value
text        complete description text; FTS indexed
repo        repository name; scalar filter
commit      indexed commit/content hash; scalar metadata
path        source file path; scalar metadata
qname       qualified symbol name; scalar metadata
kind        function | method; scalar filter
signature   source signature; result metadata
start_line  source location; result metadata
end_line    source location; result metadata
```

The collection contains neither document records nor a `source` discriminator. zvec text is
derived data and never changes the graph structure.

## Description construction

For every function or method, construct exactly one description document:

```text
description_text =
    normalized_name
  + "\n" + qname
  + "\n" + signature
  + "\n" + docstring                     # when present
  + "\n" + attached_markdown_descriptions # zero or more, when resolved
```

`normalized_name` splits snake_case and camelCase, for example `chooseBestStandby` becomes
`choose best standby`. Name, qualified name, and signature are always present, so undocumented
symbols remain retrievable.

### Markdown enrichment

Markdown is deliberately conservative because it is not a graph entity:

1. Discover `*.md` files, excluding the repository's normal ignored/build/vendor directories.
2. Split by heading hierarchy; fenced code blocks are retained as their own chunks.
3. Extract explicit qualified symbol mentions from inline/fenced code and dotted identifiers.
4. Resolve only exact mentions against the snapshot's function/method `qname` map.
5. Append a matching chunk's text to every referenced symbol's description record.

An unqualified or ambiguous mention is ignored. A chunk may be duplicated into several symbol
descriptions; documentation volume is expected to be small, and this keeps the query path simple.
The returned search result can include a short zvec snippet, but it always identifies a code node.

## Indexing and consistency

`codekg index PATH` and `codekg reindex PATH` are the only indexing entry points. There is no
separate `index-search` operation in the final design.

For a replacement or deletion, use this order:

```text
1. Delete all zvec records whose repo scalar equals the repository name.
2. Delete or replace the Neo4j repository snapshot.
3. Load the new Neo4j code graph.
4. Build one description record per newly loaded Function/Method.
5. Upsert those records into zvec and flush it.
```

Two local stores cannot provide one cross-store transaction. This ordering intentionally favours a
temporarily missing derived index over an index that returns a deleted graph node. A failed index
can be rerun safely.

After every successful index, validate:

```text
set(description keys built from the snapshot) == set(live Neo4j Function/Method keys for repo)
every live graph key deterministically fetches a zvec record with the same key field
every known pre-replacement key no longer fetches after replacement
```

zvec 0.5.1 has no collection-scan API for FTS-only collections, so it cannot enumerate arbitrary
records by repository. The writer is the only producer, deletes by repository filter before every
replacement, and validates all live/current and known prior snapshot keys by deterministic fetch.
This is the strongest directly testable invariant its API supports; the MCP join remains a final
read-time liveness guard.

MCP also resolves every zvec hit through Neo4j and drops a hit that no longer has a live matching
node. That is a defensive read-time guard, not a substitute for the index-time check.

## MCP design

Keep the current ten-tool surface:

- `list_repositories`
- `search_symbols`
- `get_definition`
- `find_callers`
- `find_callees`
- `trace_call_path`
- `find_importers`
- `get_class_hierarchy`
- `find_dead_code`
- `get_complexity`

Extend only `search_symbols`:

```python
search_symbols(
    q: str,
    *,
    mode: Literal["graph", "lexical"] = "graph",
    kind: Literal["function", "method", "type"] | None = None,
    repo: str | None = None,
    limit: int = 25,
) -> list[SearchResult]
```

- `mode="graph"` preserves the current Neo4j full-text identifier/qname search, including types.
- `mode="lexical"` performs zvec FTS over function/method descriptions. It filters by `repo` and
  `kind`, resolves hit `key` fields through Neo4j, and returns normal code-symbol result fields plus
  `score` and a bounded description `snippet`.
- `sources` is intentionally absent: there is only one searchable source, code descriptions.
- `semantic` and `hybrid` are deferred until an offline embedding model is deliberately added.

An agent uses lexical search to discover an unknown symbol, then uses the structural tools to ask
factual graph questions about it.

```text
“what promotes a replica?” -> search_symbols(mode="lexical")
                            -> patroni.ha.Ha.promote
                            -> get_definition / find_callers / find_callees / trace_call_path
```

## Implementation plan

1. **Simplify the IR and scanner**
   - Add `SymbolIR.docstring` and capture it during the existing AST pass.
   - Add a Markdown-only scanner/chunker used solely to attach resolved prose to symbol
     descriptions. Do not introduce `DocFileIR` or `DocChunkIR` graph entities.
   - Unit-test normalized names, docstring extraction, chunking, exact mention resolution, and
     ambiguous-mention rejection.

2. **Keep the loader graph-only**
   - Do not add `Document`, `DocChunk`, or `MENTIONS` Cypher writes or constraints.
   - Preserve existing repository replacement behavior for code nodes and edges.
   - Expose the loaded function/method keys and metadata needed to build description records.

3. **Implement the zvec repository index**
   - Create/open one FTS-only collection with the fields above.
   - Build descriptions from the in-memory repository snapshot, not by reparsing files after graph
     load.
   - Use a deterministic SHA-256 hex zvec ID for each record and retain the exact Neo4j key in a
     `key` scalar field. Delete records by repository filter, upsert a complete replacement set, flush,
     and validate exact `key` equality with live graph function/method keys.

4. **Couple zvec to normal index/delete commands**
   - Invoke the derived-index lifecycle from `index_repository` and the repository-delete command.
   - Remove any independent `index-search` CLI command and script step.
   - Keep zvec failure explicit: an index must not report success when graph and derived index have
     diverged.

5. **Add lexical MCP lookup**
   - Add the explicit `mode` parameter to the existing `search_symbols` tool.
   - For lexical mode, open zvec read-only, fetch ranked ids, resolve them in one Neo4j query, and
     preserve ranking order while omitting stale hits.
   - Keep graph mode unchanged as the dependency-free fallback.

6. **Package and harden the runtime**
   - Vendor `zvec-0.5.1` for supported image architectures under `third_party/wheels/` and install
     it with `pip --no-index --find-links ...`.
   - Mount `zvec_data` read-write in ingestion and read-only in MCP.
   - Preserve MCP read-only root filesystem, dropped capabilities, no-new-privileges, tmpfs, and
     no-egress network posture.

7. **Verify the complete slice**
   - Unit tests for description construction, zvec adapter, lifecycle ordering, and MCP result
     merging.
   - Integration test with Neo4j and real zvec FTS: docstring keyword and normalized-name lookup
     return the expected code node.
   - Replacement test at a new commit proves that no old zvec ids remain and all current ids resolve
     to live code nodes.
   - MCP read-only collection test and container no-egress test.

## Acceptance criteria for Phase A

```text
ruff check .
ruff format --check .
pytest -m unit
pytest -m integration
```

In addition to the commands above:

- A docstring-bearing function is found by a docstring keyword.
- An undocumented function is found by its normalized name.
- Markdown prose that explicitly names a function improves retrieval of that function.
- Lexical search always returns a live Neo4j code node, never a document-like result.
- A replace index leaves no old repository zvec records and passes the exact-key consistency check.
- MCP cannot write the collection and has no network egress.
- The final schema has no `Document` or `DocChunk` labels/constraints added by this feature.

## Deferred follow-ups

- **Semantic mode:** vendor an evaluated CPU embedding model, embed the same description text at
  ingest, and add `mode="semantic"` with a recall@5 regression fixture.
- **Hybrid mode:** combine FTS and vector ranking only after semantic mode is proven useful.
- **Comments:** add carefully filtered comment enrichment only if evaluation shows it improves
  retrieval. It remains outside Phase A.
