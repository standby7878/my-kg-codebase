from __future__ import annotations

from pathlib import Path

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
