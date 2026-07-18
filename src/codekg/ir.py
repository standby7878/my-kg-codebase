from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ImportIR:
    module: str
    name: str
    alias: str | None = None


@dataclass(frozen=True)
class CallIR:
    owner_qname: str
    raw_callee: str
    callee_name: str | None
    callee_qname_hint: str | None
    receiver_kind: Literal["none", "name", "self", "cls", "super", "attribute", "dynamic"]
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    ordinal: int


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
class ModuleInitIR:
    """The executable module scope for top-level statements and calls."""

    qname: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ParseDiagnosticIR:
    category: Literal["syntax_error"]
    severity: Literal["error"]
    line: int | None
    column: int | None
    message: str


@dataclass(frozen=True)
class FileIR:
    path: str
    language: str
    loc: int
    module_qname: str
    module_init: ModuleInitIR | None = None
    parse_status: Literal["ok", "error"] = "ok"
    diagnostics: tuple[ParseDiagnosticIR, ...] = ()
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
