"""Build-free extraction of PostgreSQL routine bindings and pg_proc records."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from dataclasses import replace

from pglast import ast as pgast
from pglast import parse_sql as pg_parse_sql
from pglast import scan as pg_scan
from pglast.parser import ParseError
from pglast.stream import RawStream

from codekg.native_ir import (
    NativeDiagnosticIR,
    NativeFileFacts,
    RoutineIR,
    SourceEvidenceIR,
)
from codekg.source_locations import ByteLocations, Utf8Offsets
from codekg.sql_config import SqlConfig
from codekg.sql_parser import (
    _routine_input_args,
    _signature_identifier,
    _split_statements,
    _type_name,
)
from codekg.sql_parser import parse_sql as existing_parse_sql


def parse_routine_source(raw: bytes, path: str, config: SqlConfig) -> NativeFileFacts:
    """Extract CREATE FUNCTION/PROCEDURE declarations without expanding templates."""
    source = raw.decode("utf-8", "replace")
    masked, template_lines = (
        _template_view(source) if path.lower().endswith(".sql.in") else (source, [])
    )
    masked, attribute_count = (
        _mask_template_attributes(masked) if path.lower().endswith(".sql.in") else (masked, 0)
    )
    masked_raw = masked.encode("utf-8")
    routines: list[RoutineIR] = []
    evidence: list[SourceEvidenceIR] = []
    diagnostics: list[NativeDiagnosticIR] = []
    locations = ByteLocations(raw)
    if attribute_count:
        diagnostics.append(
            NativeDiagnosticIR(
                "sql_template_attribute",
                "warning",
                None,
                None,
                f"{attribute_count} unexpanded PostGIS cost attributes omitted from parsing; "
                "routine names/bindings remain source facts, attributes are not evaluated",
            )
        )
    if masked != source:
        diagnostics.append(
            NativeDiagnosticIR(
                "sql_template_directive",
                "info",
                None,
                None,
                "preprocessor directives were masked; template was not expanded",
            )
        )
    if re.search(r"(?<!\w)(?:@[^@\s]+@|\$\{[^}]+\})", masked):
        diagnostics.append(
            NativeDiagnosticIR(
                "sql_incomplete_template",
                "warning",
                None,
                None,
                "unexpanded template tokens remain; extraction may be incomplete",
            )
        )
    for segment in _split_statements(masked, masked_raw):
        segment_raw = masked_raw[segment.start : segment.end]
        try:
            statements = pg_parse_sql(segment_raw.decode("utf-8"))
        except (ParseError, UnicodeDecodeError) as error:
            line, column = locations.position(segment.start)
            diagnostics.append(
                NativeDiagnosticIR("routine_sql_parse_error", "warning", line, column, str(error))
            )
            continue
        if not statements:
            continue
        raw_stmt = statements[0]
        stmt = raw_stmt.stmt
        if not isinstance(stmt, pgast.CreateFunctionStmt):
            continue
        names = [_pg_name(part) for part in stmt.funcname]
        name = names[-1] if names else ""
        schema = names[-2] if len(names) > 1 else config.default_schema
        options = {option.defname.lower(): option.arg for option in (stmt.options or ())}
        language = (_pg_name(options.get("language")) or "unknown").lower()
        as_values = options.get("as")
        if not isinstance(as_values, (tuple, list)):
            as_values = (as_values,) if as_values is not None else ()
        as_values = tuple(_pg_name(value) or "" for value in as_values)
        library = as_values[0] if language == "c" and as_values else None
        if language == "c":
            entrypoint = as_values[1] if len(as_values) > 1 else name
        elif language == "internal":
            entrypoint = as_values[0] if as_values else None
        else:
            entrypoint = None
        args = _routine_input_args(stmt)
        signature = f"{_signature_identifier(name)}({','.join(args)})"
        start = segment.start
        end = segment.end
        start_line, start_column = locations.position(start)
        end_line, end_column = locations.position(end)
        template_index = bisect_right(template_lines, start_line, key=lambda point: point[0]) - 1
        conditional = template_index >= 0 and template_lines[template_index][1] > 0
        body = as_values[0] if language not in {"c", "internal"} and as_values else None
        body_hash = hashlib.sha256(body.encode()).hexdigest() if body is not None else None
        if language in {"sql", "plpgsql", "c", "internal"}:
            coverage = (
                "declaration_only"
                if language in {"c", "internal"}
                else ("body_available" if body else "body_missing")
            )
        else:
            coverage = "declaration_only"
            diagnostics.append(
                NativeDiagnosticIR(
                    "unsupported_routine_language",
                    "warning",
                    start_line,
                    start_column,
                    f"body analysis is unsupported for LANGUAGE {language}",
                )
            )
        parameters = tuple(stmt.parameters or ())
        defaults = sum(1 for item in parameters if getattr(item, "defexpr", None) is not None)
        variadic = sum(
            1 for item in parameters if getattr(getattr(item, "mode", None), "value", "i") == "v"
        )
        routines.append(
            RoutineIR(
                schema,
                name,
                signature,
                len(args),
                language,
                library,
                entrypoint,
                body_hash,
                coverage,
                start_line,
                start_column,
                end_line,
                end_column,
                "procedure" if stmt.is_procedure else "function",
                defaults,
                variadic,
                return_type=(
                    ("setof " if getattr(stmt.returnType, "setof", False) else "")
                    + _type_name(stmt.returnType)
                    if stmt.returnType is not None
                    else None
                ),
                definition_hash=hashlib.sha256(RawStream()(stmt).encode()).hexdigest(),
                condition="unevaluated SQL template guard" if conditional else None,
                out_arg_count=sum(
                    1
                    for item in parameters
                    if getattr(getattr(item, "mode", None), "value", "i") == "o"
                ),
            )
        )
        # The established SQL parser already handles SQL and statically
        # analyzable PL/pgSQL bodies. Reuse its exact call references here.
        parsed_file = (
            existing_parse_sql(segment_raw, context={"path": path}, config=config)
            if language in {"sql", "plpgsql"}
            else None
        )
        statement_contexts = (
            {item.ordinal: item.control_context for item in parsed_file.sql_statements}
            if parsed_file is not None
            else {}
        )
        segment_line, segment_column = locations.position(segment.start)
        if parsed_file is not None:
            if parsed_file.diagnostics and routines[-1].coverage == "body_available":
                routines[-1] = replace(routines[-1], coverage="body_partial")
            for diagnostic in parsed_file.diagnostics:
                diagnostic_line = diagnostic.line
                diagnostic_column = diagnostic.column
                if diagnostic_line is not None:
                    mapped_line = segment_line + diagnostic_line - 1
                    mapped_column = (
                        segment_column + max(0, (diagnostic_column or 1) - 1)
                        if diagnostic_line == 1
                        else max(0, (diagnostic_column or 1) - 1)
                    )
                else:
                    mapped_line = mapped_column = None
                diagnostics.append(
                    NativeDiagnosticIR(
                        diagnostic.category,
                        diagnostic.severity,
                        mapped_line,
                        mapped_column,
                        diagnostic.message,
                    )
                )
        for reference in parsed_file.sql_object_refs if parsed_file is not None else ():
            if reference.role != "call" and not reference.dynamic:
                continue
            evidence_start_line = segment_line + reference.start_line - 1
            evidence_end_line = segment_line + reference.end_line - 1
            start_column = (
                segment_column + reference.start_column - 1
                if reference.start_line == 1
                else reference.start_column - 1
            )
            end_column = (
                segment_column + reference.end_column - 1
                if reference.end_line == 1
                else reference.end_column - 1
            )
            evidence.append(
                SourceEvidenceIR(
                    "routine_body",
                    reference.schema_name,
                    reference.object_name,
                    reference.call_arity,
                    f"{schema}.{name}",
                    start_line,
                    evidence_start_line,
                    start_column,
                    evidence_end_line,
                    end_column,
                    reference.dynamic,
                    reference.raw_name if not reference.dynamic else None,
                    hashlib.sha256(reference.raw_name.encode("utf-8")).hexdigest()
                    if not reference.dynamic
                    else None,
                    condition=" AND ".join(
                        value
                        for value in (
                            "unevaluated SQL template guard" if conditional else None,
                            (
                                statement_contexts.get(reference.statement_ordinal)
                                if str(
                                    statement_contexts.get(reference.statement_ordinal) or ""
                                ).endswith(":conditional")
                                else None
                            ),
                        )
                        if value
                    )
                    or None,
                    routine_kind=reference.object_kind_hint,
                )
            )
    return NativeFileFacts(
        routines=tuple(routines),
        evidence=tuple(dict.fromkeys(evidence)),
        diagnostics=tuple(diagnostics),
    )


def parse_pg_proc_catalog(raw: bytes, path: str) -> NativeFileFacts:
    """Safely parse pg_proc.dat's quoted key/value record subset (never eval Perl)."""
    del path
    source = raw.decode("utf-8", "replace")
    locations = ByteLocations(raw)
    offsets = Utf8Offsets(source)
    routines: list[RoutineIR] = []
    diagnostics: list[NativeDiagnosticIR] = []
    record_start: int | None = None
    depth = 0
    quote = False
    escaped = False
    record_chars: list[str] = []
    for index, char in enumerate(source):
        if record_start is None:
            if char == "{":
                record_start, depth, quote, escaped = index, 1, False, False
                record_chars = [char]
            continue
        record_chars.append(char)
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                quote = False
        elif char == "'":
            quote = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                record = "".join(record_chars)
                fields, valid = _catalog_fields(record)
                if not valid:
                    line, col = locations.position(offsets.byte_offset(record_start))
                    diagnostics.append(
                        NativeDiagnosticIR(
                            "catalog_record_unsupported",
                            "warning",
                            line,
                            col,
                            "record contains unsupported or malformed value syntax",
                        )
                    )
                elif "proname" in fields:
                    args = fields.get("proargtypes", "").split()
                    schema = fields.get("pronamespace", "pg_catalog")
                    line, col = locations.position(offsets.byte_offset(record_start))
                    language = fields.get("prolang", "internal")
                    entrypoint = fields.get("prosrc") if language in {"internal", "c"} else None
                    default_count = fields.get("pronargdefaults", "0")
                    if not default_count.isascii() or not default_count.isdecimal():
                        diagnostics.append(
                            NativeDiagnosticIR(
                                "catalog_record_unsupported",
                                "warning",
                                line,
                                col,
                                "invalid pronargdefaults; routine arity is incomplete",
                            )
                        )
                        default_count = "0"
                    routines.append(
                        RoutineIR(
                            schema,
                            fields["proname"],
                            f"{_signature_identifier(fields['proname'])}({','.join(args)})",
                            len(args),
                            language,
                            fields.get("probin") if language == "c" else None,
                            entrypoint,
                            hashlib.sha256(fields.get("prosrc", "").encode()).hexdigest()
                            if (
                                fields.get("prosrc") is not None
                                and language not in {"c", "internal"}
                            )
                            else None,
                            "catalog_mapping" if entrypoint else "catalog_declaration_only",
                            line,
                            col,
                            *locations.position(offsets.byte_offset(index + 1)),
                            kind={"a": "aggregate", "w": "window", "p": "procedure"}.get(
                                fields.get("prokind", "f"), "function"
                            ),
                            default_arg_count=int(default_count),
                            out_arg_count=fields.get("proargmodes", "{}")
                            .strip("{}")
                            .split(",")
                            .count("o"),
                            return_type=(
                                ("setof " if fields.get("proretset") == "t" else "")
                                + fields["prorettype"]
                                if fields.get("prorettype")
                                else None
                            ),
                            definition_hash=hashlib.sha256(
                                repr(
                                    sorted((k, v) for k, v in fields.items() if k != "descr")
                                ).encode()
                            ).hexdigest(),
                        )
                    )
                record_start = None
    if record_start is not None:
        line, col = locations.position(offsets.byte_offset(record_start))
        diagnostics.append(
            NativeDiagnosticIR(
                "catalog_incomplete_record", "warning", line, col, "unterminated pg_proc.dat record"
            )
        )
    return NativeFileFacts(routines=tuple(routines), diagnostics=tuple(diagnostics))


