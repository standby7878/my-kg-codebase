#!/usr/bin/env python3
"""Aggregate measured Requests benchmark trials (warm-ups are always excluded)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from benchmark_lib import (
    MANIFEST_PATH,
    balanced_schedule,
    load_manifest,
    read_events,
    read_json,
    summarize_values,
    write_json_new,
)

SCALAR_METRICS = (
    "wall_seconds",
    "turns",
    "tool_calls",
    "response_bytes",
    "search_calls",
    "candidate_count",
    "target_rank",
    "searches_before_target",
    "reciprocal_rank",
    "unsupported_claim_count",
    "relationship_location_completeness",
)
TOKEN_METRICS = (
    "input",
    "cached_input",
    "uncached_input",
    "cache_hit_ratio",
    "output",
    "reasoning",
)
RATE_METRICS = (
    "infrastructure_success",
    "valid",
    "evidence_compliant",
    "correct",
    "success",
    "recall_at_1",
    "recall_at_5",
)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _summary(trials: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    summary: dict[str, Any] = {"count": len(trials), "rates": {}, "metrics": {}, "tokens": {}}
    for name in RATE_METRICS:
        values = [bool(trial["metrics"].get(name)) for trial in trials]
        summary["rates"][name] = sum(values) / len(values) if values else None
    for offset, name in enumerate(SCALAR_METRICS):
        values = [
            number
            for trial in trials
            if (number := _number(trial["metrics"].get(name))) is not None
        ]
        summary["metrics"][name] = summarize_values(values, seed=seed + offset)
    for offset, name in enumerate(TOKEN_METRICS, start=len(SCALAR_METRICS)):
        values = [
            number
            for trial in trials
            if (number := _number(trial["metrics"].get("tokens", {}).get(name))) is not None
        ]
        summary["tokens"][name] = summarize_values(values, seed=seed + offset)
    return summary


def _paired_deltas(trials: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_repetition: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trial in trials:
        by_repetition[trial["metadata"]["repetition"]][trial["metadata"]["arm"]] = trial
    result: dict[str, Any] = {
        "direction": "codekg_minus_native",
        "complete_pairs": sum(set(pair) == {"codekg", "native"} for pair in by_repetition.values()),
        "metrics": {},
        "tokens": {},
    }
    for offset, name in enumerate(SCALAR_METRICS):
        values = []
        for pair in by_repetition.values():
            if set(pair) != {"codekg", "native"}:
                continue
            codekg = _number(pair["codekg"]["metrics"].get(name))
            native = _number(pair["native"]["metrics"].get(name))
            if codekg is not None and native is not None:
                values.append(codekg - native)
        result["metrics"][name] = summarize_values(values, seed=seed + offset)
    for offset, name in enumerate(TOKEN_METRICS, start=len(SCALAR_METRICS)):
        values = []
        for pair in by_repetition.values():
            if set(pair) != {"codekg", "native"}:
                continue
            codekg = _number(pair["codekg"]["metrics"].get("tokens", {}).get(name))
            native = _number(pair["native"]["metrics"].get("tokens", {}).get(name))
            if codekg is not None and native is not None:
                values.append(codekg - native)
        result["tokens"][name] = summarize_values(values, seed=seed + offset)
    return result


def aggregate(run_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    all_trials = []
    for metadata_path in sorted((run_dir / "trials").glob("*/metadata.json")):
        metadata = read_json(metadata_path)
        metrics_path = metadata_path.parent / "metrics.json"
        validation_path = metadata_path.parent / "validation.json"
        if not metrics_path.is_file() or not validation_path.is_file():
            raise ValueError(f"incomplete trial artifacts: {metadata_path.parent}")
        events, event_errors = read_events(metadata_path.parent / "events.jsonl")
        if event_errors:
            raise ValueError(f"invalid trial events: {metadata_path.parent}: {event_errors}")
        thread_ids = [
            event.get("thread_id") for event in events if event.get("type") == "thread.started"
        ]
        if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not thread_ids[0]:
            raise ValueError(f"trial has invalid thread identity: {metadata_path.parent}")
        all_trials.append(
            {
                "metadata": metadata,
                "metrics": read_json(metrics_path),
                "validation": read_json(validation_path),
                "thread_id": thread_ids[0],
            }
        )
    expected_entries = balanced_schedule(
        arms=manifest["arms"],
        repetitions=manifest["measured_repetitions"],
        seed=manifest["seed"],
    )
    expected_membership = sorted(
        (entry.ordinal, entry.arm, entry.repetition, entry.warmup) for entry in expected_entries
    )
    observed_membership = []
    for trial in all_trials:
        metadata = trial["metadata"]
        ordinal = metadata.get("ordinal")
        arm = metadata.get("arm")
        repetition = metadata.get("repetition")
        warmup = metadata.get("warmup")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not isinstance(arm, str)
            or isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or not isinstance(warmup, bool)
        ):
            raise ValueError(f"trial has malformed schedule metadata: {metadata}")
        observed_membership.append((ordinal, arm, repetition, warmup))
    observed_membership.sort()
    if observed_membership != expected_membership:
        raise ValueError(
            "trial artifacts do not match the frozen schedule: "
            f"expected={expected_membership}, observed={observed_membership}"
        )
    thread_ids = [trial["thread_id"] for trial in all_trials]
    if len(thread_ids) != len(set(thread_ids)):
        raise ValueError("trials reused a Codex thread_id")
    trials = [trial for trial in all_trials if not trial["metadata"]["warmup"]]
    expected = len(manifest["arms"]) * manifest["measured_repetitions"]
    if len(trials) != expected:
        raise ValueError(f"expected {expected} measured trials, found {len(trials)}")
    by_arm = {
        arm: [trial for trial in trials if trial["metadata"]["arm"] == arm]
        for arm in manifest["arms"]
    }
    valid_by_arm = {
        arm: [trial for trial in arm_trials if trial["metrics"].get("valid")]
        for arm, arm_trials in by_arm.items()
    }
    seed = int(manifest["seed"])
    return {
        "task_id": manifest["task_id"],
        "repository_commit": manifest["repository_commit"],
        "warmups_excluded": True,
        "expected_measured_trials": expected,
        "observed_measured_trials": len(trials),
        "intention_to_treat": {
            arm: _summary(arm_trials, seed + index * 100)
            for index, (arm, arm_trials) in enumerate(by_arm.items())
        },
        "valid_only": {
            arm: _summary(arm_trials, seed + 1_000 + index * 100)
            for index, (arm, arm_trials) in enumerate(valid_by_arm.items())
        },
        "paired_deltas": _paired_deltas(trials, seed + 2_000),
        "trial_outcomes": [
            {
                "arm": trial["metadata"]["arm"],
                "repetition": trial["metadata"]["repetition"],
                "valid": trial["metrics"].get("valid"),
                "correct": trial["metrics"].get("correct"),
                "success": trial["metrics"].get("success"),
                "evidence_compliant": trial["metrics"].get("evidence_compliant"),
            }
            for trial in trials
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = aggregate(args.run_dir.resolve(), args.manifest.resolve())
    if args.output:
        write_json_new(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"aggregation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
