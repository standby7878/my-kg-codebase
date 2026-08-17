#!/usr/bin/env bash
# Run repeatable bulk-ingestion stress measurements without changing Docker volumes directly.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
PYTHON="${PYTHON:-python3}"

PRESET="medium"
WARMUPS=1
RUNS=3
RESULTS_ROOT="$REPO_ROOT/.stress-results"
CORPUS=""
RUN_DIR=""
BUILD=false
ALLOW_MEMORY_HEAVY_VERIFICATION=false
DRY_RUN=false

usage() {
    cat <<'EOF'
Usage: tools/stress_corpus/run-ingestion-stress.sh [options]

Generate (when absent), verify, and bulk-index a deterministic stress corpus.

Options:
  --preset NAME                     Corpus preset (default: medium)
  --warmups COUNT                   Unmeasured warm-up runs (default: 1)
  --runs COUNT                      Measured runs (default: 3)
  --results-root PATH               Parent for result directories (default: .stress-results)
  --corpus PATH                     Corpus path (default: .stress-corpora/<preset>)
  --run-dir PATH                    New result directory; must not already exist
  --build                           Build Compose images before running
  --allow-memory-heavy-verification Run the scanner oracle for large/huge corpora
  --dry-run                         Print the plan without generating, verifying, or using Docker
  -h, --help                        Show this help
EOF
}

die() { echo "error: $*" >&2; exit 2; }
is_count() { [[ "$1" =~ ^[0-9]+$ ]]; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --preset|--warmups|--runs|--results-root|--corpus|--run-dir)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            case "$1" in
                --preset) PRESET="$2" ;;
                --warmups) WARMUPS="$2" ;;
                --runs) RUNS="$2" ;;
                --results-root) RESULTS_ROOT="$2" ;;
                --corpus) CORPUS="$2" ;;
                --run-dir) RUN_DIR="$2" ;;
            esac
            shift 2
            ;;
        --build) BUILD=true; shift ;;
        --allow-memory-heavy-verification) ALLOW_MEMORY_HEAVY_VERIFICATION=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

is_count "$WARMUPS" || die "--warmups must be a non-negative integer"
is_count "$RUNS" || die "--runs must be a non-negative integer"
(( RUNS > 0 )) || die "--runs must be greater than zero"

