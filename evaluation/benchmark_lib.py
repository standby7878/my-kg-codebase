"""Shared, dependency-free helpers for the Requests Codex benchmark."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

EVALUATION_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EVALUATION_DIR / "benchmark-manifest.json"
ALLOWED_CODEKG_TOOLS = {
    "search_symbols",
    "get_definition",
    "find_callers",
    "find_callees",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_new(path: Path, value: object) -> None:
    """Create a JSON artifact without replacing an existing result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_text_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as handle:
        handle.write(value)


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError(f"unsupported benchmark manifest: {path}")
    return value


def resolve_manifest_file(manifest_path: Path, relative: str) -> Path:
    path = (manifest_path.resolve().parent / relative).resolve()
    path.relative_to(manifest_path.resolve().parent)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_hashes(manifest_path: Path, profile_path: Path) -> dict[str, str]:
    manifest = load_manifest(manifest_path)
    relative_files = [
        manifest["schema_file"],
        manifest["gold_file"],
        *manifest["prompt_files"].values(),
    ]
    files = [
        manifest_path,
        *(resolve_manifest_file(manifest_path, item) for item in relative_files),
    ]
    files.extend(
        [
            EVALUATION_DIR / "benchmark_lib.py",
            EVALUATION_DIR / "run_benchmark.py",
            EVALUATION_DIR / "validate_benchmark.py",
            EVALUATION_DIR / "aggregate_benchmark.py",
            profile_path,
        ]
    )
    return {str(path.resolve()): sha256_file(path) for path in files}


@dataclass(frozen=True)
class ScheduleEntry:
    ordinal: int
    arm: str
    repetition: int
    warmup: bool

    @property
    def trial_name(self) -> str:
        if self.warmup:
            return f"warmup-{self.arm}"
        return f"rep-{self.repetition:02d}-{self.arm}"


