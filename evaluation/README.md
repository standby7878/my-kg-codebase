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

## Requests Codex benchmark

This directory is also the sole canonical package for the paired Requests
agent benchmark. The benchmark is intentionally separate from `codekg
evaluate`: it compares a four-tool CodeKG investigation with a native
repository-search investigation while holding the model, reasoning effort,
output schema, sandbox, rules, and run order constant.

The frozen benchmark inputs are:

* [`benchmark-manifest.json`](benchmark-manifest.json), which pins the Requests
  commit, model (`gpt-5.4-mini`), reasoning effort (`low`), seed, trial counts,
  arms, and package paths;
* [`gold/requests-intent-001.gold.json`](gold/requests-intent-001.gold.json),
  which records exact definition ranges for the primary symbol, caller, and
  delegated helper at that commit;
* [`codex-answer.schema.json`](codex-answer.schema.json), which requires
  positive non-null line numbers and repository-relative POSIX paths;
* the CodeKG and native prompts under [`prompts/`](prompts/).

The measured CodeKG task can use only `search_symbols`, `get_definition`,
`find_callers`, and `find_callees`. Repository/index discovery is a batch
preflight operation and is not part of a measured task. The CodeKG prompt
allows no shell or web use, caps search at two calls, and limits conclusions to
structural evidence available from definition and relationship metadata.

### Plan and preflight

The runner defaults to a free plan operation. It verifies the frozen Requests
checkout and package files, freezes their SHA-256 hashes, and prints the seeded
schedule without launching a task. It also records `codex --version` and
validates the readable model, reasoning, web, multi-agent, and CodeKG tool
contract from the benchmark profile:

```bash
python3 evaluation/run_benchmark.py plan \
  --requests /media/alex/MYSSD/BACKUP/workspace/codekg-corpus/requests
```

`--profile benchmark` always loads
`$CODEX_HOME/benchmark.config.toml` (or `~/.codex/benchmark.config.toml` when
`CODEX_HOME` is unset). Consequently, `--profile-path` is an integrity-check
argument, not an alternate profile loader: when supplied, it must resolve to
that exact path. The frozen runner likewise rejects a manifest whose `profile`
field is anything other than `benchmark`.

A real batch run requires the explicit `--execute` acknowledgement:

```bash
python3 evaluation/run_benchmark.py run \
  --execute \
  --requests /media/alex/MYSSD/BACKUP/workspace/codekg-corpus/requests \
  --output runs/requests-20260726
```

Do not reuse an output directory. The runner refuses to overwrite it or any
trial artifact. It uses the `benchmark` profile, `--strict-config`,
`--ignore-user-config`, `--ephemeral`, sequential execution, disabled
multi-agent behavior, and a fresh `codex exec` process for every trial. The
profile remains explicitly selected, hashed, and contract-validated; ignoring
the unrelated base user config prevents other MCP servers and settings from
entering the benchmark. Batch preflight temporarily exposes
only `list_repositories` for repository/commit discovery, then uses the four
measured tools for a graph smoke test. The smoke test requires the exact
`Session.prepare_request` definition plus its `Session.request` caller and
`PreparedRequest.prepare` callee, including complete locations. Measured
CodeKG trials expose exactly those same four tools, while native trials set the
CodeKG server to both disabled and not required.

Immediately before and after each repository preflight, graph preflight,
warm-up, and measured trial, the runner rechecks every frozen input hash plus
the Requests Git root, pinned commit, and clean working tree. Any drift aborts
the batch before another Codex process starts. This is especially important for
the native arm's `danger-full-access` execution: a repository write leaves the
partial immutable artifacts in place for diagnosis but cannot contaminate a
later trial.

The schedule contains one excluded warm-up per arm followed by ten paired,
measured repetitions per arm. Exactly half of the measured pairs run CodeKG
first and half native first; the manifest seed deterministically randomizes
which repetitions use each ordering. Failed or invalid trials remain in place
and are never replaced.

The repository checkout must be trusted: both arms use
`danger-full-access` because the benchmark host may not support the read-only
Bubblewrap network namespace. The prompts prohibit writes, but that instruction
is not a security boundary.

### Artifacts, validation, and aggregation

Each trial directory contains:

```text
answer.json
events.jsonl
stderr.log
metadata.json
validation.json
metrics.json
```

Validation checks the JSONL/final-answer match, schema rules, exact Git-relative
paths, complete ranges, required tool order and arguments, native command
evidence, and CodeKG provenance. Every CodeKG symbol, file, and range in the
answer must occur in a successful structured MCP result. The two relationship
calls must both succeed for the selected symbol, with at least one nonempty
relationship result. Every trial must have one nonempty thread ID, and
aggregation rejects a reused thread ID. Aggregation also requires the exact
seeded `(ordinal, arm, repetition, warmup)` schedule rather than accepting a
batch merely because it has the expected total number of results.

To validate an independently captured trial:

```bash
python3 evaluation/validate_benchmark.py \
  runs/requests-20260726/trials/02-rep-01-codekg \
  --requests /media/alex/MYSSD/BACKUP/workspace/codekg-corpus/requests
```

To aggregate a completed immutable batch:

```bash
python3 evaluation/aggregate_benchmark.py \
  runs/requests-20260726 \
  --output runs/requests-20260726-summary.json
```

The report separates infrastructure status, retrieval, and agent outcomes. It
includes raw samples, medians, nearest-rank p95, bootstrap 95% confidence
intervals for medians, paired CodeKG-minus-native deltas, success rate, and
evidence-compliance rate. It reports both intention-to-treat and valid-only
views. Token metrics distinguish total, cached, and uncached input plus output,
reasoning, and cache-hit ratio. MCP response bytes are measured from emitted
results. Unsupported-claim count is the number of reported symbols, files, or
ranges that lack tool provenance; it does not pretend to semantically grade
free-form behavioral prose. MCP latency is deliberately omitted because current
JSONL tool events do not provide reliable timestamps; it must only be added
from correlated server telemetry.

No benchmark command in this package launches paid trials during normal tests.
Focused tests use fixtures and a fake Codex process:

```bash
.venv/bin/python -m pytest -q evaluation/tests
```
