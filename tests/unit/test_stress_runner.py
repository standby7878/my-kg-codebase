from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[2] / "tools" / "stress_corpus" / "run-ingestion-stress.sh"


@pytest.mark.unit
def test_stress_runner_has_valid_bash_syntax() -> None:
    completed = subprocess.run(["bash", "-n", str(RUNNER)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.unit
def test_runner_is_bulk_only_and_records_required_artifacts() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "index-sources --mode bulk" in source
    assert "stdout.log" in source
    assert "stderr.log" in source
    assert "status.json" in source
    assert "summary.json" in source
    assert "summary.md" in source
    assert "down -v" not in source


@pytest.mark.unit
def test_dry_run_prints_bulk_only_plan_without_creating_paths(tmp_path: Path) -> None:
    results = tmp_path / "results"
    corpus = tmp_path / "corpus"
    run_dir = results / "chosen-run"
    completed = subprocess.run(
        [
            "bash",
            str(RUNNER),
            "--dry-run",
            "--preset",
            "small",
            "--corpus",
            str(corpus),
            "--results-root",
            str(results),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "index-sources --mode bulk" in completed.stdout
    assert not results.exists()
    assert not corpus.exists()


@pytest.mark.unit
@pytest.mark.parametrize("compose_exit", [0, 1])
def test_runner_records_stubbed_bulk_run_without_docker(tmp_path: Path, compose_exit: int) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = run-compose.sh ]; then\n'
        '  printf "%s\\n" "$CODEKG_REPOS_ROOT" > "$STUB_REPOS_ROOT_CAPTURE"\n'
        "  echo \"  'elapsed_seconds': 3.5,\"\n"
        '  echo "CODEKG_PHASE_START export 2026-01-01T00:00:00Z"\n'
        '  echo "CODEKG_PHASE_END export 2026-01-01T00:00:02Z"\n'
        '  exit "${STUB_COMPOSE_EXIT:-0}"\n'
        "fi\n"
        'exec /bin/bash "$@"\n',
        encoding="utf-8",
    )
    fake_bash.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nif [ "$1" = run ]; then printf \'{"counts": {}}\\n\'; fi\nexit 0\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    results = tmp_path / "results"
    corpus = tmp_path / "corpus with spaces"
    run_dir = results / "run"
    repos_root_capture = tmp_path / "repos-root.txt"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYTHON": sys.executable,
        "STUB_COMPOSE_EXIT": str(compose_exit),
        "STUB_REPOS_ROOT_CAPTURE": str(repos_root_capture),
    }

    completed = subprocess.run(
        [
            "/bin/bash",
            str(RUNNER),
            "--preset",
            "small",
            "--warmups",
            "0",
            "--runs",
            "1",
            "--corpus",
            str(corpus),
            "--results-root",
            str(results),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == compose_exit, completed.stderr
    assert repos_root_capture.read_text(encoding="utf-8").strip() == str(corpus)
    assert (run_dir / "context.json").is_file()
    assert (run_dir / "runs" / "measured-001" / "status.json").is_file()
    assert (run_dir / "summary.json").is_file()
    summary = (run_dir / "summary.json").read_text(encoding="utf-8")
    if compose_exit:
        assert '"median_successful_phase_elapsed_seconds": {}' in summary
    else:
        assert '"export": 2.0' in summary
        status = json.loads(
            (run_dir / "runs" / "measured-001" / "status.json").read_text(encoding="utf-8")
        )
        assert {"metric": "elapsed_seconds", "value": 3.5} in status["cli_metrics"]
    if compose_exit:
        assert "completed with failures" in completed.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--runs", "0"], "--runs must be greater than zero"),
        (["--warmups", "x"], "non-negative integer"),
    ],
)
def test_runner_rejects_invalid_counts(arguments: list[str], message: str) -> None:
    completed = subprocess.run(["bash", str(RUNNER), *arguments], capture_output=True, text=True)
    assert completed.returncode == 2
    assert message in completed.stderr
