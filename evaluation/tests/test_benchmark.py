from __future__ import annotations

# ruff: noqa: E402, I001

import json
import sys
from pathlib import Path

EVALUATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALUATION_DIR))

import benchmark_lib  # noqa: E402
import aggregate_benchmark  # noqa: E402
import run_benchmark  # noqa: E402


PRIMARY_ID = (
    "requests@f361ead047be:src/requests/sessions.py:"
    "src.requests.sessions.Session.prepare_request:511"
)
CALLER_ID = (
    "requests@f361ead047be:src/requests/sessions.py:src.requests.sessions.Session.request:557"
)


def _tool(tool: str, arguments: dict[str, object], rows: list[dict[str, object]]) -> dict:
    key = "results" if tool == "search_symbols" else "result"
    return {
        "type": "item.completed",
        "item": {
            "id": tool,
            "type": "mcp_tool_call",
            "server": "codekg",
            "tool": tool,
            "arguments": arguments,
            "result": {
                "content": [{"type": "text", "text": "ok"}],
                "structured_content": {key: rows},
            },
            "error": None,
            "status": "completed",
        },
    }


def _answer() -> dict:
    return {
        "task_id": "requests-intent-001",
        "answer": "Structurally, Session.prepare_request is the primary boundary.",
        "symbols": [PRIMARY_ID, CALLER_ID],
        "files": ["src/requests/sessions.py"],
        "evidence": [
            {
                "symbol": PRIMARY_ID,
                "file": "src/requests/sessions.py",
                "start_line": 511,
                "end_line": 555,
            },
            {
                "symbol": CALLER_ID,
                "file": "src/requests/sessions.py",
                "start_line": 557,
                "end_line": 653,
            },
        ],
        "confidence": 0.9,
    }


