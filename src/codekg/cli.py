from __future__ import annotations

import logging
import os
import resource
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from codekg.logging_config import configure_logging, debug_event

app = typer.Typer(help="Operate the offline code knowledge graph.")
graph_app = typer.Typer(help="Prepare, validate, and activate isolated graph generations.")
app.add_typer(graph_app, name="graph")
console = Console()
logger = logging.getLogger(__name__)


@app.callback()
def main() -> None:
    """CodeKG command line interface."""
    configure_logging()
    debug_event(logger, "cli_started")


@app.command()
def bootstrap() -> None:
    """Apply the Neo4j schema."""

    from codekg.schema.bootstrap import bootstrap_schema

    debug_event(logger, "cli_command_started", command="bootstrap")
    bootstrap_schema()
    debug_event(logger, "cli_command_completed", command="bootstrap")
    console.print("[green]CodeKG schema bootstrap complete[/green]")


@app.command("index")
def index_repo(path: Path) -> None:
    """Replace the current snapshot for a mounted repository path."""

    from codekg.ingest import index_repository

    debug_event(logger, "cli_command_started", command="index")
    result = index_repository(path, replace=True)
    debug_event(logger, "cli_command_completed", command="index", files=result.get("files", 0))
    console.print(result)


@app.command("reindex")
def reindex_repo(path: Path) -> None:
    """Delete and index a mounted repository path."""

    from codekg.ingest import index_repository

    debug_event(logger, "cli_command_started", command="reindex")
    result = index_repository(path, replace=True)
    debug_event(logger, "cli_command_completed", command="reindex", files=result.get("files", 0))
    console.print(result)


@app.command("index-all")
def index_all(root: Annotated[Path, typer.Argument()] = Path("/repos")) -> None:
    """Index every immediate repository directory under a root path."""

    from codekg.ingest import index_repository

    if not root.is_dir():
        raise typer.BadParameter(
            f"index root must be an existing directory: {root}",
            param_hint="root",
        )

    children = sorted(
        (child for child in root.iterdir() if not child.name.startswith(".") and child.is_dir()),
        key=lambda child: child.name,
    )
    debug_event(logger, "cli_command_started", command="index-all", repositories=len(children))
    for child in children:
        console.print(index_repository(child, replace=True))
    debug_event(logger, "cli_command_completed", command="index-all", repositories=len(children))


@app.command("list")
def list_repositories() -> None:
    """List indexed repositories."""

    from codekg.queries.repositories import list_repositories as query_repositories

    debug_event(logger, "cli_command_started", command="list")
    rows = query_repositories()
    for row in rows:
        console.print(row)
    debug_event(logger, "cli_command_completed", command="list", repositories=len(rows))


@app.command("delete")
def delete_repository(repo_name: str) -> None:
    """Delete an indexed repository by name."""

    from codekg.loader import delete_repository_by_name
    from codekg.zvec_store import delete_repo_records

    debug_event(logger, "cli_command_started", command="delete")
    delete_repo_records(repo_name)
    deleted = delete_repository_by_name(repo_name)
    console.print({"repo_name": repo_name, "deleted": deleted})
    debug_event(logger, "cli_command_completed", command="delete", deleted=deleted)


@app.command("evaluate")
def evaluate(
    manifest: Path = Path("evaluation/corpora.json"),
    output: Path = Path("evaluation/report.json"),
    project_root: Path = Path("."),
    zvec_root: Path = Path(".codekg-evaluation-zvec"),
    require_pins: bool = typer.Option(
        False,
        "--require-pins",
        help="Require an external pin environment value when an optional corpus path is set.",
    ),
) -> None:
    """Run pinned local corpora through Neo4j and isolated zvec indexes."""

    from codekg.evaluation import run_evaluation, write_report

    debug_event(logger, "cli_command_started", command="evaluate")
    report = run_evaluation(
        manifest.resolve(),
        project_root=project_root.resolve(),
        zvec_root=zvec_root.resolve(),
        require_pins=require_pins,
    )
    write_report(report, output.resolve())
    console.print(report["summary"])
    debug_event(logger, "cli_command_completed", command="evaluate")


