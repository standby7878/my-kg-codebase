# SQL ingestion semantic foundation

The SQL foundation parses project `.sql` files with the pinned `pglast` 7.x
family, which follows PostgreSQL 15+ grammar.  This is a PostgreSQL baseline,
not a promise that every future PostgreSQL grammar extension is supported.

Enable selection in `codekg.toml`:

```toml
[sql]
enabled = true
dialect = "postgres"
database = "management"
include = ["database/**/*.sql"]
exclude = ["database/generated/**"]
default_schema = "public"
search_path = ["public"]
```

`SqlConfig` is frozen and defaults to disabled, `database = "management"`, and
`include = ["**/*.sql"]`.  Glob matching is path-segment aware: `**` consumes
zero or more directories, while `*` never crosses a directory separator.

`parse_sql(source, context=None, config=None)` returns the existing immutable
`FileIR`.  Its SQL fields are tuples of parser-independent IR:

- `SqlArtifactIR` retains the complete source and SHA-256 UTF-8 hash.
- `SqlStatementIR` records source-ordered statements, including nested static
  SQL statements found in SQL and PL/pgSQL routine bodies.
- `SqlObjectRefIR` records each definition occurrence separately from identity,
  with define/read/write/call/alter/drop roles and canonical PostgreSQL names.

Line and column positions are one-based and columns are UTF-8 byte columns.
The pglast wrapper's character indices are converted to byte offsets.
Dynamic `EXECUTE` names are never guessed;
they receive a dynamic marker and `sql_dynamic_reference` warning. Unsupported
or malformed statements preserve successfully parsed siblings and report
`sql_unsupported_construct` or `sql_parse_error` diagnostics with a `partial`
status. CTE names and aliases are local query scope and are not emitted as
global object references. Search-path changes are captured when their values
are statically identifiable; otherwise unqualified references abstain from an
exact schema.

Before any session setting, unqualified definitions use `default_schema`.
Thereafter, creation schemas come from the first known search-path component. `SET
search_path` is read from its parsed AST, including separate string-valued
components such as `'app', public`. Empty paths, resets/defaults without known
session state, `$user`, `pg_temp`, and compound comma-containing GUC strings
are conservatively represented by an empty path and no creation schema; the
parser does not substitute `default_schema` after such a setting. Routine
`SET search_path` options apply only within that routine's body, and the outer
path is restored afterward. Unqualified read/write/call references retain
`schema_name=None` plus their ordered path for downstream resolution.

Routine type signatures quote and escape identifier components containing
uppercase letters or punctuation, keeping `"x,y"` distinct from two arguments,
and `"x[]"` distinct from an array of `x`. PostgreSQL-parser builtin aliases
remain canonical. Data-modifying CTE targets retain write occurrences, and
UPDATE/DELETE/MERGE targets also retain read occurrences.

## Known unsupported boundaries

- Standard SQL routine bodies using `BEGIN ATOMIC ... END` are not supported
  in this delivery. Their internal semicolons are not grouped as a routine;
  diagnostics mark the result incomplete and the routine definition is not
  available. Do not interpret surviving fragments as complete routine coverage.
- PL/pgSQL extraction is partial static SQL extraction, not a control-flow
  graph. Control-flow diagnostics indicate incomplete coverage; conditions,
  procedural expressions, and dynamic SQL do not promise complete dependencies.
- Routine signature hints for calls with untyped expressions remain unknown;
  explicit casts and zero-argument calls are distinguishable.

This first delivery covers schema/data-definition statements, views, CTAS,
common DML reads and writes, routine signatures, and statically visible SQL in
routine bodies. It intentionally does not cover Python string SQL, runbooks,
live catalog inspection, column lineage, or migration replay.
