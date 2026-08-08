#!/usr/bin/env python3
"""Run the paired multi-repository CodeKG/native benchmark.

The default ``plan`` command is free and side-effect-light. ``run`` refuses to
launch Claude Code unless ``--execute`` is supplied; a complete run performs one
batch preflight and one CodeKG/native pair for each of ten indexed tasks.

The runner is Claude Code CLI (``claude -p --output-format stream-json``). Its raw
event stream is normalized into the same event schema the evaluator already
understands by ``claude_events.py``, so ``benchmark_lib.py`` needed no changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmark_lib import (
    ALLOWED_CODEKG_TOOLS,
    MANIFEST_PATH,
    completed_items,
    load_manifest,
    read_events,
    read_json,
    repository_snapshot_commit,
    resolve_manifest_file,
    result_rows,
    snapshot_hashes,
    structured_content,
    successful_mcp_items,
    suite_schedule,
    validate_codekg_protocol,
    validate_trial,
    write_json_new,
)
from claude_events import finalize_claude_trial

MODEL_ENV_VAR = "CODEKG_BENCHMARK_MODEL"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ALLOWED_MODELS = frozenset({"claude-haiku-4-5-20251001", "claude-sonnet-5"})
EFFORT_ENV_VAR = "CODEKG_BENCHMARK_EFFORT"
DEFAULT_EFFORT = "high"
CODEKG_MCP_URL = "http://127.0.0.1:8765/mcp"
MCP_CONFIG_PATH = Path(__file__).resolve().parent / "claude-codekg-mcp.json"
JSON_ONLY_SYSTEM_PROMPT = (
    "Your final assistant message of this session must be the JSON answer object "
    "only: no markdown code fences, no prose before or after it, no explanation "
    "outside the JSON object's own fields."
)


def resolve_model() -> str:
    model = os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL)
    if model not in ALLOWED_MODELS:
        raise RuntimeError(
            f"{MODEL_ENV_VAR}={model!r} is not one of the allowed benchmark models: "
            f"{sorted(ALLOWED_MODELS)}"
        )
    return model


def resolve_effort() -> str:
    return os.environ.get(EFFORT_ENV_VAR, DEFAULT_EFFORT)


def _git(repository: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or "git command failed")
    return process.stdout.strip()


def claude_version(claude: str) -> str:
    process = subprocess.run(
        [claude, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode or not process.stdout.strip():
        raise RuntimeError(process.stderr.strip() or "cannot determine Claude Code version")
    return process.stdout.strip()


def claude_auth_status(claude: str) -> dict[str, Any]:
    process = subprocess.run(
        [claude, "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode or not process.stdout.strip():
        raise RuntimeError(process.stderr.strip() or "cannot determine Claude Code auth status")
    try:
        status = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("cannot parse 'claude auth status' output") from exc
    if not status.get("loggedIn"):
        raise RuntimeError("Claude Code is not logged in; run 'claude auth login' first")
    return status


def runner_contract(*, model: str, effort: str) -> dict[str, Any]:
    return {
        "runner": "claude-code",
        "model": model,
        "effort": effort,
        "codekg_url": CODEKG_MCP_URL,
        "codekg_enabled_tools": sorted(ALLOWED_CODEKG_TOOLS),
        "mcp_config_path": str(MCP_CONFIG_PATH.resolve()),
    }


def repository_states(corpus_root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    corpus_root = corpus_root.resolve()
    states: dict[str, dict[str, Any]] = {}
    checked_git_roots: set[Path] = set()
    for config in manifest["repositories"]:
        repository = (corpus_root / config["source_path"]).resolve()
        try:
            repository.relative_to(corpus_root)
        except ValueError as exc:
            raise RuntimeError(
                f"repository source_path escapes corpus root: {config['source_path']}"
            ) from exc
        if not repository.is_dir():
            raise RuntimeError(f"repository path does not exist: {repository}")
        git_root = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
        git_commit = _git(git_root, "rev-parse", "HEAD")
        if git_commit != config["git_commit"]:
            raise RuntimeError(
                f"{config['name']} Git commit mismatch: "
                f"expected {config['git_commit']}, got {git_commit}"
            )
        if git_root not in checked_git_roots:
            checked_git_roots.add(git_root)
            if _git(git_root, "status", "--porcelain"):
                raise RuntimeError(
                    f"checkout is dirty; benchmark requires a frozen corpus: {git_root}"
                )
        indexed_commit = repository_snapshot_commit(repository, config["identity"])
        if not indexed_commit.startswith(config["indexed_commit"]):
            raise RuntimeError(
                f"{config['name']} indexed identity mismatch: "
                f"expected {config['indexed_commit']}, got {indexed_commit}"
            )
        states[config["name"]] = {
            "repository_root": str(repository),
            "git_root": str(git_root),
            "git_commit": git_commit,
            "identity": config["identity"],
            "indexed_commit": config["indexed_commit"],
        }
    return states


def local_preflight(
    *, corpus_root: Path, manifest_path: Path, claude: str, model: str, effort: str
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    repositories = repository_states(corpus_root, manifest)
    if not MCP_CONFIG_PATH.is_file():
        raise RuntimeError(f"CodeKG MCP config does not exist: {MCP_CONFIG_PATH}")
    contract = runner_contract(model=model, effort=effort)
    expected_files = [resolve_manifest_file(manifest_path, manifest["schema_file"])]
    for task in manifest["tasks"]:
        expected_files.append(resolve_manifest_file(manifest_path, task["gold_file"]))
        expected_files.extend(
            resolve_manifest_file(manifest_path, relative)
            for relative in task["prompt_files"].values()
        )
    missing = [str(path) for path in expected_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"benchmark package is incomplete: {missing}")
    return {
        "corpus_root": str(corpus_root.resolve()),
        "repositories": repositories,
        "runner_contract": contract,
        "claude_version": claude_version(claude),
        "claude_auth": claude_auth_status(claude),
        "hashes": snapshot_hashes(manifest_path, MCP_CONFIG_PATH),
    }


def assert_frozen_state(
    *,
    corpus_root: Path,
    manifest_path: Path,
    expected_hashes: dict[str, str],
) -> None:
    manifest = load_manifest(manifest_path)
    repository_states(corpus_root, manifest)
    observed_hashes = snapshot_hashes(manifest_path, MCP_CONFIG_PATH)
    if observed_hashes != expected_hashes:
        changed = sorted(
            path
            for path in set(expected_hashes) | set(observed_hashes)
            if expected_hashes.get(path) != observed_hashes.get(path)
        )
        raise RuntimeError(f"frozen benchmark inputs drifted: {changed}")


def base_claude_command(
    *,
    claude: str,
    contract: dict[str, Any],
    arm: str,
    json_schema: str | None = None,
) -> list[str]:
    command = [
        claude,
        "-p",
        "--model",
        contract["model"],
        "--effort",
        contract["effort"],
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        JSON_ONLY_SYSTEM_PROMPT,
    ]
    if json_schema is not None:
        command.extend(["--json-schema", json_schema])
    if arm == "codekg":
        command.extend(
            [
                "--mcp-config",
                str(MCP_CONFIG_PATH.resolve()),
                "--tools",
                "",
                "--allowedTools",
                "mcp__codekg__search_symbols",
                "mcp__codekg__get_definition",
                "mcp__codekg__find_callers",
                "mcp__codekg__find_callees",
            ]
        )
    elif arm == "native":
        command.extend(["--tools", "Bash", "--allowedTools", "Bash"])
    else:
        raise ValueError(f"unknown arm: {arm}")
    return command


def trial_command(*, arm: str, claude: str, contract: dict[str, Any], json_schema: str) -> list[str]:
    return base_claude_command(claude=claude, contract=contract, arm=arm, json_schema=json_schema)


def graph_preflight_command(*, claude: str, contract: dict[str, Any]) -> list[str]:
    return base_claude_command(claude=claude, contract=contract, arm="codekg")


def graph_preflight_prompt(gold: dict[str, Any]) -> str:
    target = gold["primary"]
    return (
        "Preflight the indexed graph using exactly these calls in order: "
        f"search_symbols(repository='{gold['repository']}', scope='source', "
        f"limit=1) for {target['symbol']}; get_definition for its "
        "returned exact symbol ID; then find_callers with limit=5 and find_callees "
        "with limit=5 for that same ID. Do not use shell, web, or other tools. "
        "Return a terse structural status only."
    )


def _launch(
    *,
    command: list[str],
    prompt: str,
    cwd: Path,
    events_path: Path,
    stderr_path: Path,
    timeout: float,
    answer_path: Path | None = None,
) -> tuple[int, float]:
    events_path.parent.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    with (
        events_path.open("x", encoding="utf-8") as stdout,
        stderr_path.open("x", encoding="utf-8") as stderr,
    ):
        try:
            process = subprocess.run(
                command,
                input=prompt,
                cwd=str(cwd.resolve()),
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
                timeout=timeout,
            )
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
    finalize_claude_trial(events_path, answer_path)
    return exit_code, time.perf_counter() - started


def _launch_with_frozen_state(
    *,
    command: list[str],
    prompt: str,
    cwd: Path,
    events_path: Path,
    stderr_path: Path,
    timeout: float,
    frozen_state_arguments: dict[str, Any],
    answer_path: Path | None = None,
) -> tuple[int, float]:
    assert_frozen_state(**frozen_state_arguments)
    try:
        return _launch(
            command=command,
            prompt=prompt,
            cwd=cwd,
            events_path=events_path,
            stderr_path=stderr_path,
            timeout=timeout,
            answer_path=answer_path,
        )
    finally:
        assert_frozen_state(**frozen_state_arguments)


def _direct_repository_preflight(
    *,
    url: str,
    events_path: Path,
    stderr_path: Path,
    frozen_state_arguments: dict[str, Any],
) -> tuple[int, float]:
    """Call repository discovery directly so deferred-tool routing cannot hide it."""
    assert_frozen_state(**frozen_state_arguments)
    events_path.parent.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    exit_code = 0
    item: dict[str, Any]
    error_text = ""
    try:
        from fastmcp import Client

        async def call() -> Any:
            async with Client(url) as client:
                return await client.call_tool("list_repositories", {})

        result = asyncio.run(call())
        item = {
            "id": "repository-preflight",
            "type": "mcp_tool_call",
            "server": "codekg",
            "tool": "list_repositories",
            "arguments": {},
            "result": {
                "content": [
                    value.model_dump(mode="json", exclude_none=True) for value in result.content
                ],
                "structured_content": result.structured_content,
            },
            "error": None,
            "status": "completed",
        }
    except Exception as exc:  # pragma: no cover - exercised only by live infrastructure
        exit_code = 1
        error_text = f"{type(exc).__name__}: {exc}\n"
        item = {
            "id": "repository-preflight",
            "type": "mcp_tool_call",
            "server": "codekg",
            "tool": "list_repositories",
            "arguments": {},
            "result": None,
            "error": error_text.strip(),
            "status": "failed",
        }
    events = [
        {"type": "thread.started", "thread_id": "direct-mcp-repository-preflight"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": item},
        {"type": "turn.completed", "usage": {}},
    ]
    events_path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    stderr_path.write_text(error_text, encoding="utf-8")
    assert_frozen_state(**frozen_state_arguments)
    return exit_code, time.perf_counter() - started


def validate_preflight(
    events_path: Path, expected_repositories: dict[str, str], exit_code: int
) -> list[str]:
    errors: list[str] = []
    if exit_code:
        errors.append(f"Codex preflight exited with {exit_code}")
    events, parse_errors = read_events(events_path)
    errors.extend(parse_errors)
    tools = [item for item in completed_items(events) if item.get("type") == "mcp_tool_call"]
    if len(tools) != 1 or tools[0].get("tool") != "list_repositories":
        errors.append("preflight must call list_repositories exactly once")
        return errors
    item = tools[0]
    if item.get("status") != "completed" or item.get("error"):
        errors.append("list_repositories did not complete successfully")
        return errors
    payload = structured_content(item)
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        rows = [row for row in payload["result"] if isinstance(row, dict)]
    for repository, expected_commit in expected_repositories.items():
        matching = [row for row in rows if row.get("repo_name") == repository]
        if len(matching) != 1:
            errors.append(f"preflight did not find exactly one indexed {repository} repository")
            continue
        indexed = matching[0].get("commit")
        if not isinstance(indexed, str) or indexed != expected_commit:
            errors.append(
                f"indexed {repository} commit mismatch: expected {expected_commit}, got {indexed}"
            )
    return errors


def validate_graph_preflight(events_path: Path, gold: dict[str, Any], exit_code: int) -> list[str]:
    errors: list[str] = []
    if exit_code:
        errors.append(f"Codex graph preflight exited with {exit_code}")
    events, parse_errors = read_events(events_path)
    errors.extend(parse_errors)
    protocol_errors, _ = validate_codekg_protocol(events, gold["repository"])
    errors.extend(protocol_errors)
    by_tool = {str(item.get("tool")): item for item in successful_mcp_items(events)}
    for tool in ALLOWED_CODEKG_TOOLS:
        if tool not in by_tool:
            errors.append(f"graph preflight did not successfully complete {tool}")

    target = gold["primary"]
    definition_rows = result_rows(by_tool.get("get_definition", {}))
    observed_definitions = {
        (
            row.get("file"),
            row.get("start_line"),
            row.get("end_line"),
        )
        for row in definition_rows
    }
    record = (target["file"], target["start_line"], target["end_line"])
    if record not in observed_definitions:
        errors.append(f"graph preflight did not return expected target definition range: {record}")
    for relationship, tool in (("caller", "find_callers"), ("callee", "find_callees")):
        expected = next(item for item in gold["relevant"] if item["relationship"] == relationship)
        expected_record = (expected["file"], expected["start_line"], expected["end_line"])
        observed_edges = {
            (row.get("file"), row.get("start_line"), row.get("end_line"))
            for row in result_rows(by_tool.get(tool, {}))
        }
        if expected_record not in observed_edges:
            errors.append(
                f"graph preflight is missing {relationship} edge for {expected['symbol']}"
            )
    return errors


def _frozen_schedule(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": entry.ordinal,
            "trial_name": entry.trial_name,
            "task_index": entry.task_index,
            "task_id": entry.task_id,
            "slug": entry.slug,
            "repository": entry.repository,
            "arm": entry.arm,
        }
        for entry in suite_schedule(
            tasks=manifest["tasks"],
            arms=manifest["arms"],
            seed=manifest["seed"],
        )
    ]


def execute(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    corpus_root = args.corpus_root.resolve()
    model = resolve_model()
    effort = resolve_effort()
    local = local_preflight(
        corpus_root=corpus_root,
        manifest_path=manifest_path,
        claude=args.claude,
        model=model,
        effort=effort,
    )
    repository_paths = {
        name: Path(state["repository_root"]) for name, state in local["repositories"].items()
    }
    tasks = {int(task["index"]): task for task in manifest["tasks"]}
    schedule_entries = suite_schedule(
        tasks=manifest["tasks"],
        arms=manifest["arms"],
        seed=manifest["seed"],
    )
    schedule = _frozen_schedule(manifest)
    if args.command == "plan":
        print(
            json.dumps(
                {
                    "local_preflight": local,
                    "schedule": schedule,
                    "codekg_tools": sorted(ALLOWED_CODEKG_TOOLS),
                    "note": "No Claude Code process was launched.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.execute:
        raise RuntimeError("refusing to launch paid trials without --execute")
    run_dir = args.output.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json_new(
        run_dir / "batch.json",
        {
            "manifest": manifest,
            "local_preflight": local,
            "schedule": schedule,
        },
    )
    frozen_state_arguments = {
        "corpus_root": corpus_root,
        "manifest_path": manifest_path,
        "expected_hashes": local["hashes"],
    }

    first_task = manifest["tasks"][0]
    first_repository = repository_paths[first_task["repository"]]
    preflight_dir = run_dir / "preflight"
    repository_preflight_dir = preflight_dir / "repository"
    preflight_events = repository_preflight_dir / "events.jsonl"
    preflight_exit, preflight_wall = _direct_repository_preflight(
        url=local["runner_contract"]["codekg_url"],
        events_path=preflight_events,
        stderr_path=repository_preflight_dir / "stderr.log",
        frozen_state_arguments=frozen_state_arguments,
    )
    expected_repositories = {
        config["name"]: config["indexed_commit"] for config in manifest["repositories"]
    }
    preflight_errors = validate_preflight(
        preflight_events,
        expected_repositories,
        preflight_exit,
    )
    write_json_new(
        repository_preflight_dir / "result.json",
        {
            "exit_code": preflight_exit,
            "wall_seconds": preflight_wall,
            "valid": not preflight_errors,
            "errors": preflight_errors,
        },
    )
    if preflight_errors:
        print("batch preflight failed; measured trials were not started", file=sys.stderr)
        return 2

    graph_gold_path = resolve_manifest_file(manifest_path, first_task["gold_file"])
    graph_gold = read_json(graph_gold_path)
    graph_preflight_dir = preflight_dir / "graph"
    graph_events = graph_preflight_dir / "events.jsonl"
    graph_exit, graph_wall = _launch_with_frozen_state(
        command=graph_preflight_command(claude=args.claude, contract=local["runner_contract"]),
        prompt=graph_preflight_prompt(graph_gold),
        cwd=first_repository,
        events_path=graph_events,
        stderr_path=graph_preflight_dir / "stderr.log",
        timeout=args.timeout,
        frozen_state_arguments=frozen_state_arguments,
    )
    graph_errors = validate_graph_preflight(graph_events, graph_gold, graph_exit)
    write_json_new(
        graph_preflight_dir / "result.json",
        {
            "exit_code": graph_exit,
            "wall_seconds": graph_wall,
            "valid": not graph_errors,
            "errors": graph_errors,
        },
    )
    write_json_new(
        preflight_dir / "result.json",
        {
            "valid": not preflight_errors and not graph_errors,
            "repository_errors": preflight_errors,
            "graph_errors": graph_errors,
        },
    )
    if graph_errors:
        print("graph preflight failed; measured trials were not started", file=sys.stderr)
        return 2

    schema_path = resolve_manifest_file(manifest_path, manifest["schema_file"])
    json_schema = json.dumps(read_json(schema_path), separators=(",", ":"))
    for entry in schedule_entries:
        task = tasks[entry.task_index]
        repository = repository_paths[entry.repository]
        trial_dir = run_dir / "tasks" / entry.trial_name / entry.arm
        answer_path = trial_dir / "answer.json"
        prompt_path = resolve_manifest_file(manifest_path, task["prompt_files"][entry.arm])
        gold_path = resolve_manifest_file(manifest_path, task["gold_file"])
        command = trial_command(
            arm=entry.arm, claude=args.claude, contract=local["runner_contract"], json_schema=json_schema
        )
        exit_code, wall_seconds = _launch_with_frozen_state(
            command=command,
            prompt=prompt_path.read_text(encoding="utf-8"),
            cwd=repository,
            events_path=trial_dir / "events.jsonl",
            stderr_path=trial_dir / "stderr.log",
            timeout=args.timeout,
            frozen_state_arguments=frozen_state_arguments,
            answer_path=answer_path,
        )
        metadata = {
            "ordinal": entry.ordinal,
            "task_index": entry.task_index,
            "task_id": entry.task_id,
            "slug": entry.slug,
            "repository": entry.repository,
            "arm": entry.arm,
            "exit_code": exit_code,
            "wall_seconds": wall_seconds,
            "command": command,
            "runner": "claude-code",
            "runner_version": local["claude_version"],
            "model": model,
            "effort": effort,
            "prompt_sha256": local["hashes"][str(prompt_path.resolve())],
        }
        write_json_new(trial_dir / "metadata.json", metadata)
        validation, metrics = validate_trial(
            arm=entry.arm,
            events_path=trial_dir / "events.jsonl",
            answer_path=answer_path,
            repository=repository,
            gold_path=gold_path,
            wall_seconds=wall_seconds,
            exit_code=exit_code,
        )
        write_json_new(trial_dir / "validation.json", validation)
        write_json_new(trial_dir / "metrics.json", metrics)
    print(run_dir)
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--corpus-root", type=Path, required=True)
        command.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
        command.add_argument("--claude", default="claude")
        command.add_argument("--timeout", type=float, default=300.0)
        command.add_argument(
            "--output",
            type=Path,
            default=Path("runs/codekg-native-intent-suite"),
        )
        command.add_argument(
            "--execute",
            action="store_true",
            help="required for run; acknowledges that Claude Code calls may incur charges",
        )
    return value


def main() -> None:
    try:
        raise SystemExit(execute(parser().parse_args()))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
