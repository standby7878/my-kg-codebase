from pathlib import Path

from codekg.bulk_identity import content_hash
from codekg.ingest import _content_hash


def test_content_hash_matches_legacy_path_component_order(tmp_path: Path) -> None:
    files = {
        "a.py": b"root",
        "a.z.py": b"dot",
        "a0.py": b"digit",
        "a_z.py": b"underscore",
        "a/nested.py": b"nested",
        "a/!punctuation.py": b"punctuation",
        "README.md": b"readme",
        "docs/a.md": b"docs",
    }
    for relative_path, contents in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    (tmp_path / "ignored.txt").write_bytes(b"ignored")

    assert content_hash(tmp_path) == _content_hash(tmp_path)


def test_content_hash_skips_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("def visible():\n    return 1\n", encoding="utf-8")
    blocked = tmp_path / "secret.py"
    blocked.write_text("def secret():\n    return 2\n", encoding="utf-8")
    blocked.chmod(0o000)

    assert content_hash(tmp_path) == _content_hash(tmp_path)


def test_content_hash_streams_large_files_without_read_bytes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "large.py"
    path.write_bytes(b"0123456789abcdef" * 200_000)
    expected = _content_hash(tmp_path)
    original_open = Path.open
    read_sizes: list[int] = []

    class CountingReader:
        def __init__(self, file_path: Path, *args, **kwargs) -> None:
            self._file = original_open(file_path, *args, **kwargs)

        def __enter__(self):
            self._file.__enter__()
            return self

        def __exit__(self, *args) -> None:
            self._file.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self._file.read(size)

    def counting_open(self: Path, *args, **kwargs):
        return CountingReader(self, *args, **kwargs)

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError("content_hash must read files in chunks")

    monkeypatch.setattr(Path, "open", counting_open)
    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    assert content_hash(tmp_path) == expected
    assert len(read_sizes) > 1
    assert all(size > 0 for size in read_sizes)
