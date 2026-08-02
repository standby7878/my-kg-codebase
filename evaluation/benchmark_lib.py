"""Shared, dependency-free helpers for the multi-repository Codex benchmark."""

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
SNAPSHOT_SUFFIXES = {".py", ".md"}
SNAPSHOT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
    "vendor",
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
    if not isinstance(value, dict) or value.get("version") != 2:
        raise ValueError(f"unsupported benchmark manifest: {path}")
    repositories = value.get("repositories")
    tasks = value.get("tasks")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("benchmark manifest must contain repositories")
    if not isinstance(tasks, list) or len(tasks) != 10:
        raise ValueError("benchmark manifest must contain exactly ten tasks")
    repository_names = [item.get("name") for item in repositories if isinstance(item, dict)]
    invalid_repository_names = len(repository_names) != len(repositories)
    duplicate_repository_names = len(set(repository_names)) != len(repositories)
    if invalid_repository_names or duplicate_repository_names:
        raise ValueError("benchmark repository names must be unique strings")
    expected_indices = list(range(1, len(tasks) + 1))
    indices = [item.get("index") for item in tasks if isinstance(item, dict)]
    if indices != expected_indices:
        raise ValueError(f"benchmark task indices must be contiguous: {expected_indices}")
    task_ids = [item.get("task_id") for item in tasks if isinstance(item, dict)]
    slugs = [item.get("slug") for item in tasks if isinstance(item, dict)]
    if len(set(task_ids)) != len(tasks) or len(set(slugs)) != len(tasks):
        raise ValueError("benchmark task IDs and slugs must be unique")
    if any(item.get("repository") not in repository_names for item in tasks):
        raise ValueError("benchmark task references an unknown repository")
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


def source_snapshot_hash(root: Path) -> str:
    """Match CodeKG's content identity for mounted non-Git source subtrees."""
    digest = hashlib.sha256()
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SNAPSHOT_SUFFIXES
        and not any(part in SNAPSHOT_SKIP_DIRS for part in path.relative_to(root).parts)
    ]
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def repository_snapshot_commit(repository: Path, identity: str) -> str:
    if identity == "content":
        return source_snapshot_hash(repository)
    if identity != "git":
        raise ValueError(f"unsupported repository identity: {identity}")
    process = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        return ""
    return process.stdout.strip()


def snapshot_hashes(manifest_path: Path, profile_path: Path) -> dict[str, str]:
    manifest = load_manifest(manifest_path)
    relative_files = [manifest["schema_file"]]
    for task in manifest["tasks"]:
        relative_files.append(task["gold_file"])
        relative_files.extend(task["prompt_files"].values())
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
    task_index: int
    task_id: str
    slug: str
    repository: str
    arm: str

    @property
    def trial_name(self) -> str:
        return f"{self.task_index:02d}-{self.slug}"


