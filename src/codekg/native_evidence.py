"""Literal SQL and documentation evidence extraction for supplemental corpus facts."""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from collections import Counter

from pglast import ast as pgast
from pglast import parse_sql
from pglast.parser import ParseError

from codekg.native_ir import NativeDiagnosticIR, NativeFileFacts, SourceEvidenceIR
from codekg.source_locations import ByteLocations, Utf8Offsets


def parse_python_sql(raw: bytes, path: str) -> NativeFileFacts:
    """Extract literal/constant SQL supplied to DB ``execute`` methods."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        source = raw.decode(encoding)
        tree = ast.parse(source, filename=path)
    except (LookupError, UnicodeError) as error:
        return NativeFileFacts(
            diagnostics=(
                NativeDiagnosticIR(
                    "python_source_encoding_error",
                    "warning",
                    None,
                    None,
                    str(error),
                ),
            )
        )
    except SyntaxError as error:
        return NativeFileFacts(
            diagnostics=(
                NativeDiagnosticIR(
                    "python_parse_error",
                    "warning",
                    error.lineno,
                    max(0, (error.offset or 1) - 1),
                    str(error),
                ),
            )
        )
    constants: dict[str, str] = {}
    module_stores = _scope_store_counts(tree.body)
    module_non_assignment_bindings: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_non_assignment_bindings.add(statement.name)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            module_non_assignment_bindings.update(_imported_names(statement))
        elif not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            bound, external = _scope_bindings([statement])
            module_non_assignment_bindings.update(bound)
            module_non_assignment_bindings.update(external)
    module_constants = {}
    potentially_rebound_globals = _global_rebound_names(tree)
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = _literal_string(statement.value, {})
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and module_stores[target.id] == 1
                    and target.id not in module_non_assignment_bindings
                    and "*" not in module_non_assignment_bindings
                    and target.id not in potentially_rebound_globals
                    and value is not None
                ):
                    module_constants[target.id] = value
    parents: list[tuple[str, int]] = []
    external_scopes: list[set[str]] = []
    # Function-local values are deliberately not treated as closure constants.
    # A nested body can run after those bindings have changed, and this visitor
    # does not model execution or cell-variable mutation.
    lexical_scopes: list[set[str]] = []
    evidence: list[SourceEvidenceIR] = []
    diagnostics: list[NativeDiagnosticIR] = []

    def enclosing_lexical_names() -> set[str]:
        return set().union(*lexical_scopes) if lexical_scopes else set()

    class Visitor(ast.NodeVisitor):
        guarded = 0

        def visit_Assign(self, node):
            value = _literal_string(node.value, constants)
            for target in node.targets:
                for binding in ast.walk(target):
                    if isinstance(binding, ast.Name):
                        constants.pop(binding.id, None)
                if isinstance(target, ast.Name) and value is not None and not self.guarded:
                    if (not parents and target.id in potentially_rebound_globals) or any(
                        target.id in names for names in external_scopes
                    ):
                        constants.pop(target.id, None)
                    else:
                        constants[target.id] = value
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            value = _literal_string(node.value, constants)
            if isinstance(node.target, ast.Name):
                constants.pop(node.target.id, None)
                if value is not None and not self.guarded:
                    if (not parents and node.target.id in potentially_rebound_globals) or any(
                        node.target.id in names for names in external_scopes
                    ):
                        constants.pop(node.target.id, None)
                    else:
                        constants[node.target.id] = value
            self.generic_visit(node)

        def visit_AugAssign(self, node):
            if isinstance(node.target, ast.Name):
                constants.pop(node.target.id, None)
            self.generic_visit(node)

        def visit_NamedExpr(self, node):
            if isinstance(node.target, ast.Name):
                constants.pop(node.target.id, None)
                value = _literal_string(node.value, constants)
                if (
                    value is not None
                    and not self.guarded
                    and not any(node.target.id in names for names in external_scopes)
                ):
                    constants[node.target.id] = value
            self.generic_visit(node)

        def visit_Delete(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants.pop(target.id, None)

        def visit_Import(self, node):
            for name in _imported_names(node):
                if name == "*":
                    constants.clear()
                else:
                    constants.pop(name, None)

        def visit_ImportFrom(self, node):
            for name in _imported_names(node):
                if name == "*":
                    constants.clear()
                else:
                    constants.pop(name, None)

        def visit_ExceptHandler(self, node):
            if node.name:
                constants.pop(node.name, None)
            self.generic_visit(node)

        def visit_Match(self, node):
            for name in _match_capture_names(node):
                constants.pop(name, None)
            self.guarded += 1
            self.visit(node.subject)
            for case in node.cases:
                self.visit(case)
            self.guarded -= 1
            for name in _match_capture_names(node):
                constants.pop(name, None)

        def visit_Lambda(self, node):
            # Defaults execute in the enclosing scope; the body executes in a
            # fresh implicit function scope whose parameters shadow outer names.
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            saved_constants = constants.copy()
            constants.clear()
            constants.update(module_constants)
            local_names = {
                arg.arg
                for arg in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
            }
            if node.args.vararg:
                local_names.add(node.args.vararg.arg)
            if node.args.kwarg:
                local_names.add(node.args.kwarg.arg)
            local_names.update(_walrus_targets(node.body))
            for name in local_names:
                constants.pop(name, None)
            for name in enclosing_lexical_names():
                constants.pop(name, None)
            lexical_scopes.append(local_names)
            self.visit(node.body)
            lexical_scopes.pop()
            constants.clear()
            constants.update(saved_constants)

        def visit_comprehension_scope(self, node):
            # The first iterable is evaluated outside the implicit scope.
            self.visit(node.generators[0].iter)
            saved_constants = constants.copy()
            local_names: set[str] = set()
            for generator in node.generators:
                local_names.update(_target_names(generator.target))
            local_names.update(_walrus_targets(node))
            for name in local_names:
                constants.pop(name, None)
            for name in enclosing_lexical_names():
                constants.pop(name, None)
            lexical_scopes.append(local_names - _walrus_targets(node))
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
            lexical_scopes.pop()
            constants.clear()
            constants.update(saved_constants)
            # Assignment expressions in comprehensions bind in the containing
            # scope. We invalidate them rather than infer execution-dependent values.
            for name in _walrus_targets(node):
                constants.pop(name, None)

        def visit_ListComp(self, node):
            self.visit_comprehension_scope(node)

        visit_SetComp = visit_ListComp

        def visit_GeneratorExp(self, node):
            # The first iterable is evaluated immediately, but the generator
            # body runs later. Use only stable module constants there; captured
            # lexical names and comprehension targets are runtime bindings.
            self.visit(node.generators[0].iter)
            saved_constants = constants.copy()
            constants.clear()
            constants.update(module_constants)
            local_names: set[str] = set()
            for generator in node.generators:
                local_names.update(_target_names(generator.target))
            local_names.update(_walrus_targets(node))
            for name in local_names | enclosing_lexical_names():
                constants.pop(name, None)
            lexical_scopes.append(local_names - _walrus_targets(node))
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            self.visit(node.elt)
            lexical_scopes.pop()
            constants.clear()
            constants.update(saved_constants)
            for name in _walrus_targets(node):
                constants.pop(name, None)

        def visit_DictComp(self, node):
            self.visit_comprehension_scope(node)

        def visit_guarded(self, node):
            names, external_names = _scope_bindings([node])
            names.update(external_names)
            for name in names:
                constants.pop(name, None)
            self.guarded += 1
            self.generic_visit(node)
            self.guarded -= 1
            for name in names:
                constants.pop(name, None)

        visit_If = visit_For = visit_AsyncFor = visit_While = visit_Try = visit_guarded
        visit_With = visit_AsyncWith = visit_guarded

        def visit_FunctionDef(self, node):
            self.visit_definition_expressions(node)
            constants.pop(node.name, None)
            saved_constants = constants.copy()
            constants.clear()
            constants.update(module_constants)
            local_names, external_names = _scope_bindings(node.body)
            local_names.update(
                arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            )
            if node.args.vararg:
                local_names.add(node.args.vararg.arg)
            if node.args.kwarg:
                local_names.add(node.args.kwarg.arg)
            global_names = _declared_global_names(node.body)
            for name in (
                local_names
                | (external_names - global_names)
                | (external_names & potentially_rebound_globals)
            ):
                constants.pop(name, None)
            for name in enclosing_lexical_names() - _declared_global_names(node.body):
                constants.pop(name, None)
            if "*" in local_names:
                constants.clear()
            parents.append((node.name, node.lineno))
            external_scopes.append(external_names)
            lexical_scopes.append(local_names - external_names)
            for statement in node.body:
                self.visit(statement)
            lexical_scopes.pop()
            external_scopes.pop()
            parents.pop()
            constants.clear()
            constants.update(saved_constants)

        def visit_definition_expressions(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            arguments = node.args
            for default in [*arguments.defaults, *arguments.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for arg in [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ]:
                if arg.annotation is not None:
                    self.visit(arg.annotation)
            if arguments.vararg and arguments.vararg.annotation:
                self.visit(arguments.vararg.annotation)
            if arguments.kwarg and arguments.kwarg.annotation:
                self.visit(arguments.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            constants.pop(node.name, None)
            saved_constants = constants.copy()
            constants.clear()
            constants.update(module_constants)
            local_names, external_names = _scope_bindings(node.body)
            for name in local_names | external_names:
                constants.pop(name, None)
            for name in enclosing_lexical_names():
                constants.pop(name, None)
            if "*" in local_names:
                constants.clear()
            parents.append((node.name, node.lineno))
            external_scopes.append(external_names)
            for statement in node.body:
                self.visit(statement)
            external_scopes.pop()
            parents.pop()
            constants.clear()
            constants.update(saved_constants)

        def visit_Call(self, node):
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if method in {"execute", "executemany"} and node.args:
                sql = _literal_string(node.args[0], constants)
                start = _node_location(source, node)
                end = _node_end_location(source, node)
                owner = ".".join(name for name, _ in parents) or None
                owner_line = parents[-1][1] if parents else None
                if sql is None:
                    evidence.append(
                        _evidence(
                            "python_execute",
                            None,
                            None,
                            owner,
                            start,
                            end,
                            True,
                            None,
                            owner_line=owner_line,
                        )
                    )
                else:
                    found, errors = _sql_evidence(sql, "python_execute", owner, start, end)
                    # Embedded SQL has no one-to-one character mapping to Python
                    # (escapes/concatenation); anchor each fact to its execute call.
                    evidence.extend(
                        SourceEvidenceIR(
                            item.origin,
                            item.schema_name,
                            item.object_name,
                            item.arity,
                            item.owner_qname,
                            owner_line,
                            start[0],
                            start[1],
                            end[0],
                            end[1],
                            item.dynamic,
                            item.text,
                            item.text_hash,
                        )
                        for item in found
                    )
                    diagnostics.extend(errors)
            self.generic_visit(node)

    Visitor().visit(tree)
    return NativeFileFacts(evidence=tuple(evidence), diagnostics=tuple(diagnostics))


def parse_sql_source_evidence(raw: bytes, path: str) -> NativeFileFacts:
    """Extract static top-level SQL calls; routine bodies use the routine adapter."""
    del path
    source = raw.decode("utf-8", "replace")
    end = ByteLocations(raw).position(len(raw))
    found, diagnostics = _sql_evidence(source, "sql_source", None, (1, 0), end)
    return NativeFileFacts(evidence=tuple(found), diagnostics=tuple(diagnostics))


def _imported_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    return {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}


def _declared_global_names(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()

    class Globals(ast.NodeVisitor):
        def visit_Global(self, node: ast.Global) -> None:
            names.update(node.names)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    visitor = Globals()
    for statement in statements:
        visitor.visit(statement)
    return names


def _global_rebound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    class GlobalRebindings(ast.NodeVisitor):
        def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            _, external = _scope_bindings(node.body)
            globals_here = _declared_global_names(node.body)
            stores = _scope_store_counts(node.body)
            names.update(globals_here & external & stores.keys())
            for statement in node.body:
                self.visit(statement)

        visit_FunctionDef = _scope
        visit_AsyncFunctionDef = _scope

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for statement in node.body:
                self.visit(statement)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    GlobalRebindings().visit(tree)
    return names


def _match_capture_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
            names.add(item.name)
        elif isinstance(item, ast.MatchMapping) and item.rest:
            names.add(item.rest)
    return names


def _target_names(node: ast.AST) -> set[str]:
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def _walrus_targets(node: ast.AST) -> set[str]:
    names: set[str] = set()

    class WalrusTargets(ast.NodeVisitor):
        def visit_NamedExpr(self, item: ast.NamedExpr) -> None:
            if isinstance(item.target, ast.Name):
                names.add(item.target.id)
            self.visit(item.value)

        def visit_Lambda(self, item: ast.Lambda) -> None:
            return

    WalrusTargets().visit(node)
    return names


def _scope_bindings(statements: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """Return names bound in a lexical scope without crossing nested scopes."""
    bound: set[str] = set()
    external: set[str] = set()

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            bound.update(_imported_names(node))

        visit_ImportFrom = visit_Import

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            bound.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            bound.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                bound.add(node.name)
            if node.type:
                self.visit(node.type)
            for statement in node.body:
                self.visit(statement)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name:
                bound.add(node.name)
            if node.pattern:
                self.visit(node.pattern)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name:
                bound.add(node.name)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest:
                bound.add(node.rest)
            self.generic_visit(node)

        def visit_Global(self, node: ast.Global) -> None:
            external.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            external.update(node.names)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            # Comprehension targets live in their implicit nested scope.
            self.visit(node.iter)
            for condition in node.ifs:
                self.visit(condition)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            for generator in node.generators:
                self.visit(generator)
            self.visit(node.elt)

        visit_SetComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_DictComp(self, node: ast.DictComp) -> None:
            for generator in node.generators:
                self.visit(generator)
            self.visit(node.key)
            self.visit(node.value)

    visitor = Bindings()
    for statement in statements:
        visitor.visit(statement)
    return bound, external


def _scope_store_counts(statements: list[ast.stmt]) -> Counter[str]:
    """Count simple name stores in one scope without counting child scopes."""
    counts: Counter[str] = Counter()

    class Stores(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                counts[node.id] += 1

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_comprehension(self, node: ast.comprehension) -> None:
            self.visit(node.iter)
            for condition in node.ifs:
                self.visit(condition)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            for generator in node.generators:
                self.visit(generator)
            self.visit(node.elt)

        visit_SetComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_DictComp(self, node: ast.DictComp) -> None:
            for generator in node.generators:
                self.visit(generator)
            self.visit(node.key)
            self.visit(node.value)

    visitor = Stores()
    for statement in statements:
        visitor.visit(statement)
    return counts


def parse_markdown_evidence(raw: bytes, path: str) -> NativeFileFacts:
    """Extract fenced SQL calls and explicitly qualified backticked mentions."""
    del path
    source = raw.decode("utf-8", "replace")
    locations = ByteLocations(raw)
    offsets = Utf8Offsets(source)
    evidence: list[SourceEvidenceIR] = []
    diagnostics: list[NativeDiagnosticIR] = []
    fence = re.compile(
        r"(?m)^([ \t]*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)^\1\2\s*$", re.S
    )
    for match in fence.finditer(source):
        if match.group(3).lower() not in {"sql", "postgres", "postgresql"}:
            continue
        snippet = match.group(4)
        base_line = locations.position(offsets.byte_offset(match.start(4)))[0]
        start = (base_line, 0)
        end = (base_line + snippet.count("\n"), len(snippet.rsplit("\n", 1)[-1].encode("utf-8")))
        found, errors = _sql_evidence(
            snippet, "markdown_sql", None, start, end, line_offset=base_line - 1
        )
        evidence.extend(found)
        diagnostics.extend(errors)
    mention = re.compile(
        r"`((?:[A-Za-z_][\w$]*|\"(?:[^\"]|\"\")+\")(?:\.(?:[A-Za-z_][\w$]*|\"(?:[^\"]|\"\")+\"))+)\s*\(([^`]*)\)`"
    )
    for match in mention.finditer(source):
        qualified = match.group(1)
        parts = _split_qualified(qualified)
        if len(parts) < 2:
            continue
        line, col = locations.position(offsets.byte_offset(match.start()))
        end = locations.position(offsets.byte_offset(match.end()))
        args = match.group(2).strip()
        arity = 0 if not args else _count_commas(args) + 1
        evidence.append(
            _evidence(
                "markdown_mention",
                parts[-2],
                parts[-1],
                None,
                (line, col),
                end,
                False,
                match.group(0),
                arity,
            )
        )
    return NativeFileFacts(evidence=tuple(evidence), diagnostics=tuple(diagnostics))


def _sql_evidence(
    sql: str,
    origin: str,
    owner: str | None,
    outer_start: tuple[int, int],
    outer_end: tuple[int, int],
    line_offset: int = 0,
):
    found: list[SourceEvidenceIR] = []
    diagnostics: list[NativeDiagnosticIR] = []
    raw = sql.encode("utf-8")
    locations = ByteLocations(raw)
    offsets = Utf8Offsets(sql)
    text_hash = hashlib.sha256(raw).hexdigest()
    try:
        statements = parse_sql(sql)
    except ParseError as error:
        diagnostics.append(
            NativeDiagnosticIR(
                "embedded_sql_parse_error", "warning", outer_start[0], outer_start[1], str(error)
            )
        )
        return found, diagnostics
    for item in statements:
        for node in _walk(item.stmt):
            if not isinstance(node, pgast.FuncCall):
                continue
            names = [_pg_name(part) for part in node.funcname]
            if not names:
                continue
            if node.location is None:
                start, end = outer_start, outer_end
            else:
                byte_offset = offsets.byte_offset(int(node.location))
                start = locations.position(byte_offset)
                end_offset = raw.find(b"(", byte_offset)
                if end_offset < 0:
                    end_offset = byte_offset + len(".".join(names).encode("utf-8"))
                end = locations.position(end_offset)
                start = (start[0] + line_offset, start[1])
                end = (end[0] + line_offset, end[1])
            args = tuple(node.args or ())
            # One source hash and location per occurrence. Storing the complete
            # SQL body on every reference would make JSON staging O(body*refs).
            found.append(
                SourceEvidenceIR(
                    origin,
                    names[-2] if len(names) > 1 else None,
                    names[-1],
                    len(args),
                    owner,
                    None,
                    start[0],
                    start[1],
                    end[0],
                    end[1],
                    False,
                    None,
                    text_hash,
                )
            )
    return found, diagnostics


def _evidence(origin, schema, name, owner, start, end, dynamic, text, arity=None, owner_line=None):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None
    return SourceEvidenceIR(
        origin,
        schema,
        name,
        arity,
        owner,
        owner_line if owner else None,
        start[0],
        start[1],
        end[0],
        end[1],
        dynamic,
        text,
        digest,
    )


def _literal_string(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    # Concatenation of literals/constants is concrete; interpolation/joining is not.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left, constants), _literal_string(node.right, constants)
        return left + right if left is not None and right is not None else None
    return None


def _walk(node):
    yield node
    for key in getattr(node, "__slots__", ()):
        value = getattr(node, key, None)
        if isinstance(value, pgast.Node):
            yield from _walk(value)
        elif isinstance(value, (tuple, list)):
            for child in value:
                if isinstance(child, pgast.Node):
                    yield from _walk(child)


def _pg_name(value):
    return value.sval if isinstance(value, pgast.String) else None


def _node_location(source, node):
    del source  # Python AST columns are UTF-8 byte offsets.
    return node.lineno, node.col_offset


def _node_end_location(source, node):
    del source
    return node.end_lineno, node.end_col_offset


def _split_qualified(name):
    return [
        part[1:-1].replace('""', '"') if part.startswith('"') else part.lower()
        for part in re.findall(r'"(?:[^\"]|\"\")+"|[A-Za-z_][\w$]*', name)
    ]


def _count_commas(args):
    depth = 0
    count = 0
    quote = None
    for char in args:
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count
