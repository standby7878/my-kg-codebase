# Offline Code Knowledge Graph + MCP — Phased Implementation Plan

**Scope (v1):** A self-hosted, network-isolated system that parses one or more proprietary Python code repositories into a Neo4j knowledge graph and exposes read-only graph queries to an AI agent over MCP. Language: **Python**. No docs ingestion in v1 (schema kept forward-compatible for it). Transports: **stdio subprocess** + **Streamable HTTP**.

> Implementation note: the current parser is Python-only and uses the standard
> library `ast` module. Earlier multi-language/tree-sitter phases below are
> retained as historical design notes and deferred expansion work, not as the
> current implementation contract.

**Hard constraints (from requirements):**

| # | Constraint | How it lands in this plan |
|---|---|---|
| 0 | Neo4j CE is the only graph engine | `neo4j:5.26-community` (LTS, GPLv3, single instance). No Enterprise features assumed. |
| 1 | KG-organization & parsing principles from CGC / similar | Tree-sitter per-language extraction → normalized IR → graph; lineage documented in §Design Lineage. |
| 2 | MCP tools/prompts from similar projects, **≤ 10 tools** | 10 read-only query tools, curated from the CGC tool surface (§Phase 7). |
| 3 | Neo4j + ingestion + MCP each in dedicated containers, Compose-managed, **isolated from the web** | Single `internal: true` Docker network, no egress, read-only repo mounts (§Phase 2, §Phase 8). |
| 4 | MCP runnable as stdio subprocess **or** HTTP server | One FastMCP app, transport selected by env (§Phase 7). |
| 5 | FastMCP, Python 3, ruff + pytest | `fastmcp` (v2.x), `src/` layout, ruff + pytest gates per phase (§Phase 0). |

---

## Design Lineage (the "study" — requirement 1 & 2)

**Parsing & graph organization — borrowed from CodeGraphContext (CGC) and SCIP:**

- CGC's model is *code-symbol-centric*: each language parser extracts functions, classes, methods, parameters, inheritance, calls, and imports via Tree-sitter, then writes a graph keyed on those symbols. We reuse this ontology shape and the Tree-sitter-first strategy, but target Neo4j exclusively (CGC abstracts over Kuzu/FalkorDB/Neo4j; we don't need that abstraction).
- **Known limitation we inherit and must design around:** Tree-sitter call/inheritance resolution is *heuristic* (name- and import-based). CGC's own repo carries `CGC_CALL_GRAPH_AUDIT_REPORT.md` / `CGC_GRAPH_INCONSISTENCIES.md` documenting false edges. CGC reaches precision only for C/C++ (scip-clang) and C# (scip-dotnet) via optional SCIP. We therefore (a) treat v1 call edges as *best-effort*, marking resolution confidence on the edge, and (b) keep SCIP as a deferred precision upgrade (§Phase 9), for which scip-python, scip-go, scip-java, scip-clang all exist.
- **Idempotent identity:** unlike a naive "create on parse" loader, every node carries a deterministic key so re-ingestion is a `MERGE`, not a duplicate. This is the main correctness discipline that makes the pipeline re-runnable and testable.

**MCP tool prompts — adapted from CGC's natural-language tool surface:**
CGC exposes ~12–15 tools (index, watch, find-definition, callers, callees, call-chain, importers, complexity, dead-code, list, delete, visualize). We curate to 10 **read-only** tools and rewrite their descriptions for precision (§Phase 7). Indexing/watch are deliberately **not** agent-exposed in v1 — ingestion is an operator action, so the agent cannot be steered into indexing arbitrary paths.

---

## Target Architecture

```
                 ┌─────────────────── docker compose ───────────────────┐
                 │  network: backend  (internal: true → NO egress)        │
   AI agent      │                                                        │
  (stdio spawn   │   ┌──────────┐   Bolt 7687   ┌──────────────────┐     │
   OR HTTP) ─────┼──▶│   mcp    │──────────────▶│      neo4j        │     │
 127.0.0.1:8765  │   │ FastMCP  │   (read-only) │ 5.26-community    │     │
                 │   └──────────┘                │ vol: neo4j_data  │     │
                 │                                └──────────────────┘     │
                 │   ┌────────────┐  Bolt 7687 (write)        ▲            │
                 │   │ ingestion  │───────────────────────────┘            │
                 │   │ (one-shot) │   repos mounted :ro                    │
                 │   └────────────┘                                        │
                 └────────────────────────────────────────────────────────┘
```