def suite_schedule(
    *, tasks: Sequence[Mapping[str, Any]], arms: Sequence[str], seed: int
) -> list[ScheduleEntry]:
    if list(arms) != ["codekg", "native"]:
        raise ValueError("the benchmark requires codekg and native arms")
    if not tasks or len(tasks) % 2:
        raise ValueError("the benchmark requires a positive even task count")

    randomizer = random.Random(seed)
    first_arms = ["codekg"] * (len(tasks) // 2) + ["native"] * (len(tasks) // 2)
    randomizer.shuffle(first_arms)
    entries: list[ScheduleEntry] = []
    for task, first in zip(tasks, first_arms, strict=True):
        second = "native" if first == "codekg" else "codekg"
        for arm in (first, second):
            entries.append(
                ScheduleEntry(
                    ordinal=len(entries),
                    task_index=int(task["index"]),
                    task_id=str(task["task_id"]),
                    slug=str(task["slug"]),
                    repository=str(task["repository"]),
                    arm=arm,
                )
            )
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
    required = {"task_id", "answer", "primary", "related", "confidence"}
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
    primary = answer.get("primary")
    related = answer.get("related")
    if not isinstance(primary, dict):
        errors.append("primary must be an object")
    if not isinstance(related, list):
        errors.append("related must be an array")
        related = []
    elif len(related) > 1:
        errors.append("related must contain at most one entry")
    claims = ([primary] if isinstance(primary, dict) else []) + related
    for index, item in enumerate(claims):
        label = "primary" if index == 0 and item is primary else "related[0]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        expected_fields = {"symbol", "file", "start_line", "end_line"}
        if label != "primary":
            expected_fields.add("relationship")
        if set(item) != expected_fields:
            errors.append(f"{label} has incorrect fields")
        if not isinstance(item.get("symbol"), str) or not item.get("symbol"):
            errors.append(f"{label}.symbol must be a nonempty string")
        if not _valid_relative_path(item.get("file")):
            errors.append(f"{label}.file is not a relative POSIX path")
        if label != "primary" and item.get("relationship") not in {"caller", "callee"}:
            errors.append(f"{label}.relationship must be caller or callee")
        start = item.get("start_line")
        end = item.get("end_line")
        if isinstance(start, bool) or not isinstance(start, int) or start < 1:
            errors.append(f"{label}.start_line must be a positive integer")
        if isinstance(end, bool) or not isinstance(end, int) or end < 1:
            errors.append(f"{label}.end_line must be a positive integer")
        if isinstance(start, int) and isinstance(end, int) and end < start:
            errors.append(f"{label} has end_line before start_line")
    confidence = answer.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        errors.append("confidence must be a number between 0 and 1")
    return errors


def _answer_claims(answer: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    claims: list[tuple[str, Mapping[str, Any]]] = []
    primary = answer.get("primary")
    if isinstance(primary, Mapping):
        claims.append(("primary", primary))
    related = answer.get("related")
    if isinstance(related, list):
        claims.extend(
            (f"related[{index}]", item)
            for index, item in enumerate(related)
            if isinstance(item, Mapping)
        )
    return claims


def validate_repository_identity_and_paths(
    answer: Mapping[str, Any],
    repository: Path,
    expected_commit: str,
    identity: str,
) -> list[str]:
    errors: list[str] = []
    observed_commit = repository_snapshot_commit(repository, identity)
    if not observed_commit or not observed_commit.startswith(expected_commit):
        errors.append(
            f"repository commit mismatch: expected {expected_commit}, "
            f"got {observed_commit or '<unavailable>'}"
        )
    paths = [claim.get("file") for _, claim in _answer_claims(answer)]
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


def validate_codekg_protocol(
    events: Sequence[dict[str, Any]], repository: str
) -> tuple[list[str], dict[str, Any]]:
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
    attempts_by_tool: dict[str, list[dict[str, Any]]] = {}
    for item in mcp_items:
        attempts_by_tool.setdefault(str(item.get("tool")), []).append(item)
    searches = attempts_by_tool.get("search_symbols", [])
    definitions = attempts_by_tool.get("get_definition", [])
    callers = attempts_by_tool.get("find_callers", [])
    callees = attempts_by_tool.get("find_callees", [])
    if not 1 <= len(searches) <= 2:
        errors.append("CodeKG arm must make one or two search_symbols calls")
    if not 1 <= len(definitions) <= 2:
        errors.append("CodeKG arm must make one or two get_definition calls")
    if len(callers) != 1:
        errors.append("CodeKG arm must make exactly one find_callers call")
    if len(callees) != 1:
        errors.append("CodeKG arm must make exactly one find_callees call")
    for search in searches:
        arguments = search.get("arguments")
        if not isinstance(arguments, Mapping):
            errors.append("search_symbols arguments are missing")
            continue
        if arguments.get("repository") != repository:
            errors.append(f"search_symbols must use repository={repository}")
        if arguments.get("scope") != "source":
            errors.append("search_symbols must use scope=source")
        limit = arguments.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            errors.append("search_symbols limit must be an integer from 1 to 5")

    positions = {id(item): index for index, item in enumerate(mcp_items)}
    for definition in definitions:
        selected_at_step = _identifier(definition.get("arguments"))
        if not selected_at_step:
            errors.append("get_definition did not use an exact symbol identifier")
            continue
        preceding_search_strings = {
            value
            for search in searches
            if positions[id(search)] < positions[id(definition)]
            and search.get("status") == "completed"
            and not search.get("error")
            for value in _all_strings(structured_content(search))
        }
        if selected_at_step not in preceding_search_strings:
            errors.append(
                "get_definition identifier was not returned by a preceding search_symbols"
            )

    selected = _identifier(definitions[-1].get("arguments")) if definitions else None
    if selected:
        for relationship in [*callers, *callees]:
            if _identifier(relationship.get("arguments")) != selected:
                errors.append("relationship calls must use the final definition symbol ID")
    if definitions and callers and callees:
        final_definition_position = positions[id(definitions[-1])]
        if any(positions[id(search)] > final_definition_position for search in searches):
            errors.append("search_symbols calls must precede the final get_definition call")
        if any(
            positions[id(relationship)] < final_definition_position
            for relationship in [*callers, *callees]
        ):
            errors.append("relationship calls must follow the final get_definition call")

    successful_relations = [
        item
        for item in [*callers, *callees]
        if item.get("status") == "completed" and not item.get("error") and item.get("result")
    ]
    relation_rows = [row for item in successful_relations for row in result_rows(item)]
    return errors, {
        "selected_symbol_id": selected,
        "search_calls": len(searches),
        "definition_calls": len(definitions),
        "relationship_rows": len(relation_rows),
    }


def _codekg_claim_sources(events: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    attempts = [item for item in completed_items(events) if item.get("type") == "mcp_tool_call"]
    definitions = [item for item in attempts if item.get("tool") == "get_definition"]
    final_definition = definitions[-1] if definitions else None
    final_identifier = (
        _identifier(final_definition.get("arguments")) if final_definition is not None else None
    )

    def successful_rows(item: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if (
            item is None
            or item.get("status") != "completed"
            or item.get("error")
            or not isinstance(item.get("result"), Mapping)
        ):
            return []
        return result_rows(item)

    def relationship_rows(tool: str) -> list[dict[str, Any]]:
        matching_attempts = [
            item
            for item in attempts
            if item.get("tool") == tool
            and final_identifier is not None
            and _identifier(item.get("arguments")) == final_identifier
        ]
        return successful_rows(matching_attempts[0]) if len(matching_attempts) == 1 else []

    return {
        "primary": successful_rows(final_definition),
        "caller": relationship_rows("find_callers"),
        "callee": relationship_rows("find_callees"),
    }


def validate_codekg_provenance(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]]
) -> list[str]:
    sources = _codekg_claim_sources(events)
    errors: list[str] = []
    for label, claim in _answer_claims(answer):
        source = "primary" if label == "primary" else str(claim.get("relationship"))
        if not any(_row_matches_claim(row, claim) for row in sources.get(source, [])):
            errors.append(f"{label} claim was not returned by its CodeKG {source} result")
    return errors


def _validate_codekg_locations(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]]
) -> list[str]:
    sources = _codekg_claim_sources(events)
    errors: list[str] = []
    for label, claim in _answer_claims(answer):
        source = "primary" if label == "primary" else str(claim.get("relationship"))
        matching_rows = [row for row in sources.get(source, []) if _row_matches_claim(row, claim)]
        if matching_rows and not any(
            row.get("start_line") == claim.get("start_line")
            and row.get("end_line") == claim.get("end_line")
            for row in matching_rows
        ):
            errors.append(f"{label} claim range was not returned by its CodeKG {source} result")
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
    details = _validate_native(answer, events, repository)
    return [*details["protocol"], *details["provenance"], *details["location"]]


