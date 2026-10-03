from __future__ import annotations

import gc
import logging
from pathlib import Path

import pytest

from codekg.ingest import (
    _content_hash,
    _iter_source_files,
    _scan_file,
    index_repository,
    iter_markdown_files,
    scan_repository,
)
from codekg.ir import FileIR, RepositoryIR, SymbolIR
from codekg.search_index import callable_docs_from_repository
from codekg.zvec_store import fetch_symbol_docs, open_write, upsert_symbol_docs

pytestmark = pytest.mark.unit


def test_scan_repository_extracts_python_symbols(tmp_path: Path) -> None:
    package = tmp_path / "sample"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "worker.py").write_text(
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "",
                "class BaseWorker:",
                "    pass",
                "",
                "class Worker(BaseWorker):",
                "    def run(self, value):",
                "        if value:",
                "            return Path(os.getcwd())",
                "        return None",
                "",
                "def build():",
                "    worker = Worker()",
                "    return worker.run()",
                "",
                "build()",
            ]
        ),
        encoding="utf-8",
    )

    repo = scan_repository(package)

    worker_file = next(file for file in repo.files if file.path == "worker.py")
    assert worker_file.language == "python"
    assert worker_file.module_init is not None
    assert worker_file.module_init.qname == "worker.__module__"
    assert [symbol.qname for symbol in worker_file.symbols] == [
        "worker.BaseWorker",
        "worker.Worker",
        "worker.Worker.run",
        "worker.build",
    ]
    assert worker_file.symbols[2].kind == "method"
    assert worker_file.symbols[2].cyclomatic == 2
    inheritance = [
        (edge.type_qname, edge.base_name, edge.base_qname) for edge in worker_file.inheritance
    ]
    assert inheritance == [("worker.Worker", "BaseWorker", "worker.BaseWorker")]
    assert {import_ir.module for import_ir in worker_file.imports} == {"os", "pathlib"}
    calls = [
        (call.owner_qname, call.raw_callee, call.callee_name, call.callee_qname_hint)
        for call in worker_file.calls
    ]
    assert calls == [
        ("worker.Worker.run", "Path", "Path", "worker.Path"),
        ("worker.Worker.run", "os.getcwd", "getcwd", "os.getcwd"),
        ("worker.build", "Worker", "Worker", "worker.Worker"),
        ("worker.build", "worker.run", "run", "worker.run"),
        ("worker.__module__", "build", "build", "worker.build"),
    ]
    assert [call.receiver_kind for call in worker_file.calls] == [
        "none",
        "name",
        "none",
        "attribute",
        "none",
    ]
    assert [call.ordinal for call in worker_file.calls] == [1, 2, 3, 4, 5]
    assert [
        (
            binding.owner_qname,
            binding.target_name,
            binding.value_kind,
            binding.value_name,
            binding.guarded,
        )
        for binding in worker_file.local_bindings
    ] == [("worker.build", "worker", "call", "Worker", False)]


