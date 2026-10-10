"""PostgreSQL SQL semantic extraction.

The parser targets PostgreSQL 15 and later syntax exposed by the pinned
``pglast`` family.  It records source facts conservatively: an unresolvable
dynamic name is represented as dynamic rather than guessed as a table.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from pglast import ast as pgast
from pglast import parse_plpgsql
from pglast import parse_sql as pg_parse_sql
from pglast import scan as pg_scan
from pglast import split as pg_split
from pglast.parser import ParseError

from codekg.ir import FileIR, ParseDiagnosticIR
from codekg.source_locations import ByteLocations, Utf8Offsets
from codekg.sql_config import SqlConfig
from codekg.sql_ir import SqlArtifactIR, SqlObjectRefIR, SqlStatementIR

_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[^\s.(),;]+)'
_QUALIFIED_IDENTIFIER = re.compile(rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER}){{0,2}}")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int


@dataclass(frozen=True)
class _Location:
    start_line: int
    start_column: int
    end_line: int
    end_column: int


def parse_sql(
    text: str | bytes,
    context: Mapping[str, object] | str | None = None,
    config: SqlConfig | None = None,
) -> FileIR:
    """Parse one SQL file into :class:`~codekg.ir.FileIR`.

    ``context`` may be a project-relative path or a mapping containing
    ``path``, ``database``, ``default_schema`` and ``search_path``.  The
    returned ``FileIR`` is always complete and immutable, including the full
    source text in its single ``SqlArtifactIR``.
    """

    path, context_values = _context_values(context)
    sql_config = config or SqlConfig()
    if isinstance(text, bytes):
        raw = text
        try:
            source = text.decode("utf-8")
        except UnicodeDecodeError as error:
            diagnostic = ParseDiagnosticIR(
                "source_decode_error",
                "error",
                None,
                None,
                f"SQL source is not valid UTF-8: {error}",
            )
            artifact = _artifact("", raw, sql_config.dialect)
            return _file(path, artifact, (), (), (diagnostic,))
    elif isinstance(text, str):
        source = text
        raw = text.encode("utf-8")
    else:
        raise TypeError("parse_sql expects str or bytes source")

    default_schema = str(context_values.get("default_schema", sql_config.default_schema))
    search_path_value = context_values.get("search_path", sql_config.search_path)
    search_path = _as_search_path(search_path_value)
    database = _optional_string(context_values.get("database", sql_config.database))
    artifact = _artifact(source, raw, sql_config.dialect)
    builder = _Builder(source, raw, artifact, default_schema, search_path, database)
    for segment in _split_statements(source, raw):
        builder.process_segment(segment)
    return _file(
        path,
        artifact,
        tuple(builder.statements),
        tuple(builder.object_refs),
        tuple(builder.diagnostics),
    )


class _Builder:
    def __init__(
        self,
        source: str,
        raw: bytes,
        artifact: SqlArtifactIR,
        default_schema: str,
        search_path: tuple[str, ...],
        database: str | None,
    ) -> None:
        self.source = source
        self.raw = raw
        self.locations = ByteLocations(raw)
        self._offset_segment: tuple[int, int] | None = None
        self._node_offsets: Utf8Offsets | None = None
        self._node_source = ""
        self._parse_prefix_chars = 0
        self.artifact = artifact
        self.default_schema = default_schema
        self.search_path = search_path
        self.creation_schema: str | None = default_schema
        self.database = database
        self.temporary_names: set[str] = set()
        self.statements: list[SqlStatementIR] = []
        self.object_refs: list[SqlObjectRefIR] = []
        self.diagnostics: list[ParseDiagnosticIR] = []

    def process_segment(
        self,
        segment: _Segment,
        *,
        parent_ordinal: int | None = None,
        control_context: str | None = None,
        parse_prefix: str = "",
    ) -> int | None:
        source_bytes = self.raw[segment.start : segment.end]
        parse_source = parse_prefix + source_bytes.decode("utf-8", errors="replace")
        self._parse_prefix_chars = len(parse_prefix)
        # PL/pgSQL PERFORM has SELECT semantics, but is not standalone SQL.
        # The equal-width replacement keeps every parser location aligned to
        # the original source bytes.
        if re.match(r"\s*PERFORM\b", parse_source, flags=re.I):
            match = re.search(r"PERFORM", parse_source, flags=re.I)
            assert match is not None
            parse_source = parse_source[: match.start()] + "SELECT " + parse_source[match.end() :]
        try:
            parsed = pg_parse_sql(parse_source)
        except (ParseError, UnicodeDecodeError) as error:
            position = _parse_error_position(error)
            source_text = source_bytes.decode("utf-8", errors="replace")
            source_position = len(
                source_text[: max(0, position - self._parse_prefix_chars)].encode("utf-8")
            )
            position = segment.start + source_position
            location = self._location(position, segment.end)
            self.diagnostics.append(
                ParseDiagnosticIR(
                    "sql_parse_error",
                    "error",
                    location.start_line,
                    location.start_column,
                    str(error),
                )
            )
            return None
        if not parsed:
            return None
        root = parsed[0]
        statement = root.stmt
        ordinal = len(self.statements) + 1
        kind = _statement_kind(statement)
        statement_start = _trim_leading_sql(self.raw, segment.start, segment.end)
        self.statements.append(
            SqlStatementIR(
                artifact_ordinal=self.artifact.ordinal,
                ordinal=ordinal,
                kind=kind,
                parent_ordinal=parent_ordinal,
                control_context=control_context,
                **_location_values(self._location(statement_start, segment.end)),
            )
        )
        self._extract_statement(statement, ordinal, segment, control_context)
        if kind == "set" and _is_search_path_set(statement):
            self.search_path = _search_path_from_statement(statement)
            self.creation_schema = self.search_path[0] if self.search_path else None
        return ordinal

    def _extract_statement(
        self,
        statement: pgast.Node,
        ordinal: int,
        segment: _Segment,
        control_context: str | None,
    ) -> None:
        known = {
            "SelectStmt",
            "InsertStmt",
            "UpdateStmt",
            "DeleteStmt",
            "MergeStmt",
            "CreateStmt",
            "CreateTableAsStmt",
            "ViewStmt",
            "CreateFunctionStmt",
            "CreateSchemaStmt",
            "CreateSeqStmt",
            "CreateIndexStmt",
            "CreateExtensionStmt",
            "IndexStmt",
            "AlterTableStmt",
            "AlterFunctionStmt",
            "AlterObjectSchemaStmt",
            "AlterObjectDependsStmt",
            "RenameStmt",
            "DropStmt",
            "TruncateStmt",
            "VariableSetStmt",
            "CallStmt",
        }
        class_name = type(statement).__name__
        if class_name not in known:
            self._diagnostic(
                "sql_unsupported_construct",
                "warning",
                segment.start,
                f"SQL construct {class_name} is parsed but not semantically extracted",
            )

        skip: set[int] = set()
        temporary_ctas_name: str | None = None
        if class_name in {"CreateStmt", "CreateSeqStmt", "CreateIndexStmt", "CreateStatsStmt"}:
            relation = getattr(statement, "relation", None) or getattr(statement, "sequence", None)
            if relation is not None:
                skip.add(id(relation))
                role = "define"
                kind = _definition_kind(statement)
                self._range_ref(relation, ordinal, role, kind, segment)
                if kind == "temporary_table" and getattr(relation, "relname", None):
                    self.temporary_names.add(str(relation.relname))
        elif class_name == "IndexStmt":
            index_name = _name(getattr(statement, "idxname", None))
            if index_name:
                relation = getattr(statement, "relation", None)
                schema = (
                    _name(getattr(relation, "schemaname", None)) if relation is not None else None
                )
                if schema is not None:
                    self._add_ref(
                        ordinal,
                        "define",
                        index_name,
                        None,
                        schema,
                        index_name,
                        "index",
                        None,
                        statement,
                        segment,
                        dynamic=False,
                    )
            relation = getattr(statement, "relation", None)
            if relation is not None:
                skip.add(id(relation))
                self._range_ref(relation, ordinal, "read", "table", segment)
        elif class_name == "CreateTableAsStmt":
            relation = getattr(statement, "into", None)
            relation = getattr(relation, "rel", relation)
            if relation is not None:
                skip.add(id(relation))
                if getattr(relation, "relpersistence", "p") == "t" and getattr(
                    relation, "relname", None
                ):
                    temporary_ctas_name = str(relation.relname)
                self._range_ref(
                    relation,
                    ordinal,
                    "define",
                    "temporary_table"
                    if getattr(relation, "relpersistence", "p") == "t"
                    else (
                        "materialized_view"
                        if (
                            getattr(statement, "objtype", None)
                            and getattr(statement.objtype, "name", None) == "OBJECT_MATVIEW"
                        )
                        else "table"
                    ),
                    segment,
                )
        elif class_name == "ViewStmt":
            relation = getattr(statement, "view", None)
            if relation is not None:
                skip.add(id(relation))
                self._range_ref(relation, ordinal, "define", "view", segment)
        elif class_name == "CreateFunctionStmt":
            routine = self._routine_ref(statement, ordinal, segment)
            body = _function_body(statement)
            language = _function_language(statement)
            if body is not None:
                outer_path = self.search_path
                outer_creation_schema = self.creation_schema
                try:
                    for option in statement.options or ():
                        if option.defname == "set" and _is_search_path_set(option.arg):
                            self.search_path = _search_path_from_statement(option.arg)
                            self.creation_schema = self.search_path[0] if self.search_path else None
                    self._extract_function_body(
                        statement, ordinal, routine, body, language, segment
                    )
                finally:
                    self.search_path = outer_path
                    self.creation_schema = outer_creation_schema
        elif class_name in {"InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt"}:
            relation = getattr(statement, "relation", None)
            if relation is not None:
                skip.add(id(relation))
                self._range_ref(relation, ordinal, "write", "table", segment)
                if class_name in {"UpdateStmt", "DeleteStmt", "MergeStmt"}:
                    self._range_ref(relation, ordinal, "read", "table", segment)
        elif class_name == "CreateSchemaStmt":
            schema_name = _name(getattr(statement, "schemaname", None))
            if schema_name:
                self._add_ref(
                    ordinal,
                    "define",
                    schema_name,
                    None,
                    None,
                    schema_name,
                    "schema",
                    None,
                    statement,
                    segment,
                    dynamic=False,
                )
        elif class_name == "CreateExtensionStmt":
            extension_name = _name(getattr(statement, "extname", None))
            if extension_name:
                self._add_ref(
                    ordinal,
                    "define",
                    extension_name,
                    None,
                    None,
                    extension_name,
                    "extension",
                    None,
                    statement,
                    segment,
                    dynamic=False,
                )
        elif class_name in {
            "AlterTableStmt",
            "AlterFunctionStmt",
            "AlterObjectSchemaStmt",
            "RenameStmt",
        }:
            relation = getattr(statement, "relation", None)
            if relation is not None:
                skip.add(id(relation))
                self._range_ref(relation, ordinal, "alter", _definition_kind(statement), segment)
            elif class_name == "AlterFunctionStmt":
                self._object_with_args_ref(
                    getattr(statement, "func", None), ordinal, "alter", "function", segment
                )
        elif class_name == "CallStmt":
            call = getattr(statement, "funccall", None) or getattr(statement, "funcexpr", None)
            if call is not None:
                skip.add(id(call))
                self._object_with_args_ref(call, ordinal, "call", "procedure", segment)
        elif class_name == "DropStmt":
            objects = getattr(statement, "objects", ()) or ()
            for item in objects:
                self._drop_object(item, ordinal, segment, _drop_kind(statement))
        elif class_name == "TruncateStmt":
            for relation in getattr(statement, "relations", ()) or ():
                self._range_ref(relation, ordinal, "write", "table", segment)
                skip.add(id(relation))
        self._walk(statement, ordinal, segment, set(), skip)
        if temporary_ctas_name is not None:
            self.temporary_names.add(temporary_ctas_name)

    def _walk(
        self,
        node: object,
        ordinal: int,
        segment: _Segment,
        scope: set[str],
        skip: set[int],
    ) -> None:
        if node is None or isinstance(node, (str, bytes, int, bool)):
            return
        if isinstance(node, pgast.SelectStmt):
            with_clause = getattr(node, "withClause", None)
            next_scope = self._walk_with_clause(with_clause, ordinal, segment, scope, skip)
            for name in node:
                if name == "withClause":
                    continue
                try:
                    value = getattr(node, name)
                except AttributeError:
                    continue
                self._walk(value, ordinal, segment, next_scope, skip)
            return
        if isinstance(
            node, (pgast.InsertStmt, pgast.UpdateStmt, pgast.DeleteStmt, pgast.MergeStmt)
        ):
            relation = node.relation
            if relation is not None and id(relation) not in skip:
                skip = skip | {id(relation)}
                self._range_ref(relation, ordinal, "write", "table", segment)
                if not isinstance(node, pgast.InsertStmt):
                    self._range_ref(relation, ordinal, "read", "table", segment)
            with_clause = getattr(node, "withClause", None)
            next_scope = self._walk_with_clause(with_clause, ordinal, segment, scope, skip)
            for name in node:
                if name == "withClause":
                    continue
                try:
                    value = getattr(node, name)
                except AttributeError:
                    continue
                self._walk(value, ordinal, segment, next_scope, skip)
            return
        if isinstance(node, pgast.RangeVar):
            relname = getattr(node, "relname", None)
            if id(node) not in skip and (
                relname not in scope or getattr(node, "schemaname", None) is not None
            ):
                self._range_ref(node, ordinal, "read", "table", segment)
            return
        if isinstance(node, pgast.FuncCall) and id(node) not in skip:
            self._object_with_args_ref(node, ordinal, "call", "function", segment)
        if isinstance(node, pgast.Node):
            for field in node:
                try:
                    value = getattr(node, field)
                except AttributeError:
                    continue
                self._walk(value, ordinal, segment, scope, skip)
            return
        if isinstance(node, (tuple, list)):
            for value in node:
                self._walk(value, ordinal, segment, scope, skip)

    def _walk_with_clause(
        self,
        with_clause: object,
        ordinal: int,
        segment: _Segment,
        outer_scope: set[str],
        skip: set[int],
    ) -> set[str]:
        if with_clause is None:
            return outer_scope
        ctes = tuple(getattr(with_clause, "ctes", ()) or ())
        names = {name for cte in ctes if (name := _name(getattr(cte, "ctename", None))) is not None}
        available = set(outer_scope)
        recursive = bool(getattr(with_clause, "recursive", False))
        for cte in ctes:
            query = getattr(cte, "ctequery", None)
            query_scope = available | names if recursive else available
            self._walk(query, ordinal, segment, query_scope, skip)
            name = _name(getattr(cte, "ctename", None))
            if name:
                available.add(name)
        return available

    def _range_ref(
        self,
        node: object,
        statement_ordinal: int,
        role: str,
        kind: str | None,
        segment: _Segment,
    ) -> None:
        database = _name(getattr(node, "catalogname", None))
        schema = _name(getattr(node, "schemaname", None))
        object_name = _name(getattr(node, "relname", None))
        if (
            role == "define"
            and kind != "temporary_table"
            and schema is None
            and object_name is not None
        ):
            schema = self.creation_schema
        raw_name = self._raw_name(node, segment, database, schema, object_name)
        if object_name in self.temporary_names and kind == "table" and schema is None:
            kind = "temporary_table"
        self._add_ref(
            statement_ordinal,
            role,
            raw_name,
            database,
            schema,
            object_name,
            kind,
            None,
            node,
            segment,
            dynamic=False,
        )

    def _routine_ref(
        self, node: pgast.CreateFunctionStmt, statement_ordinal: int, segment: _Segment
    ) -> SqlObjectRefIR:
        names = tuple(_name(value) for value in (getattr(node, "funcname", ()) or ()))
        names = tuple(value for value in names if value is not None)
        object_name = names[-1] if names else None
        schema = names[-2] if len(names) >= 2 else self.creation_schema
        database = names[-3] if len(names) >= 3 else None
        args = _routine_input_args(node)
        signature = f"{object_name}({','.join(args)})" if object_name else None
        raw_name = self._routine_raw_name(segment, database, schema, object_name)
        name_position = self._routine_name_position(segment)
        return self._add_ref(
            statement_ordinal,
            "define",
            raw_name,
            database,
            schema,
            object_name,
            "procedure" if getattr(node, "is_procedure", False) else "function",
            signature,
            node,
            segment,
            dynamic=False,
            position=name_position,
        )

    def _routine_raw_name(
        self,
        segment: _Segment,
        database: str | None,
        schema: str | None,
        object_name: str | None,
    ) -> str:
        source = self.raw[segment.start : segment.end].decode("utf-8", errors="replace")
        match = re.search(
            rf"\b(?:function|procedure)\s+({_QUALIFIED_IDENTIFIER.pattern})",
            source,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        pieces = [value for value in (database, schema, object_name) if value is not None]
        return ".".join(pieces)

    def _routine_name_position(self, segment: _Segment) -> int:
        source = self.raw[segment.start : segment.end].decode("utf-8", errors="replace")
        match = re.search(
            rf"\b(?:function|procedure)\s+({_QUALIFIED_IDENTIFIER.pattern})",
            source,
            flags=re.IGNORECASE,
        )
        if not match:
            return segment.start
        return segment.start + len(source[: match.start(1)].encode("utf-8"))

    def _object_with_args_ref(
        self,
        node: object,
        statement_ordinal: int,
        role: str,
        kind: str,
        segment: _Segment,
    ) -> None:
        if node is None:
            return
        names = tuple(_name(value) for value in (getattr(node, "funcname", None) or ()))
        if not names:
            names = tuple(_name(value) for value in (getattr(node, "objname", None) or ()))
        names = tuple(value for value in names if value is not None)
        if not names:
            return
        object_name = names[-1]
        schema = names[-2] if len(names) >= 2 else None
        database = names[-3] if len(names) >= 3 else None
        args = getattr(node, "args", None)
        if args is None:
            args = getattr(node, "objargs", None)
        args = args or ()
        canonical_args = _canonical_args(args)
        signature = (
            f"{object_name}({canonical_args})" if not args or canonical_args is not None else None
        )
        self._add_ref(
            statement_ordinal,
            role,
            self._raw_name(node, segment, database, schema, object_name),
            database,
            schema,
            object_name,
            kind,
            signature,
            node,
            segment,
            dynamic=False,
            call_arity=len(args) if role == "call" and isinstance(node, pgast.FuncCall) else None,
        )

    def _drop_object(
        self, node: object, statement_ordinal: int, segment: _Segment, kind: str
    ) -> None:
        if isinstance(node, pgast.RangeVar):
            self._range_ref(node, statement_ordinal, "drop", "object", segment)
        elif isinstance(node, pgast.ObjectWithArgs):
            self._object_with_args_ref(node, statement_ordinal, "drop", "routine", segment)
        else:
            values = node if isinstance(node, (tuple, list)) else (node,)
            names = tuple(_name(value) for value in values)
            if not any(names):
                names = tuple(_name(value) for value in (getattr(node, "objname", ()) or ()))
            names = tuple(value for value in names if value is not None)
            if names:
                object_name = names[-1]
                schema = names[-2] if len(names) >= 2 else None
                self._add_ref(
                    statement_ordinal,
                    "drop",
                    ".".join(names),
                    None,
                    schema,
                    object_name,
                    kind,
                    None,
                    node,
                    segment,
                    dynamic=False,
                )

    def _extract_function_body(
        self,
        statement: pgast.CreateFunctionStmt,
        parent_ordinal: int,
        routine: SqlObjectRefIR,
        body: str,
        language: str | None,
        segment: _Segment,
    ) -> None:
        body_bytes = body.encode("utf-8")
        body_offset = _function_body_offset(statement, body, segment, self.raw)
        if body_offset < 0:
            self._diagnostic(
                "sql_unsupported_construct",
                "warning",
                segment.start,
                "routine body source span could not be located; body dependencies omitted",
            )
            return
        absolute_body = segment.start + body_offset
        if language in {None, "sql"}:
            body_segments = _split_statements(body, body_bytes)
            for body_segment in body_segments:
                self.process_segment(
                    _Segment(absolute_body + body_segment.start, absolute_body + body_segment.end),
                    parent_ordinal=parent_ordinal,
                    control_context=f"routine:{routine.raw_name}",
                )
            return
        if language == "plpgsql":
            try:
                parsed = parse_plpgsql(self.raw[segment.start : segment.end].decode())
            except ParseError as error:
                self._diagnostic(
                    "sql_unsupported_construct",
                    "warning",
                    absolute_body,
                    f"PL/pgSQL body was not statically parsed: {error}",
                )
                return
            self._extract_plpgsql(
                parsed, body, absolute_body, parent_ordinal, routine.raw_name, segment
            )
            return
        self._diagnostic(
            "sql_unsupported_construct",
            "warning",
            absolute_body,
            f"routine language {language!r} is not statically supported",
        )

    def _extract_plpgsql(
        self,
        parsed: object,
        body: str,
        body_offset: int,
        parent_ordinal: int,
        context: str,
        segment: _Segment,
    ) -> None:
        protected = _protected_query_spans(body)
        body_offsets = Utf8Offsets(body)
        body_locations = ByteLocations(body.encode("utf-8"))
        begin_cursor = 0
        begin_offset = None
        while match := re.search(r"\bBEGIN\b", body[begin_cursor:], flags=re.I):
            begin_start = begin_cursor + match.start()
            begin_end = begin_cursor + match.end()
            if _query_span_is_source(body, begin_start, begin_end, protected=protected):
                begin_offset = begin_end
                break
            begin_cursor = begin_start + 1
        cursor = begin_offset or 0

        conditional_warning = False
        default_warning = False

        def visit(value: object, conditional: bool = False) -> None:
            nonlocal cursor
            nonlocal conditional_warning
            nonlocal default_warning
            if isinstance(value, dict):
                if "default_val" in value and not default_warning:
                    self._diagnostic(
                        "sql_unsupported_construct",
                        "warning",
                        body_offset,
                        "PL/pgSQL declaration default expressions are not statically extracted",
                    )
                    default_warning = True
                child_conditional = conditional or any(
                    key.startswith("PLpgSQL_stmt_")
                    and key.removeprefix("PLpgSQL_stmt_").lower()
                    in {"if", "case", "loop", "while", "for", "foreach"}
                    for key in value
                )
                if child_conditional and not conditional_warning:
                    self._diagnostic(
                        "sql_unsupported_construct",
                        "warning",
                        body_offset,
                        "PL/pgSQL conditional/control-flow context is only partially extracted",
                    )
                    conditional_warning = True
                execsql = value.get("PLpgSQL_stmt_execsql")
                perform = value.get("PLpgSQL_stmt_perform")
                called = value.get("PLpgSQL_stmt_call")
                returned = value.get("PLpgSQL_stmt_return")
                assigned = value.get("PLpgSQL_stmt_assign")
                expression = None
                expression_prefix = ""
                if isinstance(execsql, dict):
                    query_value = execsql.get("sqlstmt", {})
                    expression = (
                        query_value.get("PLpgSQL_expr") if isinstance(query_value, dict) else None
                    )
                elif isinstance(perform, dict):
                    expression = perform.get("expr", {}).get("PLpgSQL_expr")
                elif isinstance(called, dict):
                    expression = called.get("expr", {}).get("PLpgSQL_expr")
                elif isinstance(returned, dict):
                    expression = returned.get("expr", {}).get("PLpgSQL_expr")
                    expression_prefix = "SELECT "
                elif isinstance(assigned, dict):
                    expression = assigned.get("expr", {}).get("PLpgSQL_expr")
                    expression_prefix = "SELECT "
                query = expression.get("query") if isinstance(expression, dict) else None
                if isinstance(query, str):
                    source_query = query
                    if isinstance(returned, dict):
                        source_query = query
                    elif isinstance(assigned, dict):
                        # PL/pgSQL's parser returns the assignment as ``name :=
                        # expression``. Only the RHS is SQL-expression source.
                        assignment = re.match(r"\s*[\w.]+\s*:=\s*", query)
                        if assignment is None:
                            self._diagnostic(
                                "sql_unsupported_construct",
                                "warning",
                                body_offset,
                                "PL/pgSQL assignment expression could not be isolated",
                            )
                            source_query = ""
                        else:
                            source_query = query[assignment.end() :]
                    found = (
                        _find_query(body, source_query, cursor, protected=protected)
                        if source_query
                        else None
                    )
                    if found is not None:
                        if isinstance(execsql, dict) or isinstance(perform, dict):
                            source_query = (
                                "PERFORM" + query[6:]
                                if query[:6].upper() == "SELECT"
                                and body[found : found + 7].upper() == "PERFORM"
                                else query
                            )
                        cursor = found + len(source_query)
                        self.process_segment(
                            _Segment(
                                body_offset + body_offsets.byte_offset(found),
                                body_offset + body_offsets.byte_offset(found + len(source_query)),
                            ),
                            parent_ordinal=parent_ordinal,
                            control_context=(
                                f"{context}:conditional" if child_conditional else context
                            ),
                            parse_prefix=expression_prefix,
                        )
                    else:
                        self._diagnostic(
                            "sql_unsupported_construct",
                            "warning",
                            body_offset,
                            "static PL/pgSQL SQL statement source span was not located",
                        )
                dynamic = value.get("PLpgSQL_stmt_dynexecute")
                if dynamic is not None:
                    line = dynamic.get("lineno") if isinstance(dynamic, dict) else None
                    offset = body_offset + body_locations.line_start(int(line or 1))
                    self._diagnostic(
                        "sql_dynamic_reference",
                        "warning",
                        offset,
                        "dynamic EXECUTE reference was not resolved",
                    )
                    self._add_ref(
                        parent_ordinal,
                        "read",
                        "<dynamic EXECUTE>",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        segment,
                        dynamic=True,
                        position=offset,
                    )
                for child in value.values():
                    visit(child, child_conditional)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, conditional)

        visit(parsed)

    def _add_ref(
        self,
        statement_ordinal: int,
        role: str,
        raw_name: str,
        database: str | None,
        schema: str | None,
        object_name: str | None,
        kind: str | None,
        signature: str | None,
        node: object | None,
        segment: _Segment,
        *,
        dynamic: bool,
        position: int | None = None,
        call_arity: int | None = None,
    ) -> SqlObjectRefIR:
        if position is None:
            position = self._node_position(node, segment)
        name_end = position + len(raw_name.encode("utf-8")) if raw_name else position
        location = self._location(position, min(name_end, len(self.raw)))
        ref = SqlObjectRefIR(
            artifact_ordinal=self.artifact.ordinal,
            statement_ordinal=statement_ordinal,
            ordinal=len(self.object_refs) + 1,
            role=role,  # type: ignore[arg-type]
            raw_name=raw_name,
            database_name=database or self.database,
            schema_name=schema,
            object_name=object_name,
            object_kind_hint=kind,
            signature_hint=signature,
            dynamic=dynamic,
            search_path=self.search_path,
            call_arity=call_arity,
            **_location_values(location),
        )
        self.object_refs.append(ref)
        return ref

    def _raw_name(
        self,
        node: object,
        segment: _Segment,
        database: str | None,
        schema: str | None,
        object_name: str | None,
    ) -> str:
        self._index_segment(segment)
        offset = getattr(node, "location", None)
        offset = offset if isinstance(offset, int) and offset >= 0 else 0
        offset = max(0, offset - self._parse_prefix_chars)
        match = _QUALIFIED_IDENTIFIER.match(self._node_source, offset)
        if match:
            return match.group(0)
        pieces = [value for value in (database, schema, object_name) if value is not None]
        return ".".join(pieces)

    def _location(self, start: int, end: int) -> _Location:
        start_line, start_column = self.locations.position(max(0, start))
        end_line, end_column = self.locations.position(max(start, end))
        return _Location(start_line, start_column + 1, end_line, end_column + 1)

    def _node_position(self, node: object | None, segment: _Segment) -> int:
        value = getattr(node, "location", None)
        if not isinstance(value, int) or value < 0:
            return segment.start
        self._index_segment(segment)
        assert self._node_offsets is not None
        source_char_offset = max(0, value - self._parse_prefix_chars)
        return segment.start + self._node_offsets.byte_offset(source_char_offset)

    def _index_segment(self, segment: _Segment) -> None:
        identity = (segment.start, segment.end)
        if identity != self._offset_segment:
            self._node_source = self.raw[segment.start : segment.end].decode(
                "utf-8", errors="replace"
            )
            self._node_offsets = Utf8Offsets(self._node_source)
            self._offset_segment = identity

    def _diagnostic(self, category: str, severity: str, position: int, message: str) -> None:
        location = self._location(position, position)
        self.diagnostics.append(
            ParseDiagnosticIR(
                category,  # type: ignore[arg-type]
                severity,  # type: ignore[arg-type]
                location.start_line,
                location.start_column,
                message,
            )
        )


def _file(
    path: str,
    artifact: SqlArtifactIR,
    statements: tuple[SqlStatementIR, ...],
    refs: tuple[SqlObjectRefIR, ...],
    diagnostics: tuple[ParseDiagnosticIR, ...],
) -> FileIR:
    has_error = any(item.severity == "error" for item in diagnostics)
    has_warning = bool(diagnostics)
    if has_error and statements:
        status = "partial"
    elif has_error:
        status = "error"
    elif has_warning:
        status = "partial"
    else:
        status = "ok"
    return FileIR(
        path=path,
        language="sql",
        loc=artifact.text.count("\n") + 1,
        module_qname=f"sql:{path}",
        parse_status=status,
        diagnostics=diagnostics,
        sql_artifacts=(artifact,),
        sql_statements=statements,
        sql_object_refs=refs,
    )


def _artifact(source: str, raw: bytes, dialect: str) -> SqlArtifactIR:
    end = _location(raw, 0, len(raw))
    return SqlArtifactIR(
        ordinal=1,
        origin="sql_file",
        dialect=dialect,
        text=source,
        text_hash=hashlib.sha256(raw).hexdigest(),
        start_line=1,
        start_column=1,
        end_line=end.end_line,
        end_column=end.end_column,
    )


def _context_values(context: Mapping[str, object] | str | None) -> tuple[str, Mapping[str, object]]:
    if context is None:
        return "<sql>", {}
    if isinstance(context, str):
        return PurePosixPath(context).as_posix(), {}
    values = dict(context)
    path = str(values.pop("path", "<sql>"))
    return PurePosixPath(path).as_posix(), values


def _split_statements(source: str, raw: bytes) -> tuple[_Segment, ...]:
    """Split using PostgreSQL's scanner, with a malformed-input fallback."""

    try:
        slices = pg_split(source, with_parser=False, only_slices=True)
    except ParseError:
        return _fallback_split(raw)
    segments = []
    offsets = Utf8Offsets(source)
    for item in slices:
        start = offsets.byte_offset(item.start)
        end = offsets.byte_offset(item.stop)
        start = _trim_leading_sql(raw, start, end)
        if raw[start:end].strip():
            segments.append(_Segment(start, end))
    return tuple(segments)