def _catalog_fields(record: str) -> tuple[dict[str, str], bool]:
    pattern = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*=>\s*'((?:\\.|[^'\\])*)'")
    fields = {key: _unescape_perl(value) for key, value in pattern.findall(record)}
    residue = pattern.sub("", record)
    residue = re.sub(r"[{}\s,]+", "", residue)
    return fields, not residue and len(fields) == len(re.findall(r"=>", record))


def _unescape_perl(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _mask_template_directives(source: str) -> str:
    return _template_view(source)[0]


def _template_view(source: str) -> tuple[str, list[tuple[int, int]]]:
    """Mask real template lines, not directives inside SQL bodies/comments."""
    try:
        protected = [
            (token.start, token.end + 1)
            for token in pg_scan(source)
            if token.name in {"SCONST", "C_COMMENT", "SQL_COMMENT"}
        ]
    except ParseError:
        # No lexical certainty: retain source and let parsing diagnose it.
        return source, []
    span_index = 0
    offset = depth = 0
    contexts = []
    lines = []
    for line_number, line in enumerate(source.splitlines(keepends=True), 1):
        while span_index < len(protected) and protected[span_index][1] <= offset:
            span_index += 1
        is_protected = (
            span_index < len(protected)
            and protected[span_index][0] <= offset < protected[span_index][1]
        )
        directive = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|include)\b", line)
        if directive and not is_protected:
            kind = directive.group(1)
            if kind in {"if", "ifdef", "ifndef"}:
                depth += 1
            elif kind == "endif":
                depth = max(0, depth - 1)
            contexts.append((line_number, depth))
            lines.append(
                "".join("\n" if ch == "\n" else " " * len(ch.encode("utf-8")) for ch in line)
            )
        else:
            lines.append(line)
        offset += len(line)
    return "".join(lines), contexts


