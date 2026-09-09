from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codekg import ingest
from codekg.bulk_identity import content_hash

pytestmark = pytest.mark.unit


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_sql_disabled_preserves_python_discovery_and_legacy_hash(tmp_path: Path) -> None:
    python = _write(tmp_path, "app.py", "def run(): pass\n")
    sql = _write(tmp_path, "query.sql", "SELECT 1;")
    expected = hashlib.sha256(b"app.py" + python.read_bytes()).hexdigest()[:12]
    for config in (None, '[sql]\nenabled = false\ndatabase = "ignored"\n'):
        if config is not None:
            _write(tmp_path, "codekg.toml", config)
        assert list(ingest._iter_source_files(tmp_path)) == [python]
        assert ingest._content_hash(tmp_path) == content_hash(tmp_path) == expected
        sql.write_text("SELECT 2;", encoding="utf-8")
        assert content_hash(tmp_path) == expected


def test_sql_globs_exclusions_and_single_walk(tmp_path: Path, monkeypatch) -> None:
    _write(
        tmp_path,
        "codekg.toml",
        '[sql]\nenabled = true\ninclude = ["**/*.sql"]\nexclude = ["**/skip.sql", "ignored/**"]\n',
    )
    for relative in (
        "root.sql",
        "nested/query.sql",
        "nested/deeper/query.sql",
        "skip.sql",
        "nested/skip.sql",
        "ignored/query.sql",
        ".venv/hidden.sql",
        "app.py",
        "nested/app.py",
        "query.sql.txt",
    ):
        _write(tmp_path, relative, "")
    original = ingest.os.scandir
    visited: list[Path] = []

    def scandir(path):
        visited.append(Path(path))
        return original(path)

    original_load = ingest.load_sql_config
    loads: list[Path] = []

    def load(root):
        loads.append(root)
        return original_load(root)

    monkeypatch.setattr(ingest.os, "scandir", scandir)
    monkeypatch.setattr(ingest, "load_sql_config", load)
    paths = {path.relative_to(tmp_path).as_posix() for path in ingest._iter_source_files(tmp_path)}
    assert paths == {
        "root.sql",
        "nested/query.sql",
        "nested/deeper/query.sql",
        "app.py",
        "nested/app.py",
    }
    assert len(visited) == len(set(visited))
    assert tmp_path / ".venv" not in visited
    assert loads == [tmp_path]


def test_mixed_repository_dispatches_real_sql_parser(tmp_path: Path) -> None:
    _write(tmp_path, "codekg.toml", '[sql]\nenabled = true\ndatabase = "warehouse"\n')
    _write(tmp_path, "app.py", 'def run():\n    return "SELECT * FROM ignored;"\n')
    _write(tmp_path, "query.sql", "SELECT * FROM public.widgets;")
    repo = ingest.scan_repository(tmp_path)
    files = {file.path: file for file in repo.files}
    assert set(files) == {"app.py", "query.sql"}
    assert files["app.py"].language == "python"
    assert files["app.py"].symbols
    assert not files["app.py"].sql_artifacts
    sql = files["query.sql"]
    assert sql.language == "sql"
    assert sql.parse_status == "ok"
    assert sql.module_init is None
    assert not (sql.symbols or sql.imports or sql.calls or sql.local_bindings)
    assert sql.sql_artifacts and sql.sql_statements and sql.sql_object_refs
    assert repo.commit == content_hash(tmp_path)


def test_enabled_sql_and_semantic_config_change_identity(tmp_path: Path) -> None:
    config = _write(tmp_path, "codekg.toml", "[sql]\nenabled = true\n")
    sql = _write(tmp_path, "query.sql", "SELECT 1;")
    first = content_hash(tmp_path)
    sql.write_text("SELECT 2;", encoding="utf-8")
    second = content_hash(tmp_path)
    assert first != second
    config.write_text('[sql]\nenabled = true\ndatabase = "other"\n', encoding="utf-8")
    third = content_hash(tmp_path)
    assert second != third
    assert ingest._content_hash(tmp_path) == third
    config.write_text('# comment\n[sql]\ndatabase = "other"\nenabled = true\n', encoding="utf-8")
    assert ingest._content_hash(tmp_path) == content_hash(tmp_path) == third


@pytest.mark.parametrize("operation", [ingest._content_hash, content_hash, ingest.scan_repository])
def test_malformed_sql_config_fails_clearly(tmp_path: Path, operation) -> None:
    _write(tmp_path, "codekg.toml", '[sql]\nenabled = "yes"\n')
    with pytest.raises(TypeError, match=r"\[sql\].enabled must be a boolean"):
        operation(tmp_path)


def test_discovery_rejects_malformed_toml(tmp_path: Path) -> None:
    _write(tmp_path, "codekg.toml", "[sql\n")
    with pytest.raises(ValueError):
        list(ingest._iter_source_files(tmp_path))
