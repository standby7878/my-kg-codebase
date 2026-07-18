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
    assert [symbol.qname for symbol in worker_file.symbols] == [
        "worker.__module__",
        "worker.BaseWorker",
        "worker.Worker",
        "worker.Worker.run",
        "worker.build",
    ]
    assert worker_file.symbols[3].kind == "method"
    assert worker_file.symbols[3].cyclomatic == 2
    inheritance = [
        (edge.type_qname, edge.base_name, edge.base_qname) for edge in worker_file.inheritance
    ]
    assert inheritance == [("worker.Worker", "BaseWorker", "worker.BaseWorker")]
    assert {import_ir.module for import_ir in worker_file.imports} == {"os", "pathlib"}
    calls = [(call.caller_qname, call.callee_name, call.callee_qname) for call in worker_file.calls]
    assert calls == [
        ("worker.Worker.run", "Path", "worker.Path"),
        ("worker.Worker.run", "getcwd", "os.getcwd"),
        ("worker.build", "Worker", "worker.Worker"),
        ("worker.build", "run", "worker.run"),
        ("worker.__module__", "build", "worker.build"),
    ]


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
    assert worker_file.symbols[1].docstring == "Create a worker from the configured defaults."
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
        "sample.worker.__module__",
        "sample.worker.outer_one",
        "sample.worker.outer_one.<locals>.inner",
        "sample.worker.outer_two",
        "sample.worker.outer_two.<locals>.inner",
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
