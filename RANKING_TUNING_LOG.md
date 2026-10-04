# Ranking parameter change log

## 2026-10-03 — recover the unchanged baseline inventory

The unit suite references this log, but it was absent from the checkout and
its Git history. This entry records the current values from
`src/codekg/queries/code.py`, unchanged from baseline commit `394f0d1`.
The PostgreSQL corpus work does not modify ranking parameters. This is an
inventory recovery, **not a new tuning result or a claim of a historical
evaluation replay**.

### Generic code terms

Sorted terms: `['class', 'data', 'function', 'method', 'object', 'value']`.

SHA-256 of `repr(sorted(_GENERIC_CODE_TERMS)).encode()`:

`06fb2ff845d7c9e0c6e5779d03e7d6d80dbd8936af1e54f313c03e199638c4aa`

### Qualified-name role weights

Tuple order: owner specific/generic, module specific/generic, package
specific/generic, sorted package roots.

Current tuple: `(350, 115, 300, 100, 60, 20, ('lib', 'src'))`.

SHA-256 of the tuple's encoded `repr`:

`4b8312c2c4643e0c589b94e858f5bc9556f5228bcb6fe56c1c5abd4e5f464dd7`

## Future changes

For a parameter change, append a dated entry with old/new values and hashes,
the reason, evaluation command and results, and any known regressions. Keep
inventory recovery separate from evidence of improved ranking. Do not update
hashes merely to hide an unintended parameter change.
