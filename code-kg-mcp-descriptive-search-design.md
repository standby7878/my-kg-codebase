# Code-KG Descriptive Search — Implemented Design

## Target

Enable an MCP agent to find code from an intent-level description, for example:

> “the function that promotes a standby to primary”

The result must be a live Neo4j `Function` or `Method` node that the agent can immediately use with
the existing structural code-graph queries: callers, callees, call paths, imports, hierarchy,
complexity, and dead-code candidates.

The target is **descriptive code discovery**, not a separate documentation knowledge graph.

## Chosen design

Neo4j is the structural source of truth. zvec implements a local, derived
lexical description index and ranking layer. It is FTS-only: it does not create
embeddings or a vector index.

```text
Python + Markdown at index time
        |
        v
one description per Function/Method
        |
        v
zvec FTS record ── exact key field ──> Neo4j code node
        |
        v
search_symbols(mode="lexical") returns the live code node
```

### Neo4j: code graph only

Neo4j stores repositories, files, functions, methods, types, calls, imports, inheritance, and
method ownership. It does not store Markdown documents, document chunks, mention edges, or prose
blobs introduced by this feature.

### zvec: one description record per callable

Every live Neo4j `Function` and `Method` has one zvec record. The record contains:

| Field | Purpose |
|---|---|
| `id` | Fixed-length SHA-256 hex digest used as zvec's internal safe identifier. |
| `key` | Exact Neo4j node key; the authoritative cross-store join value. |
| `text` | FTS-indexed descriptive text. |
| `repo`, `commit`, `path`, `qname`, `kind`, `signature`, lines | Filtering and result metadata. |

The SHA-256 ID is necessary because zvec rejects the repository key format (`@`, `:`, and long
identifiers) as a document ID. The original key is preserved unchanged in the scalar `key` field,
so zvec never becomes the authority for code identity.

## Description construction

At scan time, each callable description is assembled from:

```text
normalized symbol name
qualified name
signature
AST docstring, if present
Markdown chunks that explicitly name that exact callable qname
```

`chooseBestStandby` therefore contributes `choose best standby` even without a docstring.

Markdown is deliberately enrichment rather than a graph entity:

1. Only `*.md` files are considered.
2. Files are split by heading hierarchy; fenced code is retained as its own chunk.
3. Only explicit, exact qualified callable references attach text to a description.
4. Ambiguous or unqualified mentions are ignored.

Small documentation passages may be duplicated into several matching descriptions. This is an
intentional trade-off: it makes the read path one ranked code record → one graph node lookup.

## MCP behavior

The tool set remains capped at ten tools. No new search tool is added.

```python
search_symbols(q, mode="graph" | "lexical", kind=None, repo=None, limit=25)
```

- `mode="graph"` preserves the existing Neo4j identifier and qualified-name search.
- `mode="lexical"` runs zvec FTS over descriptions, resolves returned `key` values in one Neo4j
  query, preserves zvec ranking, and drops stale hits defensively.

The lexical mode returns code nodes only—never a document-like result.

## Lifecycle and consistency principles

### Derived-index ownership

Only normal operator indexing writes zvec. MCP opens the collection through zvec's read-only API.
There is no separate `index-search` command, so graph and description indexing cannot silently be
run as unrelated jobs.

### Replacement order

For `replace=True` and repository deletion:

```text
delete zvec records for repository
optimize and flush zvec publication
mutate/delete Neo4j graph snapshot
load new graph snapshot
build, upsert, optimize, and flush new descriptions
```

This favours a temporarily absent derived index over a stale index that points to deleted code.

### Validation

After indexing, the implementation verifies:

- description keys generated from the snapshot equal the live Neo4j `Function`/`Method` keys;
- every live graph key deterministically fetches a zvec record whose stored `key` matches it;
- known keys from a replaced snapshot no longer fetch.

zvec's FTS-only API cannot enumerate every arbitrary document by repository. The writer is therefore
the sole producer, deletes by repository filter before replacement, and validates all current and
known prior snapshot keys. Lexical MCP reads also resolve every hit through Neo4j and omit any stale
key.

## Operational principles

- **Offline runtime:** zvec is vendored as a pinned `0.5.1` wheel and installed offline in the app
  image. Runtime containers do not need model or wheel downloads.
- **Single writer, many readers:** ingestion is the only component that calls zvec write APIs. MCP
  mounts `zvec_data` read-write solely because zvec `0.5.1` opens its `LOCK` file even when the
  collection option is read-only; MCP opens the collection with the read-only option and has no
  mutation tool or code path.
- **Reader publication:** zvec requires `optimize()` followed by `flush()` before a fresh read-only
  process sees FTS results. Both insertion and deletion use that sequence.
- **No speculative enrichment:** comments, embeddings, semantic ranking, hybrid search, and extra
  document formats are deferred until separately evaluated.
- **Bounded responsibility:** zvec ranks descriptions; Neo4j answers structural code questions.

## Verification coverage

The implementation is verified with unit tests and a real Neo4j/zvec integration test covering:

- docstring keyword retrieval;
- normalized-name retrieval for undocumented callables;
- Markdown-enriched retrieval;
- SHA-256 zvec IDs and exact key joins;
- replacement cleanup and fresh read-only zvec queries;
- code-only result types.

Performance is measured separately using a fixed corpus and query set: scanner duration, callable
and index-record counts, index publication duration, zvec storage size, and lexical p50/p95 latency.
