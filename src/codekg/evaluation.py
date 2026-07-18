"""Frozen-corpus evaluation for the CodeKG ingestion and search pipeline.

This module deliberately evaluates the public pipeline instead of reusing
private loader helpers: every selected corpus is scanned, loaded into Neo4j,
published to its own zvec directory, and queried through the public lexical
search function.  It is local-only; corpus acquisition is explicitly outside
of the evaluator's responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from codekg.ingest import index_repository, scan_repository
from codekg.ir import RepositoryIR
from codekg.neo4j_client import Neo4jClient, get_client
from codekg.queries.code import search_symbols
from codekg.resolver import EXACT_RESOLUTION_STATUSES

MANIFEST_VERSION = 1
_CORPUS_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_REQUIRED_CORPUS_KINDS = frozenset({"synthetic", "dogfood"})


class EvaluationError(ValueError):
    """The frozen evaluation contract cannot be executed safely."""


@dataclass(frozen=True)
class CorpusSpec:
    """One locally available, pinned corpus from the JSON manifest."""

    corpus_id: str
    kind: Literal["synthetic", "dogfood", "external"]
    path: str | None
    path_env: str | None
    required: bool
    pin: Mapping[str, str]
    ground_truth: str | None


@dataclass(frozen=True)
class EvaluationManifest:
    version: int
    corpora: tuple[CorpusSpec, ...]


@dataclass(frozen=True)
class PreparedCorpus:
    """A corpus proved locally valid before evaluation mutates an index."""

    spec: CorpusSpec
    path: Path
    repository: RepositoryIR
    identity: Mapping[str, Any]
    truth: Mapping[str, Any]


def load_manifest(path: Path) -> EvaluationManifest:
    """Load and validate the small, stdlib-only corpus manifest."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"evaluation manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid evaluation manifest JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise EvaluationError(f"evaluation manifest must have version {MANIFEST_VERSION}")
    raw_corpora = payload.get("corpora")
    if not isinstance(raw_corpora, list) or not raw_corpora:
        raise EvaluationError("evaluation manifest requires a non-empty corpora list")

    corpora = tuple(_parse_corpus(value, index) for index, value in enumerate(raw_corpora))
    ids = [corpus.corpus_id for corpus in corpora]
    if len(ids) != len(set(ids)):
        raise EvaluationError("evaluation corpus ids must be unique")
    kinds = {corpus.kind for corpus in corpora if corpus.required}
    missing = sorted(_REQUIRED_CORPUS_KINDS - kinds)
    if missing:
        raise EvaluationError(
            f"evaluation manifest lacks required corpus kind(s): {', '.join(missing)}"
        )
    return EvaluationManifest(version=MANIFEST_VERSION, corpora=corpora)


def run_evaluation(
    manifest_path: Path,
    *,
    project_root: Path,
    zvec_root: Path,
    client: Neo4jClient | None = None,
    require_pins: bool = False,
) -> dict[str, Any]:
    """Run the selected frozen corpora and return a JSON-safe report.

    ``project_root`` intentionally anchors corpus paths.  That makes the same
    manifest usable from CI, a checkout, and an air-gapped mounted workspace.
    Missing optional corpora are reported as skipped; missing required corpora
    and pin mismatches fail before Neo4j is modified.
    """

    manifest = load_manifest(manifest_path)
    selected = _select_corpora(manifest, project_root)
    prepared, skipped = _preflight_corpora(
        selected,
        project_root=project_root,
        require_pins=require_pins,
    )
    # Do not open Neo4j or create a zvec directory until every selected corpus
    # has resolved, pinned, scanned, and loaded its truth contract.  A later
    # mismatch must leave all index state untouched.
    db = (client or get_client()) if prepared else None
    if prepared:
        zvec_root.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    prepared_by_id = {corpus.spec.corpus_id: corpus for corpus in prepared}
    skipped_by_id = {str(row["id"]): row for row in skipped}
    for spec in manifest.corpora:
        if spec.corpus_id in skipped_by_id:
            reports.append(skipped_by_id[spec.corpus_id])
        else:
            assert db is not None
            corpus = prepared_by_id[spec.corpus_id]
            reports.append(_evaluate_corpus(corpus, db, zvec_root / spec.corpus_id))
    return {
        "format_version": 1,
        "manifest": str(manifest_path),
        "corpora": reports,
        "summary": _summary(reports),
    }


