from __future__ import annotations

import gc
from pathlib import Path

import pytest

from codekg.ingest import index_repository, scan_repository
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
