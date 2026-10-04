from __future__ import annotations

import csv
import io
import json

import pytest

from codekg.corpus_export import _bounded_csv_reader, export_corpus


def _config(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n")
    (root / "api.c").write_text("int api(void) { return 1; }\n")
    (root / "guide.md").write_text("`public.api()`\n")
    (root / "schema.sql").write_text("CREATE TABLE items(id integer);\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\nrole="application"\npath="src"\nsql={enabled=true,include=["**/*.sql"]}\n'
    )
    return root, config


def test_export_publishes_registry_without_scan_repository(tmp_path, monkeypatch):
    import codekg.ingest

    _, config = _config(tmp_path)
    monkeypatch.setattr(
        codekg.ingest,
        "scan_repository",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scan_repository called")),
    )
    result = export_corpus(config, tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert result["snapshots"][0]["revision"] == manifest["snapshots"][0]["revision"]
    assert (tmp_path / "out" / manifest["registry"]).is_file()
    assert manifest["metrics"]["native_extraction"] == "serial_file_at_a_time"
    assert "NativeSymbol" in manifest["nodes"]
    assert "CorpusSnapshot" in manifest["nodes"]
    assert manifest["search_stage"]["version"] == 1
    assert (tmp_path / "out" / manifest["output_dir"] / manifest["search_stage"]["file"]).is_file()
    defines = (
        tmp_path / "out" / manifest["output_dir"] / manifest["relationships"]["DEFINES"]["file"]
    )
    assert "role" in defines.read_text(encoding="utf-8").splitlines()[0]
    from codekg.bulk_export import load_bulk_export
    from codekg.bulk_search import load_search_stage

    loaded = load_bulk_export(tmp_path / "out" / "manifest.json")
    assert loaded.node_groups["NativeSymbol"]
    assert load_search_stage(tmp_path / "out" / "manifest.json") == loaded.search_stage


def test_large_sql_artifact_survives_composition_validation_and_search_stage(tmp_path):
    root, config = _config(tmp_path)
    (root / "a.py").write_text("def searchable():\n    return 1\n", encoding="utf-8")
    large_sql = '-- "quoted, λ"\n' * 14_000 + "SELECT 1;\n"
    assert len(large_sql.encode("utf-8")) > 131_072
    (root / "schema.sql").write_text(large_sql, encoding="utf-8")
    output = tmp_path / "out"

    export_corpus(config, output)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    generation = output / manifest["output_dir"]
    artifact_path = generation / manifest["nodes"]["SqlArtifact"]["file"]
    with artifact_path.open(encoding="utf-8", newline="") as source:
        rows = list(_bounded_csv_reader(source))
    artifact = next(row for row in rows[1:] if row[1] == "schema.sql")
    assert artifact[5] == large_sql
    assert manifest["search_stage"]["documents"] >= 1
    assert (generation / manifest["search_stage"]["file"]).is_file()


def test_csv_reader_rejects_fields_over_a_small_injected_bound():
    source = io.StringIO("payload\n" + "x" * 129 + "\n")
    try:
        with pytest.raises(csv.Error, match="field larger than field limit"):
            list(_bounded_csv_reader(source, field_size_limit=128))
    finally:
        _bounded_csv_reader(io.StringIO(""))


def test_csv_composition_keeps_headerless_near_prefix_keys(tmp_path):
    from codekg.corpus_export import _merge_csv_group

    data = tmp_path / "headerless.csv"
    data.write_text("keykeyboard@revision:a.py,Repositorykeykeyboard,Source\n", encoding="utf-8")
    output = tmp_path / "merged.csv"
    count, _ = _merge_csv_group(
        [data], output, (("key", "key:ID(CodeKG)"), ("name", "name")), label="Source"
    )
    with output.open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    assert count == 1
    assert rows[1] == ["keykeyboard@revision:a.py", "Repositorykeykeyboard", "Source"]


def test_csv_composition_carries_header_sidecar_layout_only_within_its_group(tmp_path):
    from codekg.corpus_export import _SUPPLEMENTAL_REL_COLUMNS, _merge_csv_group

    headers = tmp_path / "headers"
    headers.mkdir()
    header_file = headers / "defines.csv"
    header_file.write_text(":START_ID(CodeKG),:END_ID(CodeKG),:TYPE\n", encoding="utf-8")
    shard = tmp_path / "defines-part.csv"
    shard.write_text("legacy-start,legacy-end,DEFINES\n", encoding="utf-8")
    inline = tmp_path / "supplemental.csv"
    inline_headers = [header for _, header in _SUPPLEMENTAL_REL_COLUMNS]
    inline.write_text(
        ",".join(inline_headers)
        + "\n"
        + "supplemental-key,supplemental-start,supplemental-end,ok,path.sql,7,9,,DEFINES\n",
        encoding="utf-8",
    )
    output = tmp_path / "merged.csv"
    columns = _SUPPLEMENTAL_REL_COLUMNS
    count, _ = _merge_csv_group([header_file, shard, inline], output, columns)
    with output.open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    assert count == 2
    assert rows[1] == [
        "",
        "legacy-start",
        "legacy-end",
        "",
        "",
        "",
        "",
        "",
        "DEFINES",
    ]
    assert rows[2] == [
        "supplemental-key",
        "supplemental-start",
        "supplemental-end",
        "ok",
        "path.sql",
        "7",
        "9",
        "",
        "DEFINES",
    ]


def test_full_export_preserves_keyboard_alias_key(tmp_path):
    root = tmp_path / "Repositorykeykeyboard"
    root.mkdir()
    (root / "a.py").write_text("def keyboard():\n    return 1\n", encoding="utf-8")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="keykeyboard"\nlogical_repo="keykeyboard"\n'
        'version="1"\nrole="application"\npath="Repositorykeykeyboard"\n',
        encoding="utf-8",
    )
    output = tmp_path / "out"
    result = export_corpus(config, output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    with (output / manifest["nodes"]["File"]["file"]).open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    assert result["nodes"]["File"]["count"] > 0
    assert any(row[0].startswith("keykeyboard@") and row[1] == "a.py" for row in rows[1:])


@pytest.mark.parametrize("owner_count", [16, 64])
def test_supplemental_python_owner_projection_uses_indexed_occurrences(
    tmp_path, monkeypatch, owner_count
):
    import codekg.corpus_export as corpus_export
    from codekg.corpus_registry import create_native_registry as create_registry

    root = tmp_path / "src"
    root.mkdir()
    source = "\n".join(
        f"def run_{index}(db):\n    db.execute('SELECT api_{index}()')"
        for index in range(owner_count)
    )
    (root / "app.py").write_text(source + "\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="src"\nsql={enabled=true}\n'
    )
    owner_queries = 0

    class GuardedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            nonlocal owner_queries
            normalized = " ".join(sql.lower().split())
            if "select ordinal from symbols" in normalized and "fact=?" in normalized:
                raise AssertionError("supplemental symbol owner fact equality lookup")
            if "select ordinal from routines" in normalized and "fact=?" in normalized:
                raise AssertionError("supplemental routine owner fact equality lookup")
            if "select key from python_owners" in normalized and "and start_line=?" in normalized:
                owner_queries += 1
            return self.connection.execute(sql, parameters)

        def executemany(self, sql, parameters):
            return self.connection.executemany(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        corpus_export,
        "create_native_registry",
        lambda path: GuardedConnection(create_registry(path)),
    )
    result = export_corpus(config, tmp_path / "out")
    assert owner_queries == owner_count
    assert result["nodes"]["SourceEvidence"]["count"] == owner_count


def test_midbuild_source_change_preserves_published_manifest(tmp_path, monkeypatch):
    import codekg.corpus_export as module

    root, config = _config(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    existing = output / "manifest.json"
    existing.write_text('{"generation":"old"}')
    original = module.extract_snapshot_facts

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "a.py").write_text("value = 2\n")
        return result

    monkeypatch.setattr(module, "extract_snapshot_facts", mutate)
    with pytest.raises(RuntimeError, match="identity changed"):
        export_corpus(config, output)
    assert json.loads(existing.read_text())["generation"] == "old"


def test_explicit_variant_dependency_resolves_app_to_extension_to_pg_api(tmp_path):
    import sqlite3

    from codekg.corpus_queries import search, trace

    for version, c_signature in (("18", "int pg_api(int x)"), ("19", "long pg_api(long x)")):
        root = tmp_path / f"pg{version}"
        root.mkdir()
        (root / "api.c").write_text(f"{c_signature} {{ return 1; }}\n")
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "cron.c").write_text(
        "int pg_api(int); int cron_schedule(int x) { return pg_api(x); }\n"
    )
    (extension / "cron.sql").write_text(
        "CREATE FUNCTION cron.cron_schedule(integer) RETURNS integer "
        "AS 'MODULE_PATHNAME', 'cron_schedule' LANGUAGE C;\n"
    )
    (extension / "cron.control").write_text("module_pathname = '$libdir/cron'\n")
    app = tmp_path / "app"
    app.mkdir()
    (app / "db.py").write_text(
        'def run(db):\n    SQL = "SELECT cron.cron_schedule(1)"\n    db.execute(SQL)\n'
    )
    (app / "runbook.md").write_text("`cron.cron_schedule(1)`\n")
    (app / "setup.sql").write_text("SELECT cron.cron_schedule(1);\n")
    config = tmp_path / "corpus.toml"
    config.write_text(
        '[[snapshots]]\nalias="pg18"\nlogical_repo="postgres"\nversion="18"\n'
        'role="postgres"\npath="pg18"\n'
        '[[snapshots]]\nalias="pg19"\nlogical_repo="postgres"\nversion="19"\n'
        'role="postgres"\npath="pg19"\n'
        '[[snapshots]]\nalias="cron18"\nlogical_repo="cron"\nversion="1"\n'
        'role="extension"\npath="extension"\ndependencies=["pg18"]\n'
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="app"\ndependencies=["cron18"]\nsql={enabled=true}\n'
    )
    output = tmp_path / "corpus-out"
    export_corpus(config, output)
    manifest = output / "manifest.json"
    assert (
        search(manifest, "pg_api", snapshot="pg18", kind="native")[0]["signature"]
        != search(manifest, "pg_api", snapshot="pg19", kind="native")[0]["signature"]
    )
    registry = sqlite3.connect(output / json.loads(manifest.read_text())["registry"])
    try:
        edge_types = {
            row[0] for row in registry.execute("SELECT kind FROM edges WHERE status='exact'")
        }
        assert {"INVOKES_ROUTINE", "BINDS_TO_NATIVE", "CALLS_NATIVE"} <= edge_types
        assert registry.execute(
            "SELECT 1 FROM edges WHERE kind='DOCUMENTS_ROUTINE' AND status='exact' LIMIT 1"
        ).fetchone()
        assert registry.execute(
            "SELECT 1 FROM edges e JOIN fact_keys k ON k.key=e.source_key "
            "JOIN evidence v ON v.snapshot_alias=k.snapshot_alias AND v.path=k.path "
            "AND v.ordinal=k.ordinal WHERE e.kind='INVOKES_ROUTINE' AND e.status='exact' "
            "AND k.snapshot_alias='app' AND json_extract(v.fact,'$.origin')='sql_source' LIMIT 1"
        ).fetchone()
        assert registry.execute(
            "SELECT 1 FROM edges WHERE kind='DESCRIBES_SQL_OBJECT' AND status='exact' LIMIT 1"
        ).fetchone()
        evidence_count = registry.execute("SELECT count(*) FROM evidence").fetchone()[0]
        file_owner_count = registry.execute(
            "SELECT count(*) FROM edges e WHERE e.kind='HAS_EVIDENCE' "
            "AND e.target_key IN (SELECT key FROM fact_keys WHERE table_name='evidence') "
            "AND e.source_key NOT IN (SELECT key FROM fact_keys "
            "UNION SELECT key FROM python_owners)"
        ).fetchone()[0]
        lexical_owner_count = registry.execute(
            "SELECT count(*) FROM edges e WHERE e.kind='HAS_EVIDENCE' "
            "AND e.source_key IN (SELECT key FROM fact_keys UNION SELECT key FROM python_owners) "
            "AND e.target_key IN (SELECT key FROM fact_keys WHERE table_name='evidence')"
        ).fetchone()[0]
        assert file_owner_count == evidence_count
        python_owner_evidence_count = registry.execute(
            "SELECT count(*) FROM edges e WHERE e.kind='HAS_EVIDENCE' "
            "AND e.source_key IN (SELECT key FROM python_owners) "
            "AND e.target_key IN (SELECT key FROM fact_keys WHERE table_name='evidence')"
        ).fetchone()[0]
        assert lexical_owner_count >= 1
        assert python_owner_evidence_count == 1
        published = json.loads(manifest.read_text())
        assert published["nodes"]["SourceEvidence"]["count"] == evidence_count
        assert published["relationships"]["HAS_EVIDENCE"]["count"] == (
            file_owner_count + lexical_owner_count
        )
        relationships_csv = (
            output / published["output_dir"] / published["relationships"]["HAS_EVIDENCE"]["file"]
        )
        with relationships_csv.open(encoding="utf-8", newline="") as source:
            relationship_keys = [row["key"] for row in csv.DictReader(source)]
        assert len(relationship_keys) == len(set(relationship_keys))
        assert registry.execute(
            "SELECT 1 FROM edges WHERE kind='CALLS_NATIVE' AND status='exact' "
            "AND target_key IN (SELECT k.key FROM fact_keys k JOIN symbols s "
            "ON k.snapshot_alias=s.snapshot_alias AND k.path=s.path AND k.ordinal=s.ordinal "
            "WHERE k.snapshot_alias='pg18' AND s.path='api.c' "
            "AND json_extract(s.fact,'$.name')='pg_api') LIMIT 1"
        ).fetchone()
        app_start = registry.execute(
            "SELECT source_key FROM edges WHERE kind='INVOKES_ROUTINE' AND status='exact' LIMIT 1"
        ).fetchone()[0]
        pg_api_key = registry.execute(
            "SELECT target_key FROM edges WHERE kind='CALLS_NATIVE' "
            "AND status='exact' AND target_key IN (SELECT k.key FROM fact_keys k JOIN symbols s "
            "ON k.snapshot_alias=s.snapshot_alias AND k.path=s.path AND k.ordinal=s.ordinal "
            "WHERE k.snapshot_alias='pg18' AND s.path='api.c' "
            "AND json_extract(s.fact,'$.name')='pg_api') LIMIT 1"
        ).fetchone()[0]
        path = trace(manifest, app_start, pg_api_key)
        assert path and [edge["kind"] for edge in path[0]] == [
            "INVOKES_ROUTINE",
            "BINDS_TO_NATIVE",
            "CALLS_NATIVE",
        ]
    finally:
        registry.close()


def test_control_identity_selects_pg_cron_and_keeps_postgis_placeholders_unresolved(tmp_path):
    import sqlite3

    extension = tmp_path / "extension"
    (extension / "raster").mkdir(parents=True)
    (extension / "cron.c").write_text("int cron_schedule(void) { return 1; }\n")
    (extension / "postgis.c").write_text("int postgis_entry(void) { return 1; }\n")
    (extension / "raster.c").write_text("int raster_entry(void) { return 1; }\n")
    (extension / "pg_cron.control").write_text("module_pathname = '$libdir/pg_cron'\n")
    (extension / "postgis.control.in").write_text("module_pathname = '@MODULEPATH@'\n")
    (extension / "postgis_raster.control.in").write_text("module_pathname = '@MODULEPATH@'\n")
    (extension / "pg_cron.sql").write_text(
        "CREATE FUNCTION cron.schedule() RETURNS int AS 'MODULE_PATHNAME', "
        "'cron_schedule' LANGUAGE C;\n"
    )
    (extension / "postgis.sql.in").write_text(
        "CREATE FUNCTION public.postgis_fn() RETURNS int AS 'MODULE_PATHNAME', "
        "'postgis_entry' LANGUAGE C;\n"
    )
    (extension / "raster" / "rtpostgis.sql.in").write_text(
        "CREATE FUNCTION public.raster_fn() RETURNS int AS 'MODULE_PATHNAME', "
        "'raster_entry' LANGUAGE C;\n"
    )
    config = tmp_path / "corpus.toml"
    postgres = tmp_path / "postgres"
    postgres.mkdir()
    config.write_text(
        '[[snapshots]]\nalias="pg"\nlogical_repo="postgres"\nversion="18"\n'
        'role="postgres"\npath="postgres"\n'
        '[[snapshots]]\nalias="ext"\nlogical_repo="extensions"\nversion="1"\n'
        'role="extension"\npath="extension"\ndependencies=["pg"]\n'
        '[snapshots.sql]\nenabled=true\ninclude=["**/*.sql", "**/*.sql.in"]\n'
    )
    output = tmp_path / "out"
    manifest = export_corpus(config, output)
    db = sqlite3.connect(output / manifest["registry"])
    try:
        rows = db.execute(
            "SELECT k.path,e.kind,e.status,e.target_key FROM edges e "
            "JOIN fact_keys k ON k.key=e.source_key WHERE k.table_name='routines' "
            "AND e.kind IN ('BINDS_TO_NATIVE','NATIVE_CANDIDATE') ORDER BY k.path"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in rows] == [
            ("pg_cron.sql", "BINDS_TO_NATIVE", "exact"),
            ("postgis.sql.in", "NATIVE_CANDIDATE", "unresolved"),
            ("raster/rtpostgis.sql.in", "NATIVE_CANDIDATE", "unresolved"),
        ]
        assert all(row[3] for row in rows)
        unresolved = db.execute(
            "SELECT count(*) FROM diagnostics WHERE snapshot_alias='ext' "
            "AND json_extract(fact,'$.category')='unresolved_module_pathname'"
        ).fetchone()[0]
        assert unresolved == 2
        routine_csv = output / manifest["nodes"]["Routine"]["file"]
        assert "condition" in routine_csv.read_text(encoding="utf-8").splitlines()[0]
    finally:
        db.close()


def test_application_sql_routines_and_python_link_through_extension_to_postgres(tmp_path):
    import sqlite3

    from codekg.corpus_config import CorpusConfig, CorpusSnapshotConfig
    from codekg.corpus_export import export_corpus
    from codekg.sql_config import SqlConfig

    pg = tmp_path / "pg"
    extension = tmp_path / "extension"
    app = tmp_path / "app"
    for root in (pg, extension, app):
        root.mkdir()
    (pg / "api.c").write_text("int pg_api(void) { return 1; }\n")
    (extension / "cron.c").write_text(
        "int pg_api(void); int cron_schedule(void) { return pg_api(); }\n"
    )
    (extension / "pg_cron.control").write_text("module_pathname = '$libdir/pg_cron'\n")
    (extension / "pg_cron.sql").write_text(
        "CREATE FUNCTION cron.schedule() RETURNS integer "
        "AS 'MODULE_PATHNAME', 'cron_schedule' LANGUAGE C;\n"
    )
    sql_source = (
        "-- π source header\n"
        "CREATE FUNCTION app.sql_wrapper() RETURNS integer LANGUAGE sql AS $$\n"
        "  SELECT cron.schedule();\n"
        "$$;\n"
        "CREATE FUNCTION app.pl_wrapper() RETURNS integer LANGUAGE plpgsql AS $body$\n"
        "BEGIN\n"
        "  IF true THEN\n"
        "    PERFORM cron.schedule();\n"
        "  END IF;\n"
        "  RETURN 1;\n"
        "END\n"
        "$body$;\n"
        "CREATE PROCEDURE app.run_schedule() LANGUAGE sql AS $$\n"
        "  SELECT cron.schedule();\n"
        "$$;\n"
    )
    (app / "procedures.sql").write_text(sql_source)
    (app / "client.py").write_text('def run(db):\n    db.execute("SELECT app.pl_wrapper()")\n')
    corpus = CorpusConfig(
        tmp_path / "manifest.toml",
        (
            CorpusSnapshotConfig("pg", "postgres", "18", "postgres", pg),
            CorpusSnapshotConfig("ext", "pg_cron", "1", "extension", extension, ("pg",)),
            CorpusSnapshotConfig(
                "app",
                "application",
                "1",
                "application",
                app,
                ("ext",),
                SqlConfig(enabled=True, include=("**/*.sql",)),
            ),
        ),
    )
    output = tmp_path / "out"
    manifest = export_corpus(corpus, output)
    db = sqlite3.connect(output / manifest["registry"])
    try:
        routines = db.execute(
            "SELECT json_extract(fact,'$.name'),json_extract(fact,'$.kind') "
            "FROM routines WHERE snapshot_alias='app' ORDER BY ordinal"
        ).fetchall()
        assert routines == [
            ("sql_wrapper", "function"),
            ("pl_wrapper", "function"),
            ("run_schedule", "procedure"),
        ]
        body_evidence = db.execute(
            "SELECT e.fact,edge.status,edge.condition FROM evidence e "
            "JOIN fact_keys k ON k.snapshot_alias=e.snapshot_alias AND k.path=e.path "
            "AND k.ordinal=e.ordinal JOIN edges edge ON edge.target_key=k.key "
            "JOIN fact_keys owner ON owner.key=edge.source_key "
            "WHERE e.snapshot_alias='app' AND json_extract(e.fact,'$.origin')='routine_body' "
            "AND edge.kind='HAS_EVIDENCE' ORDER BY e.path,e.ordinal"
        ).fetchall()
        assert len(body_evidence) == 3
        pl_guarded = [
            (json.loads(fact), status, condition)
            for fact, status, condition in body_evidence
            if json.loads(fact).get("condition")
        ]
        assert len(pl_guarded) == 1
        guarded_fact, guarded_status, guarded_condition = pl_guarded[0]
        assert guarded_status == "conditional"
        assert guarded_condition == "app.pl_wrapper:conditional"
        source_line = (
            (app / "procedures.sql").read_bytes().splitlines()[guarded_fact["start_line"] - 1]
        )
        assert (
            source_line[guarded_fact["start_column"] : guarded_fact["end_column"]].decode()
            == guarded_fact["text"]
            == "cron.schedule"
        )
        assert db.execute(
            "SELECT 1 FROM edges e JOIN fact_keys sk ON sk.key=e.source_key "
            "JOIN evidence v ON v.snapshot_alias=sk.snapshot_alias AND v.path=sk.path "
            "AND v.ordinal=sk.ordinal JOIN fact_keys tk ON tk.key=e.target_key "
            "WHERE e.kind='INVOKES_ROUTINE' AND sk.snapshot_alias='app' "
            "AND json_extract(v.fact,'$.owner_qname')='app.pl_wrapper' "
            "AND tk.snapshot_alias='ext' AND e.status='conditional' LIMIT 1"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges WHERE kind='DESCRIBES_SQL_OBJECT' "
            "AND source_key IN (SELECT key FROM fact_keys WHERE snapshot_alias='app' "
            "AND table_name='routines') LIMIT 1"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges WHERE kind='INVOKES_ROUTINE' AND source_key IN "
            "(SELECT k.key FROM fact_keys k JOIN evidence e ON k.snapshot_alias=e.snapshot_alias "
            "AND k.path=e.path AND k.ordinal=e.ordinal WHERE k.snapshot_alias='app' "
            "AND json_extract(e.fact,'$.origin')='python_execute') AND status='exact' LIMIT 1"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges WHERE kind='BINDS_TO_NATIVE' AND status='exact' "
            "AND source_key IN (SELECT key FROM fact_keys WHERE snapshot_alias='ext' "
            "AND table_name='routines')"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM edges WHERE kind='CALLS_NATIVE' AND status='exact' "
            "AND source_key IN (SELECT key FROM fact_keys WHERE snapshot_alias='ext' "
            "AND table_name='symbols') AND target_key IN "
            "(SELECT key FROM fact_keys WHERE snapshot_alias='pg' AND table_name='symbols')"
        ).fetchone()
    finally:
        db.close()


def test_application_sql_disabled_policy_does_not_extract_routines(tmp_path):
    from codekg.corpus_config import CorpusSnapshotConfig
    from codekg.corpus_registry import create_native_registry, extract_snapshot_facts
    from codekg.sql_config import SqlConfig

    app = tmp_path / "app"
    app.mkdir()
    (app / "routines.sql").write_text(
        "CREATE FUNCTION app.hidden() RETURNS integer LANGUAGE sql AS 'SELECT 1';\n"
    )
    snapshot = CorpusSnapshotConfig(
        "app", "app", "1", "application", app, (), SqlConfig(enabled=False)
    )
    db = create_native_registry(tmp_path / "registry.sqlite")
    counts = extract_snapshot_facts(db, snapshot, tmp_path / "out")
    assert counts["routines"] == 0
    assert db.execute("SELECT count(*) FROM routines WHERE snapshot_alias='app'").fetchone()[0] == 0
    db.close()
