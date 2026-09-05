# Frozen corpus evaluation

`codekg evaluate` measures the complete local CodeKG pipeline:

```text
source files → Python IR → Neo4j graph → isolated zvec-backed lexical description index → public lexical query
```

It never clones, downloads, or calls a network service beyond the locally
configured Neo4j instance. The evaluation corpus is selected by
[`corpora.json`](corpora.json); every selected source directory is pinned
before it is indexed.

Run it from the project root after Neo4j is available:

```bash
codekg evaluate
```

The command writes `evaluation/report.json` and uses one zvec-backed lexical
description index per corpus under `.codekg-evaluation-zvec`. This FTS-only
index contains no embeddings or vectors. Both locations can be changed with the
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

## Multi-repository Codex benchmark

This directory is the canonical package for the paired CodeKG-versus-native
intent benchmark. It is separate from `codekg evaluate`. The frozen suite has
ten tasks and runs each task once through each arm, for 20 measured Codex
trials:

| Index | CodeKG repository | Intent |
|---:|---|---|
| 01 | `requests` | prepare a request with session state |
| 02 | `requests` | merge environment and session settings |
| 03 | `click` | construct a command context |
| 04 | `click` | parse command arguments |
| 05 | `engine` | create a SQLAlchemy connection |
| 06 | `engine` | execute through a dialect context |
| 07 | `pool` | check out a pooled connection record |
| 08 | `pool` | acquire from a queue pool |
| 09 | `sql` | compile a select statement |
| 10 | `sql` | compose a select body |

The `engine`, `pool`, and `sql` indexes are separately content-pinned source
subtrees of one pinned SQLAlchemy checkout. `requests` and `click` are pinned
Git checkouts. Exact paths, identities, commits, prompts, and gold files are in
[`benchmark-manifest.json`](benchmark-manifest.json). Prompt and gold filenames
start with a three-digit task index.

The CodeKG arm can use only `search_symbols`, `get_definition`,
`find_callers`, and `find_callees`. The native arm has CodeKG disabled and uses
repository commands. Both arms hold the model (`gpt-5.4-mini`), reasoning
effort (`low`), output schema, rules, and sandbox constant. Multi-agent behavior
is disabled and every trial uses a new ephemeral Codex process.

Both arms return one explicit `primary` claim and zero or one directed
`related` claim. Each claim contains a qualified symbol, repository-relative
POSIX path, and complete definition range; a related claim also declares
`relationship` as `caller` or `callee`. This removes the former duplicate
symbol/file/evidence lists and makes the selected responsibility boundary
unambiguous.

The normal CodeKG path uses one search, one definition inspection, and the two
relationship calls. Bounded recovery may inspect one additional definition,
either from an earlier search result or after one additional search. The last
definition inspected is final, and caller/callee expansion is allowed only for
that final symbol. `search_symbols` no longer returns `recommended_symbol_id`
(removed per `codekg-ranking-presentation-spec.md` B4 -- no score-margin
threshold separated correct from wrong rank-1 recommendations), so the agent
must judge the strongest candidate itself. The limits are two searches, two
definition inspections, and one call to each relationship tool.

### Run the whole suite

Set the corpus root that contains `requests`, `click`, and `sqlalchemy`, and
choose a new output directory:

```bash
CORPUS_ROOT=/media/alex/MYSSD/BACKUP/workspace/codekg-corpus
RUN_DIR="$PWD/runs/codekg-native-intent-suite-$(date +%Y%m%d-%H%M%S)"
```

First run the free plan/preflight view. It verifies all pinned local sources,
the benchmark profile, and the 20-entry seeded schedule without launching
trials:

```bash
.venv/bin/python evaluation/run_benchmark.py plan \
  --corpus-root "$CORPUS_ROOT"
```

Then start the complete sequential suite:

```bash
.venv/bin/python evaluation/run_benchmark.py run \
  --execute \
  --corpus-root "$CORPUS_ROOT" \
  --output "$RUN_DIR"
```

The run performs a direct local MCP repository preflight and one Codex graph
preflight before the 20 measured trials, so it launches 21 Codex processes in
total. This can consume paid model usage.
Do not reuse an output directory: artifacts are immutable and the runner
refuses to overwrite them.

`--profile benchmark` loads `$CODEX_HOME/benchmark.config.toml`, or
`~/.codex/benchmark.config.toml` when `CODEX_HOME` is unset. If supplied,
`--profile-path` is an integrity check and must resolve to that same file.
The runner uses `--strict-config`, `--ignore-user-config`, `--ephemeral`, and
sequential execution. It rechecks all frozen package hashes, source identities,
Git commits, and clean working trees around every Codex process. Drift aborts
the run and leaves completed artifacts in place.

The seeded schedule pairs both arms for every task and balances ordering:
CodeKG runs first for five tasks and native first for five. Failed or invalid
trials are retained and never replaced.

### Indexed artifact layout

Every task has a two-digit output directory corresponding to its prompt index:

```text
$RUN_DIR/
  run-metadata.json
  tasks/
    01-requests-prepare-request/
      codekg/
      native/
    02-requests-environment-settings/
      codekg/
      native/
    ...
    10-sql-compose-select-body/
      codekg/
      native/
```

Each arm directory contains `answer.json`, `events.jsonl`, `stderr.log`,
`metadata.json`, `validation.json`, and `metrics.json`.

Validate one captured task arm:

```bash
.venv/bin/python evaluation/validate_benchmark.py \
  "$RUN_DIR/tasks/03-click-command-context/codekg" \
  --corpus-root "$CORPUS_ROOT"
```

Aggregate the complete suite:

```bash
.venv/bin/python evaluation/aggregate_benchmark.py \
  "$RUN_DIR" \
  --output "$RUN_DIR-summary.json"
```

Validation checks the JSONL/final-answer match, schema, repository identity,
paths, complete ranges, tool protocol, native command evidence, and CodeKG
provenance. Aggregation accepts only the exact frozen 20-entry schedule with
one unique thread per trial. The report includes intention-to-treat and
valid-only results, per-task outcomes, paired CodeKG-minus-native deltas,
latency summaries, retrieval measures, evidence compliance, and token usage.

Evaluator diagnostics separate schema validity, infrastructure success,
protocol compliance, provenance compliance, primary and related semantic
correctness, and exact-location correctness. A reported location is classified
as exact, containing the canonical definition, or invalid; strict pass still
requires exact locations. Unsupported claims, location errors, and protocol
errors are counted independently. MCP-only retrieval measures are not
applicable to native trials and are represented as JSON `null`, so aggregate
rates omit them rather than treating them as failed retrievals. Legacy
`valid`, `correct`, `evidence_compliant`, and `success` summaries remain
available for compatibility.

Normal tests never launch paid trials:

```bash
.venv/bin/python -m pytest -q evaluation/tests
```
