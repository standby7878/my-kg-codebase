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
CALLEE_ID = (
    "requests@f361ead047be:src/requests/models.py:src.requests.models.PreparedRequest.prepare:424"
)
GOLD_PATH = EVALUATION_DIR / "gold" / "001-requests-prepare-request.gold.json"


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
        "primary": {
            "symbol": "src.requests.sessions.Session.prepare_request",
            "file": "src/requests/sessions.py",
            "start_line": 511,
            "end_line": 555,
        },
        "related": [
            {
                "symbol": "src.requests.sessions.Session.request",
                "relationship": "caller",
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
    callee = {
        "key": CALLEE_ID,
        "qname": "src.requests.models.PreparedRequest.prepare",
        "file": "src/requests/models.py",
        "start_line": 424,
        "end_line": 451,
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
        _tool("find_callees", {"identifier": PRIMARY_ID}, [callee]),
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


def test_suite_schedule_is_deterministic_balanced_and_paired() -> None:
    manifest = benchmark_lib.load_manifest()
    arguments = {
        "tasks": manifest["tasks"],
        "arms": manifest["arms"],
        "seed": manifest["seed"],
    }
    first = benchmark_lib.suite_schedule(**arguments)
    second = benchmark_lib.suite_schedule(**arguments)
    assert first == second
    assert len(first) == 20
    assert sum(entry.arm == "codekg" for entry in first) == 10
    assert sum(entry.arm == "native" for entry in first) == 10
    assert sum(first[index].arm == "codekg" for index in range(0, len(first), 2)) == 5
    assert {entry.task_index for entry in first} == set(range(1, 11))
    assert all(
        sum(entry.task_index == task_index for entry in first) == 2 for task_index in range(1, 11)
    )


def test_codekg_prompt_matches_measured_tool_contract() -> None:
    manifest = benchmark_lib.load_manifest()
    expected_tools = "`search_symbols`, `get_definition`, `find_callers`, and `find_callees`"
    for task in manifest["tasks"]:
        prompt_path = benchmark_lib.resolve_manifest_file(
            EVALUATION_DIR / "benchmark-manifest.json",
            task["prompt_files"]["codekg"],
        )
        prompt = prompt_path.read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "preflight" in prompt.lower()
        assert expected_tools in normalized
        # B4 (codekg-ranking-presentation-spec.md): recommended_symbol_id was
        # removed from the MCP response via the B4.4 escape hatch, so the
        # prompt no longer references it.
        assert "recommended_symbol_id" not in normalized
        assert "final" in normalized and "find_callers" in normalized
        assert "search_symbols" in prompt
        assert "Do not call `list_repositories`" in prompt
        assert "Use `list_repositories`" not in prompt
        assert f"repository `{task['repository']}`" in prompt


def test_graph_preflight_prompt_and_validation_share_primary_target(tmp_path: Path) -> None:
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    prompt = run_benchmark.graph_preflight_prompt(gold)
    assert gold["primary"]["symbol"] in prompt
    assert all(item["symbol"] not in prompt for item in gold["relevant"])
    assert "find_callers with limit=5" in prompt
    assert "find_callees with limit=5" in prompt

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in _events()), encoding="utf-8"
    )

    assert run_benchmark.validate_graph_preflight(events_path, gold, 0) == []


def test_manifest_has_ten_indexed_tasks_across_multiple_repositories() -> None:
    manifest = benchmark_lib.load_manifest()
    assert [task["index"] for task in manifest["tasks"]] == list(range(1, 11))
    assert {task["repository"] for task in manifest["tasks"]} == {
        "requests",
        "click",
        "engine",
        "pool",
        "sql",
    }
    for task in manifest["tasks"]:
        prefix = f"{task['index']:03d}-"
        for prompt_file in task["prompt_files"].values():
            assert Path(prompt_file).name.startswith(prefix)
            assert benchmark_lib.resolve_manifest_file(
                EVALUATION_DIR / "benchmark-manifest.json", prompt_file
            ).is_file()
        assert Path(task["gold_file"]).name.startswith(prefix)
        assert benchmark_lib.resolve_manifest_file(
            EVALUATION_DIR / "benchmark-manifest.json", task["gold_file"]
        ).is_file()


def test_schema_validation_rejects_null_and_unsafe_paths() -> None:
    answer = _answer()
    answer["primary"]["file"] = "/tmp/source.py"
    answer["related"][0]["file"] = r"src\source.py"
    answer["primary"]["start_line"] = None
    errors = benchmark_lib.validate_answer_schema(answer)
    assert any("relative POSIX path" in error for error in errors)
    assert any("positive integer" in error for error in errors)


def test_schema_validation_rejects_empty_evidence_symbol() -> None:
    answer = _answer()
    answer["primary"]["symbol"] = ""
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


def test_schema_validation_rejects_more_than_one_related_claim() -> None:
    answer = _answer()
    answer["related"].append(dict(answer["related"][0]))
    errors = benchmark_lib.validate_answer_schema(answer)
    assert "related must contain at most one entry" in errors


def test_schema_validation_allows_empty_related_evidence() -> None:
    answer = _answer()
    answer["related"] = []

    errors = benchmark_lib.validate_answer_schema(answer)

    assert errors == []


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
        lambda answer, repository, gold: ([], []),
    )
    validation, metrics = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=events_path,
        answer_path=answer_path,
        repository=tmp_path,
        gold_path=GOLD_PATH,
        wall_seconds=1.25,
        exit_code=0,
    )
    assert validation["structural_valid"], validation["errors"]
    assert validation["schema_valid"]
    assert validation["infrastructure_success"]
    assert validation["protocol_compliant"]
    assert validation["provenance_compliant"]
    assert validation["semantic_correct"]
    assert validation["answer_correct"]
    assert validation["location_exact"]
    assert validation["strict_pass"]
    assert validation["correctness"]["correct"]
    assert metrics["structural_valid"]
    assert metrics["correct"] == metrics["semantic_correct"]
    assert metrics["success"] == metrics["strict_pass"]
    assert metrics["target_rank"] == 1
    assert metrics["searches_before_target"] == 0
    assert metrics["recall_at_1"]
    assert metrics["tokens"]["uncached_input"] == 40
    assert metrics["relationship_location_completeness"] == 1


