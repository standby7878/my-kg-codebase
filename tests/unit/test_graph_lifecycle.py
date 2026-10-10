import json
import subprocess

import pytest

from codekg.corpus_registry import create_native_registry
from codekg.graph_lifecycle import (
    GraphLifecycleError,
    _build_generation_import_args,
    activate_registry,
    open_graph_client,
    prepare_graph_candidate,
    rollback_registry,
)
from codekg.graph_registry import GraphRegistry


def _make_registry(root, filename="registry.toml", *, endpoint="APP_URI", prefix="APP", max_csv=0):
    generation = root / "generations/g1"
    generation.mkdir(parents=True, exist_ok=True)
    db_path = generation / "corpus.sqlite"
    if not db_path.exists():
        db = create_native_registry(db_path)
        db.execute("INSERT INTO metadata VALUES ('app.revision','r1')")
        db.commit()
        db.close()
        graph_dir = generation / "graph"
        graph_dir.mkdir()
        (graph_dir / "nodes.csv").write_text(
            "key:ID(CodeKG),repo_name,commit,root_path,:LABEL\nrepo,repo,r1,/repo,Repository\n"
        )
        (generation / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "codekg-corpus",
                    "registry": "corpus.sqlite",
                    "nodes": {"Repository": {"file": "graph/nodes.csv", "count": 1}},
                    "relationships": {},
                    "max_csv_field_size_bytes": max_csv,
                    "snapshots": [{"alias": "app", "role": "application", "revision": "r1"}],
                }
            )
        )
    path = root / filename
    path.write_text(
        "schema_version = 1\ndefault_application_graph = 'app'\n"
        "[[graphs]]\nid = 'app'\nkind = 'application'\n"
        "generation_manifest = 'generations/g1/manifest.json'\n"
        f"endpoint_env = '{endpoint}'\ncredential_env_prefix = '{prefix}'\n"
    )
    return path


class DockerRunner:
    def __init__(self, *, running="true", fail_server=False):
        self.running = running
        self.fail_server = fail_server
        self.calls = []
        self.token = ""

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "container ls" in joined or "volume ls" in joined:
            stdout = ""
            code = 0
        elif "network ls" in joined:
            stdout = "codekg-graphs"
            code = 0
        elif "volume create" in joined:
            self.token = next(arg.rsplit("=", 1)[-1] for arg in args if "candidate-token=" in arg)
            stdout = args[-1]
            code = 0
        elif "volume inspect" in joined:
            stdout = self.token
            code = 0
        elif "neo4j-admin" in joined:
            stdout = "imported"
            code = 0
        elif args[1:2] == ["run"]:
            stdout = "candidate-id"
            code = 8 if self.fail_server else 0
        elif "inspect" in joined and "State.Running" in joined:
            stdout = self.running
            code = 0
        elif "inspect" in joined and "candidate-token" in joined:
            stdout = self.token
            code = 0
        else:
            stdout = ""
            code = 0
        return subprocess.CompletedProcess(args, code, stdout, "secret diagnostic")


def test_candidate_failure_cleans_its_container_and_preserves_active_registry(tmp_path):
    active = _make_registry(tmp_path)
    active_bytes = active.read_bytes()
    registry = GraphRegistry.load(active)
    env_file = tmp_path / "neo4j.env"
    env_file.write_text("NEO4J_AUTH=neo4j/not-for-output\n")
    runner = DockerRunner(running="false")

    with pytest.raises(GraphLifecycleError, match="did not enter running"):
        prepare_graph_candidate(
            registry,
            "app",
            env_file=env_file,
            runner=runner,
            candidate_token="a1b2c3d4",
        )

    assert active.read_bytes() == active_bytes
    assert any(call[1:3] == ["rm", "--force"] for call in runner.calls)
    assert not any("volume" in call and "rm" in call for call in runner.calls)
    assert not any("stop" in call for call in runner.calls)
    assert all("not-for-output" not in " ".join(call) for call in runner.calls)
    commands = [" ".join(call) for call in runner.calls]
    importer = next(i for i, command in enumerate(commands) if "neo4j-admin" in command)
    server = next(
        i
        for i, call in enumerate(runner.calls)
        if call[1:2] == ["run"] and "neo4j-admin" not in call
    )
    assert importer < server
    import_call = runner.calls[importer]
    assert "--max-off-heap-memory=2G" in import_call
    assert "--max-memory=2G" not in import_call


