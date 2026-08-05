from __future__ import annotations

import ast
import hashlib
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from codekg.docs import resolve_markdown_descriptions
from codekg.ir import (
    CallIR,
    FileIR,
    ImportIR,
    InheritanceIR,
    LocalBindingIR,
    ModuleInitIR,
    ParseDiagnosticIR,
    RepositoryIR,
    SymbolIR,
)
from codekg.loader import load_repository
from codekg.neo4j_client import Neo4jClient, get_client
from codekg.search_index import (
    callable_docs_from_repository,
    iter_callable_rows,
    validate_search_index_consistency,
)
from codekg.zvec_store import delete_repo, open_write, optimize_and_flush, upsert_symbol_docs

LANGUAGES_BY_SUFFIX = {
    ".py": "python",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
    "vendor",
}


@dataclass(frozen=True)
class _LexicalScope:
    """A lexical declaration scope used to construct Python qualified names."""

    kind: str
    qname: str


class _PythonExtractor(ast.NodeVisitor):
    def __init__(self, path: str, module_qname: str, source: str) -> None:
        self.path = path
        self.module_qname = module_qname
        self.source = source
        self.imports: list[ImportIR] = []
        self.symbols: list[SymbolIR] = []
        self.inheritance: list[InheritanceIR] = []
        self.calls: list[CallIR] = []
        self.local_bindings: list[LocalBindingIR] = []
        self._scope_stack: list[_LexicalScope] = []
        self._module_callable_qname = f"{module_qname}.__module__"
        self._callable_stack: list[str] = [self._module_callable_qname]
        self._call_ordinal = 0
        self._guard_depth = 0
        self._comprehension_scopes: list[set[str]] = []
        self._loop_rebound_scopes: list[set[str]] = []

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
        qname = self._child_qname(node.name)
        self.symbols.append(
            SymbolIR(
                kind="type",
                name=node.name,
                qname=qname,
                signature=f"class {node.name}",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=ast.get_docstring(node, clean=True),
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
        # Class decorators, bases, keywords, and body expressions are evaluated
        # while creating the class, not while calling one of its methods.  There
        # is no separate class-runtime graph node, so retain their enclosing
        # callable (or ModuleInit) attribution while using the lexical class
        # scope for names declared in the body.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

        self._scope_stack.append(_LexicalScope(kind="class", qname=qname))
        for statement in node.body:
            self.visit(statement)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> None:
        if self._callable_stack:
            callee_name, callee_qname_hint, receiver_kind = self._callee_from_expr(node.func)
            self._call_ordinal += 1
            self.calls.append(
                CallIR(
                    owner_qname=self._callable_stack[-1],
                    raw_callee=self._source_for(node.func),
                    callee_name=callee_name,
                    callee_qname_hint=callee_qname_hint,
                    receiver_kind=receiver_kind,
                    start_line=node.lineno,
                    start_column=node.col_offset,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    end_column=getattr(node, "end_col_offset", node.col_offset),
                    ordinal=self._call_ordinal,
                )
            )
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Lambda defaults execute in the enclosing context, but the body has no
        # graph owner of its own.  Omitting body calls is conservative and
        # prevents lambda parameters from inheriting enclosing local types.
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
        if not self._in_function_body():
            return
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._record_local_binding(node.targets[0].id, node.value, node)
            return
        for name in _bound_names(node.targets):
            self._record_unknown_binding(name, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        if not self._in_function_body() or not isinstance(node.target, ast.Name):
            return
        annotation = self._source_for(node.annotation)
        if node.value is None:
            return
        value_kind, value_name, value_hint = self._binding_value(node.value)
        self._append_local_binding(
            node.target.id,
            value_kind,
            value_name,
            value_hint,
            annotation,
            node,
        )

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)
        if self._in_function_body() and isinstance(node.target, ast.Name):
            self._record_unknown_binding(node.target.id, node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        if self._in_function_body() and isinstance(node.target, ast.Name):
            self._record_unknown_binding(node.target.id, node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.generic_visit(node)
        if not self._in_function_body():
            return
        for name in _bound_names(node.targets):
            self._record_unknown_binding(name, node)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_guarded(node.body)
        self._visit_guarded(node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        rebound_names = set(_bound_names((node.target,)))
        rebound_names.update(_loop_rebound_names(node.body))
        self._loop_rebound_scopes.append(rebound_names)
        self._guard_depth += 1
        try:
            for name in _bound_names((node.target,)):
                self._record_unknown_binding(name, node.target)
            self.visit(node.target)
            for child in (*node.body, *node.orelse):
                self.visit(child)
        finally:
            self._guard_depth -= 1
            self._loop_rebound_scopes.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_While(self, node: ast.While) -> None:
        self._loop_rebound_scopes.append(_loop_rebound_names((node.test, *node.body)))
        try:
            self.visit(node.test)
            self._visit_guarded((*node.body, *node.orelse))
        finally:
            self._loop_rebound_scopes.pop()

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                for name in _bound_names((item.optional_vars,)):
                    self._record_unknown_binding(name, item.optional_vars)
        for child in node.body:
            self.visit(child)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_Try(self, node: ast.Try) -> None:
        guarded_nodes: list[ast.AST] = [*node.body, *node.orelse, *node.finalbody]
        for handler in node.handlers:
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name:
                self._guard_depth += 1
                try:
                    self._record_unknown_binding(handler.name, handler)
                finally:
                    self._guard_depth -= 1
            guarded_nodes.extend(handler.body)
        self._visit_guarded(guarded_nodes)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self.visit_Try(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        guarded_nodes: list[ast.AST] = []
        for case in node.cases:
            guarded_nodes.append(case.pattern)
            self._guard_depth += 1
            try:
                for name in _pattern_bound_names(case.pattern):
                    self._record_unknown_binding(name, case.pattern)
            finally:
                self._guard_depth -= 1
            if case.guard is not None:
                guarded_nodes.append(case.guard)
            guarded_nodes.extend(case.body)
        self._visit_guarded(guarded_nodes)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> None:
        parent = self._scope_stack[-1] if self._scope_stack else None
        parent_qname = parent.qname if parent and parent.kind == "class" else None
        qname = self._child_qname(node.name)
        kind = "method" if parent and parent.kind == "class" else "function"
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
                docstring=ast.get_docstring(node, clean=True),
                return_annotation=self._source_for(node.returns) if node.returns else None,
            )
        )
        # These expressions execute when the function is defined.  Visiting
        # them before installing the new callable scope prevents decorator,
        # default, and annotation calls from being attributed to the function
        # body that has not executed yet.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

        self._scope_stack.append(_LexicalScope(kind="function", qname=qname))
        self._callable_stack.append(qname)
        self._record_parameter_bindings(node, qname)
        for statement in node.body:
            self.visit(statement)
        self._callable_stack.pop()
        self._scope_stack.pop()

    def _child_qname(self, name: str) -> str:
        if not self._scope_stack:
            return f"{self.module_qname}.{name}"
        parent = self._scope_stack[-1]
        separator = ".<locals>." if parent.kind == "function" else "."
        return f"{parent.qname}{separator}{name}"

    def _nearest_class_qname(self) -> str | None:
        for scope in reversed(self._scope_stack):
            if scope.kind == "class":
                return scope.qname
        return None

    def _callee_from_expr(
        self, node: ast.expr
    ) -> tuple[
        str | None,
        str | None,
        str,
    ]:
        if isinstance(node, ast.Name):
            if self._is_locally_shadowed_or_rebound(node.id):
                return node.id, None, "dynamic"
            return node.id, f"{self.module_qname}.{node.id}", "none"
        if not isinstance(node, ast.Attribute):
            return None, None, "dynamic"

        chain = _attribute_chain(node)
        if not chain:
            if _is_super_call(node.value):
                return node.attr, None, "super"
            return node.attr, None, "dynamic"

        receiver = chain[0]
        if self._is_locally_shadowed_or_rebound(receiver):
            return node.attr, None, "dynamic"
        if receiver in {"self", "cls"} and len(chain) == 2:
            owner_qname = self._nearest_class_qname()
            qname = f"{owner_qname}.{node.attr}" if owner_qname else None
            return node.attr, qname, receiver

        if len(chain) == 2 and receiver in self._imported_names():
            return node.attr, ".".join(chain), "name"
        return node.attr, ".".join(chain), "attribute"

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        result_expressions: tuple[ast.expr, ...],
    ) -> None:
        if not generators:
            for expression in result_expressions:
                self.visit(expression)
            return

        self.visit(generators[0].iter)
        repeating_nodes: list[ast.AST] = [*result_expressions]
        for index, generator in enumerate(generators):
            if index:
                repeating_nodes.append(generator.iter)
            repeating_nodes.extend(generator.ifs)
        self._loop_rebound_scopes.append(_loop_rebound_names(repeating_nodes))
        scopes_added = 0
        try:
            for index, generator in enumerate(generators):
                # The iterable is evaluated before this generator's target is
                # bound, but after all preceding generator targets are bound.
                if index:
                    self.visit(generator.iter)
                self._comprehension_scopes.append(set(_bound_names((generator.target,))))
                scopes_added += 1
                self.visit(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            for expression in result_expressions:
                self.visit(expression)
        finally:
            if scopes_added:
                del self._comprehension_scopes[-scopes_added:]
            self._loop_rebound_scopes.pop()

    def _is_locally_shadowed_or_rebound(self, name: str) -> bool:
        return any(
            name in scope
            for scope in reversed((*self._comprehension_scopes, *self._loop_rebound_scopes))
        )

    def _imported_names(self) -> set[str]:
        names: set[str] = set()
        for import_ir in self.imports:
            if import_ir.alias:
                names.add(import_ir.alias)
            else:
                names.add(import_ir.name)
                names.add(import_ir.module.split(".", maxsplit=1)[0])
        return names

    def call_sites(self) -> tuple[CallIR, ...]:
        ordered = sorted(
            self.calls,
            key=lambda call: (
                call.start_line,
                call.start_column,
                call.ordinal,
            ),
        )
        return tuple(replace(call, ordinal=index) for index, call in enumerate(ordered, start=1))

    def ordered_local_bindings(self) -> tuple[LocalBindingIR, ...]:
        return tuple(
            sorted(
                self.local_bindings,
                key=lambda binding: (
                    binding.start_line,
                    binding.start_column,
                    binding.target_name,
                ),
            )
        )

    def _in_function_body(self) -> bool:
        return bool(self._scope_stack and self._scope_stack[-1].kind == "function")

    def _record_parameter_bindings(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        owner_qname: str,
    ) -> None:
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        for argument in arguments:
            if argument.annotation is None:
                continue
            self.local_bindings.append(
                LocalBindingIR(
                    owner_qname=owner_qname,
                    target_name=argument.arg,
                    value_kind="annotation",
                    value_name=None,
                    value_qname_hint=None,
                    annotation=self._source_for(argument.annotation),
                    start_line=node.lineno,
                    start_column=-1,
                )
            )

    def _record_local_binding(self, target_name: str, value: ast.expr, node: ast.AST) -> None:
        value_kind, value_name, value_hint = self._binding_value(value)
        self._append_local_binding(
            target_name,
            value_kind,
            value_name,
            value_hint,
            None,
            node,
        )

    def _record_unknown_binding(self, target_name: str, node: ast.AST) -> None:
        if not self._in_function_body():
            return
        self._append_local_binding(target_name, "unknown", None, None, None, node)

    def _append_local_binding(
        self,
        target_name: str,
        value_kind: str,
        value_name: str | None,
        value_qname_hint: str | None,
        annotation: str | None,
        node: ast.AST,
    ) -> None:
        self.local_bindings.append(
            LocalBindingIR(
                owner_qname=self._callable_stack[-1],
                target_name=target_name,
                value_kind=value_kind,  # type: ignore[arg-type]
                value_name=value_name,
                value_qname_hint=value_qname_hint,
                annotation=annotation,
                start_line=getattr(node, "end_lineno", node.lineno),
                start_column=getattr(node, "end_col_offset", node.col_offset),
                guarded=self._guard_depth > 0,
            )
        )

    def _binding_value(self, value: ast.expr) -> tuple[str, str | None, str | None]:
        if isinstance(value, ast.Name):
            return "name", value.id, None
        if isinstance(value, ast.Call):
            name, hint, receiver_kind = self._callee_from_expr(value.func)
            if receiver_kind in {"none", "name"}:
                return "call", name, hint
        return "unknown", None, None

    def _visit_guarded(self, nodes: Iterable[ast.AST]) -> None:
        self._guard_depth += 1
        try:
            for node in nodes:
                self.visit(node)
        finally:
            self._guard_depth -= 1

    def _source_for(self, node: ast.AST) -> str:
        source = ast.get_source_segment(self.source, node)
        if source:
            return source
        try:
            return ast.unparse(node)
        except (AttributeError, ValueError):
            return type(node).__name__

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


def index_repository(
    path: Path,
    *,
    replace: bool,
    client: Neo4jClient | None = None,
    zvec_path: str | None = None,
) -> dict[str, int | str]:
    """Replace a repository graph and its one-per-callable descriptions."""

    repo = scan_repository(path)
    db = client or get_client()
    collection = open_write(zvec_path)
    replaced_keys = (
        {str(row["key"]) for row in iter_callable_rows(repo=repo.repo_name, client=db)}
        if replace
        else set()
    )
    if replace:
        # Remove stale search hits before the corresponding graph nodes disappear.
        delete_repo(collection, repo.repo_name)
        optimize_and_flush(collection)

    result = load_repository(repo, replace=replace, client=db)
    descriptions = callable_docs_from_repository(repo)
    indexed = upsert_symbol_docs(collection, descriptions)
    optimize_and_flush(collection)
    live_graph_keys = {
        str(row["key"]) for row in iter_callable_rows(repo=repo.repo_name, client=db)
    }
    consistency = validate_search_index_consistency(
        descriptions,
        live_graph_keys=live_graph_keys,
        replaced_keys=replaced_keys,
        collection=collection,
    )
    if not consistency["ok"]:
        raise RuntimeError(
            "zvec description index is inconsistent after ingest: "
            f"missing_in_graph={consistency['missing_in_graph']}, "
            f"unexpected_in_graph={consistency['unexpected_in_graph']}, "
            f"missing={consistency['missing_in_zvec']}, "
            f"stale_after_replace={consistency['stale_after_replace']}"
        )
    return {
        "repo_name": repo.repo_name,
        "commit": repo.commit,
        "files": len(repo.files),
        "descriptions": indexed,
        **result,
    }


def scan_repository(path: Path) -> RepositoryIR:
    root = path.resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {path}")

    repo_name = root.name
    commit = _git_commit(root) or _content_hash(root)
    files = tuple(_scan_file(root, file_path) for file_path in _iter_source_files(root))
    callable_qnames = {
        symbol.qname
        for file in files
        for symbol in file.symbols
        if symbol.kind in {"function", "method"}
    }
    markdown_descriptions = resolve_markdown_descriptions(
        iter_markdown_files(root), callable_qnames
    )
    return RepositoryIR(
        repo_name=repo_name,
        commit=commit,
        root_path=str(root),
        files=files,
        markdown_descriptions=markdown_descriptions,
    )


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in LANGUAGES_BY_SUFFIX:
            yield path


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() == ".md":
            yield path


def _scan_file(root: Path, path: Path) -> FileIR:
    rel_path = path.relative_to(root).as_posix()
    language = LANGUAGES_BY_SUFFIX[path.suffix.lower()]
    text = path.read_text(encoding="utf-8", errors="replace")
    loc = len(text.splitlines())
    module_qname = _module_qname(rel_path, repository_name=root.name)
    if language != "python":
        return FileIR(path=rel_path, language=language, loc=loc, module_qname=module_qname)

    module_init = ModuleInitIR(
        qname=f"{module_qname}.__module__",
        start_line=1,
        end_line=max(1, loc),
    )
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError as error:
        return FileIR(
            path=rel_path,
            language=language,
            loc=loc,
            module_qname=module_qname,
            module_init=module_init,
            parse_status="error",
            diagnostics=(
                ParseDiagnosticIR(
                    category="syntax_error",
                    severity="error",
                    line=error.lineno,
                    column=error.offset,
                    message=error.msg,
                ),
            ),
        )

    extractor = _PythonExtractor(rel_path, module_qname, text)
    extractor.visit(tree)
    return FileIR(
        path=rel_path,
        language=language,
        loc=loc,
        module_qname=module_qname,
        module_init=module_init,
        imports=tuple(extractor.imports),
        symbols=tuple(extractor.symbols),
        inheritance=tuple(extractor.inheritance),
        calls=extractor.call_sites(),
        local_bindings=extractor.ordered_local_bindings(),
    )


def _module_qname(rel_path: str, *, repository_name: str | None = None) -> str:
    path = Path(rel_path)
    if path.suffix == ".py":
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) if parts else (repository_name or path.parent.name)
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


def _bound_names(nodes: Iterable[ast.AST]) -> tuple[str, ...]:
    names: list[str] = []

    def collect(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Starred):
            collect(node.value)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                collect(element)

    for node in nodes:
        collect(node)
    return tuple(dict.fromkeys(names))


def _pattern_bound_names(pattern: ast.pattern) -> tuple[str, ...]:
    names: list[str] = []

    class _PatternNames(ast.NodeVisitor):
        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name:
                names.append(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name:
                names.append(node.name)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest:
                names.append(node.rest)
            self.generic_visit(node)

    _PatternNames().visit(pattern)
    return tuple(dict.fromkeys(names))


def _loop_rebound_names(nodes: Iterable[ast.AST]) -> set[str]:
    class _RebindingVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def record(self, target: ast.AST) -> None:
            self.names.update(_bound_names((target,)))

        def visit_Assign(self, node: ast.Assign) -> None:
            self.visit(node.value)
            for target in node.targets:
                self.record(target)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.visit(node.annotation)
            if node.value is not None:
                self.visit(node.value)
            self.record(node.target)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self.visit(node.value)
            self.record(node.target)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.visit(node.value)
            self.record(node.target)

        def visit_Delete(self, node: ast.Delete) -> None:
            for target in node.targets:
                self.record(target)

        def visit_For(self, node: ast.For) -> None:
            self.visit(node.iter)
            self.record(node.target)
            for child in (*node.body, *node.orelse):
                self.visit(child)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.visit_For(node)

        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    self.record(item.optional_vars)
            for child in node.body:
                self.visit(child)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            self.visit_With(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is not None:
                self.visit(node.type)
            if node.name:
                self.names.add(node.name)
            for child in node.body:
                self.visit(child)

        def visit_Match(self, node: ast.Match) -> None:
            self.visit(node.subject)
            for case in node.cases:
                self.names.update(_pattern_bound_names(case.pattern))
                if case.guard is not None:
                    self.visit(case.guard)
                for child in case.body:
                    self.visit(child)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.names.add(alias.asname or alias.name.split(".", maxsplit=1)[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                self.names.add(alias.asname or alias.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.names.add(node.name)
            self._visit_definition_expressions(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.names.add(node.name)
            self._visit_definition_expressions(node)

        def _visit_definition_expressions(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
            if node.returns is not None:
                self.visit(node.returns)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.names.add(node.name)
            for expression in (*node.decorator_list, *node.bases):
                self.visit(expression)
            for keyword in node.keywords:
                self.visit(keyword.value)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)

        def _visit_comprehension(
            self,
            generators: list[ast.comprehension],
            result_expressions: tuple[ast.expr, ...],
        ) -> None:
            for generator in generators:
                self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            for expression in result_expressions:
                self.visit(expression)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            self._visit_comprehension(node.generators, (node.elt,))

        def visit_SetComp(self, node: ast.SetComp) -> None:
            self._visit_comprehension(node.generators, (node.elt,))

        def visit_DictComp(self, node: ast.DictComp) -> None:
            self._visit_comprehension(node.generators, (node.key, node.value))

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            self._visit_comprehension(node.generators, (node.elt,))

    visitor = _RebindingVisitor()
    for node in nodes:
        visitor.visit(node)
    return visitor.names


def _is_super_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"
    )


def _cyclomatic(node: ast.AST) -> int:
    class _DecisionCounter(ast.NodeVisitor):
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

        def __init__(self) -> None:
            self.count = 0

        def visit(self, current: ast.AST) -> None:
            if isinstance(current, self.decision_nodes):
                self.count += 1
            if isinstance(
                current,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            ):
                return
            super().visit(current)

    counter = _DecisionCounter()
    body = getattr(node, "body", ())
    for statement in body:
        counter.visit(statement)
    return 1 + counter.count


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
    for path in sorted([*_iter_source_files(root), *iter_markdown_files(root)]):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