def balanced_schedule(*, arms: Sequence[str], repetitions: int, seed: int) -> list[ScheduleEntry]:
    if list(arms) != ["codekg", "native"]:
        raise ValueError("the Requests benchmark requires codekg and native arms")
    if repetitions <= 0 or repetitions % 2:
        raise ValueError("measured repetitions must be a positive even integer")

    randomizer = random.Random(seed)
    warmup_order = list(arms)
    randomizer.shuffle(warmup_order)
    first_arms = ["codekg"] * (repetitions // 2) + ["native"] * (repetitions // 2)
    randomizer.shuffle(first_arms)
    entries: list[ScheduleEntry] = []
    for arm in warmup_order:
        entries.append(ScheduleEntry(len(entries), arm, 0, True))
    for repetition, first in enumerate(first_arms, start=1):
        second = "native" if first == "codekg" else "codekg"
        for arm in (first, second):
            entries.append(ScheduleEntry(len(entries), arm, repetition, False))
    return entries


def read_events(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"events.jsonl line {line_number}: {exc.msg}")
            continue
        if not isinstance(value, dict):
            errors.append(f"events.jsonl line {line_number}: event is not an object")
            continue
        events.append(value)
    return events, errors


def completed_items(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for event in events:
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            result.append(item)
    return result


def successful_mcp_items(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in completed_items(events)
        if item.get("type") == "mcp_tool_call"
        and item.get("status") == "completed"
        and not item.get("error")
        and isinstance(item.get("result"), dict)
    ]


def structured_content(item: Mapping[str, Any]) -> Any:
    result = item.get("result")
    if not isinstance(result, Mapping):
        return None
    return result.get("structured_content", result.get("structuredContent"))


def result_rows(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    structured = structured_content(item)
    if isinstance(structured, list):
        return [row for row in structured if isinstance(row, dict)]
    if not isinstance(structured, Mapping):
        return []
    for key in ("results", "result"):
        rows = structured.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            return [rows]
    return []


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _all_strings(value: Any) -> set[str]:
    return {item for item in _walk(value) if isinstance(item, str)}


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    return all(part not in {"", ".", ".."} for part in path.parts)


def validate_answer_schema(answer: object) -> list[str]:
    if not isinstance(answer, dict):
        return ["answer is not a JSON object"]
    errors: list[str] = []
    required = {"task_id", "answer", "symbols", "files", "evidence", "confidence"}
    unknown = set(answer) - required
    missing = required - set(answer)
    if missing:
        errors.append(f"answer missing fields: {sorted(missing)}")
    if unknown:
        errors.append(f"answer has unknown fields: {sorted(unknown)}")
    if not isinstance(answer.get("task_id"), str):
        errors.append("task_id must be a string")
    if not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
        errors.append("answer must be a nonempty string")
    symbols = answer.get("symbols")
    if not isinstance(symbols, list) or any(not isinstance(item, str) for item in symbols):
        errors.append("symbols must be an array of strings")
    elif len(symbols) < 2 or len(set(symbols)) != len(symbols):
        errors.append("symbols must contain at least two unique entries")
    files = answer.get("files")
    if not isinstance(files, list):
        errors.append("files must be an array")
    else:
        if not files:
            errors.append("files must contain at least one entry")
        elif all(isinstance(item, str) for item in files) and len(set(files)) != len(files):
            errors.append("files entries must be unique")
        for path in files:
            if not _valid_relative_path(path):
                errors.append(f"invalid repository-relative POSIX path: {path!r}")
    evidence = answer.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be an array")
    else:
        if len(evidence) < 2:
            errors.append("evidence must contain the primary symbol and a related symbol")
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}] must be an object")
                continue
            if set(item) != {"symbol", "file", "start_line", "end_line"}:
                errors.append(f"evidence[{index}] has incorrect fields")
            if not isinstance(item.get("symbol"), str) or not item.get("symbol"):
                errors.append(f"evidence[{index}].symbol must be a nonempty string")
            if not _valid_relative_path(item.get("file")):
                errors.append(f"evidence[{index}].file is not a relative POSIX path")
            start = item.get("start_line")
            end = item.get("end_line")
            if isinstance(start, bool) or not isinstance(start, int) or start < 1:
                errors.append(f"evidence[{index}].start_line must be a positive integer")
            if isinstance(end, bool) or not isinstance(end, int) or end < 1:
                errors.append(f"evidence[{index}].end_line must be a positive integer")
            if isinstance(start, int) and isinstance(end, int) and end < start:
                errors.append(f"evidence[{index}] has end_line before start_line")
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        errors.append("confidence must be a number between 0 and 1")
    if (
        isinstance(symbols, list)
        and all(isinstance(item, str) for item in symbols)
        and isinstance(evidence, list)
    ):
        evidence_symbols = {item.get("symbol") for item in evidence if isinstance(item, Mapping)}
        if (
            all(isinstance(item, str) for item in evidence_symbols)
            and set(symbols) != evidence_symbols
        ):
            errors.append("symbols must equal the symbols represented in evidence")
    if (
        isinstance(files, list)
        and all(isinstance(item, str) for item in files)
        and isinstance(evidence, list)
    ):
        evidence_files = {item.get("file") for item in evidence if isinstance(item, Mapping)}
        if all(isinstance(item, str) for item in evidence_files) and set(files) != evidence_files:
            errors.append("files must equal the files represented in evidence")
    return errors


def validate_repository_identity_and_paths(
    answer: Mapping[str, Any], repository: Path, expected_commit: str
) -> list[str]:
    errors: list[str] = []
    process = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    observed_commit = process.stdout.strip()
    if process.returncode or observed_commit != expected_commit:
        errors.append(
            f"repository commit mismatch: expected {expected_commit}, "
            f"got {observed_commit or '<unavailable>'}"
        )
    files = answer.get("files")
    evidence = answer.get("evidence")
    paths = [
        *(files if isinstance(files, list) else []),
        *[
            item.get("file")
            for item in (evidence if isinstance(evidence, list) else [])
            if isinstance(item, Mapping)
        ],
    ]
    for value in paths:
        if not _valid_relative_path(value):
            continue
        resolved = (repository / str(value)).resolve()
        try:
            resolved.relative_to(repository.resolve())
        except ValueError:
            errors.append(f"reported path escapes repository through a symlink: {value}")
            continue
        if not resolved.is_file():
            errors.append(f"reported path does not exist in repository: {value}")
    return errors


def _answer_from_final_message(events: Sequence[dict[str, Any]]) -> Any:
    messages = [
        item.get("text")
        for item in completed_items(events)
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str)
    ]
    if not messages:
        return None
    try:
        return json.loads(messages[-1])
    except json.JSONDecodeError:
        return None


