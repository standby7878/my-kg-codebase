#!/usr/bin/env bash
# Run the native Requests benchmark with verified shell-command evidence.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
codekg_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
requests_dir=${REQUESTS_DIR:-/media/alex/MYSSD/BACKUP/workspace/codekg-corpus/requests}
run_dir=${RUN_DIR:-"$codekg_root/runs/requests-intent-001"}

if [[ "$run_dir" != /* ]]; then
    run_dir="$codekg_root/$run_dir"
fi

if [[ ! -d "$requests_dir" ]]; then
    printf 'REQUESTS_DIR is not a directory: %s\n' "$requests_dir" >&2
    exit 2
fi
requests_dir=$(CDPATH= cd -- "$requests_dir" && pwd -P)
git_root=$(git -C "$requests_dir" rev-parse --show-toplevel 2>/dev/null) || {
    printf 'REQUESTS_DIR is not a Git checkout: %s\n' "$requests_dir" >&2
    exit 2
}
git_root=$(CDPATH= cd -- "$git_root" && pwd -P)
if [[ "$git_root" != "$requests_dir" ]]; then
    printf 'REQUESTS_DIR must be the Git root, not a subdirectory: %s\n' "$requests_dir" >&2
    exit 2
fi

prompt_file="$codekg_root/evaluation/prompts/requests-intent-001-native.txt"
schema_file="$codekg_root/evaluation/codex-answer.schema.json"
for required_file in "$prompt_file" "$schema_file"; do
    if [[ ! -f "$required_file" ]]; then
        printf 'Required benchmark file is missing: %s\n' "$required_file" >&2
        exit 2
    fi
done

preflight_events="$run_dir/native-preflight.jsonl"
preflight_stderr="$run_dir/native-preflight.stderr.log"
native_dir="$run_dir/native"
native_events="$native_dir/events.jsonl"
native_stderr="$native_dir/stderr.log"
native_answer="$native_dir/answer.json"

if [[ ${BENCHMARK_OVERWRITE:-0} != 1 ]]; then
    for result_file in "$preflight_events" "$preflight_stderr" "$native_events" "$native_stderr" "$native_answer"; do
        if [[ -e "$result_file" ]]; then
            printf 'Refusing to overwrite existing benchmark result: %s\n' "$result_file" >&2
            printf 'Set BENCHMARK_OVERWRITE=1 only for a deliberate replacement.\n' >&2
            exit 2
        fi
    done
fi

mkdir -p "$run_dir" "$native_dir"

printf '%s\n' 'WARNING: this host cannot use Codex read-only sandboxing because Bubblewrap loopback setup fails.' >&2
printf '%s\n' 'WARNING: this wrapper uses danger-full-access. Run only against a trusted, dedicated benchmark checkout.' >&2

validate_artifacts() {
    local events_file=$1
    local answer_file=$2
    local require_preflight_marker=$3
    local repository_root=$4

    python3 - "$events_file" "$answer_file" "$require_preflight_marker" "$repository_root" <<'PY'
import ast
import json
import sys
from pathlib import Path

events_path = Path(sys.argv[1])
answer_path = Path(sys.argv[2]) if sys.argv[2] else None
require_marker = sys.argv[3] == "1"
repository_root = Path(sys.argv[4]).resolve() if sys.argv[4] else None
issues = []
has_successful_command = False
has_marker = False

try:
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item", {})
            if (
                item.get("type") == "command_execution"
                and item.get("status") == "completed"
                and item.get("exit_code") == 0
            ):
                has_successful_command = True
            if item.get("type") == "agent_message" and "NATIVE_PREFLIGHT_OK" in item.get("text", ""):
                has_marker = True
except OSError as exc:
    issues.append(f"cannot read events JSONL {events_path}: {exc}")

if not has_successful_command:
    issues.append(f"missing completed successful command_execution in {events_path}")
if require_marker and not has_marker:
    issues.append(f"missing NATIVE_PREFLIGHT_OK agent message in {events_path}")

if answer_path is not None:
    try:
        with answer_path.open(encoding="utf-8") as stream:
            answer_document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"missing or invalid JSON answer file {answer_path}: {exc}")
    else:
        answer = answer_document.get("answer") if isinstance(answer_document, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            issues.append(f"missing or empty answer string in {answer_path}")
        elif answer.lstrip().startswith("INFRA_ERROR:"):
            issues.append(f"infra answer is invalid without a normal benchmark result in {answer_path}")
        evidence = answer_document.get("evidence") if isinstance(answer_document, dict) else None
        if not isinstance(evidence, list) or not evidence:
            issues.append(f"missing nonempty evidence list in {answer_path}")
        elif repository_root is None:
            issues.append(f"missing repository root for evidence validation of {answer_path}")
        else:
            evidence_symbols: set[str] = set()
            evidence_files: set[str] = set()
            for index, item in enumerate(evidence):
                prefix = f"evidence[{index}] in {answer_path}"
                if not isinstance(item, dict):
                    issues.append(f"{prefix} is not an object")
                    continue
                symbol = item.get("symbol")
                file_name = item.get("file")
                start_line = item.get("start_line")
                end_line = item.get("end_line")
                if not isinstance(symbol, str) or not symbol.strip():
                    issues.append(f"{prefix} has missing or invalid symbol")
                    continue
                if not isinstance(file_name, str) or not file_name.strip():
                    issues.append(f"{prefix} has missing or invalid file")
                    continue
                evidence_symbols.add(symbol)
                evidence_files.add(file_name)
                if type(start_line) is not int or type(end_line) is not int:
                    issues.append(f"{prefix} has non-integer start_line/end_line")
                    continue
                relative_path = Path(file_name)
                if relative_path.is_absolute():
                    issues.append(f"{prefix} uses an absolute file path: {file_name}")
                    continue
                source_path = (repository_root / relative_path).resolve()
                if not source_path.is_relative_to(repository_root):
                    issues.append(f"{prefix} escapes repository root: {file_name}")
                    continue
                if source_path.suffix != ".py":
                    issues.append(f"{prefix} is not a Python file: {file_name}")
                    continue
                try:
                    source_lines = source_path.read_text(encoding="utf-8").splitlines()
                    tree = ast.parse("\n".join(source_lines), filename=str(source_path))
                except (OSError, SyntaxError) as exc:
                    issues.append(f"{prefix} cannot parse Python file {source_path}: {exc}")
                    continue

                definitions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

                def collect(nodes: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
                    for node in nodes:
                        if isinstance(node, ast.ClassDef):
                            collect(node.body, (*parents, node.name))
                        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                            definitions.append((".".join((*parents, node.name)), node))
                            collect(node.body, (*parents, node.name))

                collect(tree.body)
                symbol_parts = symbol.strip().split(".")
                exact_suffix = ".".join(symbol_parts[-2:]) if len(symbol_parts) > 1 else symbol_parts[-1]
                candidates = [
                    node
                    for qualified_name, node in definitions
                    if qualified_name == exact_suffix
                    or (len(symbol_parts) > 1 and qualified_name.endswith(f".{exact_suffix}"))
                    or (
                        len(symbol_parts) == 1
                        and qualified_name.rsplit(".", maxsplit=1)[-1] == symbol_parts[0]
                    )
                ]
                if not candidates:
                    issues.append(f"{prefix} cannot resolve symbol {symbol!r} in {file_name}")
                    continue
                if len(candidates) != 1:
                    issues.append(f"{prefix} resolves symbol {symbol!r} ambiguously in {file_name}")
                    continue
                node = candidates[0]
                decorator_lines = [decorator.lineno for decorator in node.decorator_list]
                accepted_starts = {node.lineno}
                if decorator_lines:
                    accepted_starts.add(min(decorator_lines))
                last_complete_line = node.end_lineno
                while last_complete_line < len(source_lines) and not source_lines[last_complete_line].strip():
                    last_complete_line += 1
                accepted_ranges = [
                    f"{accepted_start}-{accepted_end}"
                    for accepted_start in sorted(accepted_starts)
                    for accepted_end in range(node.end_lineno, last_complete_line + 1)
                ]
                if start_line not in accepted_starts or not node.end_lineno <= end_line <= last_complete_line:
                    issues.append(
                        f"{prefix} has approximate or incomplete range {start_line}-{end_line}; "
                        f"accepted complete range(s): {', '.join(accepted_ranges)}"
                    )
            symbols = answer_document.get("symbols") if isinstance(answer_document, dict) else None
            files = answer_document.get("files") if isinstance(answer_document, dict) else None
            if not isinstance(symbols, list) or not all(
                isinstance(symbol, str) and symbol.strip() for symbol in symbols
            ):
                issues.append(f"missing or invalid symbols list in {answer_path}")
            else:
                for symbol in symbols:
                    if symbol not in evidence_symbols:
                        issues.append(
                            f"symbol {symbol!r} in {answer_path} is missing from evidence symbols"
                        )
            if not isinstance(files, list) or not all(
                isinstance(file_name, str) and file_name.strip() for file_name in files
            ):
                issues.append(f"missing or invalid files list in {answer_path}")
            else:
                for file_name in files:
                    if file_name not in evidence_files:
                        issues.append(
                            f"file {file_name!r} in {answer_path} is missing from evidence files"
                        )

if issues:
    print("Native benchmark artifact validation failed:", file=sys.stderr)
    for issue in issues:
        print(f"- {issue}", file=sys.stderr)
    sys.exit(1)
PY
}

preflight_prompt='Your first action must be a shell command. Run pwd, git rev-parse --show-toplevel, git rev-parse --short=12 HEAD, and rg -n "def prepare_request|class Session" src/requests. Return NATIVE_PREFLIGHT_OK only after a shell command completes successfully. Return NATIVE_PREFLIGHT_FAILED only after an actual shell-tool error.'

if ! printf '%s\n' "$preflight_prompt" | codex --ask-for-approval never exec \
    --profile benchmark \
    --ephemeral \
    --json \
    --sandbox danger-full-access \
    --ignore-rules \
    --cd "$requests_dir" \
    -c 'features.multi_agent=false' \
    -c 'mcp_servers.codekg.enabled=false' \
    - >"$preflight_events" 2>"$preflight_stderr"; then
    printf 'Native preflight Codex invocation failed; see %s\n' "$preflight_stderr" >&2
    exit 1
fi

if ! validate_artifacts "$preflight_events" "" 1 ""; then
    printf 'Native preflight validation failed; refusing full run. See %s\n' "$preflight_events" >&2
    exit 1
fi

if ! codex --ask-for-approval never exec \
    --profile benchmark \
    --ephemeral \
    --json \
    --sandbox danger-full-access \
    --ignore-rules \
    --cd "$requests_dir" \
    -c 'features.multi_agent=false' \
    -c 'mcp_servers.codekg.enabled=false' \
    --output-schema "$schema_file" \
    -o "$native_answer" \
    - <"$prompt_file" >"$native_events" 2>"$native_stderr"; then
    printf 'Native benchmark Codex invocation failed; preserved events/stderr under %s\n' "$native_dir" >&2
    exit 1
fi

if ! validate_artifacts "$native_events" "$native_answer" 0 "$requests_dir"; then
    printf 'Native full-run validation failed; preserved artifacts under %s\n' "$native_dir" >&2
    exit 1
fi

printf 'Native benchmark completed: %s\n' "$native_dir" >&2
