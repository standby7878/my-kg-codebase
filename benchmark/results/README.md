# Results and scoring

Store one immutable raw JSONL log per run under an operator-controlled log directory. Include the CLI's JSON output and the run metadata needed to identify the corpus, commit, prompt, model, condition, and status; do not edit raw logs.

For blinded scoring, generate a blind ID and scorer copy that omits condition, schedule position, and revealing filenames. Score each required point and forbidden claim against the matching reviewed truth record. Enter the locked score in `scoring.csv`, retain scorer notes, then restore the condition mapping only after scoring is complete. Record invalid, interrupted, and missing runs rather than dropping them. Report denominators, validity exclusions, paired deltas, and the predeclared break-even calculation with the results.