def write_report(report: Mapping[str, Any], output: Path) -> None:
    """Write stable, diff-friendly JSON without volatile wall-clock fields."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_corpus(value: object, index: int) -> CorpusSpec:
    if not isinstance(value, dict):
        raise EvaluationError(f"corpora[{index}] must be an object")
    corpus_id = value.get("id")
    kind = value.get("kind")
    path = value.get("path")
    path_env = value.get("path_env")
    required = value.get("required")
    pin = value.get("pin")
    ground_truth = value.get("ground_truth")
    if not isinstance(corpus_id, str) or not _CORPUS_ID_RE.fullmatch(corpus_id):
        raise EvaluationError(f"corpora[{index}].id must be a lowercase stable id")
    if kind not in {"synthetic", "dogfood", "external"}:
        raise EvaluationError(f"corpora[{index}].kind is invalid")
    if not isinstance(required, bool):
        raise EvaluationError(f"corpora[{index}].required must be boolean")
    if (path is None) == (path_env is None) or (path is not None and not isinstance(path, str)):
        raise EvaluationError(f"corpora[{index}] requires exactly one of path or path_env")
    if path_env is not None and not isinstance(path_env, str):
        raise EvaluationError(f"corpora[{index}].path_env must be a string")
    if ground_truth is not None and not isinstance(ground_truth, str):
        raise EvaluationError(f"corpora[{index}].ground_truth must be a string")
    if not isinstance(pin, dict) or not pin:
        raise EvaluationError(f"corpora[{index}].pin must be a non-empty object")
    if set(pin) - {"git_commit", "content_sha256", "git_commit_env", "working_tree"}:
        raise EvaluationError(f"corpora[{index}].pin has unsupported fields")
    if len(pin) != 1 or not all(isinstance(item, str) and item for item in pin.values()):
        raise EvaluationError(f"corpora[{index}].pin requires one non-empty string")
    pin_name, pin_value = next(iter(pin.items()))
    if pin_name in {"git_commit", "content_sha256"} and not re.fullmatch(
        r"[0-9a-fA-F]{40}" if pin_name == "git_commit" else r"[0-9a-fA-F]{64}", pin_value
    ):
        raise EvaluationError(f"corpora[{index}].pin.{pin_name} has an invalid digest")
    if pin_name == "git_commit_env" and not re.fullmatch(r"[A-Z][A-Z0-9_]*", pin_value):
        raise EvaluationError(
            f"corpora[{index}].pin.git_commit_env must name an environment variable"
        )
    if pin_name == "working_tree" and (kind != "dogfood" or pin_value != "report"):
        raise EvaluationError(
            f"corpora[{index}].pin.working_tree is only valid as {{'working_tree': 'report'}} "
            "for dogfood"
        )
    if kind == "synthetic" and pin_name not in {"git_commit", "content_sha256"}:
        raise EvaluationError(
            f"corpora[{index}] synthetic corpus requires a strict content or git pin"
        )
    return CorpusSpec(corpus_id, kind, path, path_env, required, pin, ground_truth)


def _select_corpora(
    manifest: EvaluationManifest,
    project_root: Path,
) -> list[tuple[CorpusSpec, Path | None, str | None]]:
    selected: list[tuple[CorpusSpec, Path | None, str | None]] = []
    for spec in manifest.corpora:
        raw_path = spec.path if spec.path is not None else os.getenv(spec.path_env or "")
        if not raw_path:
            if spec.required:
                raise EvaluationError(f"required corpus {spec.corpus_id!r} is not available")
            selected.append((spec, None, f"optional path env {spec.path_env} is unset"))
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        if not path.is_dir():
            if spec.required:
                raise EvaluationError(f"required corpus {spec.corpus_id!r} does not exist: {path}")
            selected.append((spec, None, f"optional corpus path does not exist: {path}"))
            continue
        selected.append((spec, path, None))
    return selected


def _preflight_corpora(
    selected: Iterable[tuple[CorpusSpec, Path | None, str | None]],
    *,
    project_root: Path,
    require_pins: bool,
) -> tuple[list[PreparedCorpus], list[dict[str, Any]]]:
    """Validate all input before index mutation, preserving manifest order later."""

    prepared: list[PreparedCorpus] = []
    skipped: list[dict[str, Any]] = []
    graph_repo_names: set[str] = set()
    for spec, path, skip_reason in selected:
        if skip_reason:
            skipped.append(
                {
                    "id": spec.corpus_id,
                    "kind": spec.kind,
                    "status": "skipped",
                    "reason": skip_reason,
                }
            )
            continue
        assert path is not None
        identity = _validate_pin(spec, path, require_pins=require_pins)
        repository = scan_repository(path)
        truth = _load_truth(spec, project_root)
        if repository.repo_name in graph_repo_names:
            raise EvaluationError(
                f"corpora resolve to duplicate repository name {repository.repo_name!r}; "
                "rename a local checkout before evaluating it"
            )
        graph_repo_names.add(repository.repo_name)
        prepared.append(PreparedCorpus(spec, path, repository, identity, truth))
    return prepared, skipped


def _validate_pin(
    spec: CorpusSpec,
    path: Path,
    *,
    require_pins: bool,
) -> dict[str, Any]:
    pin_name, expected = next(iter(spec.pin.items()))
    if pin_name == "working_tree":
        return {
            "policy": "working_tree_report",
            "git_commit": _git_commit(path),
            "dirty": _git_dirty(path),
            "content_sha256": _source_content_digest(path),
            "verified": False,
        }
    if pin_name == "content_sha256":
        actual = _source_content_digest(path)
    else:
        if pin_name == "git_commit_env":
            expected = os.getenv(expected, "")
            if not expected:
                if not require_pins:
                    return {
                        "policy": "optional_unpinned",
                        "git_commit": _git_commit(path),
                        "dirty": _git_dirty(path),
                        "content_sha256": _source_content_digest(path),
                        "verified": False,
                    }
                raise EvaluationError(
                    f"corpus {spec.corpus_id!r} requires pin environment variable "
                    f"{next(iter(spec.pin.values()))}"
                )
        actual = _git_commit(path)
        if actual is None:
            raise EvaluationError(
                f"corpus {spec.corpus_id!r} has no readable git commit for pinning"
            )
    if actual.lower() != expected.lower():
        raise EvaluationError(
            f"corpus {spec.corpus_id!r} pin mismatch: expected {expected}, got {actual}"
        )
    return {"policy": pin_name, "expected": expected, "actual": actual, "verified": True}


def _source_content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        file for file in path.rglob("*") if file.is_file() and file.suffix.lower() in {".py", ".md"}
    )
    for file in files:
        digest.update(file.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _git_dirty(path: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(result.stdout.strip())


def _evaluate_corpus(
    prepared: PreparedCorpus,
    client: Neo4jClient,
    zvec_path: Path,
) -> dict[str, Any]:
    spec = prepared.spec
    path = prepared.path
    repository = prepared.repository
    scan_started = perf_counter()
    # Scan once more inside the timed observable operation.  The initial scan
    # above is only identity validation and prevents colliding repository names.
    timed_repository = scan_repository(path)
    scan_ms = _milliseconds(scan_started)
    index_started = perf_counter()
    index_result = index_repository(path, replace=True, client=client, zvec_path=str(zvec_path))
    index_ms = _milliseconds(index_started)
    consistency = _consistency_counts(timed_repository, client)
    structural_metrics = _structural_metrics(timed_repository, client)
    truth_result = _evaluate_truth(prepared.truth, timed_repository, client, structural_metrics)
    lexical = _evaluate_lexical_queries(
        prepared.truth.get("lexical_queries", []),
        repo=repository.repo_name,
        zvec_path=str(zvec_path),
        client=client,
    )
    passed = consistency["ok"] and truth_result["ok"] and lexical["ok"]
    return {
        "id": spec.corpus_id,
        "kind": spec.kind,
        "status": "passed" if passed else "failed",
        "path": str(path),
        "repo": repository.repo_name,
        "commit": repository.commit,
        "identity": dict(prepared.identity),
        "timing_ms": {"scan": scan_ms, "index": index_ms, "lexical": lexical["timing_ms"]},
        "index": dict(index_result),
        "consistency": consistency,
        "structural_metrics": structural_metrics,
        "truth": truth_result,
        "lexical": lexical,
    }


def _consistency_counts(repository, client: Neo4jClient) -> dict[str, Any]:
    source = {
        "files": len(repository.files),
        "parse_error_files": sum(file.parse_status == "error" for file in repository.files),
        "parse_diagnostics": sum(len(file.diagnostics) for file in repository.files),
        "module_inits": sum(file.module_init is not None for file in repository.files),
        "types": sum(symbol.kind == "type" for file in repository.files for symbol in file.symbols),
        "callables": sum(
            symbol.kind in {"function", "method"}
            for file in repository.files
            for symbol in file.symbols
        ),
        "call_sites": sum(len(file.calls) for file in repository.files),
    }
    graph_rows = client.execute_read(
        """
        MATCH (r:Repository {repo_name: $repo})
        OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
        WITH r, collect(DISTINCT f) AS files
        CALL {
          WITH files
          UNWIND files AS file
          OPTIONAL MATCH (file)-[:CONTAINS]->(init:ModuleInit)
          OPTIONAL MATCH (file)-[:CONTAINS]->(type:Type)
          OPTIONAL MATCH (file)-[:CONTAINS]->(callable)
          WHERE callable:Function OR callable:Method
          OPTIONAL MATCH (file)-[:HAS_DIAGNOSTIC]->(diagnostic:ParseDiagnostic)
          RETURN count(DISTINCT init) AS module_inits,
                 count(DISTINCT type) AS types,
                 count(DISTINCT callable) AS callables,
                 count(DISTINCT diagnostic) AS parse_diagnostics
        }
        MATCH (site:CallSite)
        WHERE site.key STARTS WITH $prefix
        RETURN size(files) AS files,
               module_inits,
               types,
               callables,
               parse_diagnostics,
               size([file IN files WHERE file.parse_status = 'error']) AS parse_error_files,
               count(site) AS call_sites
        """,
        {"repo": repository.repo_name, "prefix": f"{repository.repo_name}@{repository.commit}:"},
    )
    graph = graph_rows[0] if graph_rows else {key: 0 for key in source}
    graph = {key: int(graph.get(key, 0)) for key in source}
    mismatches = {
        key: {"source": source[key], "graph": graph[key]}
        for key in source
        if source[key] != graph[key]
    }
    return {"ok": not mismatches, "source": source, "graph": graph, "mismatches": mismatches}


def _structural_metrics(repository: RepositoryIR, client: Neo4jClient) -> dict[str, Any]:
    """Report structural coverage and projection health for every corpus."""

    prefix = f"{repository.repo_name}@{repository.commit}:"
    type_qnames = {
        symbol.qname
        for file in repository.files
        for symbol in file.symbols
        if symbol.kind == "type"
    }
    source_has_method = sum(
        symbol.kind == "method" and symbol.parent_qname in type_qnames
        for file in repository.files
        for symbol in file.symbols
    )
    source_inheritance = sum(len(file.inheritance) for file in repository.files)
    graph_rows = client.execute_read(
        """
        CALL {
          MATCH (type:Type)-[has_method:HAS_METHOD]->(method:Method)
          WHERE type.key STARTS WITH $prefix AND method.key STARTS WITH $prefix
          RETURN count(has_method) AS has_method_edges
        }
        CALL {
          MATCH (child:Type)-[inherits:INHERITS]->(parent:Type)
          WHERE child.key STARTS WITH $prefix AND parent.key STARTS WITH $prefix
          RETURN count(inherits) AS internal_inherits
        }
        CALL {
          MATCH (site:CallSite)
          WHERE site.key STARTS WITH $prefix
          RETURN count(site) AS call_sites
        }
        CALL {
          MATCH ()-[resolution:RESOLVES_TO]->()
          WHERE resolution.key STARTS WITH $prefix
          RETURN count(resolution) AS resolves_to
        }
        CALL {
          MATCH ()-[calls:CALLS]->()
          WHERE calls.key STARTS WITH $prefix
          RETURN count(calls) AS calls
        }
        CALL {
          MATCH ()-[exact:EXACT_CALLS]->()
          WHERE exact.key STARTS WITH $prefix
          RETURN count(exact) AS exact_calls
        }
        RETURN has_method_edges,
               internal_inherits,
               call_sites,
               resolves_to,
               calls,
               exact_calls
        """,
        {"prefix": prefix},
    )
    graph = graph_rows[0] if graph_rows else {}
    status_rows = client.execute_read(
        """
        MATCH (site:CallSite)
        WHERE site.key STARTS WITH $prefix
        RETURN site.status AS status, count(site) AS count
        ORDER BY status
        """,
        {"prefix": prefix},
    )
    projection_rows = client.execute_read(
        """
        MATCH (site:CallSite)
        WHERE site.key STARTS WITH $prefix
        OPTIONAL MATCH (site)-[resolves_to:RESOLVES_TO]->()
        OPTIONAL MATCH (owner)-[:HAS_CALLSITE]->(site)
        OPTIONAL MATCH (owner)-[calls:CALLS {key: site.key}]->()
        OPTIONAL MATCH (owner)-[exact_calls:EXACT_CALLS {key: site.key}]->()
        WITH site,
             count(DISTINCT resolves_to) > 0 AS has_resolves_to,
             count(DISTINCT calls) > 0 AS has_calls,
             count(DISTINCT exact_calls) > 0 AS has_exact_calls
        WITH site,
             has_resolves_to AND has_calls AND has_exact_calls AS has_all_projections,
             has_resolves_to OR has_calls OR has_exact_calls AS has_any_projection
        RETURN count(site) AS total,
               sum(CASE
                   WHEN site.status IN $exact_statuses AND has_all_projections THEN 1
                   WHEN NOT (site.status IN $exact_statuses) AND NOT has_any_projection THEN 1
                   ELSE 0
               END) AS complete
        """,
        {"prefix": prefix, "exact_statuses": sorted(EXACT_RESOLUTION_STATUSES)},
    )
    projection = projection_rows[0] if projection_rows else {"total": 0, "complete": 0}
    statuses = {str(row["status"]): int(row["count"]) for row in status_rows}
    projection_total = int(projection["total"] or 0)
    projection_complete = int(projection["complete"] or 0)
    return {
        "has_method": {
            "source_declared": source_has_method,
            "graph_edges": int(graph.get("has_method_edges", 0)),
        },
        "inherits": {
            "source_clauses": source_inheritance,
            "graph_internal_edges": int(graph.get("internal_inherits", 0)),
        },
        "call_sites": {
            "source_total": sum(len(file.calls) for file in repository.files),
            "graph_total": int(graph.get("call_sites", 0)),
            "statuses": statuses,
            "resolves_to": int(graph.get("resolves_to", 0)),
            "calls": int(graph.get("calls", 0)),
            "exact_calls": int(graph.get("exact_calls", 0)),
            "projection_complete": projection_total == projection_complete,
            "projection_complete_count": projection_complete,
        },
    }


def _load_truth(spec: CorpusSpec, project_root: Path) -> dict[str, Any]:
    if spec.ground_truth is None:
        return {}
    path = (project_root / spec.ground_truth).resolve()
    try:
        truth = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot load truth for {spec.corpus_id!r}: {exc}") from exc
    if not isinstance(truth, dict) or truth.get("version") != 1:
        raise EvaluationError(f"truth for {spec.corpus_id!r} must have version 1")
    return truth


def _evaluate_truth(
    truth: Mapping[str, Any],
    repository: RepositoryIR,
    client: Neo4jClient,
    structural_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    call_sites = _evaluate_callsite_truth(truth, repository, client)
    structural = _evaluate_structural_truth(truth, repository, client, structural_metrics)
    return {
        "ok": call_sites["ok"] and structural["ok"],
        "call_sites": call_sites,
        "structural": structural,
    }


def _evaluate_callsite_truth(
    truth: Mapping[str, Any],
    repository: RepositoryIR,
    client: Neo4jClient,
) -> dict[str, Any]:
    """Compare every source-located CallSite exactly once against the graph."""

    if "call_sites" not in truth:
        return {"ok": True, "skipped": "no call-site truth declared"}
    expectations = truth["call_sites"]
    if not isinstance(expectations, list):
        raise EvaluationError("truth.call_sites must be a list")
    expected_by_identity: dict[tuple[str, int, int, int, str], dict[str, Any]] = {}
    for expectation in expectations:
        if not isinstance(expectation, dict):
            raise EvaluationError("each truth.call_sites item must be an object")
        identity = _callsite_identity(expectation, source="truth")
        status = expectation.get("status")
        projection = expectation.get("projection")
        target = expectation.get("target_qname")
        if not isinstance(status, str) or not isinstance(projection, str):
            raise EvaluationError("truth call-site expectation has missing status or projection")
        if target is not None and not isinstance(target, str):
            raise EvaluationError("truth call-site target_qname must be a string or null")
        if identity in expected_by_identity:
            raise EvaluationError(f"duplicate truth CallSite locator: {identity!r}")
        expected_by_identity[identity] = dict(expectation)

    rows = client.execute_read(
        """
        MATCH (site:CallSite)
        WHERE site.key STARTS WITH $prefix
        OPTIONAL MATCH (site)-[:RESOLVES_TO]->(target)
        OPTIONAL MATCH (owner)-[:HAS_CALLSITE]->(site)
        OPTIONAL MATCH (owner)-[calls:CALLS {key: site.key}]->()
        OPTIONAL MATCH (owner)-[exact:EXACT_CALLS {key: site.key}]->()
        RETURN site.owner_qname AS owner_qname,
               site.start_line AS start_line,
               site.start_column AS start_column,
               site.ordinal AS ordinal,
               site.raw_callee AS raw_callee,
               site.status AS status,
               target.qname AS target_qname,
               count(DISTINCT calls) > 0 AS calls,
               count(DISTINCT exact) > 0 AS exact_calls,
               count(DISTINCT target) > 0 AS resolves_to
        ORDER BY owner_qname, start_line, start_column, ordinal, raw_callee
        """,
        {"prefix": f"{repository.repo_name}@{repository.commit}:"},
    )
    actual_by_identity: dict[tuple[str, int, int, int, str], dict[str, Any]] = {}
    for row in rows:
        actual = dict(row)
        identity = _callsite_identity(actual, source="graph")
        if identity in actual_by_identity:
            raise EvaluationError(f"duplicate graph CallSite locator: {identity!r}")
        actual_by_identity[identity] = actual

    expected_ids = set(expected_by_identity)
    actual_ids = set(actual_by_identity)
    missing = [_identity_row(identity) for identity in sorted(expected_ids - actual_ids)]
    extras = [_identity_row(identity) for identity in sorted(actual_ids - expected_ids)]
    mismatches: list[dict[str, Any]] = []
    for identity in sorted(expected_ids & actual_ids):
        expected = expected_by_identity[identity]
        actual = actual_by_identity[identity]
        if (
            actual["status"] != expected["status"]
            or actual["target_qname"] != expected.get("target_qname")
            or not _projection_matches(str(expected["projection"]), actual)
        ):
            mismatches.append(
                {
                    "locator": _identity_row(identity),
                    "expected": {
                        "status": expected["status"],
                        "target_qname": expected.get("target_qname"),
                        "projection": expected["projection"],
                    },
                    "actual": {
                        "status": actual["status"],
                        "target_qname": actual["target_qname"],
                        "calls": actual["calls"],
                        "exact_calls": actual["exact_calls"],
                        "resolves_to": actual["resolves_to"],
                    },
                }
            )
    return {
        "ok": not (missing or extras or mismatches),
        "expected_count": len(expected_by_identity),
        "actual_count": len(actual_by_identity),
        "missing": missing,
        "extras": extras,
        "mismatches": mismatches,
    }


def _callsite_identity(
    value: Mapping[str, Any],
    *,
    source: str,
) -> tuple[str, int, int, int, str]:
    owner = value.get("owner_qname")
    raw_callee = value.get("raw_callee")
    positions = tuple(value.get(name) for name in ("start_line", "start_column", "ordinal"))
    if (
        not isinstance(owner, str)
        or not isinstance(raw_callee, str)
        or not all(isinstance(position, int) for position in positions)
    ):
        raise EvaluationError(f"{source} CallSite is missing an exact source locator")
    return owner, positions[0], positions[1], positions[2], raw_callee


def _identity_row(identity: tuple[str, int, int, int, str]) -> dict[str, Any]:
    owner, line, column, ordinal, raw_callee = identity
    return {
        "owner_qname": owner,
        "start_line": line,
        "start_column": column,
        "ordinal": ordinal,
        "raw_callee": raw_callee,
    }


def _evaluate_structural_truth(
    truth: Mapping[str, Any],
    repository: RepositoryIR,
    client: Neo4jClient,
    structural_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """Assert complete synthetic graph sets, not a hand-picked edge sample."""

    structural_keys = {"has_method_edges", "internal_inherits", "call_site_summary"}
    if not (structural_keys & truth.keys()):
        return {"ok": True, "skipped": "no structural truth declared"}
    if not structural_keys <= truth.keys():
        raise EvaluationError(
            "structural truth requires has_method_edges, internal_inherits, and call_site_summary"
        )
    expected_methods = _edge_set(truth.get("has_method_edges", []), "type_qname", "method_qname")
    expected_inherits = _edge_set(truth.get("internal_inherits", []), "child_qname", "parent_qname")
    prefix = f"{repository.repo_name}@{repository.commit}:"
    method_rows = client.execute_read(
        """
        MATCH (type:Type)-[:HAS_METHOD]->(method:Method)
        WHERE type.key STARTS WITH $prefix AND method.key STARTS WITH $prefix
        RETURN type.qname AS left, method.qname AS right
        ORDER BY left, right
        """,
        {"prefix": prefix},
    )
    inherit_rows = client.execute_read(
        """
        MATCH (child:Type)-[:INHERITS]->(parent:Type)
        WHERE child.key STARTS WITH $prefix AND parent.key STARTS WITH $prefix
        RETURN child.qname AS left, parent.qname AS right
        ORDER BY left, right
        """,
        {"prefix": prefix},
    )
    actual_methods = {(str(row["left"]), str(row["right"])) for row in method_rows}
    actual_inherits = {(str(row["left"]), str(row["right"])) for row in inherit_rows}
    expected_summary = truth.get("call_site_summary", {})
    if not isinstance(expected_summary, dict):
        raise EvaluationError("truth.call_site_summary must be an object")
    actual_summary = structural_metrics["call_sites"]
    summary_keys = {
        "total": "graph_total",
        "statuses": "statuses",
        "resolves_to": "resolves_to",
        "calls": "calls",
        "exact_calls": "exact_calls",
        "projection_complete": "projection_complete",
    }
    expected_summary_normalized = {
        key: expected_summary.get(key) for key in summary_keys if key in expected_summary
    }
    actual_summary_normalized = {
        expected_key: actual_summary[actual_key]
        for expected_key, actual_key in summary_keys.items()
        if expected_key in expected_summary_normalized
    }
    method_ok = actual_methods == expected_methods
    inherit_ok = actual_inherits == expected_inherits
    summary_ok = actual_summary_normalized == expected_summary_normalized
    return {
        "ok": method_ok and inherit_ok and summary_ok,
        "has_method_edges": {
            "ok": method_ok,
            "expected": _edge_rows(expected_methods, "type_qname", "method_qname"),
            "actual": _edge_rows(actual_methods, "type_qname", "method_qname"),
        },
        "internal_inherits": {
            "ok": inherit_ok,
            "expected": _edge_rows(expected_inherits, "child_qname", "parent_qname"),
            "actual": _edge_rows(actual_inherits, "child_qname", "parent_qname"),
        },
        "call_site_summary": {
            "ok": summary_ok,
            "expected": expected_summary_normalized,
            "actual": actual_summary_normalized,
        },
    }


def _edge_set(value: object, left_name: str, right_name: str) -> set[tuple[str, str]]:
    if not isinstance(value, list):
        raise EvaluationError(f"truth.{left_name} edges must be a list")
    edges: set[tuple[str, str]] = set()
    for edge in value:
        if not isinstance(edge, dict):
            raise EvaluationError("truth structural edge must be an object")
        left = edge.get(left_name)
        right = edge.get(right_name)
        if not isinstance(left, str) or not isinstance(right, str):
            raise EvaluationError("truth structural edge has missing qualified names")
        edges.add((left, right))
    if len(edges) != len(value):
        raise EvaluationError("truth structural edges must be unique")
    return edges


def _edge_rows(
    edges: Iterable[tuple[str, str]],
    left_name: str,
    right_name: str,
) -> list[dict[str, str]]:
    return [{left_name: left, right_name: right} for left, right in sorted(edges)]


def _projection_matches(expected: str, row: Mapping[str, Any]) -> bool:
    resolved = bool(row["resolves_to"])
    calls = bool(row["calls"])
    exact = bool(row["exact_calls"])
    if expected == "exact":
        return resolved and calls and exact
    if expected == "none":
        return not resolved and not calls and not exact
    raise EvaluationError("truth call-site projection must be 'exact' or 'none'")


def _evaluate_lexical_queries(
    expectations: object,
    *,
    repo: str,
    zvec_path: str,
    client: Neo4jClient,
) -> dict[str, Any]:
    if not isinstance(expectations, list):
        raise EvaluationError("truth.lexical_queries must be a list")
    rows: list[dict[str, Any]] = []
    durations: list[float] = []
    reciprocal_ranks: list[float] = []
    for expectation in expectations:
        if not isinstance(expectation, dict) or not isinstance(expectation.get("query"), str):
            raise EvaluationError("lexical query expectation must have query")
        query = expectation["query"]
        expected = expectation.get("target_qname")
        if not isinstance(expected, str):
            raise EvaluationError("lexical query expectation must have target_qname")
        samples: list[dict[str, Any]] = []
        for _ in range(5):
            started = perf_counter()
            hits = search_symbols(
                query,
                mode="lexical",
                repo=repo,
                zvec_path=zvec_path,
                client=client,
            )
            duration = _milliseconds(started)
            durations.append(duration)
            rank = next(
                (index for index, hit in enumerate(hits, start=1) if hit["qname"] == expected),
                None,
            )
            samples.append(
                {
                    "rank": rank,
                    "top_qnames": [str(hit["qname"]) for hit in hits[:10]],
                    "latency_ms": duration,
                }
            )
        ranks = [sample["rank"] for sample in samples]
        rank = ranks[0]
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        rows.append(
            {
                "query": query,
                "target_qname": expected,
                "rank": rank,
                "all_samples_matched": all(sample["rank"] is not None for sample in samples),
                "samples": samples,
            }
        )
    found = sum(row["all_samples_matched"] for row in rows)
    total = len(rows)
    return {
        "ok": found == total,
        "queries": rows,
        "metrics": {
            "top1_accuracy": _ratio(sum(row["rank"] == 1 for row in rows), total),
            "recall_at_5": _ratio(sum((row["rank"] or 6) <= 5 for row in rows), total),
            "mrr": round(sum(reciprocal_ranks) / total, 6) if total else 1.0,
            "zero_result_rate": _ratio(
                sum(not sample["top_qnames"] for row in rows for sample in row["samples"]),
                total * 5,
            ),
        },
        "timing_ms": _latency_summary(durations),
    }


def _summary(corpora: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    materialized = list(corpora)
    return {
        "passed": sum(corpus["status"] == "passed" for corpus in materialized),
        "failed": sum(corpus["status"] == "failed" for corpus in materialized),
        "skipped": sum(corpus["status"] == "skipped" for corpus in materialized),
    }


def _milliseconds(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 3)


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"sample_count": 0, "p50": 0.0, "p95": 0.0}
    ordered = sorted(values)
    return {
        "sample_count": len(ordered),
        "p50": ordered[(len(ordered) - 1) // 2],
        "p95": ordered[_nearest_rank_index(len(ordered), 0.95)],
    }


def _nearest_rank_index(size: int, percentile: float) -> int:
    return max(0, int((size * percentile) + 0.999999) - 1)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0
