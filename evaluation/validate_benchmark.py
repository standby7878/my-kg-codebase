#!/usr/bin/env python3
"""Validate one immutable Requests benchmark trial and emit metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmark_lib import (
    MANIFEST_PATH,
    load_manifest,
    read_json,
    resolve_manifest_file,
    validate_trial,
    write_json_new,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--arm", choices=("codekg", "native"))
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--write",
        action="store_true",
        help="create validation.json and metrics.json in the trial directory",
    )
    args = parser.parse_args()

    trial = args.trial.resolve()
    metadata_path = trial / "metadata.json"
    metadata = read_json(metadata_path) if metadata_path.is_file() else {}
    arm = args.arm or metadata.get("arm")
    if arm not in {"codekg", "native"}:
        parser.error("--arm is required when metadata.json does not identify the arm")
    manifest = load_manifest(args.manifest)
    validation, metrics = validate_trial(
        arm=arm,
        events_path=trial / "events.jsonl",
        answer_path=trial / "answer.json",
        repository=args.requests.resolve(),
        gold_path=resolve_manifest_file(args.manifest, manifest["gold_file"]),
        wall_seconds=metadata.get("wall_seconds"),
        exit_code=metadata.get("exit_code"),
    )
    result = {"validation": validation, "metrics": metrics}
    if args.write:
        write_json_new(trial / "validation.json", validation)
        write_json_new(trial / "metrics.json", metrics)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if validation["valid"] else 1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