@app.command("bulk-export")
def bulk_export(
    output: Path,
    paths: Annotated[list[Path], typer.Argument(min=1)],
    workers: int = typer.Option(1, "--workers", min=1),
) -> None:
    """Export snapshots scanned from repository paths."""

    from codekg.bulk_export import export_repositories, export_repository_path
    from codekg.ingest import scan_repository

    debug_event(logger, "cli_command_started", command="bulk-export", repositories=len(paths))
    started = time.perf_counter()
    if len(paths) == 1:
        result = export_repository_path(paths[0], output, workers=workers)
        scanned_at = time.perf_counter()
    else:
        if workers != 1:
            raise typer.BadParameter("--workers is supported only for a single repository root")
        repositories = [scan_repository(path) for path in paths]
        scanned_at = time.perf_counter()
        result = export_repositories(repositories, output)
    finished = time.perf_counter()
    console.print(
        {
            "manifest": str(result.manifest_path),
            "counts": dict(result.counts),
            "metrics": {
                "scan_seconds": round(scanned_at - started, 3),
                "export_seconds": round(finished - scanned_at, 3),
                "elapsed_seconds": round(finished - started, 3),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    )
    debug_event(logger, "cli_command_completed", command="bulk-export", repositories=len(paths))


@app.command("bulk-import")
def bulk_import(
    manifest: Path,
    database: str = typer.Option("neo4j", "--database"),
    neo4j_admin: str = typer.Option("neo4j-admin", "--neo4j-admin"),
) -> None:
    """Import a bulk-export manifest into Neo4j."""

    from codekg.bulk_import import run_bulk_import

    debug_event(logger, "cli_command_started", command="bulk-import")
    result = run_bulk_import(manifest, database=database, neo4j_admin=neo4j_admin)
    console.print(result)
    debug_event(
        logger, "cli_command_completed", command="bulk-import", returncode=result.returncode
    )


@app.command("bulk-zvec")
def bulk_zvec(manifests: Annotated[list[Path], typer.Argument(min=1)]) -> None:
    """Build lexical descriptions from immutable bulk-export stages."""

    from codekg.bulk_search import iter_search_stage_docs, validate_search_manifests
    from codekg.zvec_store import open_write, optimize_and_flush, upsert_symbol_docs

    debug_event(logger, "cli_command_started", command="bulk-zvec", manifests=len(manifests))
    started = time.perf_counter()
    stages = validate_search_manifests(manifests)
    validated_at = time.perf_counter()
    collection = open_write()
    document_count = upsert_symbol_docs(
        collection,
        (doc for stage in stages for doc in iter_search_stage_docs(stage)),
    )
    optimize_and_flush(collection)
    finished = time.perf_counter()
    console.print(
        {
            "manifests": len(manifests),
            "documents": document_count,
            "metrics": {
                "stage_validation_seconds": round(validated_at - started, 3),
                "zvec_seconds": round(finished - validated_at, 3),
                "elapsed_seconds": round(finished - started, 3),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    )
    debug_event(logger, "cli_command_completed", command="bulk-zvec", documents=document_count)


@app.command("validate-bulk-index")
def validate_bulk_index(manifests: Annotated[list[Path], typer.Argument(min=1)]) -> None:
    """Validate staged lexical-description records against live graph callables."""

    from codekg.bulk_search import validate_search_manifests, validate_staged_search
    from codekg.neo4j_client import get_client
    from codekg.zvec_store import open_write

    debug_event(
        logger, "cli_command_started", command="validate-bulk-index", manifests=len(manifests)
    )
    stages = validate_search_manifests(manifests)
    result = validate_staged_search(
        stages,
        collection=open_write(),
        client=get_client(),
    )
    console.print(result)
    debug_event(
        logger,
        "cli_command_completed",
        command="validate-bulk-index",
        ok=bool(result["ok"]),
    )
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("bulk-export-corpus")
def bulk_export_corpus(
    config: Path,
    output: Path,
    workers: int = typer.Option(
        1,
        "--workers",
        min=1,
        help="Maximum projection workers; shared corpus extraction is file-at-a-time and serial.",
    ),
) -> None:
    """Build one immutable offline corpus generation from a TOML manifest."""
    from codekg.corpus_export import export_corpus

    result = export_corpus(config, output, workers=workers)
    console.print(
        {
            "manifest": str(output.resolve() / "manifest.json"),
            "snapshots": len(result["snapshots"]),
            "metrics": result["metrics"],
        }
    )


@app.command("corpus-snapshots")
def corpus_snapshots(
    manifest: Path,
    limit: int = typer.Option(100, min=1, max=100),
    offset: int = typer.Option(0, min=0, max=10_000),
) -> None:
    """List snapshots recorded in an offline corpus manifest."""
    from codekg.corpus_queries import snapshots

    for row in snapshots(manifest, limit=limit, offset=offset):
        console.print(row)


@app.command("corpus-search")
def corpus_search(
    manifest: Path,
    query: str,
    snapshot: str | None = typer.Option(None, "--snapshot"),
    kind: str = typer.Option("all", "--kind"),
    limit: int = typer.Option(20, min=1, max=100),
) -> None:
    """Search native symbols and routines in the offline SQLite corpus."""
    from codekg.corpus_queries import search

    for row in search(manifest, query, snapshot=snapshot, kind=kind, limit=limit):
        console.print(row)


@app.command("corpus-compare")
def corpus_compare(
    manifest: Path,
    left: str,
    right: str,
    limit: int = typer.Option(100, min=1, max=100),
    offset: int = typer.Option(0, min=0, max=10_000),
) -> None:
    """Compare matching logical-repository snapshots offline."""
    from codekg.corpus_queries import compare

    for row in compare(manifest, left, right, limit=limit, offset=offset):
        console.print(row)


@app.command("corpus-trace")
def corpus_trace(
    manifest: Path,
    fromkey: str,
    tokey: str,
    max_depth: int = typer.Option(6, "--max-depth", min=1, max=8),
    limit: int = typer.Option(5, min=1, max=10),
) -> None:
    """Return asserted offline dependency traces (if available)."""
    from codekg.corpus_queries import trace

    for row in trace(manifest, fromkey, tokey, max_depth=max_depth, limit=limit):
        console.print(row)


@app.command("corpus-evidence")
def corpus_evidence(
    manifest: Path,
    key: str,
    direction: str = typer.Option("outgoing", "--direction"),
    limit: int = typer.Option(50, min=1, max=100),
) -> None:
    """Return bounded offline evidence matching a fact key."""
    from codekg.corpus_queries import evidence

    for row in evidence(manifest, key, direction=direction, limit=limit):
        console.print(row)


@graph_app.command("freeze")
def graph_freeze(source_manifest: Path, generation_manifest: Path) -> None:
    """Freeze a bulk corpus manifest and rebase all generation artifact paths."""
    from codekg.graph_artifacts import freeze_generation_manifest

    try:
        path = freeze_generation_manifest(source_manifest, generation_manifest)
    except (OSError, ValueError, FileExistsError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print({"generation_manifest": str(path)})


@graph_app.command("check")
def graph_check(
    registry: Annotated[Path | None, typer.Option("--registry")] = None,
    backend: Annotated[
        bool, typer.Option("--backend", help="Verify each exact Neo4j marker.")
    ] = False,
) -> None:
    """Validate registry generations and optionally their live Neo4j backends."""
    from codekg.graph_lifecycle import check_graph_registry
    from codekg.graph_registry import GraphRegistry

    try:
        result = check_graph_registry(GraphRegistry.load(registry), include_backends=backend)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(result)


@graph_app.command("prepare")
def graph_prepare(
    graph_id: str,
    env_file: Annotated[
        Path, typer.Option("--env-file", help="External Neo4j env file; never printed.")
    ] = ...,
    registry: Annotated[Path | None, typer.Option("--registry")] = None,
    network: Annotated[str, typer.Option("--network")] = "codekg-graphs",
    import_memory: Annotated[str, typer.Option("--import-memory")] = "2G",
    heap_max: Annotated[str, typer.Option("--heap-max")] = "1G",
    pagecache: Annotated[str, typer.Option("--pagecache")] = "2G",
    http_port: Annotated[int | None, typer.Option("--http-port", min=1, max=65535)] = None,
    bolt_port: Annotated[int | None, typer.Option("--bolt-port", min=1, max=65535)] = None,
) -> None:
    """Start a new isolated Neo4j Community candidate without activating it."""
    from codekg.graph_lifecycle import GraphLifecycleError, prepare_graph_candidate
    from codekg.graph_registry import GraphRegistry

    try:
        candidate = prepare_graph_candidate(
            GraphRegistry.load(registry),
            graph_id,
            env_file=env_file,
            network=network,
            import_memory=import_memory,
            heap_max=heap_max,
            pagecache=pagecache,
            http_port=http_port,
            bolt_port=bolt_port,
        )
    except (GraphLifecycleError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(candidate)


@graph_app.command("bootstrap")
def graph_bootstrap(
    graph_id: str,
    registry: Annotated[Path | None, typer.Option("--registry")] = None,
) -> None:
    """Apply Neo4j schema and the exact generation marker to one graph."""
    from codekg.graph_lifecycle import GraphLifecycleError, bootstrap_graph
    from codekg.graph_registry import GraphRegistry, GraphRegistryError

    try:
        result = bootstrap_graph(GraphRegistry.load(registry), graph_id)
    except (GraphLifecycleError, GraphRegistryError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except Exception as exc:
        raise typer.BadParameter(f"graph bootstrap failed ({type(exc).__name__})") from None
    console.print(result)


@graph_app.command("activate")
def graph_activate(
    candidate_registry: Path,
    active_registry: Annotated[Path | None, typer.Option("--active")] = None,
) -> None:
    """Validate backends and atomically activate a sibling registry candidate."""
    from codekg.graph_lifecycle import activate_registry

    try:
        active_registry = active_registry or Path(
            os.environ.get("CODEKG_GRAPH_REGISTRY", "codekg-graphs.toml")
        )
        result = activate_registry(candidate_registry, active_registry)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(result)


@graph_app.command("rollback")
def graph_rollback(
    active_registry: Annotated[Path | None, typer.Option("--active")] = None,
    previous_registry: Annotated[Path | None, typer.Option("--previous")] = None,
) -> None:
    """Validate and restore the previous registry; MCP restart remains explicit."""
    from codekg.graph_lifecycle import rollback_registry

    try:
        active_registry = active_registry or Path(
            os.environ.get("CODEKG_GRAPH_REGISTRY", "codekg-graphs.toml")
        )
        result = rollback_registry(active_registry, previous_path=previous_registry)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(result)
