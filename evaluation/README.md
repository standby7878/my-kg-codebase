# Frozen corpus evaluation

`codekg evaluate` measures the complete local CodeKG pipeline:

```text
source files → Python IR → Neo4j graph → isolated zvec descriptions → public lexical query
```

It never clones, downloads, or calls a network service beyond the locally
configured Neo4j instance. The evaluation corpus is selected by
[`corpora.json`](corpora.json); every selected source directory is pinned
before it is indexed.

Run it from the project root after Neo4j is available:

```bash
codekg evaluate
```

The command writes `evaluation/report.json` and uses one zvec collection per
corpus under `.codekg-evaluation-zvec`. Both locations can be changed with the
`--output` and `--zvec-root` options. The JSON writer sorts keys and omits a
wall-clock timestamp, making structural portions of reports diff-friendly;
timing values naturally vary by host.

## Corpus contract

Two corpora are required:

* `synthetic-phase1` is the checked-in language fixture corpus. Its full
  source-content SHA-256 protects the exact expected call-site statuses,
  targets, and derived graph projections in `truth/synthetic-phase1.json`.
* `codekg-dogfood` is this checkout under a `working_tree: report` policy. A
  development checkout is often dirty, so its report records the observed Git
  revision, dirty flag, and full source-content SHA-256 rather than falsely
  claiming that its source matches HEAD. It gives the project a permanent
  self-regression corpus without network or licensing concerns.

An external corpus is optional. Set both of these variables to include one:

```bash
export CODEKG_EVAL_EXTERNAL_PATH=/local/path/to/pinned/repository
export CODEKG_EVAL_EXTERNAL_COMMIT=$(git -C "$CODEKG_EVAL_EXTERNAL_PATH" rev-parse HEAD)
codekg evaluate
```

If its path is unset, the report records a skip. By default a path with no
matching commit environment value runs as `optional_unpinned` and records its
observed revision/content identity. Add `--require-pins` to reject that case
before indexing. A supplied commit pin must match exactly. To add stable named
external corpora, add a new manifest entry with a fixed `git_commit` rather
than replacing the optional example.

## What the report checks

For every selected corpus the report includes:

* source/IR versus Neo4j counts for files, parse errors and diagnostics,
  module initializers, types, callables, and call sites; all-corpus
  `HAS_METHOD`, inheritance-clause/`INHERITS`, call-site status, and resolved
  projection counts are reported separately;
* scanner and index timing plus lexical-query p50/p95 timing. Every annotated
  lexical query is issued exactly five times, and the report includes the five
  samples and aggregate sample count;
* public lexical-search Top-1, Recall@5, MRR, and zero-result rate;
* for the synthetic corpus, the exact complete `HAS_METHOD` and internal
  `INHERITS` edge sets, total call-site status distribution, projection
  completeness, and every `CallSite` keyed by owner qualified name, source
  line/column, ordinal, and raw callee. Missing, extra, swapped-target, or
  changed-status call sites fail the gate.

Before opening Neo4j or creating an evaluation zvec directory, the evaluator
resolves every selected corpus, validates its pin policy, scans it, loads its
truth file, and rejects duplicate repository names. Therefore a bad later
corpus cannot partially index an earlier one.

The synthetic gate deliberately covers module execution, two calls on one
line, direct local calls, `cls`, `self`, `super`, dynamic receivers, nested
scopes, and definition-time decorator/default/annotation calls. It is a
correctness gate, not an attempt to claim semantic coverage of framework
registration or reflection.