def _events(answer: dict | None = None) -> list[dict]:
    answer = answer or _answer()
    primary = {
        "key": PRIMARY_ID,
        "symbol_id": PRIMARY_ID,
        "qname": "src.requests.sessions.Session.prepare_request",
        "file": "src/requests/sessions.py",
        "start_line": 511,
        "end_line": 555,
    }
    caller = {
        "key": CALLER_ID,
        "qname": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    return [
        {"type": "thread.started", "thread_id": "test"},
        {"type": "turn.started"},
        _tool(
            "search_symbols",
            {"repository": "requests", "scope": "source", "limit": 5, "query": "prepare"},
            [primary],
        ),
        _tool("get_definition", {"identifier": PRIMARY_ID}, [primary]),
        _tool("find_callers", {"identifier": PRIMARY_ID}, [caller]),
        _tool("find_callees", {"identifier": PRIMARY_ID}, []),
        {
            "type": "item.completed",
            "item": {
                "id": "final",
                "type": "agent_message",
                "text": json.dumps(answer, separators=(",", ":")),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        },
    ]


def test_balanced_schedule_is_deterministic_and_paired() -> None:
    first = benchmark_lib.balanced_schedule(arms=["codekg", "native"], repetitions=10, seed=123)
    second = benchmark_lib.balanced_schedule(arms=["codekg", "native"], repetitions=10, seed=123)
    assert first == second
    assert len(first) == 22
    assert sum(entry.warmup for entry in first) == 2
    measured = [entry for entry in first if not entry.warmup]
    assert sum(entry.arm == "codekg" for entry in measured) == 10
    assert sum(entry.arm == "native" for entry in measured) == 10
    assert sum(measured[index].arm == "codekg" for index in range(0, len(measured), 2)) == 5


def test_codekg_prompt_matches_measured_tool_contract() -> None:
    prompt = (EVALUATION_DIR / "prompts" / "requests-intent-001-codekg.txt").read_text(
        encoding="utf-8"
    )
    expected_tools = "`search_symbols`, `get_definition`, `find_callers`, and `find_callees`"
    expected_sequence = (
        "`search_symbols`, `get_definition`, then both `find_callers` and `find_callees`"
    )

    assert "Batch preflight has already verified" in prompt
    assert f"exposes exactly these four tools: {expected_tools}" in prompt
    assert f"in this sequence: {expected_sequence}" in prompt
    assert "The measured task begins with `search_symbols`" in prompt
    assert "Do not call `list_repositories`" in prompt
    assert "Use `list_repositories`" not in prompt


def test_schema_validation_rejects_null_and_unsafe_paths() -> None:
    answer = _answer()
    answer["files"] = ["/tmp/source.py", "../source.py", r"src\source.py"]
    answer["evidence"][0]["start_line"] = None
    errors = benchmark_lib.validate_answer_schema(answer)
    assert any("relative POSIX path" in error for error in errors)
    assert any("positive integer" in error for error in errors)


def test_schema_validation_rejects_empty_evidence_symbol() -> None:
    answer = _answer()
    answer["symbols"][0] = ""
    answer["evidence"][0]["symbol"] = ""
    errors = benchmark_lib.validate_answer_schema(answer)
    assert any("nonempty string" in error for error in errors)


def test_model_facing_schema_omits_constraints_enforced_after_generation() -> None:
    schema = json.loads((EVALUATION_DIR / "codex-answer.schema.json").read_text())

    def mappings(value):
        if isinstance(value, dict):
            yield value
            for nested in value.values():
                yield from mappings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from mappings(nested)

    for mapping in mappings(schema):
        assert "uniqueItems" not in mapping
        assert "pattern" not in mapping


def test_schema_validation_requires_summary_sets_to_match_evidence() -> None:
    answer = _answer()
    answer["symbols"] = [PRIMARY_ID]
    answer["files"] = []
    errors = benchmark_lib.validate_answer_schema(answer)
    assert "symbols must equal the symbols represented in evidence" in errors
    assert "files must equal the files represented in evidence" in errors


def test_schema_validation_requires_primary_and_related_evidence() -> None:
    answer = _answer()
    answer["symbols"] = []
    answer["files"] = []
    answer["evidence"] = []

    errors = benchmark_lib.validate_answer_schema(answer)

    assert any("at least two unique" in error for error in errors)
    assert any("at least one" in error for error in errors)
    assert any("primary symbol and a related symbol" in error for error in errors)


def test_valid_codekg_trial_and_metrics(tmp_path: Path, monkeypatch) -> None:
    events_path = tmp_path / "events.jsonl"
    answer_path = tmp_path / "answer.json"
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in _events()), encoding="utf-8"
    )
    answer_path.write_text(json.dumps(_answer()), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_lib,
        "validate_repository_identity_and_paths",
        lambda answer, repository, expected_commit: [],
    )
    validation, metrics = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=events_path,
        answer_path=answer_path,
        repository=tmp_path,
        gold_path=EVALUATION_DIR / "gold" / "requests-intent-001.gold.json",
        wall_seconds=1.25,
        exit_code=0,
    )
    assert validation["valid"], validation["errors"]
    assert validation["correctness"]["correct"]
    assert metrics["target_rank"] == 1
    assert metrics["searches_before_target"] == 0
    assert metrics["recall_at_1"]
    assert metrics["tokens"]["uncached_input"] == 40
    assert metrics["relationship_location_completeness"] == 1


def test_protocol_rejects_disallowed_tool_and_shell() -> None:
    events = _events()
    events.insert(
        2,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pwd",
                "status": "completed",
                "exit_code": 0,
            },
        },
    )
    events.insert(3, _tool("list_repositories", {}, []))
    events.insert(
        4,
        {
            "type": "item.completed",
            "item": {"type": "web_search", "status": "completed"},
        },
    )
    errors, _ = benchmark_lib.validate_codekg_protocol(events)
    assert "CodeKG arm used a shell command" in errors
    assert "CodeKG arm used web search" in errors
    assert any("disallowed tools" in error for error in errors)


def test_protocol_requires_definition_id_from_search() -> None:
    events = _events()
    definition = next(
        event["item"] for event in events if event.get("item", {}).get("tool") == "get_definition"
    )
    definition["arguments"]["identifier"] = "invented"
    errors, _ = benchmark_lib.validate_codekg_protocol(events)
    assert "get_definition identifier was not returned by search_symbols" in errors


