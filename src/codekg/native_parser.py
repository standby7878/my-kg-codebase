"""Build-free C/header extraction using tree-sitter's concrete syntax tree."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

import tree_sitter_c
from tree_sitter import Language, Parser

from codekg.native_ir import (
    NativeCallIR,
    NativeDiagnosticIR,
    NativeFileFacts,
    NativeSymbolIR,
    SourceEvidenceIR,
)

_LANGUAGE = Language(tree_sitter_c.language())


def parse_native_source(raw: bytes, path: str) -> NativeFileFacts:
    """Extract C declarations, definitions, direct/indirect calls and includes.

    Locations are one-based lines and zero-based UTF-8 byte columns (the native
    tree-sitter convention). Preprocessor conditions are retained, never evaluated.
    """
    del path  # The owning File node is supplied by the corpus integration layer.
    parser = Parser(_LANGUAGE)
    tree = parser.parse(raw)
    root = tree.root_node
    symbols: list[NativeSymbolIR] = []
    calls: list[NativeCallIR] = []
    diagnostics: list[NativeDiagnosticIR] = []
    includes: list[str] = []
    evidence: list[SourceEvidenceIR] = []

    def visit(node, owner: tuple[str, int] | None, scopes: tuple[set[str], ...]):
        if node.type in {"ERROR", "MISSING"}:
            diagnostics.append(
                NativeDiagnosticIR(
                    "c_parse_error" if node.type == "ERROR" else "c_missing_syntax",
                    "warning",
                    node.start_point.row + 1,
                    node.start_point.column,
                    f"tree-sitter C parser reported {node.type.lower()}",
                )
            )
        if node.type == "preproc_include":
            path_node = node.child_by_field_name("path")
            value = _text(raw, path_node)
            if value:
                token = value.encode("utf-8")
                clipped_value = token[:4096].decode("utf-8", "replace")
                includes.append(clipped_value.strip('"<>'))
                evidence.append(
                    SourceEvidenceIR(
                        "native_include",
                        None,
                        value.strip('"<>'),
                        None,
                        None,
                        None,
                        path_node.start_point.row + 1,
                        path_node.start_point.column,
                        path_node.end_point.row + 1,
                        path_node.end_point.column,
                        False,
                        clipped_value,
                        hashlib.sha256(token).hexdigest(),
                        _condition_context(raw, node),
                    )
                )
                if len(token) > 4096:
                    diagnostics.append(
                        NativeDiagnosticIR(
                            "native_include_truncated",
                            "warning",
                            node.start_point.row + 1,
                            node.start_point.column,
                            "include operand exceeds 4096 bytes; exact hash and location retained",
                        )
                    )
        if node.type in {"preproc_def", "preproc_function_def"}:
            macro = _text(raw, node.child_by_field_name("name"))
            if macro:
                symbols.append(
                    replace(
                        _symbol(raw, node, macro, True, False, _condition_context(raw, node), None),
                        kind="macro",
                        body_hash=hashlib.sha256(_text(raw, node).encode()).hexdigest(),
                    )
                )
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if _text(raw, function) == "PG_FUNCTION_INFO_V1":
                args = node.child_by_field_name("arguments")
                referenced = (
                    _text(raw, args.named_children[0])
                    if args and args.named_children
                    else "unknown"
                )
                diagnostics.append(
                    NativeDiagnosticIR(
                        "macro_api_reference",
                        "info",
                        node.start_point.row + 1,
                        node.start_point.column,
                        f"PG_FUNCTION_INFO_V1 references {referenced}; expansion unavailable",
                    )
                )
        if node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            name = _declarator_name(raw, declarator)
            if name:
                is_static = bool(
                    re.search(rb"\bstatic\b", raw[node.start_byte : declarator.start_byte])
                )
                body = node.child_by_field_name("body")
                body_hash = _normalized_hash(raw, body) if body else None
                symbols.append(
                    _symbol(
                        raw,
                        node,
                        name,
                        is_static,
                        False,
                        _condition_context(raw, node),
                        body_hash,
                        body.start_byte if body else node.end_byte,
                    )
                )
                owner = (name, node.start_point.row + 1)
        elif node.type == "declaration":
            for candidate in node.children_by_field_name("declarator"):
                name = _declarator_name(raw, candidate)
                if name:
                    is_static = bool(
                        re.search(rb"\bstatic\b", raw[node.start_byte : candidate.start_byte])
                    )
                    symbols.append(
                        _symbol(
                            raw, node, name, is_static, True, _condition_context(raw, node), None
                        )
                    )
        # C ordinary-identifier bindings shadow global functions, including
        # parameters whose callable type is hidden behind a typedef. Track
        # lexical scope rather than poisoning a name across the whole file.
        if node.type in {"declaration", "parameter_declaration"}:
            for declarator in node.children_by_field_name("declarator"):
                if node.type == "declaration" and _declarator_name(raw, declarator):
                    continue  # A genuine function declaration is not an object binding.
                name = _object_binding_name(raw, declarator)
                if name:
                    scopes[-1].add(name)
        if node.type == "call_expression" and owner:
            function = node.child_by_field_name("function")
            callee = _identifier_from_expression(raw, function)
            if any(callee in scope for scope in reversed(scopes)):
                callee = None
            calls.append(
                NativeCallIR(
                    owner[0],
                    owner[1],
                    callee,
                    node.start_point.row + 1,
                    node.start_point.column,
                    node.end_point.row + 1,
                    node.end_point.column,
                    callee is None,
                    _condition_context(raw, node),
                )
            )
        return owner

    pending = [(root, None, (set(),))]
    while pending:
        node, owner, scopes = pending.pop()
        if node.type in {"function_definition", "compound_statement", "for_statement"} or (
            node.type == "parameter_list"
            and node.parent is not None
            and node.parent.type == "function_declarator"
            and not _definition_parameters(node.parent)
        ):
            scopes = (*scopes, set())
        owner = visit(node, owner, scopes)
        pending.extend((child, owner, scopes) for child in reversed(node.named_children))
    static_names = {
        symbol.name for symbol in symbols if symbol.static and symbol.kind == "function"
    }
    symbols = [
        replace(symbol, static=symbol.static or symbol.name in static_names) for symbol in symbols
    ]
    if root.has_error and not diagnostics:
        diagnostics.append(
            NativeDiagnosticIR("c_parse_error", "warning", None, None, "C source has parse errors")
        )
    return NativeFileFacts(
        tuple(symbols),
        tuple(calls),
        evidence=tuple(evidence),
        diagnostics=tuple(diagnostics),
        includes=tuple(includes),
    )


def _symbol(
    raw: bytes,
    node,
    name: str,
    static: bool,
    declaration: bool,
    condition: str | None,
    body_hash: str | None,
    signature_end: int | None = None,
) -> NativeSymbolIR:
    text = raw[node.start_byte : signature_end or node.end_byte]
    text = re.sub(rb"\b(?:static|extern)\b\s*", b"", text)
    signature = re.sub(rb"\s+", b" ", text).decode("utf-8", "replace").strip()
    signature = signature.removesuffix(";").strip()
    return NativeSymbolIR(
        name,
        signature,
        node.start_point.row + 1,
        node.start_point.column,
        node.end_point.row + 1,
        node.end_point.column,
        static,
        declaration,
        condition,
        body_hash,
    )


def _declarator_name(raw: bytes, node) -> str | None:
    # C declarators bind from the identifier outward. `int *f()` is a function,
    # `int (*f)()` a pointer variable, and `int (*f())()` a function returning
    # a pointer. Never descend into parameter names to infer the declaration.
    wrappers = []
    while node is not None and node.type != "identifier":
        wrappers.append(node.type)
        child = node.child_by_field_name("declarator")
        if child is None and node.type == "parenthesized_declarator":
            child = next(iter(node.named_children), None)
        node = child
    if node is not None:
        for wrapper in reversed(wrappers):
            if wrapper in {"function_declarator", "pointer_declarator", "array_declarator"}:
                return _text(raw, node) if wrapper == "function_declarator" else None
    return None


def _identifier_from_expression(raw: bytes, node) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "field_identifier"}:
        return _text(raw, node)
    # `(foo)(x)` and member calls have a statically named expression; pointers,
    # subscripts, and arbitrary expressions stay dynamic.
    if node.type == "parenthesized_expression" and len(node.named_children) == 1:
        return _identifier_from_expression(raw, node.named_children[0])
    return None


def _descendants(node):
    stack = list(reversed(node.named_children))
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(item.named_children))


def _definition_parameters(node) -> bool:
    """Only the defining function's parameters share its body scope.

    Prototype and nested callback-declarator parameters have their own scopes.
    """
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in {"declaration", "parameter_declaration", "type_definition"}:
            return False
        if ancestor.type == "function_definition":
            declarator = ancestor.child_by_field_name("declarator")
            defining = None
            while declarator is not None:
                if declarator.type == "function_declarator":
                    defining = declarator
                child = declarator.child_by_field_name("declarator")
                if child is None and declarator.type == "parenthesized_declarator":
                    child = next(iter(declarator.named_children), None)
                declarator = child
            return defining == node
        ancestor = ancestor.parent
    return False


def _object_binding_name(raw: bytes, node) -> str | None:
    """Follow only a declarator's binding spine, never initializer/argument names."""
    while node is not None:
        if node.type in {"identifier", "type_identifier"}:
            return _text(raw, node)
        child = node.child_by_field_name("declarator")
        if child is None and node.type == "parenthesized_declarator":
            child = next(iter(node.named_children), None)
        node = child
    return None


