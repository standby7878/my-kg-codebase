from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(help="Operate the offline code knowledge graph.")
console = Console()


@app.callback()
def main() -> None:
    """CodeKG command line interface."""


@app.command()
def bootstrap() -> None:
    """Apply the Neo4j schema."""

    from codekg.schema.bootstrap import bootstrap_schema

    bootstrap_schema()
    console.print("[green]CodeKG schema bootstrap complete[/green]")


@app.command("index")
def index_repo(path: Path) -> None:
    """Replace the current snapshot for a mounted repository path."""

    from codekg.ingest import index_repository

    result = index_repository(path, replace=True)
    console.print(result)


@app.command("reindex")
def reindex_repo(path: Path) -> None:
    """Delete and index a mounted repository path."""

    from codekg.ingest import index_repository

    result = index_repository(path, replace=True)
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
    for child in children:
        console.print(index_repository(child, replace=True))


@app.command("list")
def list_repositories() -> None:
    """List indexed repositories."""

    from codekg.queries.repositories import list_repositories as query_repositories

    for row in query_repositories():
        console.print(row)


@app.command("delete")
def delete_repository(repo_name: str) -> None:
    """Delete an indexed repository by name."""

    from codekg.loader import delete_repository_by_name
    from codekg.zvec_store import delete_repo_records

    delete_repo_records(repo_name)
    deleted = delete_repository_by_name(repo_name)
    console.print({"repo_name": repo_name, "deleted": deleted})


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

    report = run_evaluation(
        manifest.resolve(),
        project_root=project_root.resolve(),
        zvec_root=zvec_root.resolve(),
        require_pins=require_pins,
    )
    write_report(report, output.resolve())
    console.print(report["summary"])


@app.command("bulk-export")
def bulk_export(
    output: Path,
    paths: Annotated[list[Path], typer.Argument(min=1)],
) -> None:
    """Export snapshots scanned from repository paths."""

    from codekg.bulk_export import export_repositories
    from codekg.ingest import scan_repository

    repositories = [scan_repository(path) for path in paths]
    result = export_repositories(repositories, output)
    console.print({"manifest": str(result.manifest_path), "counts": dict(result.counts)})


@app.command("bulk-import")
def bulk_import(
    manifest: Path,
    database: str = typer.Option("neo4j", "--database"),
    neo4j_admin: str = typer.Option("neo4j-admin", "--neo4j-admin"),
) -> None:
    """Import a bulk-export manifest into Neo4j."""

    from codekg.bulk_import import run_bulk_import

    result = run_bulk_import(manifest, database=database, neo4j_admin=neo4j_admin)
    console.print(result)


@app.command("bulk-zvec")
def bulk_zvec(paths: Annotated[list[Path], typer.Argument(min=1)]) -> None:
    """Build the derived zvec index from repository snapshots."""

    from codekg.ingest import scan_repository
    from codekg.search_index import callable_docs_from_repository
    from codekg.zvec_store import open_write, optimize_and_flush, upsert_symbol_docs

    repositories = [scan_repository(path) for path in paths]
    descriptions = [
        doc for repository in repositories for doc in callable_docs_from_repository(repository)
    ]
    collection = open_write()
    document_count = upsert_symbol_docs(collection, descriptions)
    optimize_and_flush(collection)
    console.print({"repositories": len(paths), "documents": document_count})


@app.command("validate-bulk-index")
def validate_bulk_index(paths: Annotated[list[Path], typer.Argument(min=1)]) -> None:
    """Validate staged zvec descriptions against live graph callables."""

    from codekg.ingest import scan_repository
    from codekg.search_index import (
        callable_docs_from_repository,
        iter_callable_rows,
        validate_search_index_consistency,
    )
    from codekg.zvec_store import open_write

    repositories = [scan_repository(path) for path in paths]
    descriptions = [
        doc for repository in repositories for doc in callable_docs_from_repository(repository)
    ]
    live_graph_keys = {
        str(row["key"])
        for repository in repositories
        for row in iter_callable_rows(repo=repository.repo_name)
    }
    result = validate_search_index_consistency(
        descriptions,
        live_graph_keys=live_graph_keys,
        collection=open_write(),
    )
    console.print(result)
    if not result["ok"]:
        raise typer.Exit(1)