@pytest.mark.parametrize(
    ("http_port", "bolt_port", "expected_advertised"),
    [
        (17474, None, "NEO4J_server_http_advertised__address=localhost:17474"),
        (None, 17687, "NEO4J_server_bolt_advertised__address=localhost:17687"),
        (
            18474,
            18687,
            "NEO4J_server_http_advertised__address=localhost:18474",
        ),
        (
            18474,
            18687,
            "NEO4J_server_bolt_advertised__address=localhost:18687",
        ),
    ],
)
def test_candidate_advertises_published_host_ports(
    tmp_path, http_port, bolt_port, expected_advertised
):
    registry = GraphRegistry.load(_make_registry(tmp_path))
    env_file = tmp_path / "neo4j.env"
    env_file.write_text("NEO4J_AUTH=none\n")
    runner = DockerRunner()

    prepare_graph_candidate(
        registry,
        "app",
        env_file=env_file,
        runner=runner,
        candidate_token="a1b2c3d4",
        http_port=http_port,
        bolt_port=bolt_port,
    )

    server_call = next(call for call in runner.calls if "--detach" in call)
    assert expected_advertised in server_call


def test_internal_only_candidate_keeps_default_advertised_addresses(tmp_path):
    registry = GraphRegistry.load(_make_registry(tmp_path))
    env_file = tmp_path / "neo4j.env"
    env_file.write_text("NEO4J_AUTH=none\n")
    runner = DockerRunner()

    candidate = prepare_graph_candidate(
        registry,
        "app",
        env_file=env_file,
        runner=runner,
        candidate_token="a1b2c3d4",
    )

    server_call = next(call for call in runner.calls if "--detach" in call)
    assert not any("advertised__address" in argument for argument in server_call)
    assert candidate.bolt_uri.startswith("bolt://codekg-app-")


def test_candidate_import_uses_exported_large_field_buffer_and_immutable_files(tmp_path):
    path = _make_registry(tmp_path, max_csv=5_300_007)
    registry = GraphRegistry.load(path)
    spec = registry.graphs["app"]
    command = _build_generation_import_args(
        spec,
        manifest_dir=spec.generation.manifest_path.parent,
        volume_name="candidate-data",
        docker="docker",
        memory="2G",
    )
    assert "--read-buffer-size=5365543" in command
    assert any(argument.startswith("--nodes=Repository=/codekg/import/") for argument in command)


def test_docker_run_error_is_sanitized_and_never_removes_a_volume(tmp_path):
    active = _make_registry(tmp_path)
    env_file = tmp_path / "neo4j.env"
    env_file.write_text("NEO4J_AUTH=neo4j/secret-value\n")
    runner = DockerRunner(fail_server=True)
    with pytest.raises(GraphLifecycleError) as error:
        prepare_graph_candidate(
            GraphRegistry.load(active),
            "app",
            env_file=env_file,
            runner=runner,
            candidate_token="01020304",
        )
    assert "secret-value" not in str(error.value)
    # A failed `docker run` returns no authoritative container id, so cleanup
    # must not guess by name and risk removing a preexisting collision.
    assert not any(call[1:3] == ["rm", "--force"] for call in runner.calls)
    assert not any("volume" in call and "rm" in call for call in runner.calls)


def test_activation_failure_leaves_active_bytes_unchanged_and_success_is_atomic(tmp_path):
    active = _make_registry(tmp_path, "active.toml")
    candidate = _make_registry(
        tmp_path, "candidate.toml", endpoint="CANDIDATE_URI", prefix="CANDIDATE"
    )
    active_bytes = active.read_bytes()

    def reject(_registry):
        raise RuntimeError("password-like diagnostic")

    with pytest.raises(GraphLifecycleError, match="backend validation failed") as error:
        activate_registry(candidate, active, backend_validator=reject)
    assert "password-like" not in str(error.value)
    assert active.read_bytes() == active_bytes

    result = activate_registry(candidate, active, backend_validator=lambda _registry: None)
    assert active.read_bytes() == candidate.read_bytes()
    assert (tmp_path / "active.toml.previous").read_bytes() == active_bytes
    assert result["mcp_restart_required"] is True
    assert result["mcp_restart_performed"] is False


