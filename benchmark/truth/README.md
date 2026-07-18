# Truth records

Create one reviewed JSON record per prompt/corpus pair using `schema.json`. `repo` and immutable `commit` must match the trial checkout; `prompt_id` must exist in the frozen prompt manifest. Expected symbols and directed edges define evidence targets. Required points are scored individually. Forbidden claims are explicit false, unsupported, or out-of-scope statements and must be scored as violations.

Truth records are authoritative only after corpus and truth review sign-off. Keep reviewer, review date, and sign-off evidence in the external study record or an approved companion file; do not silently revise a record after runs begin.