def _char_offset(source: str, offset: int) -> int:
    return len(source[:offset].encode("utf-8"))


def _trim_leading_sql(raw: bytes, start: int, end: int) -> int:
    i = start
    while i < end:
        if raw[i : i + 1].isspace():
            i += 1
            continue
        if raw[i : i + 2] == b"--":
            newline = raw.find(b"\n", i + 2, end)
            i = end if newline < 0 else newline + 1
            continue
        if raw[i : i + 2] == b"/*":
            depth = 1
            i += 2
            while i < end and depth:
                if raw[i : i + 2] == b"/*":
                    depth += 1
                    i += 2
                elif raw[i : i + 2] == b"*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        break
    return i


def _fallback_split(raw: bytes) -> tuple[_Segment, ...]:
    """Best-effort scanner for malformed SQL the native scanner cannot split."""

    segments: list[_Segment] = []
    start: int | None = None
    i = 0
    state = "normal"
    dollar_tag: bytes | None = None
    block_depth = 0
    e_string = False
    while i < len(raw):
        if state == "line_comment":
            if raw[i] in b"\r\n":
                state = "normal"
            i += 1
            continue
        if state == "block_comment":
            if raw[i : i + 2] == b"/*":
                block_depth += 1
                i += 2
            elif raw[i : i + 2] == b"*/":
                block_depth -= 1
                i += 2
                if block_depth == 0:
                    state = "normal"
            else:
                i += 1
            continue
        if state in {"single", "double"}:
            quote = b"'" if state == "single" else b'"'
            if (
                state == "single"
                and e_string
                and raw[i : i + 1] == b"\\"
                or raw[i : i + 2] == quote * 2
            ):
                i += 2
            elif raw[i : i + 1] == quote:
                state = "normal"
                e_string = False
                i += 1
            else:
                i += 1
            continue
        if dollar_tag is not None:
            end = raw.find(dollar_tag, i)
            if end < 0:
                break
            i = end + len(dollar_tag)
            dollar_tag = None
            continue
        if raw[i : i + 2] == b"--":
            state = "line_comment"
            i += 2
            continue
        if raw[i : i + 2] == b"/*":
            state = "block_comment"
            block_depth = 1
            i += 2
            continue
        if raw[i : i + 2].lower() == b"e'":
            if start is None:
                start = i
            state = "single"
            e_string = True
            i += 2
            continue
        if raw[i : i + 1] == b"'":
            if start is None:
                start = i
            state = "single"
            i += 1
            continue
        if raw[i : i + 1] == b'"':
            if start is None:
                start = i
            state = "double"
            i += 1
            continue
        if raw[i : i + 1] == b"$":
            match = re.match(rb"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", raw[i:])
            if match:
                if start is None:
                    start = i
                dollar_tag = match.group(0)
                i += len(dollar_tag)
                continue
        if raw[i : i + 1] == b";":
            if start is not None and raw[start:i].strip():
                segments.append(_Segment(start, i))
            start = None
            i += 1
            continue
        if start is None and raw[i : i + 1].isspace():
            i += 1
            continue
        if start is None:
            start = i
        i += 1
    if start is not None and raw[start:].strip():
        segments.append(_Segment(start, len(raw)))
    return tuple(segments)


