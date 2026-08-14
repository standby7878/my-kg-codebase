# Python ingestion stress corpus

This directory contains the tooling contract for the locally generated Python
corpus used to stress CodeKG ingestion. It exists to measure ingestion
behaviour only; it is not a source of production code, search-quality
evaluation data, or a replacement for synchronized real-repository corpora.

Generated corpora belong under `.stress-corpora/`, which is deliberately
ignored by Git. Commit the generator, verifier, presets, and documentation;
never commit generated corpus trees or bulk-import output.

## Generate a corpus

The generator is stdlib-only. From the repository root, use its stable command
shape:

```bash
python tools/stress_corpus/generate.py --preset small --output .stress-corpora/small
```

Replace `small` with the desired named preset, and choose an output directory
under `.stress-corpora/`. Presets are intended to provide progressively larger
and repeatable corpus shapes. The `huge` preset is for local benchmark hosts,
not CI.

Before generating a large corpus, first use the generator's dry-run support
when available and confirm that the target filesystem has sufficient free disk
space for the source tree, generated artifacts, and bulk-ingestion output.
Large generated Python files and later CSV output can be substantially larger
than an initial file-count estimate suggests.

For safety, output must be a child of the repository's `.stress-corpora/`
directory. The generator only overwrites an intact corpus it generated itself.
For an intentional alternate storage location, pass that directory explicitly
as `--output-root` and keep `--output` beneath it.

## Verify a generated corpus

Use the verifier against the generated corpus before treating a run as a
benchmark input:

```bash
python tools/stress_corpus/verify.py --corpus .stress-corpora/small
```

Generation produces a corpus manifest. It records the information needed to
identify and reproduce the corpus configuration, such as the selected preset,
generator version, seed/configuration, and content identity. Treat the
manifest as the identity of a benchmark input; retain a copy with benchmark
results. The verifier is the authority for checking a generated corpus against
that manifest.

## Preset use

Use small and medium presets for local correctness and regression checks. Use
large for developer performance checks when resources allow. Use huge only for
intentional stress testing on a provisioned Linux host. CI should remain on
small or medium corpora.

The stress corpus complements, rather than replaces, synchronized real
repositories: use synchronized data for semantic correctness and the generated
corpus for controlled scaling measurements.

## Benchmark protocol

Run the same corpus manifest, checkout, container/image versions, storage
location, and host for baseline and candidate measurements. Perform one
warm-up, then at least three measured runs, and compare median wall-clock time.
The acceptance condition for ingestion changes is lower wall-clock time with no
peak-RSS growth relative to the baseline.

Record at least:

- corpus manifest identity and preset;
- CodeKG revision and environment/image versions;
- host CPU, RAM, and storage details;
- wall-clock time and peak RSS for each ingestion phase;
- exported CSV size plus node and relationship counts; and
- callable/Zvec document count when that stage is included.

The current bulk workflow is:

```bash
bash run-compose.sh dev-local index-sources --mode bulk
```

It currently invokes separate bulk export, Zvec indexing, and validation
stages. Record the phase timings separately. The future `build-snapshot`
workflow is the planned hook for a single-scan, disk-spooled pipeline; do not
assume it exists until it is implemented.

## Recorded stress runs

Use the runner to generate a missing corpus, verify its manifest before every
test, and record repeated **bulk-only** ingestion measurements:

```bash
bash tools/stress_corpus/run-ingestion-stress.sh --preset medium
```

It performs one warm-up and three measured runs by default. Results are placed
in the ignored `.stress-results/` directory. Each run retains stdout, stderr,
exit status, elapsed wall-clock seconds, parsed CLI metrics when emitted, phase
timings, and the published runtime-volume pointer plus bulk CSV manifest when
available. `summary.json` and `summary.md` report medians of successful
measured runs only. Failed runs are retained and excluded from those medians.

The runner requires Docker plus the Docker Compose plugin and a working local
CodeKG image. Pass `--build` to build before measuring. Use `--dry-run` to
inspect the resolved paths and exact command without generating a corpus or
starting Docker. `--run-dir` must name a new directory, so a prior result can
never be overwritten.

The runner preflights the Docker daemon, Compose v2, and (unless `--build` is
used) the local image before any ingestion run; its output is retained in
`preflight.log`. `index-sources --mode bulk` publishes a new persistent Compose
generation and updates the Compose runtime pointer on every successful run.
The runner does not hand-edit that pointer or delete volumes; inspect and clean
old generations manually only after confirming they are not active.
