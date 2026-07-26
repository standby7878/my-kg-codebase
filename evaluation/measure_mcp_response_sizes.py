"""Compare serialized MCP response sizes from before/after JSON captures.

Each input is a JSON object keyed by tool name. A value may be either a
CallToolResult-shaped object or a JSON-RPC envelope whose ``result`` is that
object. Both ``structuredContent`` and ``structured_content`` are accepted.

Example capture shape:

    {
      "list_repositories": {
        "content": [{"type": "text", "text": "Found 5 indexed repositories."}],
        "structuredContent": {"result": [{"repo_name": "requests"}]},
        "isError": false
      }
    }

Run:

    python evaluation/measure_mcp_response_sizes.py \
      --before runs/mcp-response-before.json \
      --after runs/mcp-response-after.json \
      --output runs/mcp-response-size-report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _compact_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def _call_result(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each tool capture must be a JSON object")
    nested = value.get("result")
    if isinstance(nested, dict) and "content" in nested:
        return nested
    return value


def _dimensions(value: object) -> dict[str, int]:
    result = _call_result(value)
    content = result.get("content", [])
    structured = result.get("structuredContent", result.get("structured_content"))
    text_bytes = 0
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_bytes += len(str(item.get("text", "")).encode())
    return {
        "text_bytes": text_bytes,
        "structured_bytes": _compact_bytes(structured),
        "wire_bytes": _compact_bytes(result),
    }


def _delta(before: int, after: int) -> dict[str, int | float | None]:
    reduction = before - after
    return {
        "bytes": after - before,
        "reduction_bytes": reduction,
        "reduction_percent": round(reduction * 100 / before, 2) if before else None,
    }


def compare(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    if set(before) != set(after):
        missing_after = sorted(set(before) - set(after))
        missing_before = sorted(set(after) - set(before))
        raise ValueError(
            "capture tool sets differ: "
            f"missing_after={missing_after}, missing_before={missing_before}"
        )

    tools: dict[str, object] = {}
    before_totals = {"text_bytes": 0, "structured_bytes": 0, "wire_bytes": 0}
    after_totals = {"text_bytes": 0, "structured_bytes": 0, "wire_bytes": 0}
    for name in sorted(before):
        before_dimensions = _dimensions(before[name])
        after_dimensions = _dimensions(after[name])
        for dimension in before_totals:
            before_totals[dimension] += before_dimensions[dimension]
            after_totals[dimension] += after_dimensions[dimension]
        before_result = _call_result(before[name])
        after_result = _call_result(after[name])
        before_structured = before_result.get(
            "structuredContent", before_result.get("structured_content")
        )
        after_structured = after_result.get(
            "structuredContent", after_result.get("structured_content")
        )
        tools[name] = {
            "before": before_dimensions,
            "after": after_dimensions,
            "delta": {
                dimension: _delta(before_dimensions[dimension], after_dimensions[dimension])
                for dimension in before_dimensions
            },
            "structured_equal": before_structured == after_structured,
        }

    return {
        "tools": tools,
        "totals": {
            "before": before_totals,
            "after": after_totals,
            "delta": {
                dimension: _delta(before_totals[dimension], after_totals[dimension])
                for dimension in before_totals
            },
        },
    }


def _read_capture(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object keyed by tool name")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = compare(_read_capture(args.before), _read_capture(args.after))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
