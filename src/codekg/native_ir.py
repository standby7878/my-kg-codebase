"""Immutable facts extracted from native, routine, and documentation sources."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NativeSymbolIR:
    name: str
    signature: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    static: bool
    declaration: bool
    condition: str | None
    body_hash: str | None
    kind: str = "function"


@dataclass(frozen=True)
class NativeCallIR:
    owner_name: str
    owner_start_line: int
    callee_name: str | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    dynamic: bool
    condition: str | None


@dataclass(frozen=True)
class RoutineIR:
    schema_name: str
    name: str
    signature: str
    arity: int
    language: str
    library: str | None
    entrypoint: str | None
    body_hash: str | None
    coverage: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    kind: str = "function"
    default_arg_count: int = 0
    variadic_arg_count: int = 0
    return_type: str | None = None
    definition_hash: str | None = None
    condition: str | None = None
    out_arg_count: int = 0


@dataclass(frozen=True)
class SourceEvidenceIR:
    origin: str
    schema_name: str | None
    object_name: str | None
    arity: int | None
    owner_qname: str | None
    owner_line: int | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    dynamic: bool
    text: str | None
    text_hash: str | None
    condition: str | None = None
    receiver_status: str | None = None
    routine_kind: str | None = None


@dataclass(frozen=True)
class NativeDiagnosticIR:
    category: str
    severity: str
    line: int | None
    column: int | None
    message: str


@dataclass(frozen=True)
class NativeFileFacts:
    symbols: tuple[NativeSymbolIR, ...] = ()
    calls: tuple[NativeCallIR, ...] = ()
    routines: tuple[RoutineIR, ...] = ()
    evidence: tuple[SourceEvidenceIR, ...] = ()
    diagnostics: tuple[NativeDiagnosticIR, ...] = ()
    includes: tuple[str, ...] = ()


def routine_target_kinds(invocation_kind: str | None) -> tuple[str, ...]:
    """SQL function syntax also invokes catalog aggregates/window functions."""
    return {"function": ("function", "aggregate", "window"), "procedure": ("procedure",)}.get(
        invocation_kind, ()
    )
