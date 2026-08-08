#!/usr/bin/env python3
"""Aggregate the ten-task multi-repository CodeKG/native benchmark suite."""

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
    load_manifest,
    read_events,
    read_json,
    suite_schedule,
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
    "location_error_count",
    "protocol_error_count",
    "relationship_rows",
    "relationship_location_completeness",
)
TOKEN_METRICS = (
    "input",
    "cached_input",
    "cache_creation",
    "uncached_input",
    "cache_hit_ratio",
    "output",
    "reasoning",
)
RATE_METRICS = (
    "infrastructure_success",
    "schema_valid",
    "protocol_compliant",
    "provenance_compliant",
    "path_convention_compliant",
    "structural_valid",
    "primary_correct",
    "related_correct",
    "semantic_correct",
    "answer_correct",
    "location_exact",
    "strict_pass",
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
    summary: dict[str, Any] = {
        "count": len(trials),
        "rates": {},
        "rate_applicable_counts": {},
        "metrics": {},
        "tokens": {},
    }
    for name in RATE_METRICS:
        values = [
            value for trial in trials if isinstance((value := trial["metrics"].get(name)), bool)
        ]
        summary["rates"][name] = sum(values) / len(values) if values else None
        summary["rate_applicable_counts"][name] = len(values)
    for offset, name in enumerate(SCALAR_METRICS):
        values = [
            number
            for trial in trials
            if (number := _number(trial["metrics"].get(name))) is not None
        ]
        metric_summary = summarize_values(values, seed=seed + offset)
        metric_summary["applicable_count"] = len(values)
        summary["metrics"][name] = metric_summary
    for offset, name in enumerate(TOKEN_METRICS, start=len(SCALAR_METRICS)):
        values = [
            number
            for trial in trials
            if (number := _number(trial["metrics"].get("tokens", {}).get(name))) is not None
        ]
        token_summary = summarize_values(values, seed=seed + offset)
        token_summary["applicable_count"] = len(values)
        summary["tokens"][name] = token_summary
    return summary


def _paired_deltas(trials: Sequence[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_task: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trial in trials:
        by_task[trial["metadata"]["task_index"]][trial["metadata"]["arm"]] = trial
    result: dict[str, Any] = {
        "direction": "codekg_minus_native",
        "complete_pairs": sum(set(pair) == {"codekg", "native"} for pair in by_task.values()),
        "metrics": {},
        "tokens": {},
    }
    for offset, name in enumerate(SCALAR_METRICS):
        values = []
        for pair in by_task.values():
            if set(pair) != {"codekg", "native"}:
                continue
            codekg = _number(pair["codekg"]["metrics"].get(name))
            native = _number(pair["native"]["metrics"].get(name))
            if codekg is not None and native is not None:
                values.append(codekg - native)
        metric_summary = summarize_values(values, seed=seed + offset)
        metric_summary["applicable_count"] = len(values)
        result["metrics"][name] = metric_summary
    for offset, name in enumerate(TOKEN_METRICS, start=len(SCALAR_METRICS)):
        values = []
        for pair in by_task.values():
            if set(pair) != {"codekg", "native"}:
                continue
            codekg = _number(pair["codekg"]["metrics"].get("tokens", {}).get(name))
            native = _number(pair["native"]["metrics"].get("tokens", {}).get(name))
            if codekg is not None and native is not None:
                values.append(codekg - native)
        token_summary = summarize_values(values, seed=seed + offset)
        token_summary["applicable_count"] = len(values)
        result["tokens"][name] = token_summary
    return result


def aggregate(run_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    batch_path = run_dir / "batch.json"
    resolved_model = None
    if batch_path.is_file():
        resolved_model = read_json(batch_path).get("resolved", {}).get("model")
    all_trials = []
    for metadata_path in sorted((run_dir / "tasks").glob("*/*/metadata.json")):
        metadata = read_json(metadata_path)
        if resolved_model is not None and metadata.get("model") != resolved_model:
            # A7.1: batch.json.resolved.model is the ground truth for what
            # actually ran; a mismatch here means the run mixed models
            # mid-batch or resolved_provenance was computed from a stale
            # value, and the aggregate must not silently label it.
            raise ValueError(
                f"trial model mismatch: {metadata_path.parent} has "
                f"metadata.model={metadata.get('model')!r}, "
                f"batch.json.resolved.model={resolved_model!r}"
            )
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
    expected_entries = suite_schedule(
        tasks=manifest["tasks"],
        arms=manifest["arms"],
        seed=manifest["seed"],
    )
    expected_membership = sorted(
        (
            entry.ordinal,
            entry.task_index,
            entry.task_id,
            entry.repository,
            entry.arm,
        )
        for entry in expected_entries
    )
    observed_membership = []
    for trial in all_trials:
        metadata = trial["metadata"]
        ordinal = metadata.get("ordinal")
        arm = metadata.get("arm")
        task_index = metadata.get("task_index")
        task_id = metadata.get("task_id")
        repository = metadata.get("repository")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not isinstance(arm, str)
            or isinstance(task_index, bool)
            or not isinstance(task_index, int)
            or not isinstance(task_id, str)
            or not isinstance(repository, str)
        ):
            raise ValueError(f"trial has malformed schedule metadata: {metadata}")
        observed_membership.append((ordinal, task_index, task_id, repository, arm))
    observed_membership.sort()
    if observed_membership != expected_membership:
        raise ValueError(
            "trial artifacts do not match the frozen schedule: "
            f"expected={expected_membership}, observed={observed_membership}"
        )
    thread_ids = [trial["thread_id"] for trial in all_trials]
    if len(thread_ids) != len(set(thread_ids)):
        raise ValueError("trials reused a Codex thread_id")
    trials = all_trials
    expected = len(manifest["arms"]) * len(manifest["tasks"])
    if len(trials) != expected:
        raise ValueError(f"expected {expected} measured trials, found {len(trials)}")
    by_arm = {
        arm: [trial for trial in trials if trial["metadata"]["arm"] == arm]
        for arm in manifest["arms"]
    }
    seed = int(manifest["seed"])
    return {
        "suite_id": manifest["suite_id"],
        "repositories": manifest["repositories"],
        "expected_measured_trials": expected,
        "observed_measured_trials": len(trials),
        # Intention-to-treat over all attempted trials is the only reported
        # aggregate. A "valid_only" subgroup (conditioning on an arm-asymmetric
        # filter) previously existed here and was deleted per
        # benchmark-measurement-correctness-spec.md A1: any such filter
        # reintroduces the A0 class of bug. structural_valid is reported as
        # its own rate below instead.
        "intention_to_treat": {
            arm: _summary(arm_trials, seed + index * 100)
            for index, (arm, arm_trials) in enumerate(by_arm.items())
        },
        "paired_deltas": _paired_deltas(trials, seed + 2_000),
        "trial_outcomes": [
            {
                "arm": trial["metadata"]["arm"],
                "task_index": trial["metadata"]["task_index"],
                "task_id": trial["metadata"]["task_id"],
                "repository": trial["metadata"]["repository"],
                "structural_valid": trial["metrics"].get("structural_valid"),
                "answer_correct": trial["metrics"].get("answer_correct"),
                "correct": trial["metrics"].get("correct"),
                "success": trial["metrics"].get("success"),
                "evidence_compliant": trial["metrics"].get("evidence_compliant"),
                "schema_valid": trial["metrics"].get("schema_valid"),
                "protocol_compliant": trial["metrics"].get("protocol_compliant"),
                "provenance_compliant": trial["metrics"].get("provenance_compliant"),
                "path_convention_compliant": trial["metrics"].get("path_convention_compliant"),
                "primary_correct": trial["metrics"].get("primary_correct"),
                "related_correct": trial["metrics"].get("related_correct"),
                "semantic_correct": trial["metrics"].get("semantic_correct"),
                "location_exact": trial["metrics"].get("location_exact"),
                "strict_pass": trial["metrics"].get("strict_pass"),
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