- **No container has internet egress** (`internal: true` removes the masquerade route). Verified empirically as an acceptance test, not assumed.
- **Inbound** only to `mcp`, bound to `127.0.0.1` (HTTP mode). The strongest-isolation default is **stdio mode**, which publishes *no* port at all: the agent spawns `docker compose run --rm mcp` and speaks over stdin/stdout.
- Repos are bind-mounted **read-only** into `ingestion`. Ingestion is a one-shot container (`docker compose run --rm ingestion index /repos/<name>`), not a long-running service.

---

## Repository Layout

```
code-kg-mcp/
├── pyproject.toml                # deps, ruff, pytest, build config
├── docker-compose.yml
├── .env.example                  # NEO4J_AUTH, MCP_TRANSPORT, MCP_PORT, ...
├── docker/
│   ├── ingestion.Dockerfile
│   └── mcp.Dockerfile
├── src/codekg/
│   ├── schema/                   # Phase 1: constraints, bootstrap cypher
│   ├── parsers/                  # Phase 3: per-language tree-sitter extractors
│   │   ├── base.py               #   IR dataclasses + extractor protocol
│   │   ├── python.py go.py java.py scala.py c.py cpp.py
│   ├── ir.py                     # normalized intermediate representation
│   ├── loader/                   # Phase 4: IR → Neo4j batched writes
│   ├── queries/                  # Phase 6: read-only parameterized Cypher
│   ├── cli.py                    # Phase 5: index / list / delete / reindex
│   └── mcp/server.py             # Phase 7: FastMCP app + 10 tools
└── tests/
    ├── fixtures/<lang>/...       # tiny golden source files per language
    ├── unit/                     # parser + query unit tests (no Neo4j)
    └── integration/              # loader + MCP tests (Neo4j via testcontainers)
```

---

## Pinned Dependencies (build-time pull only; runtime is air-gapped)

| Component | Pin | Notes |
|---|---|---|
| Neo4j CE | `neo4j:5.26-community` | LTS, GPLv3, single instance. APOC **core only** if used; disable file/URL procedures. |
| Python | `3.12` | matches FalkorDB/tree-sitter support floor in the ecosystem; 3.10–3.13 acceptable |
| `fastmcp` | `>=2,<3` | standalone (gofastmcp.com), not the SDK-bundled `mcp.server.fastmcp` |
| `neo4j` (driver) | `>=5.26,<6` | Bolt driver, sync API is fine |
| `tree-sitter` | `>=0.23` | core runtime |
| `tree-sitter-language-pack` | latest | provides Python/Go/Java/Scala/C/C++ grammars in one wheel |
| `typer` + `rich` | latest | CLI |
| `pydantic` | `>=2` | IR validation / tool I/O schemas |
| `ruff`, `pytest`, `pytest-asyncio`, `testcontainers[neo4j]` | latest | quality + test gates |

> **Air-gap discipline:** all wheels and Docker base images are pulled at *build* time (egress allowed during build), then images run with no egress. For a true air-gapped target, build on a connected host and `docker save`/`load` onto the isolated host, or run a local wheelhouse + registry mirror.

---

# Phases

Each phase is independently buildable and ends at a **green gate**: `ruff check . && ruff format --check . && pytest <phase-tests>` plus the phase's explicit acceptance checks. Later phases depend only on earlier ones.

## Phase 0 — Scaffold & quality gates
**Goal:** A buildable Python package with linting/testing wired up; nothing domain-specific yet.

**Implement:**
- `src/` layout, `pyproject.toml` with project metadata + the dep pins above.
- ruff config (lint + format), pytest config (`testpaths`, markers `unit`/`integration`), `.env.example`.
- A trivial `codekg.__version__` and one smoke test.

**Acceptance (atomic, testable):**
- `ruff check .` and `ruff format --check .` exit 0.
- `pytest -m unit` collects and passes the smoke test.
- `python -c "import codekg"` works.

---

## Phase 1 — Graph schema & data model
**Goal:** Fix the ontology and the idempotency contract before any parsing.

**Node labels & key properties:**

