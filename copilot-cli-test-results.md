# Copilot CLI smoke-test results

Run date: 2026-07-19 (local time)

This report covers a read-only Copilot CLI smoke comparison. It is not the
formal benchmark: the benchmark manifest and schedule are still marked for
review, and the prompt/truth files required by the runner are not present.

## Test setup

- Copilot CLI: `1.0.34`
- Requested model: `gpt-4.1`
- Target checkout: `/media/alex/MYSSD/BACKUP/workspace/codekg-corpus/click`
- Target commit: `333c28d79cd982990ee98eef61ec20ab1a4f38ba`
- Target checkout state: clean before and after the runs
- Prompt: read-only discovery of the `click` repository, `Group` type search,
  and definition lookup
- MCP endpoint: `http://127.0.0.1:8765/mcp`
- MCP configuration: [`benchmark/config/codekg-mcp.json`](benchmark/config/codekg-mcp.json)

The prompt prohibited file changes, shell commands, and filesystem inspection.
Each run exited with code `0` and reported zero file changes.

## Copilot CLI conditions

| Condition | MCP connection evidence | Copilot response | Assessment |
|---|---|---|---|
| B (baseline) | CodeKG was not configured; only the disabled built-in GitHub MCP was reported | Correctly reported that CodeKG tools were unavailable | Pass for the baseline control |
| M | JSONL session events reported CodeKG `connected` | Claimed to call `list_repositories`, `search_symbols`, and `get_definition` | Connectivity passed, but tool execution is unverified: the raw JSONL contains no tool-call/tool-result events, and the response used an illustrative key (`click.core.Group`) rather than a returned exact key |
| MF | JSONL session events reported CodeKG `connected` | Claimed the same three CodeKG calls | Same inconclusive tool-execution evidence as M |

The M and MF responses must not be treated as confirmed MCP query results. The
Copilot CLI connected to the server, but its JSONL output did not expose a
tool-request or tool-result event for the claimed calls. The exact key claimed
by MF is also not a valid CodeKG key format for this corpus.

## Direct MCP validation

To distinguish a Copilot reporting problem from a server problem, the same
server was exercised directly with the FastMCP protocol client. These calls
returned successfully:

- `list_repositories`
- graph-mode `search_symbols`
- `get_definition`
- `find_callers` at depths 1 and 3
- `find_callees` at depths 1 and 4
- `trace_call_path`
- `find_importers` for Requests and Click
- `get_class_hierarchy` for ancestors and descendants
- `find_dead_code` for Click and Requests
- `get_complexity` for top-N and exact-key requests

Before the development mount fix, the direct lexical-search check failed with:

```text
Can't open lock file: /data/zvec/codekg/LOCK
```

This was a zvec mount issue, independent of Neo4j graph queries: zvec `0.5.1`
opens its lock file even for a read-only collection. The MCP zvec mount was
changed to writable while retaining the read-only zvec API call; the follow-up
verification succeeded after the MCP service restart. The lexical query
`open a file stream` returned five Click functions, including
`src.click.utils.open_file`.

## Other verification

- All documented Neo4j Browser Cypher queries in [`TESTING.md`](TESTING.md)
  executed successfully against the live graph.
- Unit suite: `76 passed, 31 deselected`.
- Three dependency deprecation warnings were emitted by the unit suite.

## Raw artifacts

The raw smoke artifacts are outside the repository at:

- [`B.jsonl`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/B.jsonl)
- [`M.jsonl`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/M.jsonl)
- [`MF.jsonl`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/MF.jsonl)
- [`logs-B`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/logs-B)
- [`logs-M`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/logs-M)
- [`logs-MF`](/media/alex/MYSSD/BACKUP/workspace/copilot-smoke-20260719/logs-MF)

The formal benchmark should only be run after corpus and truth review is
complete, prompt files and truth records are populated, and the schedule is
approved.