def test_scan_logs_aggregate_file_and_repository_lifecycle(caplog, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "worker.py").write_text(
        'import os\n\ndef build():\n    """private documentation"""\n    return os.getcwd()\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="codekg.ingest"):
        scan_repository(repo_root)

    assert "codekg_scan_started" in caplog.text
    assert "codekg_scan_file" in caplog.text
    assert "codekg_scan_completed" in caplog.text
    assert '"imports": 1' in caplog.text
    assert '"functions": 1' in caplog.text
    assert '"calls": 1' in caplog.text
    assert "private documentation" not in caplog.text
    assert "os.getcwd()" not in caplog.text


def test_scan_repository_extracts_docstrings_and_markdown_descriptions(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "worker.py").write_text(
        'def build():\n    """Create a worker from the configured defaults."""\n    return 1\n',
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text(
        "# Usage\nCall `worker.build` to create a worker.\n",
        encoding="utf-8",
    )

    repo = scan_repository(repo_root)

    worker_file = next(file for file in repo.files if file.path == "worker.py")
    assert worker_file.symbols[0].docstring == "Create a worker from the configured defaults."
    assert repo.markdown_descriptions == {
        "worker.build": ("# Usage\nCall `worker.build` to create a worker.",)
    }
    assert not hasattr(repo, "docs")


def test_scan_repository_keeps_markdown_descriptions_in_lexical_path_order(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "worker.py").write_text("def build():\n    return 1\n", encoding="utf-8")
    (repo_root / "a").mkdir()
    (repo_root / "a.md").write_text("Top-level worker.build description.", encoding="utf-8")
    (repo_root / "a" / "note.md").write_text("Nested worker.build description.", encoding="utf-8")

    repo = scan_repository(repo_root)

    assert repo.markdown_descriptions["worker.build"] == (
        "Top-level worker.build description.",
        "Nested worker.build description.",
    )


def test_scan_repository_excludes_virtual_environment_directories(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "app.py").write_text("def live():\n    return 1\n", encoding="utf-8")
    virtualenv = repo_root / ".venv" / "lib"
    virtualenv.mkdir(parents=True)
    (virtualenv / "hidden.py").write_text("def hidden():\n    return 1\n", encoding="utf-8")
    (virtualenv / "README.md").write_text("Use `hidden.hidden`.\n", encoding="utf-8")

    repo = scan_repository(repo_root)

    assert [file.path for file in repo.files] == ["app.py"]
    assert repo.markdown_descriptions == {}


def test_repository_file_walkers_prune_skipped_directories_and_are_deterministic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "a" / "nested").mkdir(parents=True)
    (root / "z").mkdir()
    (root / "node_modules" / "package").mkdir(parents=True)
    (root / "a" / "nested" / "worker.py").write_text("pass\n", encoding="utf-8")
    (root / "a" / "README.md").write_text("# A\n", encoding="utf-8")
    (root / "z" / "last.py").write_text("pass\n", encoding="utf-8")
    (root / "node_modules" / "package" / "ignored.py").write_text("pass\n", encoding="utf-8")
    (root / "node_modules" / "package" / "README.md").write_text("# ignored\n", encoding="utf-8")
    (root / "linked.py").symlink_to(root / "z" / "last.py")
    (root / "linked-directory").symlink_to(root / "a", target_is_directory=True)

    source_paths = [path.relative_to(root).as_posix() for path in _iter_source_files(root)]
    markdown_paths = [path.relative_to(root).as_posix() for path in iter_markdown_files(root)]

    assert source_paths == ["a/nested/worker.py", "linked.py", "z/last.py"]
    assert markdown_paths == ["a/README.md"]
    assert source_paths == [path.relative_to(root).as_posix() for path in _iter_source_files(root)]


@pytest.mark.parametrize(
    ("text", "expected_loc"),
    [
        ("", 0),
        ("pass", 1),
        ("pass\n", 1),
        ("pass\n\n", 2),
        ("pass\nnext", 2),
    ],
)
def test_scan_file_counts_lines_without_allocating_line_list(
    tmp_path: Path, text: str, expected_loc: int
) -> None:
    path = tmp_path / "module.py"
    path.write_text(text, encoding="utf-8")

    assert _scan_file(tmp_path, path).loc == expected_loc


def test_scan_repository_content_hash_includes_markdown(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "worker.py").write_text("def build():\n    return 1\n", encoding="utf-8")
    readme = repo_root / "README.md"
    readme.write_text("# Usage\nFirst version.\n", encoding="utf-8")

    first = scan_repository(repo_root).commit
    readme.write_text("# Usage\nSecond version.\n", encoding="utf-8")
    second = scan_repository(repo_root).commit

    assert first != second


def test_content_hash_ignores_skipped_files_and_is_stable_for_walk_order(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "z").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "node_modules").mkdir()
    (root / "z" / "worker.py").write_text("def worker():\n    pass\n", encoding="utf-8")
    (root / "a" / "README.md").write_text("# Usage\n", encoding="utf-8")
    (root / "node_modules" / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")

    first = _content_hash(root)
    (root / "node_modules" / "ignored.py").write_text("raise ValueError\n", encoding="utf-8")

    assert _content_hash(root) == first


def test_scan_repository_resolves_relative_imports_and_nested_function_qnames(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    package = repo_root / "sample"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "helpers.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (package / "worker.py").write_text(
        "\n".join(
            [
                "from . import helpers",
                "from .helpers import helper",
                "",
                "def outer_one():",
                "    def inner():",
                "        return helper()",
                "    return inner()",
                "",
                "def outer_two():",
                "    def inner():",
                "        return helpers.helper()",
                "    return inner()",
            ]
        ),
        encoding="utf-8",
    )

    repo = scan_repository(repo_root)

    worker_file = next(file for file in repo.files if file.path == "sample/worker.py")
    assert [(item.module, item.name) for item in worker_file.imports] == [
        ("sample", "helpers"),
        ("sample.helpers", "helper"),
    ]
    assert [symbol.qname for symbol in worker_file.symbols] == [
        "sample.worker.outer_one",
        "sample.worker.outer_one.<locals>.inner",
        "sample.worker.outer_two",
        "sample.worker.outer_two.<locals>.inner",
    ]


def test_scan_repository_excludes_nested_scopes_from_cyclomatic_complexity(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "complexity_repo"
    repo_root.mkdir()
    (repo_root / "flows.py").write_text(
        "def outer(value):\n"
        "    if value and value > 0:\n"
        "        return value\n"
        "    def helper(item):\n"
        "        if item:\n"
        "            return item\n"
        "        return 0\n"
        "    class Local:\n"
        "        if True:\n"
        "            pass\n"
        "    transform = lambda item: item if item else 0\n"
        "    return helper(transform(value))\n",
        encoding="utf-8",
    )

    repo = scan_repository(repo_root)

    flows_file = next(file for file in repo.files if file.path == "flows.py")
    assert [(symbol.qname, symbol.cyclomatic) for symbol in flows_file.symbols] == [
        ("flows.outer", 3),
        ("flows.outer.<locals>.helper", 2),
        ("flows.outer.<locals>.Local", 1),
    ]


def test_scan_repository_uses_repository_name_for_root_init_qnames(tmp_path: Path) -> None:
    repo_root = tmp_path / "package_repo"
    repo_root.mkdir()
    (repo_root / "__init__.py").write_text(
        "def function(value):\n    if value:\n        return value\n    return None\n",
        encoding="utf-8",
    )
    nested = repo_root / "pkg"
    nested.mkdir()
    (nested / "__init__.py").write_text("def nested():\n    return 1\n", encoding="utf-8")

    repo = scan_repository(repo_root)

    root_file = next(file for file in repo.files if file.path == "__init__.py")
    nested_file = next(file for file in repo.files if file.path == "pkg/__init__.py")
    assert root_file.module_qname == "package_repo"
    assert root_file.module_init is not None
    assert root_file.module_init.qname == "package_repo.__module__"
    assert [symbol.qname for symbol in root_file.symbols] == ["package_repo.function"]
    assert root_file.symbols[0].qname != ".function"
    assert root_file.module_init.qname != ".__module__"
    assert nested_file.module_qname == "pkg"
    assert nested_file.module_init is not None
    assert nested_file.module_init.qname == "pkg.__module__"
    assert [symbol.qname for symbol in nested_file.symbols] == ["pkg.nested"]
    for file in repo.files:
        assert all(not symbol.qname.startswith(".") for symbol in file.symbols)
        assert file.module_init is None or not file.module_init.qname.startswith(".")


def test_scan_repository_retains_all_call_sites_and_syntax_diagnostics(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "calls.py").write_text(
        "def run():\n"
        "    first(second())\n"
        "    self.save()\n"
        "    cls.create()\n"
        "    super().close()\n"
        "    object.attr.work()\n",
        encoding="utf-8",
    )
    (repo_root / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    repo = scan_repository(repo_root)

    calls_file = next(file for file in repo.files if file.path == "calls.py")
    assert calls_file.parse_status == "ok"
    assert calls_file.diagnostics == ()
    assert calls_file.module_init is not None
    assert [(call.raw_callee, call.ordinal) for call in calls_file.calls] == [
        ("first", 1),
        ("second", 2),
        ("self.save", 3),
        ("cls.create", 4),
        ("super().close", 5),
        ("super", 6),
        ("object.attr.work", 7),
    ]
    assert [call.receiver_kind for call in calls_file.calls] == [
        "none",
        "none",
        "self",
        "cls",
        "super",
        "none",
        "attribute",
    ]
    assert all(call.start_line <= call.end_line for call in calls_file.calls)

    broken_file = next(file for file in repo.files if file.path == "broken.py")
    assert broken_file.module_init is not None
    assert broken_file.parse_status == "error"
    assert broken_file.symbols == ()
    assert broken_file.calls == ()
    assert broken_file.diagnostics[0].category == "syntax_error"
    assert broken_file.diagnostics[0].severity == "error"
    assert broken_file.diagnostics[0].line == 1


def test_scan_repository_only_marks_genuine_self_and_cls_calls_as_direct(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "receivers.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        self.connect()\n"
        "        self.pool.connect()\n"
        "        self.connection.execute()\n"
        "        self.layer.connection.execute()\n"
        "\n"
        "    @classmethod\n"
        "    def build(cls):\n"
        "        cls.create()\n"
        "        cls.factory.create()\n",
        encoding="utf-8",
    )

    repository = scan_repository(repo_root)
    source_file = next(file for file in repository.files if file.path == "receivers.py")

    assert [
        (call.raw_callee, call.receiver_kind, call.callee_qname_hint) for call in source_file.calls
    ] == [
        ("self.connect", "self", "receivers.Worker.connect"),
        ("self.pool.connect", "attribute", "self.pool.connect"),
        ("self.connection.execute", "attribute", "self.connection.execute"),
        ("self.layer.connection.execute", "attribute", "self.layer.connection.execute"),
        ("cls.create", "cls", "receivers.Worker.create"),
        ("cls.factory.create", "attribute", "cls.factory.create"),
    ]


def test_replace_index_removes_previous_zvec_keys_before_graph_load(
    tmp_path: Path, monkeypatch
) -> None:
    old_key = "sample@old:worker.py:worker.old:1"
    repo = RepositoryIR(
        repo_name="sample",
        commit="new",
        root_path="/repos/sample",
        files=(
            FileIR(
                path="worker.py",
                language="python",
                loc=3,
                module_qname="worker",
                symbols=(
                    SymbolIR(
                        kind="function",
                        name="new",
                        qname="worker.new",
                        signature="def new()",
                        start_line=1,
                        end_line=3,
                        docstring="Create the new worker.",
                    ),
                ),
            ),
        ),
    )
    zvec_path = str(tmp_path / "zvec")
    collection = open_write(zvec_path)
    upsert_symbol_docs(
        collection,
        [
            callable_docs_from_repository(
                RepositoryIR(
                    repo_name="sample",
                    commit="old",
                    root_path="/repos/sample",
                    files=(
                        FileIR(
                            path="worker.py",
                            language="python",
                            loc=1,
                            module_qname="worker",
                            symbols=(
                                SymbolIR(
                                    kind="function",
                                    name="old",
                                    qname="worker.old",
                                    signature="def old()",
                                    start_line=1,
                                    end_line=1,
                                ),
                            ),
                        ),
                    ),
                )
            )[0]
        ],
    )
    collection.flush()
    del collection
    gc.collect()
    order: list[str] = []

    monkeypatch.setattr("codekg.ingest.scan_repository", lambda path: repo)
    new_key = callable_docs_from_repository(repo)[0].key
    graph_rows = iter([[{"key": old_key}], [{"key": new_key}]])
    monkeypatch.setattr("codekg.ingest.iter_callable_rows", lambda **kwargs: next(graph_rows))
    monkeypatch.setattr(
        "codekg.ingest.load_repository",
        lambda *args, **kwargs: order.append("graph") or {"nodes": 2},
    )
    original_delete = __import__("codekg.ingest", fromlist=["delete_repo"]).delete_repo

    def tracked_delete(collection, repo_name):
        order.append("zvec")
        original_delete(collection, repo_name)

    monkeypatch.setattr("codekg.ingest.delete_repo", tracked_delete)

    result = index_repository(
        tmp_path,
        replace=True,
        client=object(),
        zvec_path=zvec_path,
    )

    assert result["descriptions"] == 1
    assert order == ["zvec", "graph"]
    gc.collect()
    checked_collection = open_write(zvec_path)
    assert fetch_symbol_docs(checked_collection, {old_key}) == {}
    assert fetch_symbol_docs(checked_collection, {new_key})[new_key]["key"] == new_key


def test_scan_repository_skips_inaccessible_directories_and_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "visible.py").write_text("def visible():\n    return 1\n", encoding="utf-8")

    blocked_dir = repo / "aiven" / "deploy" / "repo_gpg"
    blocked_dir.parent.mkdir(parents=True)
    blocked_dir.mkdir()
    blocked_dir.chmod(0o000)

    blocked_file = repo / "secret.py"
    blocked_file.write_text("def secret():\n    return 2\n", encoding="utf-8")
    blocked_file.chmod(0o000)

    scanned = scan_repository(repo)

    assert {file.path for file in scanned.files} == {"visible.py"}
