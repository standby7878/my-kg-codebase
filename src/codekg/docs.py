from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DocChunk:
    path: str
    heading_path: str
    start_line: int
    end_line: int
    text: str
    mentions: tuple[str, ...] = ()
    chunk_index: int = 0


MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
MENTION_RE = re.compile(
    r"``([^`]+)``|`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b"
)


def chunk_docs(paths: list[Path], globs: list[str]) -> Iterator[DocChunk]:
    for root in paths:
        if root.is_file():
            candidates = [root]
            base = root.parent
        else:
            candidates = [path for path in root.rglob("*") if path.is_file()]
            base = root
        for path in sorted(candidates):
            rel = path.relative_to(base).as_posix()
            if globs and not any(fnmatch.fnmatch(rel, pattern) for pattern in globs):
                continue
            if path.suffix.lower() == ".md":
                yield from _chunk_markdown_file(path)


def extract_mentions(text: str) -> tuple[str, ...]:
    mentions: list[str] = []
    seen: set[str] = set()
    for match in MENTION_RE.finditer(text):
        value = next(group for group in match.groups() if group)
        value = value.strip()
        if not _looks_like_symbol(value) or value in seen:
            continue
        seen.add(value)
        mentions.append(value)
    return tuple(mentions)


def _chunk_markdown_file(path: Path) -> Iterator[DocChunk]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, _line in enumerate(lines):
        heading = _markdown_heading(lines, index)
        if heading:
            headings.append((index + 1, heading[0], heading[1]))

    sections: list[tuple[str, int, int]] = []
    if not headings:
        sections.append((path.name, 1, len(lines)))
    else:
        stack: list[tuple[int, str]] = []
        for index, (line_no, level, title) in enumerate(headings):
            next_line = headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append((" > ".join(item[1] for item in stack), line_no, next_line))

    chunk_index = 0
    for heading_path, start_line, end_line in sections:
        for chunk_start, chunk_end in _markdown_chunk_ranges(lines, start_line, end_line):
            text = "\n".join(lines[chunk_start - 1 : chunk_end]).strip()
            if not text:
                continue
            yield DocChunk(
                path=path.as_posix(),
                heading_path=heading_path,
                start_line=chunk_start,
                end_line=chunk_end,
                text=text,
                mentions=extract_mentions(text),
                chunk_index=chunk_index,
            )
            chunk_index += 1


def _markdown_chunk_ranges(
    lines: list[str], start_line: int, end_line: int
) -> Iterator[tuple[int, int]]:
    """Split a heading section into prose and complete fenced-code ranges."""

    cursor = start_line
    line_no = start_line
    while line_no <= end_line:
        if not _is_fence_start(lines[line_no - 1]):
            line_no += 1
            continue

        if cursor < line_no:
            yield cursor, line_no - 1
        fence = lines[line_no - 1].lstrip()[:3]
        fence_end = line_no
        for candidate in range(line_no + 1, end_line + 1):
            if _is_fence_end(lines[candidate - 1], fence):
                fence_end = candidate
                break
        yield line_no, fence_end
        cursor = fence_end + 1
        line_no = fence_end + 1

    if cursor <= end_line:
        yield cursor, end_line


def _is_fence_start(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def _is_fence_end(line: str, fence: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(fence)


def _markdown_heading(lines: list[str], index: int) -> tuple[int, str] | None:
    match = MARKDOWN_HEADING_RE.match(lines[index])
    if not match:
        return None
    return len(match.group(1)), match.group(2).strip()


def _looks_like_symbol(value: str) -> bool:
    if len(value) > 120:
        return False
    if " " in value and "." not in value:
        return False
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", value))