def _location(raw: bytes, start: int, end: int) -> _Location:
    start_line, start_column = _line_column(raw, start)
    end_line, end_column = _line_column(raw, end)
    return _Location(start_line, start_column, end_line, end_column)


def _line_column(raw: bytes, offset: int) -> tuple[int, int]:
    prefix = raw[:offset]
    line = prefix.count(b"\n") + 1
    last_newline = prefix.rfind(b"\n")
    column = len(prefix) - last_newline
    return line, column


def _location_values(location: _Location) -> dict[str, int]:
    return {
        "start_line": location.start_line,
        "start_column": location.start_column,
        "end_line": location.end_line,
        "end_column": location.end_column,
    }


def _parse_error_position(error: Exception) -> int:
    args = getattr(error, "args", ())
    return int(args[1]) if len(args) > 1 and isinstance(args[1], int) else 0


def _node_position(node: object | None, fallback: int) -> int:
    value = getattr(node, "location", None)
    return fallback + int(value) if isinstance(value, int) and value >= 0 else fallback


def _name(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return getattr(value, "sval", None) or getattr(value, "name", None)


def _statement_kind(statement: object) -> str:
    name = type(statement).__name__
    mapping = {
        "SelectStmt": "select",
        "InsertStmt": "insert",
        "UpdateStmt": "update",
        "DeleteStmt": "delete",
        "MergeStmt": "merge",
        "CreateStmt": "create_table",
        "CreateTableAsStmt": "create_table_as",
        "ViewStmt": "create_view",
        "CreateSchemaStmt": "create_schema",
        "CreateSeqStmt": "create_sequence",
        "CreateFunctionStmt": "create_procedure"
        if getattr(statement, "is_procedure", False)
        else "create_function",
        "AlterTableStmt": "alter_table",
        "DropStmt": "drop",
        "VariableSetStmt": "set",
        "CallStmt": "call",
        "DoStmt": "do",
    }
    return mapping.get(name, re.sub(r"Stmt$", "", name).lower())


def _definition_kind(statement: object) -> str:
    name = type(statement).__name__
    object_type = getattr(getattr(statement, "objtype", None), "name", None)
    if name == "RenameStmt":
        rename_type = getattr(getattr(statement, "renameType", None), "name", None)
        return {
            "OBJECT_TABLE": "table",
            "OBJECT_SEQUENCE": "sequence",
            "OBJECT_VIEW": "view",
            "OBJECT_MATVIEW": "materialized_view",
            "OBJECT_INDEX": "index",
            "OBJECT_STATISTICS": "statistics",
            "OBJECT_SCHEMA": "schema",
            "OBJECT_EXTENSION": "extension",
            "OBJECT_TYPE": "type",
        }.get(rename_type, "object")
    if object_type == "OBJECT_TABLE":
        return "table"
    if object_type == "OBJECT_INDEX":
        return "index"
    if object_type == "OBJECT_MATVIEW":
        return "materialized_view"
    if object_type == "OBJECT_SEQUENCE":
        return "sequence"
    if object_type == "OBJECT_STATISTICS":
        return "statistics"
    if object_type == "OBJECT_SCHEMA":
        return "schema"
    if object_type == "OBJECT_EXTENSION":
        return "extension"
    if object_type == "OBJECT_TYPE":
        return "type"
    if name == "AlterFunctionStmt":
        return "function"
    return {
        "CreateStmt": "temporary_table"
        if getattr(getattr(statement, "relation", None), "relpersistence", "p") == "t"
        else "table",
        "CreateSeqStmt": "sequence",
        "CreateIndexStmt": "index",
        "CreateStatsStmt": "statistics",
    }.get(name, "object")


def _drop_kind(statement: object) -> str:
    value = getattr(getattr(statement, "removeType", None), "name", "")
    return {
        "OBJECT_TABLE": "table",
        "OBJECT_SEQUENCE": "sequence",
        "OBJECT_VIEW": "view",
        "OBJECT_MATVIEW": "materialized_view",
        "OBJECT_SCHEMA": "schema",
        "OBJECT_FUNCTION": "function",
        "OBJECT_PROCEDURE": "procedure",
        "OBJECT_INDEX": "index",
        "OBJECT_TYPE": "type",
    }.get(value, "object")


def _cte_names(select: pgast.SelectStmt) -> set[str]:
    with_clause = getattr(select, "withClause", None)
    return {
        name
        for item in (getattr(with_clause, "ctes", ()) or ())
        if (name := _name(getattr(item, "ctename", None))) is not None
    }


def _function_body(statement: pgast.CreateFunctionStmt) -> str | None:
    for option in getattr(statement, "options", ()) or ():
        if getattr(option, "defname", None) == "as":
            value = getattr(option, "arg", None)
            if isinstance(value, (tuple, list)) and value:
                return _name(value[0])
            return _name(value)
    return None


def _function_language(statement: pgast.CreateFunctionStmt) -> str | None:
    for option in getattr(statement, "options", ()) or ():
        if getattr(option, "defname", None) == "language":
            return (_name(getattr(option, "arg", None)) or "").lower()
    return None


def _routine_input_args(statement: pgast.CreateFunctionStmt) -> tuple[str, ...]:
    args: list[str] = []
    for parameter in getattr(statement, "parameters", ()) or ():
        mode = getattr(getattr(parameter, "mode", None), "value", "i")
        if mode in {"o", "t"}:
            continue
        args.append(_type_name(getattr(parameter, "argType", None)))
    return tuple(args)


def _type_name(node: object) -> str:
    names = [_name(value) for value in (getattr(node, "names", ()) or ())]
    names = [value for value in names if value]
    if names and names[0] == "pg_catalog":
        names = names[1:]
    # pglast already folds unquoted names and canonicalizes builtin aliases.
    # Quote components that could otherwise collide with signature syntax.
    value = ".".join(_signature_identifier(name) for name in names) or "unknown"
    array_bounds = getattr(node, "arrayBounds", None) or ()
    return value + "[]" * len(array_bounds)


def _signature_identifier(name: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _canonical_args(args: Sequence[object]) -> str | None:
    if not args:
        return ""
    values: list[str] = []
    for value in args:
        if isinstance(value, pgast.TypeCast):
            values.append(_type_name(value.typeName))
        else:
            return None
    return ",".join(values)


def _is_search_path_set(statement: object) -> bool:
    return _name(getattr(statement, "name", None)) == "search_path"


def _search_path_from_statement(statement: object) -> tuple[str, ...]:
    if getattr(getattr(statement, "kind", None), "name", "") != "VAR_SET_VALUE":
        return ()
    values: list[str] = []
    for arg in getattr(statement, "args", ()) or ():
        if not isinstance(arg, pgast.A_Const) or not isinstance(arg.val, pgast.String):
            return ()
        item = arg.val.sval
        # Runtime substitutions and compound GUC strings need a session/catalog.
        # Abstain for the whole path, never silently skip its first component.
        if not item or "$user" in item or "," in item or item == "pg_temp":
            return ()
        values.append(item)
    return tuple(values)


def _as_search_path(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _unquote_identifier(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()


def _protected_query_spans(body: str) -> tuple[tuple[int, int], ...]:
    try:
        tokens = tuple(pg_scan(body))
    except Exception:
        return ((0, len(body)),)
    protected: list[tuple[int, int]] = []
    for token in tokens:
        token_text = body[token.start : token.end]
        if token.name in {"C_COMMENT", "SQL_COMMENT", "SCONST"} or (
            token.name == "IDENT" and token_text.startswith('"')
        ):
            protected.append((token.start, token.end))
    return tuple(protected)


def _query_span_is_source(
    body: str, start: int, end: int, *, protected: tuple[tuple[int, int], ...] | None = None
) -> bool:
    del end
    spans = protected if protected is not None else _protected_query_spans(body)
    index = bisect_right(spans, (start, float("inf"))) - 1
    return index < 0 or start >= spans[index][1]


def _find_query(
    body: str,
    query: str,
    cursor: int,
    *,
    protected: tuple[tuple[int, int], ...] | None = None,
) -> int | None:
    if protected is None:
        protected = _protected_query_spans(body)
    variants = [query]
    if re.match(r"SELECT\b", query, flags=re.I):
        variants.append(re.sub(r"^SELECT\b", "PERFORM", query, count=1, flags=re.I))
    for candidate in variants:
        exact_pattern = re.compile(re.escape(candidate), flags=re.I | re.S)
        exact = exact_pattern.search(body, cursor)
        while exact is not None:
            start = exact.start()
            end = exact.end()
            if _query_span_is_source(body, start, end, protected=protected):
                return start
            exact = exact_pattern.search(body, start + 1)
        query_words = [part for part in _WHITESPACE.split(candidate.strip()) if part]
        if not query_words:
            continue
        pattern = r"\s+".join(re.escape(part) for part in query_words)
        whitespace_pattern = re.compile(pattern, flags=re.I | re.S)
        for match in whitespace_pattern.finditer(body, cursor):
            start = match.start()
            end = match.end()
            if _query_span_is_source(body, start, end, protected=protected):
                return start
    return None


def _function_body_offset(
    statement: pgast.CreateFunctionStmt, body: str, segment: _Segment, raw: bytes
) -> int:
    """Map a parsed function body to bytes within its actual string token."""

    as_location = next(
        (
            getattr(option, "location", None)
            for option in statement.options or ()
            if getattr(option, "defname", None) == "as"
        ),
        None,
    )
    if not isinstance(as_location, int):
        return -1
    source = raw[segment.start : segment.end].decode("utf-8", errors="replace")
    tokens = tuple(pg_scan(source))
    as_token = next(
        (token for token in tokens if token.start == as_location and token.name == "AS"),
        None,
    )
    if as_token is None:
        return -1
    literal = next(
        (token for token in tokens if token.start >= as_token.end and token.name == "SCONST"),
        None,
    )
    if literal is None:
        return -1
    literal_source = source[literal.start : literal.end]
    body_offset = literal_source.find(body)
    if body_offset < 0:
        return -1
    return _char_offset(source, literal.start + body_offset)
