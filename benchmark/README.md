# CodeKG Copilot benchmark

This is a frozen, operator-run framework for the approved GPT-4.1 A/B protocol. It compares a baseline Copilot session (B) with CodeKG MCP conditions (M and MF) on the same repository, prompts, and truth records. The scaffold is intentionally inert: **do not run a trial until the corpus, typing-zone labels, and truth records have been reviewed and signed off.**

## Fairness rules

- Pin one target checkout to an immutable commit. Use the same checkout, prompt order policy, timeout, model entitlement, and Copilot CLI version for every condition.
- Randomize and balance the schedule before unblinding. Do not retry a failed trial without recording it and applying the same retry rule to all conditions.
- B has built-in MCPs disabled and no CodeKG config. M and MF use the identical HTTP MCP configuration; their distinction is defined by the approved schedule/protocol and must be recorded in the run metadata.
- Do not edit the target checkout, prompts, truth, or configuration during a run. The runner requires a clean checkout, captures and rechecks its exact HEAD commit, and rejects log directories inside that checkout.
- The runner creates a temporary local clone under the supplied log directory, detaches it at the captured commit, and runs Copilot only from that clone. The temporary clone is removed on exit after validating that cleanup is scoped to that clone.
- Keep raw JSONL logs immutable. Score against truth records without exposing the condition label to the scorer.

## Prerequisites and corpus pinning

The operator needs an installed Copilot CLI with GPT-4.1 entitlement, a reviewed corpus checkout, a signed-off truth set, and permission to reach the local CodeKG MCP endpoint when running M/MF. Confirm the CLI reports the intended version and that the account is entitled to `gpt-4.1`; record both in run metadata.

Each corpus item must record repository identity, immutable commit, checkout path, and a typing-zone label. Typing zones are reviewed labels for the code region under test (for example: `application`, `library`, `generated`, `vendor`, or `tests`); they are not a substitute for the commit pin. A trial is invalid if the checkout changes or its zone label is missing.

## Prompt and truth workflow

Freeze prompts from `prompts/` and map each prompt ID to one truth record in `truth/`. Truth records must name expected symbols/edges, required points, and forbidden claims. Reviewers sign off the corpus, labels, prompt manifest, and truth before schedule generation or execution. The prompt preamble prohibits modifications, but the operator should still use filesystem and account safeguards.

## Blinded scoring

Give each run an opaque run ID. Remove condition, order, and other revealing metadata from scorer copies. Score only the response and its matching truth record, then restore the condition mapping after all scores are locked. Use `results/scoring.csv` for one row per scored run and retain the original JSONL separately.

## Metrics and break-even

Report required-point precision, required-point recall, forbidden-claim violations, unsupported-claim count, latency, failures, and cost/token usage when available. Publish denominators and missing-data rules with the results. For paired trials, compare condition deltas within the same corpus/prompt block and report confidence intervals or the pre-approved paired test.

For an incremental CodeKG cost `C` per trial, baseline value `V_B`, and MCP value `V_M` measured in the approved utility units, the per-trial break-even success lift is:

`required lift = C / (V_M - V_B)`

Equivalently, if success is worth `V` and the condition changes success probability by `Δp`, CodeKG breaks even when `Δp × V ≥ C`. Declare the cost and utility definitions before unblinding; do not infer them from favorable results.

## Run gate

The operator must obtain explicit corpus/truth review sign-off before running `scripts/run-copilot.sh`. Supply the checkout root and a log directory outside that checkout; the runner validates a clean source checkout, pins an immutable HEAD, and uses a disposable detached clone for Copilot execution. Confirm model entitlement/version. This repository contains a runner but does not authorize execution.