def _identifier(arguments: object) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    for key in ("identifier", "symbol_id", "key"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def validate_codekg_protocol(events: Sequence[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    all_items = completed_items(events)
    mcp_items = [item for item in all_items if item.get("type") == "mcp_tool_call"]
    commands = [item for item in all_items if item.get("type") == "command_execution"]
    if commands:
        errors.append("CodeKG arm used a shell command")
    if any("web" in str(item.get("type", "")).lower() for item in all_items):
        errors.append("CodeKG arm used web search")
    wrong_server = [item for item in mcp_items if item.get("server") != "codekg"]
    if wrong_server:
        errors.append("CodeKG arm used an MCP server other than codekg")
    tools = [str(item.get("tool")) for item in mcp_items]
    forbidden = sorted(set(tools) - ALLOWED_CODEKG_TOOLS)
    if forbidden:
        errors.append(f"CodeKG arm used disallowed tools: {forbidden}")
    failed_tools = [
        str(item.get("tool"))
        for item in mcp_items
        if item.get("status") != "completed" or item.get("error") or item.get("result") is None
    ]
    if failed_tools:
        errors.append(f"CodeKG arm had failed or incomplete tool calls: {failed_tools}")

    successful = successful_mcp_items(events)
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for item in successful:
        by_tool.setdefault(str(item.get("tool")), []).append(item)
    searches = by_tool.get("search_symbols", [])
    if not 1 <= len(searches) <= 2:
        errors.append("CodeKG arm must complete one or two search_symbols calls")
    for search in searches:
        arguments = search.get("arguments")
        if not isinstance(arguments, Mapping):
            errors.append("search_symbols arguments are missing")
            continue
        if arguments.get("repository") != "requests":
            errors.append("search_symbols must use repository=requests")
        if arguments.get("scope") != "source":
            errors.append("search_symbols must use scope=source")
        limit = arguments.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            errors.append("search_symbols limit must be an integer from 1 to 5")

    definitions = by_tool.get("get_definition", [])
    callers = by_tool.get("find_callers", [])
    callees = by_tool.get("find_callees", [])
    if len(definitions) != 1:
        errors.append("CodeKG arm must complete exactly one get_definition call")
    if len(callers) != 1:
        errors.append("CodeKG arm must complete exactly one find_callers call")
    if len(callees) != 1:
        errors.append("CodeKG arm must complete exactly one find_callees call")

    selected = _identifier(definitions[0].get("arguments")) if len(definitions) == 1 else None
    if selected:
        search_strings = {
            value for search in searches for value in _all_strings(structured_content(search))
        }
        if selected not in search_strings:
            errors.append("get_definition identifier was not returned by search_symbols")
        for relationship in [*callers, *callees]:
            if _identifier(relationship.get("arguments")) != selected:
                errors.append("definition and relationship calls must use the same symbol ID")
    elif definitions:
        errors.append("get_definition did not use an exact symbol identifier")

    successful_order = [str(item.get("tool")) for item in successful]
    if searches and definitions and callers and callees:
        search_position = max(
            index for index, tool in enumerate(successful_order) if tool == "search_symbols"
        )
        definition_position = successful_order.index("get_definition")
        caller_position = successful_order.index("find_callers")
        callee_position = successful_order.index("find_callees")
        if not search_position < definition_position < min(caller_position, callee_position):
            errors.append(
                "required CodeKG calls were not completed in search/definition/relations order"
            )
    relation_rows = [row for item in [*callers, *callees] for row in result_rows(item)]
    if not relation_rows:
        errors.append("both relationship calls were empty")
    if len(searches) == 2:
        first_search = structured_content(searches[0])
        if isinstance(first_search, Mapping) and isinstance(
            first_search.get("recommended_symbol_id"), str
        ):
            errors.append("a second search followed a recommended candidate")
    return errors, {
        "selected_symbol_id": selected,
        "search_calls": len(searches),
        "relationship_rows": len(relation_rows),
    }


def validate_codekg_provenance(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    payloads = [structured_content(item) for item in successful_mcp_items(events)]
    strings: set[str] = set()
    records: set[tuple[str, int, int]] = set()
    for payload in payloads:
        strings.update(_all_strings(payload))
        for value in _walk(payload):
            if not isinstance(value, Mapping):
                continue
            file = value.get("file")
            start = value.get("start_line")
            end = value.get("end_line")
            if isinstance(file, str) and isinstance(start, int) and isinstance(end, int):
                records.add((file, start, end))
    symbols = answer.get("symbols")
    files = answer.get("files")
    evidence_items = answer.get("evidence")
    for symbol in symbols if isinstance(symbols, list) else []:
        if symbol not in strings:
            errors.append(f"reported symbol was not returned by CodeKG: {symbol}")
    for path in files if isinstance(files, list) else []:
        if path not in strings:
            errors.append(f"reported file was not returned by CodeKG: {path}")
    for evidence in evidence_items if isinstance(evidence_items, list) else []:
        if not isinstance(evidence, Mapping):
            continue
        symbol = evidence.get("symbol")
        record = (evidence.get("file"), evidence.get("start_line"), evidence.get("end_line"))
        if symbol not in strings:
            errors.append(f"evidence symbol was not returned by CodeKG: {symbol}")
        if record not in records:
            errors.append(f"evidence range was not returned by CodeKG: {record}")
    return errors


def validate_codekg_result_scope(
    events: Sequence[dict[str, Any]], *, repository: str, commit: str
) -> list[str]:
    errors: list[str] = []
    short_commit = commit[:12]
    symbol_prefix = f"{repository}@{short_commit}:"
    for item in successful_mcp_items(events):
        payload = structured_content(item)
        for value in _walk(payload):
            if not isinstance(value, Mapping):
                continue
            for key in ("repository", "repo"):
                observed_repository = value.get(key)
                if isinstance(observed_repository, str) and observed_repository != repository:
                    errors.append(
                        f"{item.get('tool')} returned cross-repository {key}: {observed_repository}"
                    )
            observed_commit = value.get("commit")
            if (
                isinstance(observed_commit, str)
                and not commit.startswith(observed_commit)
                and not observed_commit.startswith(commit)
            ):
                errors.append(f"{item.get('tool')} returned wrong commit: {observed_commit}")
            for key in ("symbol_id", "key"):
                identifier = value.get(key)
                if (
                    isinstance(identifier, str)
                    and "@" in identifier
                    and not identifier.startswith(symbol_prefix)
                ):
                    errors.append(f"{item.get('tool')} returned cross-snapshot {key}: {identifier}")
    return errors


def _definition_ranges(repository: Path) -> dict[tuple[str, str], tuple[int, int]]:
    result: dict[tuple[str, str], tuple[int, int]] = {}
    for path in repository.rglob("*.py"):
        relative = path.relative_to(repository).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        def visit(
            nodes: Iterable[ast.stmt],
            prefix: tuple[str, ...] = (),
            relative_path: str = relative,
        ) -> None:
            for node in nodes:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = ".".join((*prefix, node.name))
                    result[(relative_path, name)] = (
                        node.lineno,
                        node.end_lineno or node.lineno,
                    )
                    visit(node.body, (*prefix, node.name), relative_path)

        visit(tree.body)
    return result


def _command_proves_numbered_range(
    command_item: Mapping[str, Any], file: str, start_line: int, end_line: int
) -> bool:
    command = command_item.get("command")
    output = command_item.get("aggregated_output")
    if not isinstance(command, str) or not isinstance(output, str):
        return False
    path_pattern = rf"(?<![A-Za-z0-9_./-]){re.escape(file)}(?![A-Za-z0-9_./-])"
    if re.search(path_pattern, command) is None:
        return False
    numbered_lines = set()
    for line in output.splitlines():
        match = re.match(r"^[ \t]*(\d+)(?:\t| +)", line)
        if match:
            numbered_lines.add(int(match.group(1)))
    return set(range(start_line, end_line + 1)).issubset(numbered_lines)


def validate_native(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]], repository: Path
) -> list[str]:
    errors: list[str] = []
    items = completed_items(events)
    if any(item.get("type") == "mcp_tool_call" for item in items):
        errors.append("native arm used an MCP tool")
    if any("web" in str(item.get("type", "")).lower() for item in items):
        errors.append("native arm used web search")
    commands = [
        item
        for item in items
        if item.get("type") == "command_execution"
        and item.get("status") == "completed"
        and item.get("exit_code") == 0
    ]
    if not commands:
        return [*errors, "native arm completed no successful shell command"]
    command_text = "\n".join(
        f"{item.get('command', '')}\n{item.get('aggregated_output', '')}" for item in commands
    )
    if not re.search(r"\b(rg|grep|find)\b", command_text):
        errors.append("native arm did not perform a recognizable repository search")
    ranges = _definition_ranges(repository)
    answer_evidence = answer.get("evidence")
    for evidence in answer_evidence if isinstance(answer_evidence, list) else []:
        if not isinstance(evidence, Mapping):
            continue
        file = evidence.get("file")
        symbol = evidence.get("symbol")
        start_line = evidence.get("start_line")
        end_line = evidence.get("end_line")
        if (
            not isinstance(file, str)
            or isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or end_line < start_line
        ):
            errors.append(f"native evidence file was not inspected: {file}")
            continue
        if not any(
            _command_proves_numbered_range(command, file, start_line, end_line)
            for command in commands
        ):
            errors.append(
                "native evidence definition was not inspected with numbered "
                f"full-definition output: {file}:{start_line}-{end_line}"
            )
        suffix = str(symbol).split(":")[-2 if str(symbol).count(":") >= 2 else -1]
        suffix = suffix.removeprefix("src.")
        candidates = [
            bounds
            for (candidate_file, name), bounds in ranges.items()
            if candidate_file == file and (suffix.endswith(name) or name.endswith(suffix))
        ]
        reported = (start_line, end_line)
        if reported not in candidates:
            errors.append(f"native evidence does not match an AST definition: {symbol} {reported}")
    return errors


def _matches_symbol(value: str, accepted_suffixes: Sequence[str]) -> bool:
    return any(value.endswith(suffix) or suffix in value for suffix in accepted_suffixes)


def grade_answer(answer: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    answer_evidence = answer.get("evidence")
    evidence = [
        item
        for item in (answer_evidence if isinstance(answer_evidence, list) else [])
        if isinstance(item, Mapping)
    ]
    primary = gold["primary"]
    primary_matches = [
        item
        for item in evidence
        if item.get("file") == primary["file"]
        and item.get("start_line") == primary["start_line"]
        and item.get("end_line") == primary["end_line"]
        and _matches_symbol(str(item.get("symbol", "")), primary["accepted_suffixes"])
    ]
    relevant_matches = []
    for expected in gold["relevant"]:
        matched = any(
            item.get("file") == expected["file"]
            and item.get("start_line") == expected["start_line"]
            and item.get("end_line") == expected["end_line"]
            and _matches_symbol(str(item.get("symbol", "")), expected["accepted_suffixes"])
            for item in evidence
        )
        relevant_matches.append({"symbol": expected["symbol"], "matched": matched})
    infra_answer = str(answer.get("answer", "")).startswith("INFRA_ERROR:")
    return {
        "primary_correct": bool(primary_matches),
        "relevant_correct": any(item["matched"] for item in relevant_matches),
        "relevant_matches": relevant_matches,
        "correct": bool(primary_matches) and any(item["matched"] for item in relevant_matches),
        "infra_error_answer": infra_answer,
    }


def _target_search_metrics(
    events: Sequence[dict[str, Any]], suffixes: Sequence[str]
) -> tuple[int | None, int | None]:
    search_number = 0
    for item in successful_mcp_items(events):
        if item.get("tool") != "search_symbols":
            continue
        search_number += 1
        for rank, row in enumerate(result_rows(item), start=1):
            strings = _all_strings(row)
            if any(_matches_symbol(value, suffixes) for value in strings):
                return rank, search_number - 1
    return None, None


def collect_metrics(
    *,
    arm: str,
    events: Sequence[dict[str, Any]],
    answer: Mapping[str, Any],
    gold: Mapping[str, Any],
    wall_seconds: float | None,
    exit_code: int | None,
    valid: bool,
    evidence_compliant: bool,
    correctness: Mapping[str, Any],
    unsupported_claim_count: int,
) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for event in events:
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), Mapping):
            continue
        for key in usage:
            value = event["usage"].get(key, 0)
            if isinstance(value, int):
                usage[key] += value
    uncached = max(0, usage["input_tokens"] - usage["cached_input_tokens"])
    input_tokens = usage["input_tokens"]
    target_rank, searches_before_target = _target_search_metrics(
        events, gold["primary"]["accepted_suffixes"]
    )
    mcp_items = [item for item in completed_items(events) if item.get("type") == "mcp_tool_call"]
    response_bytes = sum(
        len(json.dumps(item.get("result"), separators=(",", ":"), ensure_ascii=False).encode())
        for item in mcp_items
        if item.get("result") is not None
    )
    relationship_rows = [
        row
        for item in successful_mcp_items(events)
        if item.get("tool") in {"find_callers", "find_callees"}
        for row in result_rows(item)
    ]
    complete_relationships = sum(
        isinstance(row.get("file"), str)
        and isinstance(row.get("start_line"), int)
        and isinstance(row.get("end_line"), int)
        for row in relationship_rows
    )
    search_items = [
        item for item in successful_mcp_items(events) if item.get("tool") == "search_symbols"
    ]
    failed_operations = [
        item
        for item in completed_items(events)
        if item.get("type") in {"mcp_tool_call", "command_execution"}
        and (
            item.get("status") != "completed"
            or item.get("error")
            or (item.get("type") == "command_execution" and item.get("exit_code") not in {None, 0})
        )
    ]
    return {
        "arm": arm,
        "infrastructure_success": exit_code in {None, 0} and not failed_operations,
        "valid": valid,
        "evidence_compliant": evidence_compliant,
        "unsupported_claim_count": unsupported_claim_count,
        "correct": bool(correctness.get("correct")),
        "success": valid and bool(correctness.get("correct")),
        "wall_seconds": wall_seconds,
        "turns": sum(event.get("type") == "turn.started" for event in events),
        "tool_calls": len(
            [
                item
                for item in completed_items(events)
                if item.get("type") in {"mcp_tool_call", "command_execution"}
            ]
        ),
        "response_bytes": response_bytes,
        "search_calls": len(search_items),
        "candidate_count": sum(len(result_rows(item)) for item in search_items),
        "target_rank": target_rank,
        "searches_before_target": searches_before_target,
        "recall_at_1": target_rank == 1,
        "recall_at_5": target_rank is not None and target_rank <= 5,
        "reciprocal_rank": 1 / target_rank if target_rank else 0.0,
        "relationship_rows": len(relationship_rows),
        "relationship_location_completeness": (
            complete_relationships / len(relationship_rows) if relationship_rows else None
        ),
        "tokens": {
            "input": input_tokens,
            "cached_input": usage["cached_input_tokens"],
            "uncached_input": uncached,
            "cache_hit_ratio": usage["cached_input_tokens"] / input_tokens
            if input_tokens
            else None,
            "output": usage["output_tokens"],
            "reasoning": usage["reasoning_output_tokens"],
        },
    }


def validate_trial(
    *,
    arm: str,
    events_path: Path,
    answer_path: Path,
    repository: Path,
    gold_path: Path,
    wall_seconds: float | None = None,
    exit_code: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    events, event_errors = read_events(events_path)
    thread_ids = [
        event.get("thread_id") for event in events if event.get("type") == "thread.started"
    ]
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not thread_ids[0]:
        event_errors.append("trial must contain exactly one nonempty thread.started thread_id")
    try:
        answer = read_json(answer_path)
    except (OSError, json.JSONDecodeError) as exc:
        answer = {}
        event_errors.append(f"cannot read answer.json: {exc}")
    errors = [*event_errors, *validate_answer_schema(answer)]
    if isinstance(answer, Mapping) and answer.get("task_id") != "requests-intent-001":
        errors.append("answer task_id does not match requests-intent-001")
    final_answer = _answer_from_final_message(events)
    if final_answer is None:
        errors.append("final JSONL agent message is not a JSON object")
    elif final_answer != answer:
        errors.append("final JSONL agent message does not match answer.json")
    gold = read_json(gold_path)
    protocol: dict[str, Any] = {}
    provenance_errors: list[str] = []
    if isinstance(answer, Mapping):
        if arm == "codekg":
            protocol_errors, protocol = validate_codekg_protocol(events)
            errors.extend(protocol_errors)
            provenance_errors = validate_codekg_provenance(answer, events)
            errors.extend(
                validate_codekg_result_scope(
                    events,
                    repository=gold["repository"],
                    commit=gold["commit"],
                )
            )
        elif arm == "native":
            provenance_errors = validate_native(answer, events, repository)
        else:
            errors.append(f"unknown arm: {arm}")
    errors.extend(provenance_errors)
    if isinstance(answer, Mapping):
        errors.extend(validate_repository_identity_and_paths(answer, repository, gold["commit"]))
    if arm == "codekg" and isinstance(answer, Mapping):
        relationship_strings = {
            value
            for item in successful_mcp_items(events)
            if item.get("tool") in {"find_callers", "find_callees"}
            for value in _all_strings(structured_content(item))
        }
        relevant_suffixes = [
            suffix for relevant in gold["relevant"] for suffix in relevant["accepted_suffixes"]
        ]
        if not any(_matches_symbol(value, relevant_suffixes) for value in relationship_strings):
            errors.append("relationship results did not contain a gold-relevant symbol")
    correctness = grade_answer(answer, gold) if isinstance(answer, Mapping) else {"correct": False}
    answer_evidence = answer.get("evidence") if isinstance(answer, Mapping) else None
    evidence_compliant = (
        not provenance_errors and isinstance(answer_evidence, list) and len(answer_evidence) >= 2
    )
    valid = not errors and exit_code in {None, 0}
    validation = {
        "arm": arm,
        "valid": valid,
        "errors": errors,
        "protocol": protocol,
        "correctness": correctness,
        "evidence_compliant": evidence_compliant,
        "unsupported_claim_count": len(provenance_errors),
    }
    metrics = collect_metrics(
        arm=arm,
        events=events,
        answer=answer if isinstance(answer, Mapping) else {},
        gold=gold,
        wall_seconds=wall_seconds,
        exit_code=exit_code,
        valid=valid,
        evidence_compliant=evidence_compliant,
        correctness=correctness,
        unsupported_claim_count=len(provenance_errors),
    )
    return validation, metrics


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[position]


def bootstrap_median_ci(
    values: Sequence[float], *, seed: int, samples: int = 10_000
) -> list[float] | None:
    if not values:
        return None
    randomizer = random.Random(seed)
    medians = []
    for _ in range(samples):
        sample = [randomizer.choice(values) for _ in values]
        medians.append(statistics.median(sample))
    medians.sort()
    return [
        medians[math.floor(0.025 * (samples - 1))],
        medians[math.floor(0.975 * (samples - 1))],
    ]


def summarize_values(values: Sequence[float], *, seed: int) -> dict[str, Any]:
    return {
        "samples": list(values),
        "median": statistics.median(values) if values else None,
        "p95": percentile(values, 0.95),
        "bootstrap_median_95_ci": bootstrap_median_ci(values, seed=seed),
    }
