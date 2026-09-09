from __future__ import annotations

import csv
from pathlib import Path

import pytest

from codekg.bulk_export import export_repositories, load_bulk_export
from codekg.bulk_projection import project_repository
from codekg.bulk_spool import build_registry, create_spool
from codekg.ir import RepositoryIR
from codekg.sql_config import SqlConfig
from codekg.sql_parser import parse_sql

pytestmark = pytest.mark.unit


def _repository() -> RepositoryIR:
    files = (
        parse_sql(
            "CREATE TABLE public.users (id int);\n"
            "COMMENT ON TABLE public.users IS 'line 1\nline 2';\n"
            "SELECT id FROM public.users;\n",
            context="sql/a.sql",
            config=SqlConfig(),
        ),
        parse_sql(
            "SELECT id FROM public.users;\n",
            context="sql/b.sql",
            config=SqlConfig(),
        ),
    )
    return RepositoryIR("sample", "abc", "/repos/sample", files)


def _streaming_export(
    repo: RepositoryIR, output: Path, workers: int, *, projection_repo: RepositoryIR | None = None
):
    spools = []
    for ordinal, file in enumerate(repo.files):
        spool = output / f"spool-{ordinal}.sqlite"
        create_spool(spool, [file])
        spools.append(spool)
    registry = output / "resolver.sqlite"
    build_registry(registry, spools, repo_prefix="sample@abc")
    result = project_repository(
        projection_repo or repo,
        spools,
        registry,
        output / f"projection-{workers}",
        workers=workers,
    )
    return load_bulk_export(result.manifest_path)


def _sql_counts(exported) -> dict[str, int]:
    return {
        key: value
        for key, value in exported.counts.items()
        if key.startswith("nodes_Sql")
        or key.startswith("nodes_Reference")
        or key.startswith("nodes_Database")
        or key.startswith("relationships_")
        and key
        in {
            "relationships_HAS_DATABASE",
            "relationships_HAS_OBJECT",
            "relationships_CONTAINS_SQL",
            "relationships_HAS_REFERENCE",
            "relationships_REFERS_TO",
            "relationships_READS_FROM",
        }
    }


def _normalized_sql_rows(exported, section: str, kind: str) -> list[tuple[str, ...]]:
    groups = exported.node_groups if section == "nodes" else exported.relationship_groups
    paths = groups[kind]
    with paths[0].open(newline="", encoding="utf-8") as handle:
        next(csv.reader(handle))
        rows = list(csv.reader(handle))
    for path in paths[1:]:
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.reader(handle))
    return sorted(tuple(row) for row in rows)


def test_legacy_and_streaming_sql_projections_match(tmp_path: Path) -> None:
    repo = _repository()
    legacy = export_repositories([repo], tmp_path / "legacy")
    identity = RepositoryIR(repo.repo_name, repo.commit, repo.root_path)
    one = _streaming_export(repo, tmp_path / "one", workers=1, projection_repo=identity)
    two = _streaming_export(repo, tmp_path / "two", workers=2, projection_repo=identity)

    assert _sql_counts(legacy) == _sql_counts(one) == _sql_counts(two)
    for section, kinds in (
        ("nodes", ("Database", "SqlObject", "SqlArtifact", "SqlStatement", "Reference")),
        (
            "relationships",
            (
                "HAS_DATABASE",
                "HAS_OBJECT",
                "CONTAINS_SQL",
                "HAS_REFERENCE",
                "REFERS_TO",
                "DEFINES",
                "READS_FROM",
            ),
        ),
    ):
        for kind in kinds:
            assert (
                _normalized_sql_rows(legacy, section, kind)
                == _normalized_sql_rows(one, section, kind)
                == _normalized_sql_rows(two, section, kind)
            )
    assert legacy.counts["nodes_Reference"] == 3
    assert legacy.counts["relationships_REFERS_TO"] == 3

    reference = list(csv.DictReader(legacy.node_files["Reference"].open(newline="")))
    assert {row["dynamic:boolean"] for row in reference} == {"false"}
    assert all(row["candidate_keys_json"].startswith("[") for row in reference)
    artifacts = list(csv.DictReader(legacy.node_files["SqlArtifact"].open(newline="")))
    assert any("line 1\nline 2" in row["text"] for row in artifacts)


def test_sql_global_object_selects_wide_defines_schema_without_exact_edge(
    tmp_path: Path,
) -> None:
    file = parse_sql("CREATE SCHEMA app;\n", context="schema.sql", config=SqlConfig())
    exported = export_repositories(
        [RepositoryIR("sample", "abc", "/repos/sample", (file,))], tmp_path / "export"
    )

    with exported.relationship_files["DEFINES"].open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == [
        "key",
        ":START_ID(CodeKG)",
        ":END_ID(CodeKG)",
        "role",
        "line:int",
        "column:int",
        ":TYPE",
    ]