def test_open_graph_client_supports_graph_local_auth_none(tmp_path, monkeypatch):
    registry = GraphRegistry.load(_make_registry(tmp_path))
    monkeypatch.setenv("APP_URI", "bolt://example.invalid:7687")
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "global-password-must-not-be-used")
    monkeypatch.setenv("APP_AUTH", "none")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    received = {}

    class Client:
        def __init__(self, **kwargs):
            received.update(kwargs)

    monkeypatch.setattr("codekg.neo4j_client.Neo4jClient", Client)
    open_graph_client(registry.graphs["app"])

    assert received["auth_enabled"] is False
    assert received["password"] is None


def test_open_graph_client_rejects_missing_graph_password_when_auth_unset(tmp_path, monkeypatch):
    registry = GraphRegistry.load(_make_registry(tmp_path))
    monkeypatch.setenv("APP_URI", "bolt://example.invalid:7687")
    monkeypatch.setenv("NEO4J_AUTH", "none")
    monkeypatch.setenv("NEO4J_PASSWORD", "global-password-must-not-be-used")
    monkeypatch.delenv("APP_AUTH", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    from codekg.graph_registry import GraphRegistryError

    with pytest.raises(GraphRegistryError, match="APP_PASSWORD"):
        open_graph_client(registry.graphs["app"])


def test_rollback_validates_before_replacing_current_active(tmp_path):
    active = _make_registry(tmp_path, "active.toml", endpoint="NEW_URI", prefix="NEW")
    previous = _make_registry(tmp_path, "active.toml.previous", endpoint="OLD_URI", prefix="OLD")
    current_bytes = active.read_bytes()

    with pytest.raises(GraphLifecycleError, match="rollback backend validation failed"):
        rollback_registry(
            active, backend_validator=lambda _registry: (_ for _ in ()).throw(ValueError())
        )
    assert active.read_bytes() == current_bytes

    result = rollback_registry(active, backend_validator=lambda _registry: None)
    assert active.read_bytes() == previous.read_bytes()
    assert result["mcp_restart_required"] is True


def test_activation_rejects_symlink_leaves_before_resolving_them(tmp_path):
    candidate = _make_registry(tmp_path, "candidate.toml")
    active_target = _make_registry(tmp_path, "target.toml", endpoint="TARGET_URI")
    active_link = tmp_path / "active.toml"
    active_link.symlink_to(active_target)
    target_bytes = active_target.read_bytes()

    with pytest.raises(GraphLifecycleError, match="symlinks"):
        activate_registry(candidate, active_link, backend_validator=lambda _registry: None)

    assert active_target.read_bytes() == target_bytes
    assert active_link.is_symlink()

    candidate_link = tmp_path / "candidate-link.toml"
    candidate_link.symlink_to(candidate)
    with pytest.raises(GraphLifecycleError, match="symlinks"):
        activate_registry(candidate_link, tmp_path / "new-active.toml")


def test_rollback_rejects_symlink_active_and_previous_paths(tmp_path):
    active_target = _make_registry(tmp_path, "active-target.toml")
    previous = _make_registry(tmp_path, "previous.toml", endpoint="OLD_URI")
    active_link = tmp_path / "active.toml"
    active_link.symlink_to(active_target)
    target_bytes = active_target.read_bytes()

    with pytest.raises(GraphLifecycleError, match="symlinks"):
        rollback_registry(active_link, previous_path=previous)
    assert active_target.read_bytes() == target_bytes

    active = _make_registry(tmp_path, "plain-active.toml")
    previous_link = tmp_path / "plain-active.toml.previous"
    previous_link.symlink_to(previous)
    active_bytes = active.read_bytes()
    with pytest.raises(GraphLifecycleError, match="symlinks"):
        rollback_registry(active)
    assert active.read_bytes() == active_bytes
