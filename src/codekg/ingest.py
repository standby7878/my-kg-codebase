from __future__ import annotations

import ast
import hashlib
import subprocess
from collections.abc import Iterable
from pathlib import Path

from codekg.ir import CallIR, FileIR, ImportIR, InheritanceIR, RepositoryIR, SymbolIR
from codekg.loader import load_repository

LANGUAGES_BY_SUFFIX = {
    ".py": "python",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}


class _PythonExtractor(ast.NodeVisitor):
    def __init__(self, path: str, module_qname: str) -> None:
        self.path = path
        self.module_qname = module_qname
        self.imports: list[ImportIR] = []
        self.symbols: list[SymbolIR] = []
        self.inheritance: list[InheritanceIR] = []
        self.calls: list[CallIR] = []
        self._class_stack: list[str] = []
        self._function_stack: list[str] = []
        self._module_callable_qname = f"{module_qname}.__module__"
        self._callable_stack: list[str] = [self._module_callable_qname]
        self.symbols.append(
            SymbolIR(
                kind="function",
                name="<module>",
                qname=self._module_callable_qname,
                signature="<module>",
                start_line=1,
                end_line=1,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(ImportIR(module=alias.name, name=alias.name, alias=alias.asname))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self._import_from_module(node)
        if module is None:
            return
        for alias in node.names:
            self.imports.append(ImportIR(module=module, name=alias.name, alias=alias.asname))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = ".".join([self.module_qname, *self._class_stack, node.name])
        self.symbols.append(
            SymbolIR(
                kind="type",
                name=node.name,
                qname=qname,
                signature=f"class {node.name}",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
            )
        )
        for base in node.bases:
            base_name, base_qname = self._base_from_expr(base)
            if base_name:
                self.inheritance.append(
                    InheritanceIR(
                        type_qname=qname,
                        base_name=base_name,
                        base_qname=base_qname,
                    )
                )
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> None:
        if self._callable_stack:
            callee_name, callee_qname, receiver = self._callee_from_expr(node.func)
            if callee_name:
                self.calls.append(
                    CallIR(
                        caller_qname=self._callable_stack[-1],
                        callee_name=callee_name,
                        callee_qname=callee_qname,
                        receiver=receiver,
                        line=node.lineno,
                    )
                )
        self.generic_visit(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        parent_qname = (
            ".".join([self.module_qname, *self._class_stack])
            if self._class_stack and not self._function_stack
            else None
        )
        parts = [self.module_qname, *self._class_stack, *self._function_stack]
        if self._function_stack:
            parts.append("<locals>")
        parts.append(node.name)
        qname = ".".join(parts)
        kind = "method" if self._class_stack and not self._function_stack else "function"
        prefix = "async " if is_async else ""
        self.symbols.append(
            SymbolIR(
                kind=kind,
                name=node.name,
                qname=qname,
                signature=f"{prefix}def {node.name}{_format_args(node.args)}",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                cyclomatic=_cyclomatic(node),
                parent_qname=parent_qname,
            )
        )
        self._function_stack.append(node.name)
        self._callable_stack.append(qname)
        self.generic_visit(node)
        self._callable_stack.pop()
        self._function_stack.pop()

    def _callee_from_expr(self, node: ast.expr) -> tuple[str | None, str | None, str | None]:
        if isinstance(node, ast.Name):
            return node.id, f"{self.module_qname}.{node.id}", None
        if not isinstance(node, ast.Attribute):
            return None, None, None

        chain = _attribute_chain(node)
        if not chain:
            return node.attr, None, None

        receiver = chain[0] if len(chain) > 1 else None
        if receiver in {"self", "cls"} and self._class_stack:
            qname = ".".join([self.module_qname, *self._class_stack, node.attr])
            return node.attr, qname, receiver

        return node.attr, ".".join(chain), receiver

    def _base_from_expr(self, node: ast.expr) -> tuple[str | None, str | None]:
        if isinstance(node, ast.Name):
            return node.id, f"{self.module_qname}.{node.id}"
        if isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if chain:
                return node.attr, ".".join(chain)
            return node.attr, None
        if isinstance(node, ast.Subscript):
            return self._base_from_expr(node.value)
        if isinstance(node, ast.Call):
            return self._base_from_expr(node.func)
        return None, None

    def _import_from_module(self, node: ast.ImportFrom) -> str | None:
        if node.level == 0:
            return node.module

        package_parts = self.module_qname.split(".")
        if not self.path.endswith("__init__.py") and package_parts:
            package_parts.pop()

        climb = max(0, node.level - 1)
        if climb:
            package_parts = package_parts[:-climb]

        if node.module:
            package_parts.extend(node.module.split("."))

        return ".".join(package_parts) if package_parts else node.module


def index_repository(path: Path, *, replace: bool) -> dict[str, int | str]:
    """Extract a repository and load it into Neo4j."""

    repo = scan_repository(path)
    result = load_repository(repo, replace=replace)
    return {
        "repo_name": repo.repo_name,
        "commit": repo.commit,
        "files": len(repo.files),
        **result,
    }


def scan_repository(path: Path) -> RepositoryIR:
    root = path.resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {path}")

    repo_name = root.name
    commit = _git_commit(root) or _content_hash(root)
    files = tuple(_scan_file(root, file_path) for file_path in _iter_source_files(root))
    return RepositoryIR(repo_name=repo_name, commit=commit, root_path=str(root), files=files)


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in LANGUAGES_BY_SUFFIX:
            yield path


def _scan_file(root: Path, path: Path) -> FileIR:
    rel_path = path.relative_to(root).as_posix()
    language = LANGUAGES_BY_SUFFIX[path.suffix.lower()]
    text = path.read_text(encoding="utf-8", errors="replace")
    loc = len(text.splitlines())
    module_qname = _module_qname(rel_path)
    if language != "python":
        return FileIR(path=rel_path, language=language, loc=loc, module_qname=module_qname)

    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return FileIR(path=rel_path, language=language, loc=loc, module_qname=module_qname)

    extractor = _PythonExtractor(rel_path, module_qname)
    extractor.visit(tree)
    return FileIR(
        path=rel_path,
        language=language,
        loc=loc,
        module_qname=module_qname,
        imports=tuple(extractor.imports),
        symbols=tuple(extractor.symbols),
        inheritance=tuple(extractor.inheritance),
        calls=tuple(extractor.calls),
    )


def _module_qname(rel_path: str) -> str:
    path = Path(rel_path)
    if path.suffix == ".py":
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) if parts else path.parent.name
    return path.as_posix()


def _format_args(args: ast.arguments) -> str:
    names = [arg.arg for arg in [*args.posonlyargs, *args.args]]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return f"({', '.join(names)})"


def _attribute_chain(node: ast.expr) -> list[str]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return []
    return list(reversed(parts))


def _cyclomatic(node: ast.AST) -> int:
    decision_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ExceptHandler,
        ast.IfExp,
        ast.BoolOp,
        ast.Try,
        ast.Match,
    )
    return 1 + sum(isinstance(child, decision_nodes) for child in ast.walk(node))


def _git_commit(root: Path) -> str | None:
    commit = _git_commit_from_dir(root)
    if commit:
        return commit[:12]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _git_commit_from_dir(root: Path) -> str | None:
    git_path = root / ".git"
    if git_path.is_file():
        line = git_path.read_text(encoding="utf-8", errors="replace").strip()
        if not line.startswith("gitdir:"):
            return None
        git_path = (root / line.removeprefix("gitdir:").strip()).resolve()
    if not git_path.is_dir():
        return None

    head_path = git_path / "HEAD"
    if not head_path.is_file():
        return None
    head = head_path.read_text(encoding="utf-8", errors="replace").strip()
    if not head.startswith("ref:"):
        return head if _looks_like_commit(head) else None

    ref_name = head.removeprefix("ref:").strip()
    ref_path = git_path / ref_name
    if ref_path.is_file():
        commit = ref_path.read_text(encoding="utf-8", errors="replace").strip()
        return commit if _looks_like_commit(commit) else None

    packed_refs = git_path / "packed-refs"
    if not packed_refs.is_file():
        return None
    for line in packed_refs.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split(" ", maxsplit=1)
        if len(parts) == 2 and parts[1] == ref_name and _looks_like_commit(parts[0]):
            return parts[0]
    return None


def _looks_like_commit(value: str) -> bool:
    return len(value) >= 12 and all(char in "0123456789abcdefABCDEF" for char in value)


def _content_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _iter_source_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
