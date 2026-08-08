"""Normalize Claude Code CLI ``stream-json`` output into the benchmark's event schema.

The rest of the benchmark (``benchmark_lib.py``'s ``read_events``/``completed_items``/
``validate_codekg_protocol``/``validate_native``/``collect_metrics``, etc.) was written
against Codex's ``exec --json`` event shape: ``thread.started``, ``turn.started``,
``item.completed`` (with ``item.type`` in ``mcp_tool_call``/``command_execution``/
``agent_message``), and ``turn.completed`` (carrying token ``usage``). This module is the
only place that understands Claude's raw stream and converts it into that same shape, so
the rest of the evaluator does not need to know which CLI produced a trial.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_EXIT_CODE_RE = re.compile(r"^Exit code (\d+)")


def _mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Split ``mcp__<server>__<tool>`` into ``(server, tool)``, else None."""
    parts = name.split("__")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    return parts[1], parts[2]


def _parse_json_maybe(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _strip_code_fence(text: str) -> str:
    value = text.strip()
    if not value.startswith("```"):
        return value
    value = value[3:]
    if value.startswith("json"):
        value = value[4:]
    end = value.rfind("```")
    if end != -1:
        value = value[:end]
    return value.strip()


def _last_balanced_json_object(text: str) -> Any:
    """Find the last top-level ``{...}`` object in ``text`` that parses as JSON.

    The prompts instruct the model to emit the JSON answer as the entire final
    message, but a model may still wrap it in prose despite that instruction. This
    scans from the end for a balanced brace span and tries each candidate.
    """
    end = len(text)
    while True:
        close = text.rfind("}", 0, end)
        if close == -1:
            return None
        depth = 0
        start = close
        while start >= 0:
            if text[start] == "}":
                depth += 1
            elif text[start] == "{":
                depth -= 1
                if depth == 0:
                    candidate = text[start : close + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
            start -= 1
        end = close


def normalize(raw_text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Convert raw Claude ``stream-json`` lines into normalized benchmark events.

    Returns ``(events, final_text)`` where ``final_text`` is the last assistant text
    block (expected to be the JSON-only final answer per the prompt's instructions).
    """
    events: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    session_id: str | None = None
    final_text: str | None = None
    started = False

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")

        if kind == "system" and payload.get("subtype") == "init":
            session_id = payload.get("session_id")
            if not started:
                events.append({"type": "thread.started", "thread_id": session_id or ""})
                events.append({"type": "turn.started"})
                started = True
            continue

        if kind == "assistant":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        final_text = text
                        events.append(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "agent_message",
                                    "status": "completed",
                                    "error": None,
                                    "text": text,
                                },
                            }
                        )
                elif block_type == "tool_use":
                    tool_id = block.get("id")
                    if isinstance(tool_id, str):
                        pending[tool_id] = block
            continue

        if kind == "user":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else payload.get("content")
            for block in content or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                tool_use = pending.pop(tool_id, None) if isinstance(tool_id, str) else None
                if tool_use is None:
                    continue
                name = tool_use.get("name")
                if name == "StructuredOutput":
                    # Synthetic tool the CLI uses internally to deliver the
                    # --json-schema-constrained answer; not a real tool call.
                    continue
                arguments = tool_use.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                raw_content = block.get("content")
                content_text = raw_content if isinstance(raw_content, str) else json.dumps(raw_content)
                is_error = bool(block.get("is_error"))
                mcp = _mcp_tool_name(name) if isinstance(name, str) else None
                if mcp is not None:
                    server, tool = mcp
                    item: dict[str, Any] = {
                        "type": "mcp_tool_call",
                        "server": server,
                        "tool": tool,
                        "arguments": arguments,
                        "result": None
                        if is_error
                        else {
                            "content": [{"type": "text", "text": content_text}],
                            "structured_content": _parse_json_maybe(content_text),
                        },
                        "error": content_text if is_error else None,
                        "status": "failed" if is_error else "completed",
                    }
                else:
                    match = _EXIT_CODE_RE.match(content_text) if is_error else None
                    exit_code = int(match.group(1)) if match else (1 if is_error else 0)
                    item = {
                        "type": "command_execution",
                        "command": arguments.get("command", ""),
                        "aggregated_output": content_text,
                        "exit_code": exit_code,
                        "status": "completed",
                        "error": None,
                    }
                events.append({"type": "item.completed", "item": item})
            continue

        if kind == "result":
            usage = payload.get("usage")
            mapped_usage = {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            }
            if isinstance(usage, dict):
                mapped_usage["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
                mapped_usage["cached_input_tokens"] = int(usage.get("cache_read_input_tokens", 0) or 0)
                mapped_usage["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
            events.append({"type": "turn.completed", "usage": mapped_usage})
            # With --json-schema, the schema-constrained answer lives on this
            # terminal event's `result` field, on a channel separate from any
            # free-form assistant text blocks emitted during the conversation.
            # Treat it as the authoritative final message so it becomes the
            # last "agent_message" item, matching what answer.json will contain.
            result_text = payload.get("result")
            if isinstance(result_text, str) and result_text:
                final_text = result_text
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "status": "completed",
                            "error": None,
                            "text": result_text,
                        },
                    }
                )
            continue

    if not started:
        events.insert(0, {"type": "turn.started"})
        events.insert(0, {"type": "thread.started", "thread_id": session_id or ""})

    return events, final_text


def extract_final_answer(final_text: str | None) -> Any:
    """Parse the final assistant message as the trial's JSON answer, or None."""
    if final_text is None:
        return None
    stripped = _strip_code_fence(final_text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return _last_balanced_json_object(stripped)


def finalize_claude_trial(raw_events_path: Path, answer_path: Path | None) -> Any:
    """Rewrite ``raw_events_path`` in place with normalized events.

    Returns the parsed final answer object (or None if absent/unparseable). Writing
    ``answer_path`` is the caller's responsibility so it can use the benchmark's
    create-only write helper.
    """
    raw_text = raw_events_path.read_text(encoding="utf-8")
    events, final_text = normalize(raw_text)
    raw_events_path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    answer = extract_final_answer(final_text)
    if answer_path is not None and answer is not None:
        answer_path.parent.mkdir(parents=True, exist_ok=True)
        if not answer_path.exists():
            answer_path.write_text(json.dumps(answer, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return answer