| Label | Identity key (`key`) | Other props |
|---|---|---|
| `Repository` | `repo_name` | `commit`, `root_path`, `indexed_at` |
| `File` | `repo@commit:path` | `path`, `language`, `loc` |
| `Module` | `repo@commit:module_qname` | `name`, `language` |
| `Function` | `repo@commit:path:qname:start_line` | `name`, `qname`, `signature`, `start_line`, `end_line`, `cyclomatic` |
| `Method` | same scheme, parented to a type | `name`, `qname`, `signature`, `cyclomatic` |
| `Type` (class/struct/interface) | `repo@commit:path:qname:start_line` | `name`, `qname`, `kind` (`class`/`struct`/`interface`/`enum`) |

**Relationships:** `CONTAINS` (Repo→File, File→Module/Type/Function), `DEFINES`, `HAS_METHOD` (Type→Method), `CALLS` (callable→callable, prop `resolution` ∈ {`exact`,`heuristic`,`unresolved`}), `IMPORTS` (File→Module), `INHERITS`/`IMPLEMENTS` (Type→Type), `REFERENCES` (fallback for unresolved targets).

**Constraints / indexes (bootstrap Cypher):**
```cypher
CREATE CONSTRAINT func_key IF NOT EXISTS FOR (n:Function) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT type_key IF NOT EXISTS FOR (n:Type)     REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT file_key IF NOT EXISTS FOR (n:File)     REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT repo_key IF NOT EXISTS FOR (n:Repository) REQUIRE n.repo_name IS UNIQUE;
CREATE INDEX func_name IF NOT EXISTS FOR (n:Function) ON (n.name);
CREATE INDEX type_name IF NOT EXISTS FOR (n:Type)     ON (n.name);
```
`commit` is part of every key so two snapshots of the same repo coexist; "current" is selected by the latest `Repository.commit`. Forward-compat for docs: reserve labels `Document`/`Section` and rel `DESCRIBES` (unused in v1).

**Acceptance:**
- `pytest -m integration` (against a throwaway Neo4j via testcontainers) applies bootstrap, asserts all constraints/indexes exist via `SHOW CONSTRAINTS`.
- Idempotency: applying bootstrap twice is a no-op (no errors, no dupes).

---

## Phase 2 — Neo4j container + schema bootstrap
**Goal:** A reproducible, isolated Neo4j service with schema applied.

**Implement:**
- Compose service `neo4j`: image pin, `NEO4J_AUTH` from a Docker secret/`.env`, heap/pagecache env, `/data` volume, healthcheck (`cypher-shell "RETURN 1"`).
- Attached **only** to `backend` (`internal: true`). No published ports except optional `127.0.0.1:7474`/`7687` for local debugging.
- A bootstrap entry (`codekg.schema.bootstrap`) run once after healthy.

**Acceptance:**
- `docker compose up neo4j` reaches healthy.
- Bootstrap applies and is idempotent.
- **Egress test:** `docker compose exec neo4j sh -c 'curl -m3 https://example.com || echo BLOCKED'` prints `BLOCKED` (or the busybox equivalent / `wget` timeout).

---

## Phase 3 — Tree-sitter parser layer (→ normalized IR)
**Goal:** Pure, in-memory extraction. No Neo4j. This is where the bulk of unit tests live.

**Implement:**
- `ir.py`: pydantic/dataclass IR — `FileIR{path, language, loc}`, `SymbolIR{kind, name, qname, signature, span, cyclomatic}`, `CallIR{caller_qname, callee_name, callee_qname?, resolution}`, `ImportIR`, `InheritIR`.
- `parsers/base.py`: `Extractor` protocol + shared Tree-sitter query runner.
- One extractor per language (Python, Go, Java, Scala, C, C++) using `tree-sitter-language-pack`. Each maps grammar nodes → IR: definitions, params, inheritance, imports, and **intra-file** call sites (cross-file resolution happens in the loader).
- Cyclomatic complexity computed here from the AST (count decision nodes).
- Mark every call `resolution="heuristic"` in v1.

**Acceptance:**
- Golden-file unit tests per language: a tiny fixture source file → assert exact IR (set of functions/types/imports/calls). Deterministic, no DB.
- Edge cases per language: nested funcs (Py), methods on structs/receivers (Go), interfaces & generics (Java/Scala), header decls vs defs (C/C++).
- Coverage gate on `parsers/` (e.g. ≥85%).

---

## Phase 4 — Graph builder / loader (IR → Neo4j)
**Goal:** Idempotent, batched write of IR into the graph, with intra-repo call resolution.