def _validate_native(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]], repository: Path
) -> dict[str, list[str]]:
    protocol_errors: list[str] = []
    provenance_errors: list[str] = []
    location_errors: list[str] = []
    items = completed_items(events)
    if any(item.get("type") == "mcp_tool_call" for item in items):
        protocol_errors.append("native arm used an MCP tool")
    if any("web" in str(item.get("type", "")).lower() for item in items):
        protocol_errors.append("native arm used web search")
    commands = [
        item
        for item in items
        if item.get("type") == "command_execution"
        and item.get("status") == "completed"
        and item.get("exit_code") == 0
    ]
    if not commands:
        protocol_errors.append("native arm completed no successful shell command")
        return {
            "protocol": protocol_errors,
            "provenance": provenance_errors,
            "location": location_errors,
        }
    command_text = "\n".join(
        f"{item.get('command', '')}\n{item.get('aggregated_output', '')}" for item in commands
    )
    if not re.search(r"\b(rg|grep|find)\b", command_text):
        protocol_errors.append("native arm did not perform a recognizable repository search")
    ranges = _definition_ranges(repository)
    for label, claim in _answer_claims(answer):
        file = claim.get("file")
        symbol = claim.get("symbol")
        start_line = claim.get("start_line")
        end_line = claim.get("end_line")
        if (
            not isinstance(file, str)
            or isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or end_line < start_line
        ):
            provenance_errors.append(f"{label} claim was not inspected: {file}")
            continue
        path_pattern = rf"(?<![A-Za-z0-9_./-]){re.escape(file)}(?![A-Za-z0-9_./-])"
        if not any(
            re.search(path_pattern, str(command.get("command", ""))) for command in commands
        ):
            provenance_errors.append(f"{label} claim file was not inspected: {file}")
            continue
        suffix = (
            str(symbol).split(":")[-2 if str(symbol).count(":") >= 2 else -1].removeprefix("src.")
        )
        candidates = [
            bounds
            for (candidate_file, name), bounds in ranges.items()
            if candidate_file == file and _symbol_boundary_match(suffix, name)
        ]
        if not candidates:
            provenance_errors.append(
                f"{label} claim symbol was not identified as an AST definition: {symbol}"
            )
            continue
        if not any(
            _command_proves_numbered_range(command, file, start_line, end_line)
            for command in commands
        ):
            location_errors.append(
                f"{label} claim lacks numbered full-definition output: "
                f"{file}:{start_line}-{end_line}"
            )
        reported = (start_line, end_line)
        if reported not in candidates:
            location_errors.append(f"{label} claim does not match an AST definition: {symbol}")
    return {
        "protocol": protocol_errors,
        "provenance": provenance_errors,
        "location": location_errors,
    }


