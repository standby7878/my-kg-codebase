# Requests benchmark review and CodeKG search improvement plan

## Evaluation corrections

The CodeKG arm found the gold symbol at rank 1, but it is not a valid formal
run because it issued only discovery calls. It did not call `get_definition`,
`find_callers`, or `find_callees`, so its behavioral explanation exceeded the
returned evidence.

The native arm did attempt two shell commands. Both failed before command
execution because Bubblewrap could not configure its loopback namespace:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

The absence of `command_execution` items in the JSONL does not prove that the
agent made no attempt; the final agent message records both attempted commands
and their common sandbox-setup failure. This is a harness failure, not a native
retrieval failure. The corrected runbook uses the same no-approval,
`danger-full-access` policy for both arms on the trusted benchmark checkout and
adds a native shell preflight.

## Reproduced search findings

The report's two main relevance findings reproduce against the live indexed
`requests` snapshot:

- The descriptive responsibility query ranks
  `src.requests.sessions.Session.prepare_request` first.
- The exact-looking query `Session.request` does not rank the exact method in
  the first five.
- Test functions occupy three of the first five results for the descriptive
  query.
- The stopword `with` is emitted as a matched term.

The graph already supports an evidence-complete expansion path:

```text
Session.request
  -> Session.prepare_request
     -> merge_setting
     -> merge_hooks
     -> merge_cookies
     -> PreparedRequest.__init__
```

Therefore the first improvement should guide the agent from discovery into
these existing expansion tools, not add more broad searches.

## Proposal assessment

### Accept for the next iteration

1. **Exact identifier fast path**
   - Normalize dotted and snake-case identifiers.
   - Query exact symbol name, exact qualified name, and qualified-name suffix
     before descriptive retrieval.
   - Place an unambiguous exact qualified-name match ahead of lexical results.
   - Add `Session.request` and `PreparedRequest.prepare` regression cases.
   - This can be implemented without re-indexing.

2. **Source-scope control**
   - Add `scope: source | tests | docs | examples | benchmarks | all`, defaulting
     to `source`.
   - Keep non-source categories searchable only when explicitly requested.
   - In this no-reindex iteration, classify and filter the fixed, bounded
     backend candidate pool before graph/lexical fusion, ranking, and
     pagination. This keeps default source discovery narrow for the returned
     candidates, but does not guarantee strict scope recall beyond that pool.
   - Strict backend pre-limit scope filtering (or an indexed scalar `scope`
     field when path filtering is insufficient) and re-ingestion remain deferred.

3. **Stopword and identifier-token normalization**
   - Remove English connector stopwords such as `with`, `the`, `and`, and
     `from`.
   - Preserve meaningful domain tokens, including `request`, but reduce the
     influence of repository-common terms.
   - Split camelCase, PascalCase, snake_case, and dotted qualified names with
     one shared analyzer used for indexing, querying, and `matched_terms`.

4. **Evidence-first agent workflow**
   - Describe `search_symbols` explicitly as discovery-only.
   - After a plausible top hit, direct the client to `get_definition`, then
     `find_callers` and `find_callees`.
   - Allow at most one reformulation when the initial result is not plausible.
   - Track searches before the first correct candidate and expansion-call
     compliance as benchmark metrics.

5. **Field-aware lexical retrieval**
   - Keep `name`, qualified name, signature, path, behavioral description, and
     docstring text as separate searchable fields.
   - Give exact/suffix identifier fields deterministic priority over prose.
   - Requires a Zvec query-capability spike and likely an index schema change
     followed by re-ingestion.

### Accept after the simpler ranking fixes

6. **Repository-local term weighting**
   - Measure document frequency within each repository and down-weight terms
     common in that repository.
   - Prefer standard BM25/IDF behavior over hand-maintained domain stopword
     lists.
   - Requires stored per-field statistics or backend support and should be
     calibrated on more than the Requests query.

7. **Structural reranking**
   - Batch-fetch immediate callers/callees for the bounded top candidate set.
   - Reward neighbors matching meaningful query concepts and keep the feature
     diagnostic.
   - Do not issue one graph query per candidate.
   - Introduce only after exact matching and source scope are correct, so graph
     signals solve residual ambiguity rather than mask lexical defects.

8. **Deterministic structured descriptions**
   - Extend the current deterministic description builder with separate
     `inputs`, `outputs`, `actions`, and related-symbol fields where static
     extraction can prove them.
   - Do not infer semantic facts that the parser or graph cannot support.
   - Re-index and compare against the existing flat-text baseline.

### Defer or modify

9. **Composite `inspect_symbol` tool**
   - Potentially useful, but it conflicts with the existing ten-tool budget and
     MCP currently stores definition metadata rather than source bodies.
   - First validate the atomic four-call workflow. If latency remains material,
     consider a bounded composite expansion response or extend
     `get_definition` deliberately.

10. **LLM-generated index descriptions**
    - Defer until the deterministic baseline is measured.
    - If evaluated, record the generating model and prompt and keep a separate
      corpus so retrieval quality is not conflated with hidden generation
      changes.

11. **Rank confidence labels**
    - Do not derive `high` or `low` confidence from arbitrary raw score gaps.
    - Add only after calibration on labeled queries, reporting the underlying
      exact-match, scope, term-coverage, and score-margin features.

## Delivery sequence

### Immediate delivery: no re-ingestion

1. Add exact name/qname/suffix fast-path ranking.
2. Add stopword-aware identifier normalization.
3. Add the six-value scope contract. Classify/filter the fixed bounded backend
   candidate pool before fusion, ranking, and pagination; expose scope and
   diagnostics without claiming strict backend-limit recall.
4. Update MCP instructions and benchmark validity checks for the expansion
   workflow.
5. Add deterministic unit and real-Requests tests.

### Deferred delivery: strict scoped index

1. Apply scope filtering inside the graph and lexical backends before their
   candidate limits.
2. Add an indexed scope field only if path filtering is insufficient.
3. Re-ingest the frozen corpora.
4. Measure strict-scope recall and production-vs-test result composition.

### Phase 3: relevance model

1. Separate index fields and add field weights.
2. Add repository-local term statistics.
3. Evaluate batched structural reranking.
4. Calibrate confidence diagnostics.

## Benchmark gates

A run is valid only if:

- both preflights pass;
- both arms use the same model, reasoning effort, approval policy, and host
  permissions;
- the CodeKG arm calls `list_repositories`, no more than two searches,
  `get_definition`, and relationship expansion;
- the native arm contains completed shell commands, source search, and
  numbered source reads;
- every reported symbol and line range is supported by tool or command
  evidence;
- token, elapsed-time, call-count, response-byte, and searches-before-correct
  metrics are recorded.

The immediate success criterion is not merely rank 1. It is:

```text
list repositories -> one search -> exact definition -> graph expansion -> grounded answer
```