absolute_path() {
    local path="$1" parent base
    [[ "$path" = /* ]] || path="$REPO_ROOT/$path"
    parent="$(dirname -- "$path")"
    base="$(basename -- "$path")"
    if [[ -d "$parent" ]]; then
        parent="$(cd -- "$parent" && pwd -P)"
    fi
    printf '%s/%s' "$parent" "$base"
}

RESULTS_ROOT="$(absolute_path "$RESULTS_ROOT")"
if [[ -z "$CORPUS" ]]; then
    CORPUS="$REPO_ROOT/.stress-corpora/$PRESET"
fi
CORPUS="$(absolute_path "$CORPUS")"

if [[ -n "$RUN_DIR" ]]; then
    RUN_DIR="$(absolute_path "$RUN_DIR")"
else
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    RUN_DIR="$RESULTS_ROOT/${PRESET}-${timestamp}-$$"
    suffix=1
    while [[ -e "$RUN_DIR" || -L "$RUN_DIR" ]]; do
        RUN_DIR="$RESULTS_ROOT/${PRESET}-${timestamp}-$$-$suffix"
        ((suffix++))
    done
fi
[[ ! -e "$RUN_DIR" && ! -L "$RUN_DIR" ]] || die "--run-dir already exists or is a symlink: $RUN_DIR"

if "$DRY_RUN"; then
    cat <<EOF
Dry-run plan
  repository: $REPO_ROOT
  corpus: $CORPUS
  preset: $PRESET
  result directory: $RUN_DIR
  warmups: $WARMUPS
  measured runs: $RUNS
  build first: $BUILD
  command: bash run-compose.sh dev-local index-sources --mode bulk
EOF
    exit 0
fi

mkdir -p -- "$(dirname -- "$RUN_DIR")"
mkdir -- "$RUN_DIR"
mkdir -- "$RUN_DIR/runs"

write_context() {
    "$PYTHON" - "$RUN_DIR/context.json" "$REPO_ROOT" "$CORPUS" "$PRESET" <<'PY'
import json
import os
import platform
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

output, root, corpus = map(Path, sys.argv[1:4])
preset = sys.argv[4]
def git(*args):
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
memory_kib = None
try:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            memory_kib = int(line.split()[1])
            break
except OSError:
    pass
def command(*args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
def free_space(path):
    try:
        stat = os.statvfs(path)
        return {"path": str(path), "free_bytes": stat.f_bavail * stat.f_frsize,
                "total_bytes": stat.f_blocks * stat.f_frsize}
    except OSError:
        return None
manifest = corpus / "codekg-stress-manifest.json"
json.dump({
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "corpus": str(corpus), "preset_requested": str(preset),
    "repository_revision": git("rev-parse", "HEAD"),
    "repository_dirty": bool(git("status", "--porcelain")),
    "corpus_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "docker_version": command("docker", "version", "--format", "{{.Server.Version}}"),
    "compose_version": command("docker", "compose", "version", "--short"),
    "local_image_id": command("docker", "image", "inspect", "--format", "{{.Id}}", "codekg-app:local"),
    "host": {"system": platform.system(), "release": platform.release(),
             "machine": platform.machine(), "cpu_count": os.cpu_count(),
             "memory_kib": memory_kib},
    "storage": [free_space(corpus), free_space(output.parent)],
}, output.open("w", encoding="utf-8"), indent=2, sort_keys=True)
PY
}
if [[ ! -d "$CORPUS" ]]; then
    mkdir -p -- "$(dirname -- "$CORPUS")"
    "$PYTHON" "$SCRIPT_DIR/generate.py" --preset "$PRESET" --output "$CORPUS" \
        --output-root "$(dirname -- "$CORPUS")" >"$RUN_DIR/generation.stdout" 2>"$RUN_DIR/generation.stderr"
fi

verify_args=("$PYTHON" "$SCRIPT_DIR/verify.py" --corpus "$CORPUS")
if "$ALLOW_MEMORY_HEAVY_VERIFICATION"; then
    verify_args+=(--allow-memory-heavy-verification)
fi
"${verify_args[@]}" >"$RUN_DIR/verification.json" 2>"$RUN_DIR/verification.stderr"
cp -- "$CORPUS/codekg-stress-manifest.json" "$RUN_DIR/corpus-manifest.json"

if ! {
    docker version --format '{{.Server.Version}}'
    docker compose version --short
    if ! "$BUILD"; then
        docker image inspect --format '{{.Id}}' codekg-app:local
    fi
} >"$RUN_DIR/preflight.log" 2>&1; then
    die "Docker preflight failed; details retained in $RUN_DIR/preflight.log"
fi
write_context

run_one() {
    local kind="$1" number="$2" destination started ended exit_code
    destination="$RUN_DIR/runs/${kind}-$(printf '%03d' "$number")"
    mkdir -- "$destination"
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    start_seconds="$(date +%s)"
    set +e
    (cd -- "$REPO_ROOT" && CODEKG_REPOS_ROOT="$CORPUS" bash run-compose.sh dev-local index-sources --mode bulk) \
        >"$destination/stdout.log" 2>"$destination/stderr.log"
    exit_code=$?
    set -e
    end_seconds="$(date +%s)"
    ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$PYTHON" - "$destination/status.json" "$kind" "$number" "$started" "$ended" "$exit_code" \
        "$((end_seconds - start_seconds))" "$destination/stdout.log" <<'PY'
import datetime as dt
import json
import re
import sys
path, kind, number, started, ended, exit_code, elapsed, stdout = sys.argv[1:]
metrics = []
phases = {}
starts = {}
for line in open(stdout, encoding="utf-8", errors="replace"):
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict) and any(key in value for key in ("elapsed_seconds", "peak_rss_kib")):
        metrics.append(value)
    metric = re.search(r"['\"]?(scan_seconds|export_seconds|zvec_seconds|elapsed_seconds|peak_rss_kib)['\"]?\s*[:=]\s*([0-9.]+)", line)
    if metric:
        name, number_value = metric.groups()
        metrics.append({"metric": name, "value": float(number_value)})
    match = re.match(r"CODEKG_PHASE_(START|END) (\w+) (\S+)$", line.strip())
    if match:
        marker, phase, timestamp = match.groups()
        if marker == "START":
            starts[phase] = timestamp
        elif phase in starts:
            try:
                duration = (dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")) -
                            dt.datetime.fromisoformat(starts[phase].replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                duration = None
            phases[phase] = {"started_at_utc": starts[phase], "ended_at_utc": timestamp,
                             "elapsed_seconds": duration}
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"kind": kind, "number": int(number), "started_at_utc": started,
               "ended_at_utc": ended, "exit_code": int(exit_code),
               "elapsed_seconds": int(elapsed), "success": int(exit_code) == 0,
               "cli_metrics": metrics, "phases": phases}, handle,
              indent=2, sort_keys=True)
    handle.write("\n")
PY
    if [[ "$exit_code" -eq 0 ]]; then
        local pointer="${CODEKG_RUNTIME_ENV_FILE:-$REPO_ROOT/compose/dev-local/runtime.env}" staging_volume generation
        if [[ -f "$pointer" ]]; then
            awk '/^CODEKG_(NEO4J_DATA|ZVEC_DATA|NEO4J_LOGS)_VOLUME=/' "$pointer" \
                >"$destination/runtime-pointer.env"
            generation="$(sed -n 's/^CODEKG_NEO4J_DATA_VOLUME=codekg-dev-local_neo4j_data_//p' \
                "$destination/runtime-pointer.env")"
            if [[ -n "$generation" ]]; then
                staging_volume="codekg-dev-local_bulk_staging_${generation}"
                docker run --rm -v "$staging_volume:/csv:ro" --entrypoint cat codekg-app:local \
                    /csv/manifest.json >"$destination/bulk-manifest.json" \
                    2>"$destination/bulk-manifest.stderr" || true
            fi
        fi
    fi
    return "$exit_code"
}

if "$BUILD"; then
    set +e
    (cd -- "$REPO_ROOT" && bash run-compose.sh dev-local build) >"$RUN_DIR/build.stdout" 2>"$RUN_DIR/build.stderr"
    build_status=$?
    set -e
    [[ "$build_status" -eq 0 ]] || die "build failed; logs retained in $RUN_DIR"
    write_context
fi

for ((index = 1; index <= WARMUPS; index++)); do
    run_one warmup "$index" || true
done
for ((index = 1; index <= RUNS; index++)); do
    run_one measured "$index" || true
done

if ! "$PYTHON" - "$RUN_DIR" <<'PY'
import json
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
statuses = [json.loads(path.read_text()) for path in sorted((root / "runs").glob("*/status.json"))]
measured = [item for item in statuses if item["kind"] == "measured"]
successful = [item["elapsed_seconds"] for item in measured if item["success"]]
phase_values = {}
for item in measured:
    if item["success"]:
        for name, phase in item.get("phases", {}).items():
            if phase["elapsed_seconds"] is not None:
                phase_values.setdefault(name, []).append(phase["elapsed_seconds"])
summary = {"measured_runs": len(measured), "successful_measured_runs": len(successful),
           "failed_measured_runs": len(measured) - len(successful),
           "median_successful_elapsed_seconds": statistics.median(successful) if successful else None,
           "median_successful_phase_elapsed_seconds": {
               name: statistics.median(values) for name, values in sorted(phase_values.items())},
           "runs": statuses}
(root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
lines = ["# Ingestion stress summary", "", f"- Measured runs: {summary['measured_runs']}",
         f"- Successful measured runs: {summary['successful_measured_runs']}",
         f"- Failed measured runs: {summary['failed_measured_runs']}",
         f"- Median successful measured elapsed seconds: {summary['median_successful_elapsed_seconds']}", "",
         "Warm-ups and failed measured runs are excluded from the median."]
if summary["median_successful_phase_elapsed_seconds"]:
    lines.extend(["", "## Successful measured phase medians (seconds)", ""])
    lines.extend(f"- {name}: {value}" for name, value in summary["median_successful_phase_elapsed_seconds"].items())
(root / "summary.md").write_text("\n".join(lines) + "\n")
raise SystemExit(1 if any(not item["success"] for item in statuses) else 0)
PY
then
    echo "Stress run completed with failures; results retained in $RUN_DIR" >&2
    exit 1
fi

echo "Stress results recorded in $RUN_DIR"