def test_provenance_rejects_invented_range() -> None:
    answer = _answer()
    answer["evidence"][1]["end_line"] = 654
    errors = benchmark_lib.validate_codekg_provenance(answer, _events(answer))
    assert any("range was not returned" in error for error in errors)


def test_native_requires_numbered_output_covering_whole_definition(tmp_path: Path) -> None:
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text(
        "class Thing:\n    def method(self):\n        value = 1\n        return value\n",
        encoding="utf-8",
    )
    answer = {
        "evidence": [
            {
                "symbol": "Thing.method",
                "file": "src/mod.py",
                "start_line": 2,
                "end_line": 4,
            }
        ]
    }
    grep_only = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/bin/bash -lc 'rg -n method src/mod.py'",
                "aggregated_output": "src/mod.py:2:    def method(self):\n",
                "status": "completed",
                "exit_code": 0,
            },
        }
    ]
    errors = benchmark_lib.validate_native(answer, grep_only, tmp_path)
    assert any("numbered full-definition output" in error for error in errors)

    numbered = [
        *grep_only,
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": ("/bin/bash -lc \"nl -ba src/mod.py | sed -n '2,4p'\""),
                "aggregated_output": (
                    "     2\t    def method(self):\n"
                    "     3\t        value = 1\n"
                    "     4\t        return value\n"
                ),
                "status": "completed",
                "exit_code": 0,
            },
        },
    ]
    assert benchmark_lib.validate_native(answer, numbered, tmp_path) == []


def test_arm_commands_have_shared_base_and_strict_mcp_isolation(tmp_path: Path) -> None:
    contract = {
        "model": "gpt-5.4-mini",
        "model_reasoning_effort": "low",
        "codekg_url": "http://127.0.0.1:8765/mcp",
        "codekg_startup_timeout_sec": 15,
        "codekg_tool_timeout_sec": 30,
    }
    common = {
        "codex": "fake-codex",
        "profile": "benchmark",
        "contract": contract,
        "repository": tmp_path,
        "schema_path": EVALUATION_DIR / "codex-answer.schema.json",
    }
    codekg = run_benchmark.trial_command(
        arm="codekg", answer_path=tmp_path / "codekg.json", **common
    )
    native = run_benchmark.trial_command(
        arm="native", answer_path=tmp_path / "native.json", **common
    )
    for flag in (
        "--strict-config",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--ignore-rules",
        "--output-schema",
    ):
        assert flag in codekg and flag in native
    assert any("enabled_tools=" in value for value in codekg)
    assert set(benchmark_lib.ALLOWED_CODEKG_TOOLS) == {
        tool
        for tool in benchmark_lib.ALLOWED_CODEKG_TOOLS
        if any(tool in value for value in codekg)
    }
    assert "mcp_servers.codekg.enabled=false" in native
    assert "mcp_servers.codekg.required=false" in native
    for expected in (
        'model="gpt-5.4-mini"',
        'model_reasoning_effort="low"',
        "features.apps=false",
        "features.plugins=false",
        'mcp_servers.codekg.url="http://127.0.0.1:8765/mcp"',
        "mcp_servers.codekg.startup_timeout_sec=15",
        "mcp_servers.codekg.tool_timeout_sec=30",
    ):
        assert expected in codekg
        assert expected in native
    assert not any("default_tools_approval_mode" in value for value in codekg)


