from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from testcontainers.neo4j import Neo4jContainer

from codekg.bulk_export import load_bulk_export
from codekg.bulk_import import build_import_command
from codekg.corpus_export import export_corpus
from codekg.neo4j_client import Neo4jClient
from codekg.queries.corpus import (
    compare_corpus_snapshots,
    get_dependency_evidence,
    list_corpus_snapshots,
    search_corpus_symbols,
    trace_corpus_path,
)
from codekg.schema.bootstrap import bootstrap_schema

pytestmark = pytest.mark.integration


def _offline_import(export, data_dir: Path) -> None:
    command = build_import_command(export, database="neo4j", neo4j_admin="neo4j-admin")
    rewritten = []
    for argument in command:
        if not argument.startswith(("--nodes=", "--relationships=")):
            rewritten.append(argument)
            continue
        option, value = argument.split("=", 1)
        label, paths = value.split("=", 1)
        mounted = [
            f"/var/lib/neo4j/import/{Path(path).resolve().relative_to(export.output_dir.resolve()).as_posix()}"
            for path in paths.split(",")
        ]
        rewritten.append(f"{option}={label}={','.join(mounted)}")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "neo4j-admin",
            "--env",
            "NEO4J_server_directories_data=/data",
            "--volume",
            f"{data_dir}:/data",
            "--volume",
            f"{export.output_dir}:/var/lib/neo4j/import:ro",
            "neo4j:5.26-community",
            *rewritten[1:],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_large_sql_artifact_import_uses_measured_neo4j_read_buffer(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    large_sql = '-- "quoted, λ"\n' * 420_000 + "SELECT 1;\n"
    assert len(large_sql.encode("utf-8")) > 5 * 1024 * 1024
    (root / "schema.sql").write_text(large_sql, encoding="utf-8")
    config = tmp_path / "fixture.toml"
    config.write_text(
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="app"\nsql={enabled=true,include=["**/*.sql"]}\n',
        encoding="utf-8",
    )
    output = tmp_path / "export"
    export_corpus(config, output)
    exported = load_bulk_export(output / "manifest.json")
    assert exported.max_csv_field_size_bytes > 4 * 1024 * 1024
    command = build_import_command(exported)
    assert any(argument.startswith("--read-buffer-size=") for argument in command)
    data_dir = tmp_path / "neo4j-data"
    data_dir.mkdir()
    _offline_import(exported, data_dir)


def test_exported_fixture_offline_import_and_five_live_queries(tmp_path, monkeypatch):
    app, cron, pg18, pg19 = (tmp_path / name for name in ("app", "cron", "pg18", "pg19"))
    for path in (app, cron, pg18, pg19):
        path.mkdir()
    (app / "main.py").write_text(
        'def run(db):\n    return db.execute("SELECT app.cron_wrapper(1)")\n',
        encoding="utf-8",
    )
    (app / "functions.sql").write_text(
        "CREATE FUNCTION app.cron_wrapper(x int) RETURNS int LANGUAGE sql "
        "AS $$ SELECT cron.cron_schedule(x); $$;\n"
        "CREATE FUNCTION app.guarded_wrapper(x int) RETURNS int LANGUAGE plpgsql AS $$ "
        "BEGIN IF x > 0 THEN PERFORM cron.cron_schedule(x); END IF; RETURN x; END $$;\n"
        "CREATE FUNCTION app.return_wrapper(x int) RETURNS int LANGUAGE plpgsql AS $$ "
        "BEGIN RETURN cron.cron_schedule(x); END $$;\n"
        "CREATE FUNCTION app.assignment_wrapper(x int) RETURNS int LANGUAGE plpgsql AS $$ "
        "DECLARE y int; BEGIN y := cron.cron_schedule(x); RETURN y; END $$;\n",
        encoding="utf-8",
    )
    (app / "schema.sql").write_text("SELECT cron.cron_schedule(1);\n", encoding="utf-8")
    (app / "runbook.md").write_text("Call `cron.cron_schedule(integer)`.\n", encoding="utf-8")
    (cron / "cron.c").write_text(
        "int pg_api(int); int maybe_api(int);\n"
        "int cron_schedule(int x) { return pg_api(x); }\n"
        "int maybe_api(int x) { return x; }\n"
        "int macro_probe(int x) { return maybe_api(x); }\n",
        encoding="utf-8",
    )
    (cron / "cron.h").write_text(
        '#ifdef USE_LOCAL_API\n#include "api.h"\n#endif\n#define maybe_api(x) ((x) + 1)\n',
        encoding="utf-8",
    )
    # A module and its same-named package share one logical Module across
    # separate worker spools while retaining both initializers/callables.
    (app / "worker.py").write_text("def callable_one():\n    return 1\n", encoding="utf-8")
    (app / "worker").mkdir()
    (app / "worker" / "__init__.py").write_text(
        "def callable_two():\n    return 2\n", encoding="utf-8"
    )
    (cron / "cron.control").write_text("module_pathname = '$libdir/cron'\n", encoding="utf-8")
    (cron / "cron--1.0.sql").write_text(
        "CREATE FUNCTION cron.cron_schedule(integer) RETURNS integer "
        "AS 'MODULE_PATHNAME', 'cron_schedule' LANGUAGE C;\n",
        encoding="utf-8",
    )
    for version, signature, rettype in (
        ("18", "int pg_api(int x)", "int4"),
        ("19", "long pg_api(long x)", "int8"),
    ):
        root = pg18 if version == "18" else pg19
        (root / "api.c").write_text(
            f"{signature} {{ return 1; }}\n"
            f"#ifdef {'OLD' if version == '18' else 'NEW'}_FEATURE\n"
            "int guarded_api(void) { return 1; }\n"
            "#endif\n",
            encoding="utf-8",
        )
        argtype = "int4" if version == "18" else "int8"
        catalog_record = (
            "{ oid => '123', proname => 'pg_api', prorettype => '"
            + rettype
            + "', proargtypes => '"
            + argtype
            + "', prosrc => 'pg_api', prolang => 'internal' }\n"
        )
        (root / "pg_proc.dat").write_text(catalog_record, encoding="utf-8")
    config = tmp_path / "fixture.toml"
    config.write_text(
        '[[snapshots]]\nalias="pg18"\nlogical_repo="postgres"\nversion="18"\nrole="postgres"\npath="pg18"\n'
        '[[snapshots]]\nalias="pg19"\nlogical_repo="postgres"\nversion="19"\nrole="postgres"\npath="pg19"\n'
        '[[snapshots]]\nalias="cron"\nlogical_repo="pg_cron"\nversion="1.0"\nrole="extension"\npath="cron"\ndependencies=["pg18"]\n'
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\nrole="application"\npath="app"\ndependencies=["cron"]\nsql={enabled=true}\n',
        encoding="utf-8",
    )
    output = tmp_path / "export"
    monkeypatch.setattr("codekg.bulk_export.CorpusSpoolBatcher.MAX_FILES", 1)
    export_corpus(config, output, workers=2)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {row["alias"] for row in manifest["snapshots"]} == {"app", "cron", "pg18", "pg19"}
    assert manifest["metrics"]["projection_workers"] == 2
    exported = load_bulk_export(output / "manifest.json")
    data_dir = tmp_path / "neo4j-data"
    data_dir.mkdir()
    _offline_import(exported, data_dir)
    with (
        Neo4jContainer("neo4j:5.26-community", password="password")
        .with_volume_mapping(str(data_dir), "/data", "rw")
        .with_env("NEO4J_server_directories_data", "/data") as container
    ):
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            bootstrap_schema(client)
            dangling = client.execute_read(
                "MATCH ()-[e]->() WHERE startNode(e).key IS NULL OR endNode(e).key IS NULL "
                "RETURN count(e) AS count"
            )
            assert dangling == [{"count": 0}]
            symbols = search_corpus_symbols("pg_api", snapshot_alias="pg18", client=client)
            assert symbols
            api_key = symbols[0]["key"]
            old_routine = search_corpus_symbols(
                "pg_api", snapshot_alias="pg18", kind="routine", client=client
            )
            new_routine = search_corpus_symbols(
                "pg_api", snapshot_alias="pg19", kind="routine", client=client
            )
            assert old_routine and new_routine
            assert old_routine[0]["return_type"] != new_routine[0]["return_type"]
            app_routines = search_corpus_symbols(
                "cron_wrapper", snapshot_alias="app", kind="routine", client=client
            )
            assert len(app_routines) == 1
            app_functions = client.execute_read(
                "MATCH (file:File)-[:CONTAINS]->(f:Function {name:'run',qname:'main.run'}) "
                "WHERE f.key STARTS WITH 'app@' "
                "RETURN f.key AS key, file.path AS path, f.start_line AS line"
            )
            assert len(app_functions) == 1
            assert (app_functions[0]["path"], app_functions[0]["line"]) == ("main.py", 1)
            app_key = app_functions[0]["key"]
            source_evidence_ownership = client.execute_read(
                "MATCH (e:SourceEvidence) WHERE e.snapshot_alias IN ['app','cron'] "
                "OPTIONAL MATCH (file:File)-[:HAS_EVIDENCE]->(e) "
                "WITH e,count(DISTINCT file) AS file_owners "
                "RETURN count(e) AS evidence, "
                "sum(CASE WHEN file_owners=1 THEN 1 ELSE 0 END) AS one_file_owner"
            )
            assert source_evidence_ownership and (
                source_evidence_ownership[0]["evidence"]
                == source_evidence_ownership[0]["one_file_owner"]
            )
            mixed_evidence = client.execute_read(
                "MATCH (file:File)-[:HAS_EVIDENCE]->(e:SourceEvidence) "
                "WHERE (file.path='main.py' AND e.origin='python_execute') OR "
                "(file.path='functions.sql' AND e.origin='routine_body') OR "
                "(file.path='cron.c' AND e.origin='native_call') OR "
                "(file.path='runbook.md' AND e.origin='markdown_mention') OR "
                "(file.path='schema.sql' AND e.origin='sql_source') "
                "RETURN DISTINCT file.path AS path,e.origin AS origin ORDER BY path"
            )
            assert {row["path"] for row in mixed_evidence} == {
                "main.py",
                "functions.sql",
                "cron.c",
                "runbook.md",
                "schema.sql",
            }
            runbook_evidence = client.execute_read(
                "MATCH (file:File {path:'runbook.md'})-[:HAS_EVIDENCE]->(e:SourceEvidence) "
                "WHERE e.snapshot_alias='app' AND e.name='cron_schedule' "
                "RETURN file.path AS path,e.origin AS origin,e.start_line AS line"
            )
            assert runbook_evidence and runbook_evidence[0]["line"] == 1
            top_level_evidence = client.execute_read(
                "MATCH (file:File {path:'schema.sql'})-[:HAS_EVIDENCE]->(e:SourceEvidence) "
                "WHERE e.snapshot_alias='app' AND e.origin='sql_source' "
                "OPTIONAL MATCH (owner)-[:HAS_EVIDENCE]->(e) WHERE NOT owner:File "
                "RETURN count(DISTINCT e) AS evidence,count(DISTINCT owner) AS lexical_owners"
            )
            assert top_level_evidence and top_level_evidence[0] == {
                "evidence": 1,
                "lexical_owners": 0,
            }
            cron_rows = search_corpus_symbols("cron_schedule", snapshot_alias="cron", client=client)
            assert cron_rows
            routine = next(row for row in cron_rows if "Routine" in row["labels"])
            cron_key = routine["key"]
            # Candidate and conditionally guarded shortcuts must not suppress the longer exact path.
            client.execute_write(
                "MATCH (a {key:$source}),(b {key:$target}) "
                "CREATE (a)-[:CALLS_NATIVE {status:'candidate',condition:'',\n"
                "path:'candidate.c',line:1}]->(b)",
                {"source": app_key, "target": api_key},
            )
            client.execute_write(
                "MATCH (a {key:$source}),(b {key:$target}) "
                "CREATE (a)-[:CALLS_NATIVE {status:'exact',condition:'FEATURE_X',\n"
                "path:'guarded.c',line:2}]->(b)",
                {"source": app_key, "target": api_key},
            )
            assert len(list_corpus_snapshots(client=client)) == 4
            assert any(
                row["relationship"] in {"CALLS_NATIVE", "BINDS_TO_NATIVE", "INVOKES_ROUTINE"}
                for row in get_dependency_evidence(cron_key, direction="outgoing", client=client)
            )
            paths = trace_corpus_path(app_key, api_key, max_depth=8, client=client)
            assert paths and len(paths[0]["evidence"]) == 6
            assert any(node["key"] == app_routines[0]["key"] for node in paths[0]["nodes"])
            assert [(edge["path"], edge["line"]) for edge in paths[0]["evidence"]] == [
                ("main.py", 2),
                ("main.py", 2),
                ("functions.sql", 1),
                ("functions.sql", 1),
                ("cron--1.0.sql", 1),
                ("cron.c", 2),
            ]
            guarded_routine = search_corpus_symbols(
                "guarded_wrapper", snapshot_alias="app", kind="routine", client=client
            )
            assert len(guarded_routine) == 1
            guarded_evidence = get_dependency_evidence(
                guarded_routine[0]["key"], direction="outgoing", client=client
            )
            guarded_paths = trace_corpus_path(
                guarded_routine[0]["key"], api_key, max_depth=8, client=client
            )
            has_conditional_evidence = any(
                row["relationship"] == "HAS_EVIDENCE" and row["status"] == "conditional"
                for row in guarded_evidence
            )
            assert has_conditional_evidence and not guarded_paths, (
                "inline PL/pgSQL PERFORM guard must remain conditional evidence, "
                "not an exact path; "
                f"evidence={guarded_evidence!r}, paths={guarded_paths!r}"
            )
            return_wrapper = search_corpus_symbols(
                "return_wrapper", snapshot_alias="app", kind="routine", client=client
            )
            assert len(return_wrapper) == 1
            return_evidence = get_dependency_evidence(
                return_wrapper[0]["key"], direction="outgoing", client=client
            )
            return_paths = trace_corpus_path(
                return_wrapper[0]["key"], api_key, max_depth=8, client=client
            )
            assert (
                any(row["relationship"] == "HAS_EVIDENCE" for row in return_evidence)
                and return_paths
            ), (
                "PL/pgSQL RETURN expression calls must be preserved as source evidence "
                "and participate in exact dependency paths; "
                f"evidence={return_evidence!r}, paths={return_paths!r}"
            )
            assignment_wrapper = search_corpus_symbols(
                "assignment_wrapper", snapshot_alias="app", kind="routine", client=client
            )
            assert len(assignment_wrapper) == 1
            assignment_evidence = get_dependency_evidence(
                assignment_wrapper[0]["key"], direction="outgoing", client=client
            )
            assignment_paths = trace_corpus_path(
                assignment_wrapper[0]["key"], api_key, max_depth=8, client=client
            )
            assert (
                any(row["relationship"] == "HAS_EVIDENCE" for row in assignment_evidence)
                and assignment_paths
            )

            guarded_includes = client.execute_read(
                "MATCH (file:File)-[:HAS_EVIDENCE]->(e:SourceEvidence) "
                "WHERE e.snapshot_alias='cron' AND file.path='cron.h' "
                "AND e.origin='native_include' AND e.condition='ifdef USE_LOCAL_API' "
                "OPTIONAL MATCH (owner)-[:HAS_EVIDENCE]->(e) WHERE NOT owner:File "
                "RETURN e.key AS key,e.start_line AS line,count(DISTINCT file) AS files, "
                "count(DISTINCT owner) AS lexical_owners"
            )
            assert len(guarded_includes) == 1
            assert {
                key: guarded_includes[0][key] for key in ("line", "files", "lexical_owners")
            } == {"line": 2, "files": 1, "lexical_owners": 0}
            assert not trace_corpus_path(
                guarded_includes[0]["key"], api_key, max_depth=8, client=client
            )

            module_graph = client.execute_read(
                "MATCH (m:Module {qname:'worker',language:'python'}) "
                "WHERE m.key STARTS WITH 'app@' "
                "OPTIONAL MATCH (f:File)-[:DEFINES]->(m) "
                "WITH m,count(DISTINCT f) AS defining_files "
                "OPTIONAL MATCH (m)-[:INITIALIZES]->(init:ModuleInit) "
                "RETURN count(DISTINCT m) AS modules, max(defining_files) AS defining_files, "
                "count(DISTINCT init) AS initializers"
            )
            assert module_graph == [{"modules": 1, "defining_files": 2, "initializers": 2}]
            module_functions = client.execute_read(
                "MATCH (file:File)-[:CONTAINS]->(f:Function) "
                "WHERE f.key STARTS WITH 'app@' AND f.qname IN "
                "['worker.callable_one','worker.callable_two'] "
                "RETURN f.qname AS qname, file.path AS path ORDER BY qname"
            )
            assert module_functions == [
                {"qname": "worker.callable_one", "path": "worker.py"},
                {"qname": "worker.callable_two", "path": "worker/__init__.py"},
            ]

            macro_probe = client.execute_read(
                "MATCH (f:NativeSymbol {name:'macro_probe',snapshot_alias:'cron'}) "
                "RETURN f.key AS key"
            )
            macro_target = client.execute_read(
                "MATCH (f:NativeSymbol {name:'maybe_api',declaration:false}) "
                "WHERE f.snapshot_alias='cron' AND f.kind='function' "
                "RETURN f.key AS key"
            )
            assert len(macro_probe) == len(macro_target) == 1
            macro_evidence = get_dependency_evidence(
                macro_probe[0]["key"], direction="outgoing", client=client
            )
            assert any(row["relationship"] == "NATIVE_CANDIDATE" for row in macro_evidence)
            assert not trace_corpus_path(
                macro_probe[0]["key"], macro_target[0]["key"], max_depth=4, client=client
            )
            native_cron = next(
                row
                for row in cron_rows
                if "NativeSymbol" in row["labels"] and row["name"] == "cron_schedule"
            )
            native_owner_edges = get_dependency_evidence(
                native_cron["key"], direction="outgoing", client=client
            )
            evidence_key = next(
                row["related_key"]
                for row in native_owner_edges
                if row["relationship"] == "HAS_EVIDENCE"
            )
            native_path = trace_corpus_path(
                native_cron["key"], evidence_key, max_depth=8, client=client
            )
            assert native_path and "HAS_EVIDENCE" in {
                edge["kind"] for edge in native_path[0]["evidence"]
            }
            comparison = compare_corpus_snapshots("pg18", "pg19", client=client)
            assert any(
                row["change"] == "signature_changed"
                and any("pg_api" in signature for signature in row["old_signatures"])
                and any("pg_api" in signature for signature in row["new_signatures"])
                for row in comparison
            )
            assert any(
                row["change"] == "condition_changed"
                and row["identity"].endswith("guarded_api")
                and row["old_signatures"] == row["new_signatures"]
                for row in comparison
            )
            selected_internal = client.execute_read(
                "MATCH (:Routine {snapshot_alias:'pg18',name:'pg_api'}) "
                "-[:BINDS_TO_NATIVE]->(native:NativeSymbol) "
                "RETURN DISTINCT native.snapshot_alias AS alias"
            )
            assert selected_internal == [{"alias": "pg18"}]
        finally:
            client.close()


def test_corpus_queries_execute_against_live_neo4j():
    with Neo4jContainer("neo4j:5.26-community", password="password") as container:
        client = Neo4jClient(
            uri=container.get_connection_url(),
            username=container.username,
            password=container.password,
        )
        try:
            client.execute_write("""
              CREATE (:CorpusSnapshot {alias:'old',logical_repo:'fixture',version:'1',role:'source',
                git_commit:'a',revision:'a',source_digest:'x',fingerprint:'x',root_path:'/old'})
              CREATE (:CorpusSnapshot {alias:'new',logical_repo:'fixture',version:'2',role:'source',
                git_commit:'b',revision:'b',source_digest:'y',fingerprint:'y',root_path:'/new'})
              CREATE (:NativeSymbol {key:'old:a',snapshot_alias:'old',name:'caller',
                signature:'void caller()',logical_id:'caller',language:'C',path:'src/a.c',
                start_line:1,end_line:2,condition:'',coverage:'complete'})
              CREATE (:NativeSymbol {key:'old:b',snapshot_alias:'old',name:'target',
                signature:'int target()',logical_id:'target',language:'C',path:'src/b.c',
                start_line:3,end_line:4,condition:'',coverage:'complete'})
              CREATE (:NativeSymbol {key:'new:b',snapshot_alias:'new',name:'target',
                signature:'long target()',logical_id:'target',language:'C',path:'src/b.c',
                start_line:3,end_line:4,condition:'',coverage:'complete'})
              CREATE (:SourceEvidence {key:'ev',snapshot_alias:'old',name:'call evidence',
                path:'src/a.c',status:'unresolved',dynamic:true,candidate_count:2})
              WITH 1 AS ignored
              MATCH (a:NativeSymbol {key:'old:a'}),(b:NativeSymbol {key:'old:b'}),
                    (e:SourceEvidence {key:'ev'})
              CREATE (a)-[:CALLS_NATIVE {status:'exact',condition:'',path:'src/a.c',line:1}]->(b)
              CREATE (a)-[:HAS_EVIDENCE {status:'exact',condition:'',path:'src/a.c',line:1}]->(e)
            """)
            snapshots = list_corpus_snapshots(client=client)
            assert {row["alias"] for row in snapshots} == {"old", "new"}
            assert (
                search_corpus_symbols("target", snapshot_alias="old", client=client)[0]["key"]
                == "old:b"
            )
            evidence = get_dependency_evidence("old:a", direction="outgoing", client=client)
            assert {row["relationship"] for row in evidence} == {"CALLS_NATIVE", "HAS_EVIDENCE"}
            ownership = next(row for row in evidence if row["relationship"] == "HAS_EVIDENCE")
            assert ownership["related_status"] == "unresolved" and ownership["dynamic"] is True
            assert ownership["candidate_count"] == 2
            paths = trace_corpus_path("old:a", "old:b", client=client)
            assert len(paths) == 1 and paths[0]["evidence"][0]["kind"] == "CALLS_NATIVE"
            comparison = compare_corpus_snapshots("old", "new", client=client)
            assert any(
                row["identity"] == "target" and row["change"] == "signature_changed"
                for row in comparison
            )
        finally:
            client.close()