def test_wrong_symbol_with_own_exact_range_remains_location_exact(
    tmp_path: Path, monkeypatch
) -> None:
    answer = _answer()
    answer["primary"] = {
        "symbol": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    answer["related"] = []
    events = _events(answer)
    wrong_row = {
        "key": CALLER_ID,
        "symbol_id": CALLER_ID,
        "qname": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    search = next(
        event["item"] for event in events if event.get("item", {}).get("tool") == "search_symbols"
    )
    search["result"]["structured_content"]["results"] = [wrong_row]
    for event in events:
        item = event.get("item", {})
        if item.get("tool") == "get_definition":
            item["arguments"]["identifier"] = CALLER_ID
            item["result"]["structured_content"]["result"] = [wrong_row]
        elif item.get("tool") in {"find_callers", "find_callees"}:
            item["arguments"]["identifier"] = CALLER_ID
    events_path = tmp_path / "events.jsonl"
    answer_path = tmp_path / "answer.json"
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    answer_path.write_text(json.dumps(answer), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_lib,
        "validate_repository_identity_and_paths",
        lambda answer, repository, gold: ([], []),
    )
    validation, metrics = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=events_path,
        answer_path=answer_path,
        repository=tmp_path,
        gold_path=GOLD_PATH,
        exit_code=0,
    )
    assert validation["primary_correct"] is False
    assert validation["semantic_correct"] is False
    assert validation["correctness"]["locations"]["primary"] == "exact"
    assert validation["location_exact"] is True
    assert metrics["location_exact"] is True


def test_grade_uses_explicit_primary_and_directed_related() -> None:
    answer = _answer()
    gold = json.loads(GOLD_PATH.read_text())
    answer["primary"]["symbol"] = "src.requests.sessions.Session.request"
    answer["primary"]["start_line"] = 557
    answer["primary"]["end_line"] = 653
    answer["related"][0].update(
        symbol="src.requests.sessions.Session.prepare_request",
        start_line=511,
        end_line=555,
    )
    graded = benchmark_lib.grade_answer(answer, gold)
    assert not graded["primary_correct"]
    assert not graded["semantic_correct"]
    answer = _answer()
    answer["related"][0]["relationship"] = "callee"
    graded = benchmark_lib.grade_answer(answer, gold)
    assert graded["primary_correct"]
    assert not graded["related_correct"]


def test_symbol_matching_uses_component_boundaries() -> None:
    assert benchmark_lib._matches_symbol(
        "src.requests.sessions.Session.prepare_request", ["Session.prepare_request"]
    )
    assert not benchmark_lib._matches_symbol(
        "src.requests.sessions.OtherSession.prepare_request", ["Session.prepare_request"]
    )
    assert not benchmark_lib._matches_symbol(
        "src.requests.sessions.Session.prepare_request_extra", ["Session.prepare_request"]
    )
    assert not benchmark_lib._matches_symbol("prepare_request", ["Session.prepare_request"])


def test_location_statuses_are_independent_of_semantic_correctness() -> None:
    gold = json.loads(GOLD_PATH.read_text())
    answer = _answer()
    assert benchmark_lib.grade_answer(answer, gold)["locations"]["primary"] == "exact"
    answer["primary"]["start_line"] = 510
    answer["primary"]["end_line"] = 556
    assert benchmark_lib.grade_answer(answer, gold)["locations"]["primary"] == "contains_definition"
    answer["primary"]["start_line"] = 512
    assert benchmark_lib.grade_answer(answer, gold)["locations"]["primary"] == "invalid"


def test_protocol_accepts_pre_definition_and_post_definition_recovery() -> None:
    primary_row = _events()[2]["item"]["result"]["structured_content"]["results"][0]
    rejected_row = {
        "key": CALLER_ID,
        "symbol_id": CALLER_ID,
        "qname": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    base = _events()
    pre_definition = [base[0], base[1]]
    first = _tool(
        "search_symbols",
        {"repository": "requests", "scope": "source", "limit": 5, "query": "request"},
        [rejected_row],
    )
    first["item"]["result"]["structured_content"]["recommended_symbol_id"] = CALLER_ID
    pre_definition.extend(
        [
            first,
            _tool(
                "search_symbols",
                {
                    "repository": "requests",
                    "scope": "source",
                    "limit": 5,
                    "query": "prepared request",
                },
                [primary_row],
            ),
            *base[3:],
        ]
    )
    assert benchmark_lib.validate_codekg_protocol(pre_definition, "requests")[0] == []

    post_definition = [base[0], base[1], first]
    post_definition.extend(
        [
            _tool("get_definition", {"identifier": CALLER_ID}, [rejected_row]),
            _tool(
                "search_symbols",
                {
                    "repository": "requests",
                    "scope": "source",
                    "limit": 5,
                    "query": "prepared request",
                },
                [primary_row],
            ),
            *base[3:],
        ]
    )
    errors, protocol = benchmark_lib.validate_codekg_protocol(post_definition, "requests")
    assert errors == []
    assert protocol["definition_calls"] == 2
    assert protocol["selected_symbol_id"] == PRIMARY_ID


def test_protocol_accepts_second_definition_from_same_search() -> None:
    events = _events()
    search = events[2]
    primary_row = search["item"]["result"]["structured_content"]["results"][0]
    rejected_row = {
        "key": CALLER_ID,
        "qname": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    search["item"]["result"]["structured_content"]["results"] = [rejected_row, primary_row]
    events.insert(3, _tool("get_definition", {"identifier": CALLER_ID}, [rejected_row]))
    errors, _ = benchmark_lib.validate_codekg_protocol(events, "requests")
    assert errors == []


def test_protocol_rejects_relationships_for_rejected_definition() -> None:
    events = _events()
    events[4]["item"]["arguments"]["identifier"] = CALLER_ID
    errors, _ = benchmark_lib.validate_codekg_protocol(events, "requests")
    assert "relationship calls must use the final definition symbol ID" in errors


def test_protocol_rejects_search_after_final_definition() -> None:
    events = _events()
    events.insert(
        4,
        _tool(
            "search_symbols",
            {"repository": "requests", "scope": "source", "limit": 5, "query": "late"},
            [],
        ),
    )
    errors, _ = benchmark_lib.validate_codekg_protocol(events, "requests")
    assert "search_symbols calls must precede the final get_definition call" in errors


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
    errors, _ = benchmark_lib.validate_codekg_protocol(events, "requests")
    assert "CodeKG arm used a shell command" in errors
    assert "CodeKG arm used web search" in errors
    assert any("disallowed tools" in error for error in errors)


def test_protocol_requires_definition_id_from_search() -> None:
    events = _events()
    definition = next(
        event["item"] for event in events if event.get("item", {}).get("tool") == "get_definition"
    )
    definition["arguments"]["identifier"] = "invented"
    errors, _ = benchmark_lib.validate_codekg_protocol(events, "requests")
    assert "get_definition identifier was not returned by a preceding search_symbols" in errors


def test_provenance_is_tool_bound_but_location_is_graded_separately() -> None:
    answer = _answer()
    answer["related"][0]["end_line"] = 654
    errors = benchmark_lib.validate_codekg_provenance(answer, _events(answer))
    assert errors == []
    assert any(
        "range was not returned" in error
        for error in benchmark_lib._validate_codekg_locations(answer, _events(answer))
    )
    graded = benchmark_lib.grade_answer(answer, json.loads(GOLD_PATH.read_text()))
    assert graded["related_correct"]
    assert graded["locations"]["related"] == "contains_definition"
    answer["related"][0]["relationship"] = "callee"
    errors = benchmark_lib.validate_codekg_provenance(answer, _events(answer))
    assert any("callee result" in error for error in errors)


def test_provenance_does_not_fall_back_after_failed_final_definition() -> None:
    answer = _answer()
    events = _events(answer)
    wrong_row = {
        "key": CALLER_ID,
        "symbol_id": CALLER_ID,
        "qname": "src.requests.sessions.Session.request",
        "file": "src/requests/sessions.py",
        "start_line": 557,
        "end_line": 653,
    }
    events.insert(
        4,
        _tool(
            "search_symbols",
            {"repository": "requests", "scope": "source", "limit": 5, "query": "request"},
            [wrong_row],
        ),
    )
    failed_definition = _tool("get_definition", {"identifier": CALLER_ID}, [wrong_row])
    failed_definition["item"]["status"] = "failed"
    failed_definition["item"]["result"] = None
    events.insert(5, failed_definition)
    for event in events:
        item = event.get("item", {})
        if item.get("tool") in {"find_callers", "find_callees"}:
            item["arguments"]["identifier"] = CALLER_ID
    errors = benchmark_lib.validate_codekg_provenance(answer, events)
    assert "primary claim was not returned by its CodeKG primary result" in errors


def test_native_requires_numbered_output_covering_whole_definition(tmp_path: Path) -> None:
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text(
        "class Thing:\n    def method(self):\n        value = 1\n        return value\n",
        encoding="utf-8",
    )
    answer = {
        "primary": {
            "symbol": "Thing.method",
            "file": "src/mod.py",
            "start_line": 2,
            "end_line": 4,
        },
        "related": [
            {
                "symbol": "Thing.method",
                "relationship": "caller",
                "file": "src/mod.py",
                "start_line": 2,
                "end_line": 4,
            }
        ],
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


def test_native_fabricated_symbol_is_unsupported_provenance(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text(
        "class Thing:\n    def method(self):\n        value = 1\n        return value\n",
        encoding="utf-8",
    )
    answer = _answer()
    answer["primary"] = {
        "symbol": "Thing.fabricated",
        "file": "src/mod.py",
        "start_line": 2,
        "end_line": 4,
    }
    answer["related"] = []
    events = [
        {"type": "thread.started", "thread_id": "native-fabricated"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "rg -n fabricated src/mod.py",
                "aggregated_output": "",
                "status": "completed",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "nl -ba src/mod.py",
                "aggregated_output": (
                    "     2\t    def method(self):\n"
                    "     3\t        value = 1\n"
                    "     4\t        return value\n"
                ),
                "status": "completed",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(answer, separators=(",", ":")),
            },
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (tmp_path / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_lib,
        "validate_repository_identity_and_paths",
        lambda answer, repository, gold: ([], []),
    )
    validation, metrics = benchmark_lib.validate_trial(
        arm="native",
        events_path=tmp_path / "events.jsonl",
        answer_path=tmp_path / "answer.json",
        repository=tmp_path,
        gold_path=GOLD_PATH,
        exit_code=0,
    )
    assert validation["provenance_compliant"] is False
    assert validation["unsupported_claim_count"] == 1
    assert metrics["unsupported_claim_count"] == 1
    assert validation["correctness"]["locations"]["primary"] is None


def test_completed_mcp_call_without_result_is_infrastructure_failure(
    tmp_path: Path, monkeypatch
) -> None:
    answer = _answer()
    events = _events(answer)
    callee = next(
        event["item"] for event in events if event.get("item", {}).get("tool") == "find_callees"
    )
    callee["result"] = None
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (tmp_path / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    monkeypatch.setattr(
        benchmark_lib,
        "validate_repository_identity_and_paths",
        lambda answer, repository, gold: ([], []),
    )
    validation, metrics = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=tmp_path / "events.jsonl",
        answer_path=tmp_path / "answer.json",
        repository=tmp_path,
        gold_path=GOLD_PATH,
        exit_code=0,
    )
    assert validation["infrastructure_success"] is False
    assert validation["structural_valid"] is False
    assert validation["strict_pass"] is False
    assert metrics["infrastructure_success"] is False


def test_native_mcp_only_metrics_are_null() -> None:
    metrics = benchmark_lib.collect_metrics(
        arm="native",
        events=[],
        answer=_answer(),
        gold=json.loads(GOLD_PATH.read_text()),
        wall_seconds=1.0,
        exit_code=0,
        structural_valid=True,
        evidence_compliant=True,
        correctness={
            "primary_correct": True,
            "related_correct": True,
            "semantic_correct": True,
            "location_exact": True,
        },
        unsupported_claim_count=0,
    )
    for name in (
        "response_bytes",
        "search_calls",
        "candidate_count",
        "target_rank",
        "searches_before_target",
        "reciprocal_rank",
        "recall_at_1",
        "recall_at_5",
        "relationship_rows",
        "relationship_location_completeness",
    ):
        assert metrics[name] is None


def test_summary_ignores_null_rates_and_reports_applicable_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        aggregate_benchmark,
        "summarize_values",
        lambda values, seed: {"samples": list(values), "median": None},
    )
    trials = [
        {"metrics": {"recall_at_1": True, "response_bytes": 10, "tokens": {}}},
        {"metrics": {"recall_at_1": None, "response_bytes": None, "tokens": {}}},
    ]
    summary = aggregate_benchmark._summary(trials, 1)
    assert summary["rates"]["recall_at_1"] == 1.0
    assert summary["rate_applicable_counts"]["recall_at_1"] == 1
    assert summary["metrics"]["response_bytes"]["applicable_count"] == 1


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
    monkeypatch.setattr(run_benchmark, "repository_states", lambda corpus_root, manifest: {})
    arguments = {
        "corpus_root": tmp_path,
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
    monkeypatch.setattr(
        run_benchmark,
        "repository_states",
        lambda corpus_root, manifest: (_ for _ in ()).throw(
            RuntimeError("repository checkout was modified")
        ),
    )
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


def test_aggregator_requires_complete_indexed_task_pairs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        aggregate_benchmark,
        "summarize_values",
        lambda values, seed: {"samples": list(values), "median": None},
    )
    manifest = benchmark_lib.load_manifest()
    schedule = benchmark_lib.suite_schedule(
        tasks=manifest["tasks"],
        arms=manifest["arms"],
        seed=manifest["seed"],
    )
    for entry in schedule:
        trial = tmp_path / "tasks" / entry.trial_name / entry.arm
        trial.mkdir(parents=True)
        (trial / "metadata.json").write_text(
            json.dumps(
                {
                    "ordinal": entry.ordinal,
                    "arm": entry.arm,
                    "task_index": entry.task_index,
                    "task_id": entry.task_id,
                    "repository": entry.repository,
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
            "wall_seconds": float(entry.task_index),
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
    first_metadata = next((tmp_path / "tasks").glob("*/*/metadata.json"))
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
        lambda answer, repository, gold: ([], []),
    )
    validation, _ = benchmark_lib.validate_trial(
        arm="codekg",
        events_path=tmp_path / "events.jsonl",
        answer_path=tmp_path / "answer.json",
        repository=tmp_path,
        gold_path=GOLD_PATH,
    )
    assert any("thread.started" in error for error in validation["errors"])
