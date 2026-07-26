#!/usr/bin/env python3
"""Run the paired Requests CodeKG/native benchmark.

The default ``plan`` command is free and side-effect-light. ``run`` refuses to
launch Codex unless ``--execute`` is supplied; a complete run performs one
batch preflight, one warm-up per arm, and ten measured repetitions per arm.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from benchmark_lib import (
    ALLOWED_CODEKG_TOOLS,
    MANIFEST_PATH,
    balanced_schedule,
    completed_items,
    load_manifest,
    read_events,
    read_json,
    resolve_manifest_file,
    result_rows,
    snapshot_hashes,
    structured_content,
    successful_mcp_items,
    validate_codekg_protocol,
    validate_trial,
    write_json_new,
)


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


def profile_contract(profile_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    with profile_path.open("rb") as handle:
        profile = tomllib.load(handle)
    codekg = profile.get("mcp_servers", {}).get("codekg", {})
    if profile.get("model") != manifest["model"]:
        raise RuntimeError("benchmark profile model does not match the manifest")
    if profile.get("model_reasoning_effort") != manifest["model_reasoning_effort"]:
        raise RuntimeError("benchmark profile reasoning effort does not match the manifest")
    if set(codekg.get("enabled_tools", [])) != ALLOWED_CODEKG_TOOLS:
        raise RuntimeError("benchmark profile must expose exactly the four CodeKG tools")
    if codekg.get("enabled") is not True or codekg.get("required") is not True:
        raise RuntimeError("benchmark profile must enable and require CodeKG")
    if codekg.get("url") != "http://127.0.0.1:8765/mcp":
        raise RuntimeError("benchmark profile must use the frozen loopback CodeKG URL")
    if codekg.get("startup_timeout_sec") != 15:
        raise RuntimeError("benchmark profile must use the frozen CodeKG startup timeout")
    if codekg.get("tool_timeout_sec") != 30:
        raise RuntimeError("benchmark profile must use the frozen CodeKG tool timeout")
    if profile.get("web_search") != "disabled":
        raise RuntimeError("benchmark profile must disable web search")
    if profile.get("features", {}).get("multi_agent") is not False:
        raise RuntimeError("benchmark profile must disable multi-agent execution")
    return {
        "model": profile["model"],
        "model_reasoning_effort": profile["model_reasoning_effort"],
        "web_search": profile["web_search"],
        "multi_agent": profile["features"]["multi_agent"],
        "codekg_enabled": codekg["enabled"],
        "codekg_required": codekg["required"],
        "codekg_url": codekg["url"],
        "codekg_startup_timeout_sec": codekg["startup_timeout_sec"],
        "codekg_tool_timeout_sec": codekg["tool_timeout_sec"],
        "codekg_enabled_tools": codekg["enabled_tools"],
    }


def codex_version(codex: str) -> str:
    process = subprocess.run(
        [codex, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode or not process.stdout.strip():
        raise RuntimeError(process.stderr.strip() or "cannot determine Codex version")
    return process.stdout.strip()


def expected_profile_path() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    codex_root = Path(configured_root) if configured_root else Path.home() / ".codex"
    return (codex_root / "benchmark.config.toml").resolve()


def enforce_profile_path(profile_path: Path) -> Path:
    resolved = profile_path.resolve()
    expected = expected_profile_path()
    if resolved != expected:
        raise RuntimeError(
            "--profile-path must identify the file loaded by '--profile benchmark': "
            f"expected {expected}, got {resolved}"
        )
    return resolved


def enforce_manifest_profile(manifest: dict[str, Any]) -> None:
    if manifest.get("profile") != "benchmark":
        raise RuntimeError("the frozen Requests runner requires manifest profile='benchmark'")


def local_preflight(
    *, repository: Path, manifest_path: Path, profile_path: Path, codex: str
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    enforce_manifest_profile(manifest)
    profile_path = enforce_profile_path(profile_path)
    root = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository.resolve():
        raise RuntimeError(f"Requests path is not its Git root: {repository} != {root}")
    commit = _git(repository, "rev-parse", "HEAD")
    if commit != manifest["repository_commit"]:
        raise RuntimeError(
            f"Requests commit mismatch: expected {manifest['repository_commit']}, got {commit}"
        )
    if _git(repository, "status", "--porcelain"):
        raise RuntimeError("Requests checkout is dirty; benchmark requires the frozen checkout")
    if not profile_path.is_file():
        raise RuntimeError(f"benchmark profile does not exist: {profile_path}")
    contract = profile_contract(profile_path, manifest)
    expected_files = [
        resolve_manifest_file(manifest_path, manifest["schema_file"]),
        resolve_manifest_file(manifest_path, manifest["gold_file"]),
        *(
            resolve_manifest_file(manifest_path, relative)
            for relative in manifest["prompt_files"].values()
        ),
    ]
    missing = [str(path) for path in expected_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"benchmark package is incomplete: {missing}")
    return {
        "repository_root": str(root),
        "repository_commit": commit,
        "profile_path": str(profile_path.resolve()),
        "profile_contract": contract,
        "codex_version": codex_version(codex),
        "hashes": snapshot_hashes(manifest_path, profile_path),
    }


def assert_frozen_state(
    *,
    repository: Path,
    manifest_path: Path,
    profile_path: Path,
    expected_hashes: dict[str, str],
) -> None:
    manifest = load_manifest(manifest_path)
    root = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository.resolve():
        raise RuntimeError(f"Requests path is no longer its Git root: {repository} != {root}")
    commit = _git(repository, "rev-parse", "HEAD")
    if commit != manifest["repository_commit"]:
        raise RuntimeError(
            "Requests commit drifted during the benchmark: "
            f"expected {manifest['repository_commit']}, got {commit}"
        )
    dirty = _git(repository, "status", "--porcelain")
    if dirty:
        raise RuntimeError(
            "Requests checkout was modified during the benchmark; aborting before another run"
        )
    observed_hashes = snapshot_hashes(manifest_path, profile_path)
    if observed_hashes != expected_hashes:
        changed = sorted(
            path
            for path in set(expected_hashes) | set(observed_hashes)
            if expected_hashes.get(path) != observed_hashes.get(path)
        )
        raise RuntimeError(f"frozen benchmark inputs drifted: {changed}")


def base_codex_command(
    *,
    codex: str,
    profile: str,
    contract: dict[str, Any],
    repository: Path,
    schema_path: Path | None,
    answer_path: Path | None,
) -> list[str]:
    command = [
        codex,
        "--ask-for-approval",
        "never",
        "exec",
        "--profile",
        profile,
        "--ignore-user-config",
        "--strict-config",
        "--ephemeral",
        "--json",
        "--sandbox",
        "danger-full-access",
        "--ignore-rules",
        "--cd",
        str(repository.resolve()),
        "-c",
        f"model={json.dumps(contract['model'])}",
        "-c",
        f"model_reasoning_effort={json.dumps(contract['model_reasoning_effort'])}",
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.plugins=false",
        "-c",
        'web_search="disabled"',
        "-c",
        f"mcp_servers.codekg.url={json.dumps(contract['codekg_url'])}",
        "-c",
        (f"mcp_servers.codekg.startup_timeout_sec={contract['codekg_startup_timeout_sec']}"),
        "-c",
        f"mcp_servers.codekg.tool_timeout_sec={contract['codekg_tool_timeout_sec']}",
    ]
    if schema_path is not None:
        command.extend(["--output-schema", str(schema_path.resolve())])
    if answer_path is not None:
        command.extend(["-o", str(answer_path.resolve())])
    return command


def trial_command(
    *,
    arm: str,
    codex: str,
    profile: str,
    contract: dict[str, Any],
    repository: Path,
    schema_path: Path,
    answer_path: Path,
) -> list[str]:
    command = base_codex_command(
        codex=codex,
        profile=profile,
        contract=contract,
        repository=repository,
        schema_path=schema_path,
        answer_path=answer_path,
    )
    if arm == "codekg":
        command.extend(
            [
                "-c",
                "mcp_servers.codekg.enabled=true",
                "-c",
                "mcp_servers.codekg.required=true",
                "-c",
                'mcp_servers.codekg.enabled_tools=["search_symbols","get_definition",'
                '"find_callers","find_callees"]',
            ]
        )
    elif arm == "native":
        command.extend(
            [
                "-c",
                "mcp_servers.codekg.enabled=false",
                "-c",
                "mcp_servers.codekg.required=false",
            ]
        )
    else:
        raise ValueError(f"unknown arm: {arm}")
    return command


def preflight_command(
    *, codex: str, profile: str, contract: dict[str, Any], repository: Path
) -> list[str]:
    command = base_codex_command(
        codex=codex,
        profile=profile,
        contract=contract,
        repository=repository,
        schema_path=None,
        answer_path=None,
    )
    command.extend(
        [
            "-c",
            "mcp_servers.codekg.enabled=true",
            "-c",
            "mcp_servers.codekg.required=true",
            "-c",
            'mcp_servers.codekg.enabled_tools=["list_repositories"]',
        ]
    )
    return command


def graph_preflight_command(
    *, codex: str, profile: str, contract: dict[str, Any], repository: Path
) -> list[str]:
    command = base_codex_command(
        codex=codex,
        profile=profile,
        contract=contract,
        repository=repository,
        schema_path=None,
        answer_path=None,
    )
    command.extend(
        [
            "-c",
            "mcp_servers.codekg.enabled=true",
            "-c",
            "mcp_servers.codekg.required=true",
            "-c",
            'mcp_servers.codekg.enabled_tools=["search_symbols","get_definition",'
            '"find_callers","find_callees"]',
        ]
    )
    return command


def _launch(
    *,
    command: list[str],
    prompt: str,
    events_path: Path,
    stderr_path: Path,
    timeout: float,
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
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
                timeout=timeout,
            )
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
    return exit_code, time.perf_counter() - started


def _launch_with_frozen_state(
    *,
    command: list[str],
    prompt: str,
    events_path: Path,
    stderr_path: Path,
    timeout: float,
    frozen_state_arguments: dict[str, Any],
) -> tuple[int, float]:
    assert_frozen_state(**frozen_state_arguments)
    try:
        return _launch(
            command=command,
            prompt=prompt,
            events_path=events_path,
            stderr_path=stderr_path,
            timeout=timeout,
        )
    finally:
        assert_frozen_state(**frozen_state_arguments)


def validate_preflight(events_path: Path, expected_commit: str, exit_code: int) -> list[str]:
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
    requests_rows = [row for row in rows if row.get("repo_name") == "requests"]
    if len(requests_rows) != 1:
        errors.append("preflight did not find exactly one indexed requests repository")
    else:
        indexed = requests_rows[0].get("commit")
        if (
            not isinstance(indexed, str)
            or len(indexed) < 12
            or not expected_commit.startswith(indexed)
        ):
            errors.append(
                f"indexed Requests commit mismatch: expected {expected_commit}, got {indexed}"
            )
    return errors


def validate_graph_preflight(events_path: Path, gold: dict[str, Any], exit_code: int) -> list[str]:
    errors: list[str] = []
    if exit_code:
        errors.append(f"Codex graph preflight exited with {exit_code}")
    events, parse_errors = read_events(events_path)
    errors.extend(parse_errors)
    protocol_errors, _ = validate_codekg_protocol(events)
    errors.extend(protocol_errors)
    by_tool = {str(item.get("tool")): item for item in successful_mcp_items(events)}
    expected = [
        gold["primary"],
        *gold["relevant"],
    ]
    observed = {
        (
            row.get("file"),
            row.get("start_line"),
            row.get("end_line"),
        )
        for item in by_tool.values()
        for row in result_rows(item)
    }
    for symbol in expected:
        record = (symbol["file"], symbol["start_line"], symbol["end_line"])
        if record not in observed:
            errors.append(f"graph preflight did not return expected definition range: {record}")
    caller_rows = result_rows(by_tool.get("find_callers", {}))
    callee_rows = result_rows(by_tool.get("find_callees", {}))
    caller = next(item for item in gold["relevant"] if item["relationship"] == "caller")
    callee = next(item for item in gold["relevant"] if item["relationship"] == "callee")
    if (caller["file"], caller["start_line"], caller["end_line"]) not in {
        (row.get("file"), row.get("start_line"), row.get("end_line")) for row in caller_rows
    }:
        errors.append("graph preflight is missing Session.request caller edge")
    if (callee["file"], callee["start_line"], callee["end_line"]) not in {
        (row.get("file"), row.get("start_line"), row.get("end_line")) for row in callee_rows
    }:
        errors.append("graph preflight is missing PreparedRequest.prepare callee edge")
    return errors


def _frozen_schedule(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": entry.ordinal,
            "trial_name": entry.trial_name,
            "arm": entry.arm,
            "repetition": entry.repetition,
            "warmup": entry.warmup,
        }
        for entry in balanced_schedule(
            arms=manifest["arms"],
            repetitions=manifest["measured_repetitions"],
            seed=manifest["seed"],
        )
    ]


def execute(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    repository = args.requests.resolve()
    local = local_preflight(
        repository=repository,
        manifest_path=manifest_path,
        profile_path=args.profile_path.resolve(),
        codex=args.codex,
    )
    schedule = _frozen_schedule(manifest)
    if args.command == "plan":
        print(
            json.dumps(
                {
                    "local_preflight": local,
                    "schedule": schedule,
                    "codekg_tools": sorted(ALLOWED_CODEKG_TOOLS),
                    "note": "No Codex process was launched.",
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
        "repository": repository,
        "manifest_path": manifest_path,
        "profile_path": Path(local["profile_path"]),
        "expected_hashes": local["hashes"],
    }

    preflight_dir = run_dir / "preflight"
    repository_preflight_dir = preflight_dir / "repository"
    preflight_events = repository_preflight_dir / "events.jsonl"
    preflight_exit, preflight_wall = _launch_with_frozen_state(
        command=preflight_command(
            codex=args.codex,
            profile=manifest["profile"],
            contract=local["profile_contract"],
            repository=repository,
        ),
        prompt=(
            "Call codekg.list_repositories exactly once. Return only whether repository "
            "'requests' is indexed. Do not call any other tool."
        ),
        events_path=preflight_events,
        stderr_path=repository_preflight_dir / "stderr.log",
        timeout=args.timeout,
        frozen_state_arguments=frozen_state_arguments,
    )
    preflight_errors = validate_preflight(
        preflight_events, manifest["repository_commit"], preflight_exit
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

    schema_path = resolve_manifest_file(manifest_path, manifest["schema_file"])
    gold_path = resolve_manifest_file(manifest_path, manifest["gold_file"])
    gold = read_json(gold_path)
    graph_preflight_dir = preflight_dir / "graph"
    graph_events = graph_preflight_dir / "events.jsonl"
    graph_exit, graph_wall = _launch_with_frozen_state(
        command=graph_preflight_command(
            codex=args.codex,
            profile=manifest["profile"],
            contract=local["profile_contract"],
            repository=repository,
        ),
        prompt=(
            "Preflight the indexed Requests graph using exactly these calls in order: "
            "search_symbols(repository='requests', scope='source', limit=1) for "
            "Session.prepare_request; get_definition for its returned exact symbol ID; "
            "then both find_callers and find_callees for that same ID. Do not use shell, "
            "web, or other tools. Return a terse structural status only."
        ),
        events_path=graph_events,
        stderr_path=graph_preflight_dir / "stderr.log",
        timeout=args.timeout,
        frozen_state_arguments=frozen_state_arguments,
    )
    graph_errors = validate_graph_preflight(graph_events, gold, graph_exit)
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

    for entry in balanced_schedule(
        arms=manifest["arms"],
        repetitions=manifest["measured_repetitions"],
        seed=manifest["seed"],
    ):
        trial_dir = run_dir / "trials" / f"{entry.ordinal:02d}-{entry.trial_name}"
        answer_path = trial_dir / "answer.json"
        prompt_path = resolve_manifest_file(manifest_path, manifest["prompt_files"][entry.arm])
        command = trial_command(
            arm=entry.arm,
            codex=args.codex,
            profile=manifest["profile"],
            contract=local["profile_contract"],
            repository=repository,
            schema_path=schema_path,
            answer_path=answer_path,
        )
        exit_code, wall_seconds = _launch_with_frozen_state(
            command=command,
            prompt=prompt_path.read_text(encoding="utf-8"),
            events_path=trial_dir / "events.jsonl",
            stderr_path=trial_dir / "stderr.log",
            timeout=args.timeout,
            frozen_state_arguments=frozen_state_arguments,
        )
        metadata = {
            "ordinal": entry.ordinal,
            "arm": entry.arm,
            "repetition": entry.repetition,
            "warmup": entry.warmup,
            "exit_code": exit_code,
            "wall_seconds": wall_seconds,
            "command": command,
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
        command.add_argument("--requests", type=Path, required=True)
        command.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
        command.add_argument(
            "--profile-path",
            type=Path,
            default=expected_profile_path(),
        )
        command.add_argument("--codex", default="codex")
        command.add_argument("--timeout", type=float, default=300.0)
        command.add_argument("--output", type=Path, default=Path("runs/requests-benchmark"))
        command.add_argument(
            "--execute",
            action="store_true",
            help="required for run; acknowledges that Codex calls may incur charges",
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