def _symbol_boundary_match(value: str, suffix: str) -> bool:
    return value == suffix or value.endswith(f".{suffix}")


def _matches_symbol(value: str, accepted_suffixes: Sequence[str]) -> bool:
    return any(_symbol_boundary_match(value, suffix) for suffix in accepted_suffixes)


def _row_matches_claim(row: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    file = claim.get("file")
    symbol = claim.get("symbol")
    if not isinstance(file, str) or not isinstance(symbol, str) or row.get("file") != file:
        return False
    row_symbols = [
        value
        for key in ("qualified_name", "qname", "symbol", "name", "symbol_id", "key")
        if isinstance((value := row.get(key)), str)
    ]
    return any(_symbol_boundary_match(value, symbol) for value in row_symbols)


def _range_status(claim: Mapping[str, Any], bounds: tuple[int, int]) -> str:
    start = claim.get("start_line")
    end = claim.get("end_line")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
        return "invalid"
    if (start, end) == bounds:
        return "exact"
    if start <= bounds[0] and end >= bounds[1]:
        return "contains_definition"
    return "invalid"


def _location_status(claim: Mapping[str, Any], expected: Mapping[str, Any] | None) -> str:
    if expected is None:
        return "invalid"
    start = expected.get("start_line")
    end = expected.get("end_line")
    if isinstance(start, bool) or not isinstance(start, int):
        return "invalid"
    if isinstance(end, bool) or not isinstance(end, int):
        return "invalid"
    return _range_status(claim, (start, end))


def _best_location_status(
    claim: Mapping[str, Any], bounds: Iterable[tuple[int, int]]
) -> str | None:
    statuses = [_range_status(claim, item) for item in bounds]
    if not statuses:
        return None
    if "exact" in statuses:
        return "exact"
    if "contains_definition" in statuses:
        return "contains_definition"
    return "invalid"


def _codekg_location_statuses(
    answer: Mapping[str, Any], events: Sequence[dict[str, Any]]
) -> dict[str, str | None]:
    sources = _codekg_claim_sources(events)
    statuses: dict[str, str | None] = {"primary": None, "related": None}
    for label, claim in _answer_claims(answer):
        source = "primary" if label == "primary" else str(claim.get("relationship"))
        bounds = [
            (row["start_line"], row["end_line"])
            for row in sources.get(source, [])
            if _row_matches_claim(row, claim)
            and isinstance(row.get("start_line"), int)
            and not isinstance(row.get("start_line"), bool)
            and isinstance(row.get("end_line"), int)
            and not isinstance(row.get("end_line"), bool)
        ]
        statuses["primary" if label == "primary" else "related"] = _best_location_status(
            claim, bounds
        )
    return statuses


def _native_location_statuses(answer: Mapping[str, Any], repository: Path) -> dict[str, str | None]:
    ranges = _definition_ranges(repository)
    statuses: dict[str, str | None] = {"primary": None, "related": None}
    for label, claim in _answer_claims(answer):
        file = claim.get("file")
        symbol = claim.get("symbol")
        suffix = str(symbol).split(":")[-2 if str(symbol).count(":") >= 2 else -1]
        suffix = suffix.removeprefix("src.")
        bounds = [
            candidate_bounds
            for (candidate_file, name), candidate_bounds in ranges.items()
            if candidate_file == file and _symbol_boundary_match(suffix, name)
        ]
        statuses["primary" if label == "primary" else "related"] = _best_location_status(
            claim, bounds
        )
    return statuses


def grade_answer(
    answer: Mapping[str, Any],
    gold: Mapping[str, Any],
    *,
    location_statuses: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    primary_claim = answer.get("primary")
    primary = gold["primary"]
    primary_correct: bool | None = None
    if isinstance(primary_claim, Mapping):
        primary_correct = (
            primary_claim.get("file") == primary["file"]
            and isinstance(primary_claim.get("symbol"), str)
            and _matches_symbol(primary_claim["symbol"], primary["accepted_suffixes"])
        )
    related_value = answer.get("related")
    related_claim = (
        related_value[0]
        if isinstance(related_value, list)
        and len(related_value) == 1
        and isinstance(related_value[0], Mapping)
        else None
    )
    relevant_matches: list[dict[str, Any]] = []
    matched_related: Mapping[str, Any] | None = None
    for expected in gold["relevant"]:
        matched = bool(
            related_claim
            and related_claim.get("file") == expected["file"]
            and related_claim.get("relationship") == expected.get("relationship")
            and isinstance(related_claim.get("symbol"), str)
            and _matches_symbol(related_claim["symbol"], expected["accepted_suffixes"])
        )
        if matched:
            matched_related = expected
        relevant_matches.append(
            {
                "symbol": expected["symbol"],
                "relationship": expected.get("relationship"),
                "matched": matched,
            }
        )
    related_correct: bool | None = None
    if isinstance(related_value, list):
        related_correct = bool(matched_related)
    if location_statuses is None:
        primary_location = (
            _location_status(primary_claim, primary)
            if isinstance(primary_claim, Mapping)
            else "invalid"
        )
        related_location = (
            _location_status(related_claim, matched_related) if related_claim is not None else None
        )
    else:
        primary_location = location_statuses.get("primary")
        related_location = location_statuses.get("related")
    claim_locations = [primary_location]
    if related_claim is not None:
        claim_locations.append(related_location)
    location_exact = bool(claim_locations) and all(status == "exact" for status in claim_locations)
    semantic_correct = (
        primary_correct and related_correct
        if primary_correct is not None and related_correct is not None
        else None
    )
    infra_answer = str(answer.get("answer", "")).startswith("INFRA_ERROR:")
    return {
        "primary_correct": primary_correct,
        "related_correct": related_correct,
        "relevant_correct": related_correct,
        "relevant_matches": relevant_matches,
        "semantic_correct": semantic_correct,
        "correct": semantic_correct,
        "locations": {"primary": primary_location, "related": related_location},
        "location_exact": location_exact,
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
    location_error_count: int = 0,
    protocol_error_count: int = 0,
    schema_valid: bool | None = None,
    infrastructure_success: bool | None = None,
    protocol_compliant: bool | None = None,
    provenance_compliant: bool | None = None,
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
            or (item.get("type") == "mcp_tool_call" and not isinstance(item.get("result"), Mapping))
            or (item.get("type") == "command_execution" and item.get("exit_code") not in {None, 0})
        )
    ]
    infrastructure_success = (
        exit_code in {None, 0} and not failed_operations
        if infrastructure_success is None
        else infrastructure_success
    )
    primary_correct = correctness.get("primary_correct")
    related_correct = correctness.get("related_correct")
    semantic_correct = correctness.get("semantic_correct", correctness.get("correct"))
    location_exact = correctness.get("location_exact")
    strict_pass = bool(valid and semantic_correct)
    result = {
        "arm": arm,
        "schema_valid": schema_valid,
        "infrastructure_success": infrastructure_success,
        "protocol_compliant": protocol_compliant,
        "provenance_compliant": provenance_compliant,
        "primary_correct": primary_correct,
        "related_correct": related_correct,
        "semantic_correct": semantic_correct,
        "location_exact": location_exact,
        "strict_pass": strict_pass,
        "valid": valid,
        "evidence_compliant": evidence_compliant,
        "unsupported_claim_count": unsupported_claim_count,
        "location_error_count": location_error_count,
        "protocol_error_count": protocol_error_count,
        "correct": semantic_correct,
        "success": strict_pass,
        "wall_seconds": wall_seconds,
        "turns": sum(event.get("type") == "turn.started" for event in events),
        "tool_calls": len(
            [
                item
                for item in completed_items(events)
                if item.get("type") in {"mcp_tool_call", "command_execution"}
            ]
        ),
        "response_bytes": response_bytes if arm == "codekg" else None,
        "search_calls": len(search_items) if arm == "codekg" else None,
        "candidate_count": (
            sum(len(result_rows(item)) for item in search_items) if arm == "codekg" else None
        ),
        "target_rank": target_rank if arm == "codekg" else None,
        "searches_before_target": searches_before_target if arm == "codekg" else None,
        "recall_at_1": target_rank == 1 if arm == "codekg" else None,
        "recall_at_5": target_rank is not None and target_rank <= 5 if arm == "codekg" else None,
        "reciprocal_rank": (1 / target_rank if target_rank else 0.0) if arm == "codekg" else None,
        "relationship_rows": len(relationship_rows) if arm == "codekg" else None,
        "relationship_location_completeness": (
            complete_relationships / len(relationship_rows) if relationship_rows else None
        )
        if arm == "codekg"
        else None,
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
    return result


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
    infrastructure_errors = list(event_errors)
    thread_ids = [
        event.get("thread_id") for event in events if event.get("type") == "thread.started"
    ]
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not thread_ids[0]:
        infrastructure_errors.append(
            "trial must contain exactly one nonempty thread.started thread_id"
        )
    schema_errors: list[str] = []
    try:
        answer = read_json(answer_path)
    except (OSError, json.JSONDecodeError) as exc:
        answer = {}
        schema_errors.append(f"cannot read answer.json: {exc}")
    gold = read_json(gold_path)
    schema_errors.extend(validate_answer_schema(answer))
    if isinstance(answer, Mapping) and answer.get("task_id") != gold["task_id"]:
        schema_errors.append(f"answer task_id does not match {gold['task_id']}")
    final_answer = _answer_from_final_message(events)
    if final_answer is None:
        schema_errors.append("final JSONL agent message is not a JSON object")
    elif final_answer != answer:
        schema_errors.append("final JSONL agent message does not match answer.json")
    protocol: dict[str, Any] = {}
    protocol_errors: list[str] = []
    provenance_errors: list[str] = []
    evidence_location_errors: list[str] = []
    location_statuses: dict[str, str | None] | None = None
    if isinstance(answer, Mapping):
        if arm == "codekg":
            protocol_errors, protocol = validate_codekg_protocol(events, gold["repository"])
            provenance_errors = validate_codekg_provenance(answer, events)
            evidence_location_errors = _validate_codekg_locations(answer, events)
            location_statuses = _codekg_location_statuses(answer, events)
            provenance_errors.extend(
                validate_codekg_result_scope(
                    events,
                    repository=gold["repository"],
                    commit=gold["commit"],
                )
            )
        elif arm == "native":
            native = _validate_native(answer, events, repository)
            protocol_errors = native["protocol"]
            provenance_errors = native["provenance"]
            evidence_location_errors = native["location"]
            location_statuses = _native_location_statuses(answer, repository)
        else:
            protocol_errors.append(f"unknown arm: {arm}")
    if isinstance(answer, Mapping):
        provenance_errors.extend(
            validate_repository_identity_and_paths(
                answer,
                repository,
                gold["commit"],
                gold["identity"],
            )
        )
    correctness = (
        grade_answer(answer, gold, location_statuses=location_statuses)
        if isinstance(answer, Mapping)
        else {"correct": False}
    )
    location_errors = [
        f"{label} location is {status}"
        for label, status in correctness.get("locations", {}).items()
        if status is not None and status != "exact"
    ]
    location_errors.extend(evidence_location_errors)
    failed_operations = [
        item
        for item in completed_items(events)
        if item.get("type") in {"mcp_tool_call", "command_execution"}
        and (
            item.get("status") != "completed"
            or item.get("error")
            or (item.get("type") == "mcp_tool_call" and not isinstance(item.get("result"), Mapping))
            or (item.get("type") == "command_execution" and item.get("exit_code") not in {None, 0})
        )
    ]
    if exit_code not in {None, 0}:
        infrastructure_errors.append(f"trial process exited with status {exit_code}")
    if failed_operations:
        infrastructure_errors.append("trial contained failed tool or command operations")
    schema_valid = not schema_errors
    infrastructure_success = not infrastructure_errors
    protocol_compliant = not protocol_errors
    provenance_compliant = not provenance_errors
    location_exact = bool(correctness.get("location_exact")) and not evidence_location_errors
    evidence_compliant = provenance_compliant and location_exact
    valid = schema_valid and infrastructure_success and protocol_compliant and evidence_compliant
    semantic_correct = correctness.get("semantic_correct")
    strict_pass = bool(valid and semantic_correct)
    errors = [
        *schema_errors,
        *infrastructure_errors,
        *protocol_errors,
        *provenance_errors,
        *location_errors,
    ]
    unsupported_claim_count = sum(
        error.startswith("primary claim") or error.startswith("related[")
        for error in provenance_errors
    )
    location_claim_labels = {
        error.split(" ", 1)[0]
        for error in location_errors
        if error.startswith("primary") or error.startswith("related[")
    }
    validation = {
        "arm": arm,
        "schema_valid": schema_valid,
        "infrastructure_success": infrastructure_success,
        "protocol_compliant": protocol_compliant,
        "provenance_compliant": provenance_compliant,
        "primary_correct": correctness.get("primary_correct"),
        "related_correct": correctness.get("related_correct"),
        "semantic_correct": semantic_correct,
        "location_exact": location_exact,
        "strict_pass": strict_pass,
        "valid": valid,
        "errors": errors,
        "error_categories": {
            "schema": schema_errors,
            "infrastructure": infrastructure_errors,
            "protocol": protocol_errors,
            "provenance": provenance_errors,
            "location": location_errors,
        },
        "protocol": protocol,
        "correctness": correctness,
        "evidence_compliant": evidence_compliant,
        "unsupported_claim_count": unsupported_claim_count,
        "location_error_count": len(location_claim_labels),
        "protocol_error_count": len(set(protocol_errors)),
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
        unsupported_claim_count=unsupported_claim_count,
        location_error_count=len(location_claim_labels),
        protocol_error_count=len(set(protocol_errors)),
        schema_valid=schema_valid,
        infrastructure_success=infrastructure_success,
        protocol_compliant=protocol_compliant,
        provenance_compliant=provenance_compliant,
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