def test_profile_contract_pins_model_reasoning_and_tools(tmp_path: Path) -> None:
    manifest = benchmark_lib.load_manifest()
    profile = tmp_path / "benchmark.config.toml"
    profile.write_text(
        'model = "gpt-5.4-mini"\n'
        'model_reasoning_effort = "low"\n'
        'web_search = "disabled"\n'
        "[features]\n"
        "multi_agent = false\n"
        "[mcp_servers.codekg]\n"
        'url = "http://127.0.0.1:8765/mcp"\n'
        "enabled = true\n"
        "required = true\n"
        "startup_timeout_sec = 15\n"
        "tool_timeout_sec = 30\n"
        'enabled_tools = ["search_symbols", "get_definition", '
        '"find_callers", "find_callees"]\n',
        encoding="utf-8",
    )
    contract = run_benchmark.profile_contract(profile, manifest)
    assert contract["model"] == manifest["model"]
    assert contract["model_reasoning_effort"] == manifest["model_reasoning_effort"]
    assert set(contract["codekg_enabled_tools"]) == benchmark_lib.ALLOWED_CODEKG_TOOLS
    profile.write_text(profile.read_text().replace('"low"', '"high"'), encoding="utf-8")
    try:
        run_benchmark.profile_contract(profile, manifest)
    except RuntimeError as exc:
        assert "reasoning effort" in str(exc)
    else:
        raise AssertionError("mismatched reasoning effort was accepted")


def test_profile_path_must_match_profile_loaded_by_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    expected = tmp_path / "codex-home" / "benchmark.config.toml"
    assert run_benchmark.enforce_profile_path(expected) == expected.resolve()
    try:
        run_benchmark.enforce_profile_path(tmp_path / "other.config.toml")
    except RuntimeError as exc:
        assert "--profile benchmark" in str(exc)
    else:
        raise AssertionError("mismatched --profile-path was accepted")


def test_frozen_runner_rejects_alternate_manifest_profile() -> None:
    manifest = benchmark_lib.load_manifest()
    manifest["profile"] = "alternate"
    try:
        run_benchmark.enforce_manifest_profile(manifest)
    except RuntimeError as exc:
        assert "profile='benchmark'" in str(exc)
    else:
        raise AssertionError("alternate manifest profile was accepted")