**Implement:**
- Two-pass load over a repo's IR:
  1. **Definitions pass** — `UNWIND $rows AS r MERGE (n:Function {key:r.key}) SET n += r.props` (and types/files/modules), batched (e.g. 5k rows/tx).
  2. **Edges pass** — resolve `CallIR.callee_name` to a def `key` within the repo (by qname, then by name within imported modules). Resolved → `CALLS {resolution:'heuristic'}`; unresolved → `REFERENCES` to a lightweight placeholder. Then `IMPORTS`/`INHERITS`/`IMPLEMENTS`.
- Re-index of a repo: delete the prior `commit` subgraph (`MATCH (r:Repository{repo_name})... DETACH DELETE`, or `apoc.periodic.iterate` if APOC-core is enabled) before loading the new one.
- All writes via the Bolt driver, parameterized, with per-tx size caps.

**Acceptance:**
- Integration test: load a fixture repo → assert exact node/rel counts and targeted queries (callers of `X`, importers of `Y`, hierarchy of `Z`).
- **Idempotency:** load same IR twice → identical counts.
- **Re-index:** load commit A, then commit B → only B present; counts match B's fixture.

---

## Phase 5 — Ingestion container + CLI
**Goal:** Package parser+loader as an operator CLI inside an isolated one-shot container.

**Implement:**
- `cli.py` (typer): `index <path>`, `reindex <path>`, `list`, `delete <repo>`. Reads repo at a mounted path, derives `repo_name`/`commit` (git rev if present, else content hash), runs Phase 3 → Phase 4.
- `docker/ingestion.Dockerfile`; compose service `ingestion` on `backend`, repos mounted `:ro`, Neo4j creds via env/secret. Non-root user, read-only rootfs.

**Acceptance:**
- `docker compose run --rm ingestion index /repos/fixture` populates Neo4j; verify counts over Bolt.
- `list` / `delete` behave; `delete` removes exactly one repo's subgraph.
- Egress test from `ingestion` prints `BLOCKED`.

---

## Phase 6 — Read-only query layer
**Goal:** All graph reads as small, tested, parameterized Cypher functions — the substrate for the MCP tools.

**Implement (`queries/`):** one function each, returning typed results, with **bounded depth** and **row caps** and read-only tx + server-side timeout:
- `search_symbols(q, kind?, repo?, limit)` — name/qname substring search.
- `get_definition(qname|key)` — node + file + span + signature.
- `find_callers(qname, depth=1, max=...)`.
- `find_callees(qname, depth=1, max=...)`.
- `trace_call_path(from_qname, to_qname, max_depth=8)` — bounded variable-length path.
- `find_importers(module_qname)`.
- `get_class_hierarchy(type_qname, direction)` — ancestors/descendants via `INHERITS|IMPLEMENTS`.
- `find_dead_code(repo)` — callables with no inbound `CALLS` and not flagged entrypoints.
- `get_complexity(qname)` / `most_complex(repo, n)`.
- `list_repositories()`.

Example (bounded callers):
```cypher
MATCH (callee:Function {qname:$qname})
MATCH (caller)-[:CALLS*1..$depth]->(callee)
RETURN DISTINCT caller.qname AS qname, caller.signature AS sig LIMIT $max
```

**Acceptance:**
- Unit/integration tests per query on the fixture graph asserting exact results.
- Bound enforcement: depth and `LIMIT` respected; a deliberately huge query returns capped rows and does not hang (timeout fires).

---

## Phase 7 — MCP server (FastMCP, ≤10 tools, dual transport)
**Goal:** Expose Phase 6 as exactly 10 read-only MCP tools, runnable as stdio **or** Streamable HTTP.

