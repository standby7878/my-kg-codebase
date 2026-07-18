from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class SourceText:
    docstring: str | None = None
    leading_comment: str | None = None
    inline_comments: tuple[str, ...] = ()


NOISE_COMMENT_RE = re.compile(
    r"(^#!|coding[:=]|#\s*(noqa|type:|pragma|pylint|mypy|ruff|fmt:|isort:))",
    re.IGNORECASE,
)
LICENSE_RE = re.compile(r"(copyright|licensed under|apache license|mit license)", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]*")


def extract_symbol_text(path: Path, qname: str, start_line: int, end_line: int) -> SourceText:
    cached = _load_source(path)
    if cached is None:
        return SourceText()

    text, lines, comments_by_line = cached
    node = _find_node(text, qname, start_line, end_line)
    docstring = ast.get_docstring(node, clean=True) if node is not None else None
    leading = _leading_comment(comments_by_line, lines, start_line)
    inline = tuple(
        comment
        for line, comment in sorted(comments_by_line.items())
        if start_line <= line <= end_line and comment != leading and _is_useful_comment(comment)
    )
    return SourceText(docstring=docstring, leading_comment=leading, inline_comments=inline)


@lru_cache(maxsize=4096)
def _load_source(path: Path) -> tuple[str, list[str], dict[int, str]] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text, text.splitlines(), _comments_by_line(text)


def _find_node(
    text: str,
    qname: str,
    start_line: int,
    end_line: int,
) -> ast.AST | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    wanted_name = qname.rsplit(".", maxsplit=1)[-1]
    if wanted_name == "__module__":
        return tree

    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name != wanted_name:
            continue
        node_start = getattr(node, "lineno", 0)
        node_end = getattr(node, "end_lineno", node_start)
        if node_start == start_line or (node_start <= start_line and end_line <= node_end):
            candidates.append(node)
    if not candidates:
        return None
    return min(candidates, key=lambda node: abs(getattr(node, "lineno", 0) - start_line))


def _comments_by_line(text: str) -> dict[int, str]:
    comments: dict[int, str] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            raw = token.string.strip()
            if _is_noise_comment(raw):
                continue
            cleaned = _clean_comment(raw)
            if _is_useful_comment(cleaned):
                comments[token.start[0]] = cleaned
    except tokenize.TokenError:
        return comments
    return comments


def _leading_comment(
    comments_by_line: dict[int, str],
    lines: list[str],
    start_line: int,
) -> str | None:
    cursor = start_line - 1
    while cursor > 0 and lines[cursor - 1].lstrip().startswith("@"):
        cursor -= 1

    block: list[str] = []
    line = cursor
    while line > 0:
        comment = comments_by_line.get(line)
        if comment is None:
            if lines[line - 1].strip():
                break
            if block:
                break
        else:
            block.append(comment)
        line -= 1

    if not block:
        return None
    text = "\n".join(reversed(block)).strip()
    return text if _is_useful_comment(text) else None


def _clean_comment(value: str) -> str:
    return value.lstrip("#").strip()


def _is_noise_comment(value: str) -> bool:
    return bool(NOISE_COMMENT_RE.search(value) or LICENSE_RE.search(value))


def _is_useful_comment(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    if len(text) < 8:
        return False
    if NOISE_COMMENT_RE.search(text) or LICENSE_RE.search(text):
        return False
    words = WORD_RE.findall(text)
    if len(words) < 3:
        return False
    return not _looks_like_code(text)


def _looks_like_code(text: str) -> bool:
    stripped_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not stripped_lines:
        return False
    source = "\n".join(stripped_lines)
    first_line = stripped_lines[0]
    code_markers = (
        "import ",
        "from ",
        "return ",
        "def ",
        "class ",
        "if ",
        "for ",
        "while ",
        "try:",
        "except ",
        "raise ",
        "yield ",
        "pass",
        "continue",
        "break",
    )
    if any(first_line.startswith(marker) for marker in code_markers):
        return True
    return bool(re.search(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", source, flags=re.MULTILINE))