def test_frozen_state_detects_input_drift_and_dirty_checkout(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "benchmark.config.toml"
    profile.write_text("version = 1\n", encoding="utf-8")
    expected_hashes = benchmark_lib.snapshot_hashes(
        EVALUATION_DIR / "benchmark-manifest.json", profile
    )
    state = {"dirty": ""}

    def fake_git(repository: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(repository.resolve())
        if arguments == ("rev-parse", "HEAD"):
            return "f361ead047be5cb873174218582f7d8b9fcd9f49"
        if arguments == ("status", "--porcelain"):
            return state["dirty"]
        raise AssertionError(arguments)

    monkeypatch.setattr(run_benchmark, "_git", fake_git)
    arguments = {
        "repository": tmp_path,
        "manifest_path": EVALUATION_DIR / "benchmark-manifest.json",
        "profile_path": profile,
        "expected_hashes": expected_hashes,
    }
    run_benchmark.assert_frozen_state(**arguments)
    profile.write_text("version = 2\n", encoding="utf-8")
    try:
        run_benchmark.assert_frozen_state(**arguments)
    except RuntimeError as exc:
        assert "inputs drifted" in str(exc)
    else:
        raise AssertionError("frozen profile drift was accepted")
    profile.write_text("version = 1\n", encoding="utf-8")
    state["dirty"] = " M src/requests/sessions.py"
    try:
        run_benchmark.assert_frozen_state(**arguments)
    except RuntimeError as exc:
        assert "checkout was modified" in str(exc)
    else:
        raise AssertionError("dirty Requests checkout was accepted")


def test_launch_works_with_fake_codex_and_preserves_artifacts(tmp_path: Path) -> None:
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        'print(\'{"type":"turn.completed"}\')\n'
        "print('fake warning', file=sys.stderr)\n",
        encoding="utf-8",
    )
    code, _ = run_benchmark._launch(
        command=[sys.executable, str(script)],
        prompt="prompt",
        events_path=tmp_path / "trial" / "events.jsonl",
        stderr_path=tmp_path / "trial" / "stderr.log",
        timeout=5,
    )
    assert code == 0
    assert '"turn.completed"' in (tmp_path / "trial" / "events.jsonl").read_text()
    assert "fake warning" in (tmp_path / "trial" / "stderr.log").read_text()


def test_launch_rechecks_frozen_state_before_and_after(tmp_path: Path, monkeypatch) -> None:
    checks = []
    monkeypatch.setattr(
        run_benchmark,
        "assert_frozen_state",
        lambda **arguments: checks.append(arguments),
    )
    monkeypatch.setattr(run_benchmark, "_launch", lambda **arguments: (0, 0.25))
    frozen = {
        "repository": tmp_path,
        "manifest_path": tmp_path / "manifest.json",
        "profile_path": tmp_path / "benchmark.config.toml",
        "expected_hashes": {},
    }
    result = run_benchmark._launch_with_frozen_state(
        command=["fake-codex"],
        prompt="prompt",
        events_path=tmp_path / "events.jsonl",
        stderr_path=tmp_path / "stderr.log",
        timeout=1,
        frozen_state_arguments=frozen,
    )
    assert result == (0, 0.25)
    assert checks == [frozen, frozen]


def test_aggregator_excludes_warmups_and_pairs_repetitions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        aggregate_benchmark,
        "summarize_values",
        lambda values, seed: {"samples": list(values), "median": None},
    )
    manifest = benchmark_lib.load_manifest()
    schedule = benchmark_lib.balanced_schedule(
        arms=manifest["arms"],
        repetitions=manifest["measured_repetitions"],
        seed=manifest["seed"],
    )
    for entry in schedule:
        trial = tmp_path / "trials" / f"{entry.ordinal:02d}-{entry.trial_name}"
        trial.mkdir(parents=True)
        (trial / "metadata.json").write_text(
            json.dumps(
                {
                    "ordinal": entry.ordinal,
                    "arm": entry.arm,
                    "repetition": entry.repetition,
                    "warmup": entry.warmup,
                }
            ),
            encoding="utf-8",
        )
        metrics = {
            "valid": True,
            "correct": True,
            "success": True,
            "evidence_compliant": True,
            "infrastructure_success": True,
            "wall_seconds": float(entry.repetition),
            "tokens": {},
        }
        (trial / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        (trial / "validation.json").write_text("{}", encoding="utf-8")
        (trial / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": f"{entry.ordinal}-{entry.arm}",
                }
            )
            + "\n",
            encoding="utf-8",
        )
    report = aggregate_benchmark.aggregate(tmp_path, EVALUATION_DIR / "benchmark-manifest.json")
    assert report["observed_measured_trials"] == 20
    assert report["intention_to_treat"]["codekg"]["count"] == 10
    assert report["paired_deltas"]["complete_pairs"] == 10
    first_metadata = next((tmp_path / "trials").glob("*/metadata.json"))
    changed = json.loads(first_metadata.read_text())
    changed["arm"] = "wrong"
    first_metadata.write_text(json.dumps(changed), encoding="utf-8")
    try:
        aggregate_benchmark.aggregate(tmp_path, EVALUATION_DIR / "benchmark-manifest.json")
    except ValueError as exc:
        assert "frozen schedule" in str(exc)
    else:
        raise AssertionError("out-of-schedule trial metadata was accepted")


def test_result_scope_rejects_cross_repository_and_commit() -> None:
    events = _events()
    search = next(
        event["item"] for event in events if event.get("item", {}).get("tool") == "search_symbols"
    )
    search["result"]["structured_content"]["repository"] = "other"
    search["result"]["structured_content"]["commit"] = "000000000000"
    errors = benchmark_lib.validate_codekg_result_scope(
        events,
        repository="requests",
        commit="f361ead047be5cb873174218582f7d8b9fcd9f49",
    )
    assert any("cross-repository" in error for error in errors)
    assert any("wrong commit" in error for error in errors)


def test_trial_requires_one_nonempty_thread_id(tmp_path: Path, monkeypatch) -> None:
    events = _events()
    events[0]["thread_id"] = ""
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (tmp_path / "answer.json").write_text(json.dumps(_answer()), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_lib,
        "validate_repository_identity_and_paths",
        lambda answer, repository, expected_commit: [],
    )
    validation, _ = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=tmp_path / "events.jsonl",
        answer_path=tmp_path / "answer.json",
        repository=tmp_path,
        gold_path=EVALUATION_DIR / "gold" / "requests-intent-001.gold.json",
    )
    assert any("thread.started" in error for error in validation["errors"])