def _text(raw: bytes, node) -> str | None:
    return raw[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else None


def _normalized_hash(raw: bytes, node) -> str:
    # Include unnamed operator/punctuation tokens: '+' -> '-' is a body change.
    # Incremental hashing avoids another full function-body materialization.
    digest = hashlib.sha256()
    stack = [node]
    while stack:
        item = stack.pop()
        if item.type == "comment":
            continue
        if item.children:
            stack.extend(reversed(item.children))
        else:
            token = raw[item.start_byte : item.end_byte]
            digest.update(len(token).to_bytes(8, "big"))
            digest.update(token)
    return digest.hexdigest()


def _condition_context(raw: bytes, node) -> str | None:
    """Read actual preprocessor ancestors, not directives inside comments/strings.

    The expression is retained as evidence only, never evaluated. This avoids a
    Python dictionary/string allocation for every conditional line in the file.
    """
    conditions = []
    parent = node.parent
    while parent is not None:
        if parent.type in {"preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"}:
            first_line = raw[parent.start_byte : min(parent.end_byte, parent.start_byte + 1024)]
            conditions.append(
                first_line.split(b"\n", 1)[0].decode("utf-8", "replace").lstrip().removeprefix("#")
            )
        parent = parent.parent
    return " && ".join(reversed(conditions)) if conditions else None