def _mask_template_attributes(source: str) -> tuple[str, int]:
    """Omit known non-identity cost attributes, only outside quoted SQL tokens.

    Source byte lengths are preserved and callers report the partial coverage.
    Never substitute schema/name/body placeholders or execute a preprocessor.
    """
    try:
        tokens = pg_scan(source)
    except ParseError:
        return source, 0
    masks = []
    for token in tokens:
        # pglast scanner token ends are inclusive character offsets.
        value = source[token.start : token.end + 1]
        if token.name == "IDENT" and value in {
            "_COST_DEFAULT",
            "_COST_LOW",
            "_COST_MEDIUM",
            "_COST_HIGH",
        }:
            masks.append((token.start, token.end + 1))
    if not masks:
        return source, 0
    parts = []
    position = 0
    for start, end in masks:
        parts.extend((source[position:start], " " * (end - start)))
        position = end
    parts.append(source[position:])
    return "".join(parts), len(masks)


def _pg_name(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, pgast.String):
        return value.sval
    return None


def _byte_location(raw: bytes, offset: int) -> tuple[int, int]:
    prefix = raw[:offset]
    line = prefix.count(b"\n") + 1
    return line, len(prefix.rsplit(b"\n", 1)[-1])


def _char_location(source: str, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    return prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1].encode("utf-8"))
