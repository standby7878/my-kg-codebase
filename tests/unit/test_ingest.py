from __future__ import annotations

from pathlib import Path

import pytest

from codekg.ingest import scan_repository

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


def test_scan_repository_extracts_docs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "worker.py").write_text("def build():\n    return 1\n", encoding="utf-8")
    (repo_root / "README.md").write_text(
        "# Usage\nCall `worker.build` to create a worker.\n",
        encoding="utf-8",
    )

    repo = scan_repository(repo_root)

    assert [doc.path for doc in repo.docs] == ["README.md"]
    assert repo.docs[0].doc_type == "markdown"
    assert repo.docs[0].chunks[0].heading_path == "Usage"
    assert repo.docs[0].chunks[0].mentions == ("worker.build",)


def test_scan_repository_content_hash_includes_docs(tmp_path: Path) -> None:
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