**The 10 tools** (each wraps one query; descriptions rewritten for precision, adapted from CGC's tool surface):

| # | Tool | Purpose |
|---|---|---|
| 1 | `search_symbols` | locate functions/types by name/substring |
| 2 | `get_definition` | full definition + file/span/signature |
| 3 | `find_callers` | who calls X (bounded depth) |
| 4 | `find_callees` | what X calls (bounded depth) |
| 5 | `trace_call_path` | path from A to B across files |
| 6 | `find_importers` | which files import a module |
| 7 | `get_class_hierarchy` | ancestors/descendants of a type |
| 8 | `find_dead_code` | unreferenced callables in a repo |
| 9 | `get_complexity` | cyclomatic complexity / top-N complex |
| 10 | `list_repositories` | indexed repos + commits |

> **Deliberate exclusions** (keeps ≤10 and reduces attack surface): `index`/`watch`/`delete` are *not* MCP tools — ingestion is an operator/CLI action, so an agent can't trigger indexing of arbitrary paths. Swap a slot if you later want agent-driven indexing.

**Transport (requirement 4):** one app, env-selected.
```python
# src/codekg/mcp/server.py
from fastmcp import FastMCP
mcp = FastMCP("codekg")
# @mcp.tool() definitions 1..10, each delegating to queries/ with read-only tx

if __name__ == "__main__":
    t = os.getenv("MCP_TRANSPORT", "stdio")
    if t == "http":
        mcp.run(transport="http", host="127.0.0.1",
                port=int(os.getenv("MCP_PORT", "8765")))   # Streamable HTTP, endpoint /mcp
    else:
        mcp.run(transport="stdio")                          # subprocess mode
```
SSE is intentionally omitted (deprecated in the MCP spec since protocol 2026-03-26; FastMCP recommends HTTP). Tool I/O validated with pydantic models; results size-capped; all DB access read-only.

**Acceptance:**
- Tool-count test asserts exactly 10 registered tools.
- FastMCP in-memory client test calls each tool and validates output schema + correctness on the fixture graph.
- Transport smoke tests: stdio (spawn + `tools/list`) and HTTP (`POST /mcp`, assert `Mcp-Session-Id` handling, 202 on notifications).

---

## Phase 8 — Compose integration & isolation hardening (E2E)
**Goal:** The whole stack stands up isolated, ingests, and serves queries end-to-end.

**Implement:**
- Final `docker-compose.yml`: `neo4j` + `ingestion` (one-shot) + `mcp`; single `backend` network `internal: true`; Neo4j auth via Docker secret; repo mounts `:ro`; `mcp` HTTP published only to `127.0.0.1:8765`.
- Hardening: non-root users, `read_only: true` rootfs + tmpfs where needed, `cap_drop: [ALL]`, memory/cpu limits, healthchecks, `depends_on` with conditions.
- `mcp` runs read-only DB credentials (a Neo4j user limited to reads — note: CE has no RBAC, so enforce read-only at the driver/tx layer and document the limitation).

**Acceptance (the isolation contract):**
- E2E: `up` → `run ingestion index /repos/fixture` → query all 10 tools over **both** transports → expected results → `down`.
- **Egress denied from every container** (curl/wget to a public host times out) — scripted assertion.
- `mcp` reachable on `127.0.0.1:8765` only; not on `0.0.0.0`; Neo4j not reachable from host except optional loopback debug.
- Restart persistence: data survives `neo4j` restart (volume).

---

## Phase 9 — Deferred precision & freshness (out of v1 scope; design hooks in place)
- **SCIP precision:** swap heuristic `CALLS` for exact edges using scip-clang (C/C++), scip-java (JVM), scip-python, scip-go; set `resolution:'exact'`. Requires build inputs (`compile_commands.json` for C/C++, restored project for JVM). Loader already records `resolution`, so this is additive.
- **Incremental freshness:** file-watch reindex (Tree-sitter re-parse of changed files; delete+reload affected subgraph).
- **Docs repo:** activate the reserved `Document`/`Section`/`DESCRIBES` schema; link docs↔code by symbol reference. No engine change needed.

---

## Risks & honest caveats
- **Call-graph precision:** v1 edges are heuristic; expect false positives/negatives, especially for dynamic dispatch, interfaces, and reflection. `resolution` on each edge lets consumers filter; Phase 9 (SCIP) is the real fix.
- **Neo4j CE single-instance:** no clustering, no native RBAC, no hot backup. Read-only enforcement is at the application layer; back up via offline dump on a stopped/quiesced instance.
- **Air-gap verification is a test, not an assumption:** egress-denied checks are first-class acceptance criteria in Phases 2/5/8 because Docker network/publish semantics can vary by host. If your platform grants egress via the publish path, add an explicit OUTPUT-drop or firewall sidecar.
- **Scala via Tree-sitter** is the weakest grammar of the five; budget extra fixture coverage and accept lower recall there until scip-java/SemanticDB (Phase 9).
```
