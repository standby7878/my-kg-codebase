from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImportIR:
    module: str
    name: str
    alias: str | None = None


@dataclass(frozen=True)
class CallIR:
    caller_qname: str
    callee_name: str
    callee_qname: str | None = None
    receiver: str | None = None
    line: int = 0


@dataclass(frozen=True)
class InheritanceIR:
    type_qname: str
    base_name: str
    base_qname: str | None = None


@dataclass(frozen=True)
class SymbolIR:
    kind: str
    name: str
    qname: str
    signature: str
    start_line: int
    end_line: int
    cyclomatic: int = 1
    parent_qname: str | None = None
    docstring: str | None = None


@dataclass(frozen=True)
class FileIR:
    path: str
    language: str
    loc: int
    module_qname: str
    imports: tuple[ImportIR, ...] = ()
    symbols: tuple[SymbolIR, ...] = ()
    inheritance: tuple[InheritanceIR, ...] = ()
    calls: tuple[CallIR, ...] = ()


@dataclass(frozen=True)
class RepositoryIR:
    repo_name: str
    commit: str
    root_path: str
    files: tuple[FileIR, ...] = field(default_factory=tuple)
    markdown_descriptions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
