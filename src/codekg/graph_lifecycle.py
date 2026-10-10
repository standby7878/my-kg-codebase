"""Safe operator operations for preparing and activating isolated graph generations."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from codekg.csv_limits import (
    MAX_GENERATED_CSV_FIELD_SIZE_BYTES,
    NEO4J_DEFAULT_READ_BUFFER_BYTES,
    NEO4J_READ_BUFFER_MARGIN_BYTES,
    validate_csv_field_size,
)
from codekg.graph_artifacts import write_generation_marker
from codekg.graph_registry import (
    GraphRegistry,
    GraphRegistryError,
    GraphSpec,
    _graph_auth_settings,
)

NEO4J_COMMUNITY_IMAGE = "neo4j:5.26-community"
DEFAULT_GRAPH_NETWORK = "codekg-graphs"


class GraphLifecycleError(RuntimeError):
    """A candidate preparation, validation, or activation operation failed."""


@dataclass(frozen=True)
class GraphCandidate:
    graph_id: str
    generation_id: str
    container_name: str
    volume_name: str
    network_name: str
    network_alias: str
    bolt_uri: str
    image: str = NEO4J_COMMUNITY_IMAGE


def prepare_graph_candidate(
    registry: GraphRegistry,
    graph_id: str,
    *,
    env_file: Path,
    docker: str = "docker",
    network: str = DEFAULT_GRAPH_NETWORK,
    runner: Callable = subprocess.run,
    candidate_token: str | None = None,
    import_memory: str = "2G",
    heap_max: str = "1G",
    pagecache: str = "2G",
    http_port: int | None = None,
    bolt_port: int | None = None,
) -> GraphCandidate:
    """Start a fresh generation-specific Community container and volume.

    This never stops or replaces another container and never removes a data
    volume. The candidate is only placed on the named private Docker network;
    it is not activated in a registry or restarted into an MCP process.
    """
    try:
        spec = registry.graphs[graph_id]
    except KeyError as exc:
        raise GraphLifecycleError(f"unknown graph id: {graph_id}") from exc
    env_file = Path(env_file).expanduser().resolve()
    if not env_file.is_file() or not os.access(env_file, os.R_OK):
        raise GraphLifecycleError("Neo4j Docker env file must be an existing readable file")
    if not _has_neo4j_auth(env_file):
        raise GraphLifecycleError("Neo4j Docker env file must define NEO4J_AUTH")
    token = candidate_token or secrets.token_hex(4)
    if not re.fullmatch(r"[a-fA-F0-9]{4,32}", token):
        raise GraphLifecycleError("candidate token must be 4-32 hexadecimal characters")
    for memory in (import_memory, heap_max, pagecache):
        if not re.fullmatch(r"[1-9][0-9]*[MG]", memory):
            raise GraphLifecycleError("memory limits must be a positive integer followed by M or G")
    for port in (http_port, bolt_port):
        if port is not None and (
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        ):
            raise GraphLifecycleError("published ports must be between 1 and 65535")
    slug = _slug(graph_id)
    generation_suffix = spec.generation.generation_id.rsplit(":", 1)[-1][:12]
    base = f"codekg-{slug}-{generation_suffix}-{token.lower()}"
    container_name = base
    volume_name = f"{base}-data"
    network_alias = base
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", network):
        raise GraphLifecycleError("invalid Docker network name")

    existing_containers = _docker_output(
        [
            docker,
            "container",
            "ls",
            "--all",
            "--filter",
            f"name=^{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        runner,
    )
    if container_name in existing_containers.splitlines():
        raise GraphLifecycleError("generation-specific candidate container already exists")
    existing_volumes = _docker_output(
        [docker, "volume", "ls", "--filter", f"name=^{volume_name}$", "--format", "{{.Name}}"],
        runner,
    )
    if volume_name in existing_volumes.splitlines():
        raise GraphLifecycleError("generation-specific data volume already exists; refusing reuse")

    networks = _docker_output(
        [docker, "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"],
        runner,
    )
    if network not in networks.splitlines():
        _docker_output([docker, "network", "create", network], runner)

    # Docker volume create is additive. Verify its ownership label after the
    # create to close the inspect/create race without deleting a collision.
    _docker_output(
        [
            docker,
            "volume",
            "create",
            "--label",
            "org.codekg.managed=true",
            "--label",
            f"org.codekg.candidate-token={token.lower()}",
            volume_name,
        ],
        runner,
    )
    volume_owner = _docker_output(
        [
            docker,
            "volume",
            "inspect",
            "--format",
            '{{ index .Labels "org.codekg.candidate-token" }}',
            volume_name,
        ],
        runner,
    )
    if volume_owner != token.lower():
        raise GraphLifecycleError("candidate volume name collided with an unowned volume")
    manifest_dir = spec.generation.manifest_path.parent.resolve()
    import_args = _build_generation_import_args(
        spec,
        manifest_dir=manifest_dir,
        volume_name=volume_name,
        docker=docker,
        memory=import_memory,
    )
    _docker_output(import_args, runner)
    container_id: str | None = None
    try:
        run_args = [
            docker,
            "run",
            "--detach",
            "--pull=missing",
            "--name",
            container_name,
            "--network",
            network,
            "--network-alias",
            network_alias,
            "--label",
            "org.codekg.managed=true",
            "--label",
            f"org.codekg.graph-id={graph_id}",
            "--label",
            f"org.codekg.generation-id={spec.generation.generation_id}",
            "--label",
            f"org.codekg.candidate-token={token.lower()}",
            "--mount",
            f"type=volume,source={volume_name},target=/data",
            "--env-file",
            str(env_file),
            "--env",
            f"NEO4J_server_memory_heap_max__size={heap_max}",
            "--env",
            f"NEO4J_server_memory_pagecache_size={pagecache}",
        ]
        if http_port is not None:
            run_args.extend(["--publish", f"127.0.0.1:{http_port}:7474"])
        if bolt_port is not None:
            run_args.extend(["--publish", f"127.0.0.1:{bolt_port}:7687"])
        run_args.append(NEO4J_COMMUNITY_IMAGE)
        container_id = _docker_output(run_args, runner)
        if not container_id:
            raise GraphLifecycleError("Docker did not return a candidate container id")
        running = _docker_output(
            [docker, "inspect", "--format", "{{.State.Running}}", container_id], runner
        )
        if running.strip() != "true":
            raise GraphLifecycleError("candidate Neo4j container did not enter running state")
    except Exception:
        # Remove only the container created by this invocation. Its uniquely
        # named data volume is intentionally retained for operator inspection.
        if container_id is not None:
            _docker_cleanup_container(docker, container_id, token.lower(), runner)
        raise
    return GraphCandidate(
        graph_id,
        spec.generation.generation_id,
        container_name,
        volume_name,
        network,
        network_alias,
        f"bolt://127.0.0.1:{bolt_port}"
        if bolt_port is not None
        else f"bolt://{network_alias}:7687",
    )


def _docker_output(args: list[str], runner: Callable) -> str:
    try:
        result = runner(args, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise GraphLifecycleError(f"could not execute Docker CLI: {exc}") from exc
    if result.returncode != 0:
        # Do not surface stderr: some Docker diagnostics can echo environment
        # values or operator-provided command-line fragments.
        raise GraphLifecycleError(f"Docker command failed with exit status {result.returncode}")
    return result.stdout.strip()


def _docker_cleanup_container(docker: str, container_id: str, token: str, runner: Callable) -> None:
    try:
        result = runner(
            [
                docker,
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.codekg.candidate-token" }}',
                container_id,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == token:
            runner(
                [docker, "rm", "--force", container_id],
                check=False,
                capture_output=True,
                text=True,
            )
    except OSError:
        pass


def _build_generation_import_args(
    spec: GraphSpec, *, manifest_dir: Path, volume_name: str, docker: str, memory: str
) -> list[str]:
    """Build offline import arguments for the exact frozen corpus CSV bundle."""
    manifest = spec.generation.manifest
    graph_groups = []
    for section, option in (("nodes", "--nodes"), ("relationships", "--relationships")):
        groups = manifest.get(section)
        if not isinstance(groups, Mapping):
            raise GraphLifecycleError(f"generation manifest has no {section} groups")
        for label, entry in sorted(groups.items()):
            if not isinstance(label, str) or not isinstance(entry, Mapping):
                raise GraphLifecycleError(f"generation manifest has a malformed {section} group")
            raw_paths = entry.get("files")
            if raw_paths is None:
                raw_paths = [entry.get("file")]
            if not isinstance(raw_paths, (list, tuple)) or not raw_paths:
                raise GraphLifecycleError(f"generation manifest has no files for {label}")
            paths = []
            for value in raw_paths:
                if not isinstance(value, str) or not value:
                    raise GraphLifecycleError(
                        f"generation manifest has an invalid file for {label}"
                    )
                path = (manifest_dir / value).resolve()
                if not path.is_relative_to(manifest_dir) or not path.is_file():
                    raise GraphLifecycleError(
                        f"generation artifact is missing or escapes its bundle: {label}"
                    )
                relative = path.relative_to(manifest_dir).as_posix()
                if "," in relative:
                    raise GraphLifecycleError(
                        "Neo4j import does not support commas in artifact names"
                    )
                paths.append(f"/codekg/import/{relative}")
            graph_groups.append((option, label, ",".join(paths)))
    if not graph_groups:
        raise GraphLifecycleError("generation manifest contains no graph CSV files")
    args = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--mount",
        f"type=volume,source={volume_name},target=/data",
        "--mount",
        f"type=bind,source={manifest_dir},target=/codekg/import,readonly",
        "--entrypoint",
        "neo4j-admin",
        NEO4J_COMMUNITY_IMAGE,
        "database",
        "import",
        "full",
        "neo4j",
        "--id-type=string",
        "--multiline-fields=true",
        f"--max-off-heap-memory={memory}",
    ]
    try:
        max_field_size = validate_csv_field_size(
            spec.generation.manifest.get("max_csv_field_size_bytes", 0)
        )
    except ValueError as exc:
        raise GraphLifecycleError(
            "generation manifest has an invalid CSV field-size bound"
        ) from exc
    if max_field_size > NEO4J_DEFAULT_READ_BUFFER_BYTES:
        read_buffer_size = max_field_size + NEO4J_READ_BUFFER_MARGIN_BYTES
        if read_buffer_size > (MAX_GENERATED_CSV_FIELD_SIZE_BYTES + NEO4J_READ_BUFFER_MARGIN_BYTES):
            raise GraphLifecycleError("generation CSV field size exceeds Neo4j import bound")
        args.append(f"--read-buffer-size={read_buffer_size}")
    args.extend(f"{option}={label}={paths}" for option, label, paths in graph_groups)
    return args


def _has_neo4j_auth(path: Path) -> bool:
    try:
        return any(
            key.strip() == "NEO4J_AUTH" and bool(value.strip())
            for line in path.read_text().splitlines()
            for key, separator, value in (line.partition("="),)
            if separator
        )
    except OSError:
        return False


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    if not slug:
        raise GraphLifecycleError("graph id cannot form a Docker resource name")
    return slug[:40]


def open_graph_client(spec: GraphSpec):
    """Construct a direct explicit client from a graph spec, without GraphHandle."""
    from codekg.neo4j_client import Neo4jClient

    uri = _required_env(spec.endpoint_env)
    prefix = spec.credential_env_prefix
    username, password, auth_enabled = _graph_auth_settings(prefix)
    database = os.environ.get(f"{prefix}_DATABASE", "neo4j")
    return Neo4jClient(
        uri=uri,
        username=username,
        password=password,
        auth_enabled=auth_enabled,
        database=database,
        connection_timeout_seconds=3.0,
        max_transaction_retry_time_seconds=2.0,
    )


def bootstrap_graph(registry: GraphRegistry, graph_id: str) -> dict[str, str]:
    """Apply schema and the exact generation marker to the explicitly selected graph."""
    try:
        spec = registry.graphs[graph_id]
    except KeyError as exc:
        raise GraphLifecycleError(f"unknown graph id: {graph_id}") from exc
    client = open_graph_client(spec)
    try:
        client.verify()
        verify_import_counts(client, spec)
        _validate_existing_marker(client, graph_id, spec.generation.generation_id)
        from codekg.schema.bootstrap import bootstrap_schema

        bootstrap_schema(client)
        write_generation_marker(client, graph_id, spec.generation.generation_id)
        verify_graph_marker(client, graph_id, spec.generation.generation_id)
    finally:
        client.close()
    return {"graph_id": graph_id, "generation_id": spec.generation.generation_id}


def verify_graph_marker(client, graph_id: str, generation_id: str) -> None:
    rows = client.execute_read(
        "MATCH (m:CodeKGGeneration) RETURN m.graph_id AS graph_id, "
        "m.generation_id AS generation_id LIMIT 2",
        max_rows=2,
        operation="verify_graph_generation",
    )
    if (
        len(rows) != 1
        or rows[0].get("graph_id") != graph_id
        or rows[0].get("generation_id") != generation_id
    ):
        raise GraphLifecycleError(f"backend marker does not match graph {graph_id!r}")


def _validate_existing_marker(client, graph_id: str, generation_id: str) -> None:
    """Reject wrong or ambiguous markers before any bootstrap schema writes."""
    rows = client.execute_read(
        "MATCH (m:CodeKGGeneration) RETURN m.graph_id AS graph_id, "
        "m.generation_id AS generation_id LIMIT 2",
        max_rows=2,
        operation="inspect_graph_generation_before_bootstrap",
    )
    if rows and (
        len(rows) != 1
        or rows[0].get("graph_id") != graph_id
        or rows[0].get("generation_id") != generation_id
    ):
        raise GraphLifecycleError(
            "backend already carries a different or ambiguous generation marker"
        )


def verify_import_counts(client, spec: GraphSpec) -> None:
    """Check offline-imported labels and relationship types before marking a KG."""
    expected_nodes = _expected_counts(spec.generation.manifest.get("nodes", {}))
    expected_relationships = _expected_counts(spec.generation.manifest.get("relationships", {}))
    node_rows = client.execute_read(
        "MATCH (n) UNWIND labels(n) AS label "
        "WITH label WHERE label <> 'CodeKGGeneration' "
        "RETURN label, count(*) AS count",
        max_rows=1000,
        operation="verify_imported_node_counts",
    )
    relationship_rows = client.execute_read(
        "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count",
        max_rows=1000,
        operation="verify_imported_relationship_counts",
    )
    actual_nodes = {str(row["label"]): int(row["count"]) for row in node_rows}
    actual_relationships = {str(row["type"]): int(row["count"]) for row in relationship_rows}
    if actual_nodes != expected_nodes:
        raise GraphLifecycleError("imported node counts do not match the frozen graph manifest")
    if actual_relationships != expected_relationships:
        raise GraphLifecycleError(
            "imported relationship counts do not match the frozen graph manifest"
        )


def _expected_counts(groups) -> dict[str, int]:
    if not isinstance(groups, Mapping):
        raise GraphLifecycleError("generation manifest graph groups are malformed")
    counts = {}
    for label, entry in groups.items():
        if not isinstance(label, str) or not isinstance(entry, Mapping):
            raise GraphLifecycleError("generation manifest graph groups are malformed")
        count = entry.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GraphLifecycleError("generation manifest contains an invalid graph count")
        if count:
            counts[label] = count
    return counts


def validate_registry_backends(registry: GraphRegistry) -> None:
    """Verify each configured endpoint serves its exact registered generation."""
    for spec in registry.graphs.values():
        client = open_graph_client(spec)
        try:
            client.verify()
            verify_graph_marker(client, spec.id, spec.generation.generation_id)
        finally:
            client.close()


def check_graph_registry(
    registry: GraphRegistry, *, include_backends: bool = False
) -> dict[str, object]:
    """Validate pinned local metadata, optionally checking every live backend."""
    if include_backends:
        validate_registry_backends(registry)
    return {
        "registry": str(registry.path),
        "graphs": registry.list_graphs(),
        "contexts": len(registry.contexts),
        "backends_checked": include_backends,
    }


def activate_registry(
    candidate_path: str | Path,
    active_path: str | Path,
    *,
    backend_validator: Callable[[GraphRegistry], None] = validate_registry_backends,
) -> dict[str, object]:
    """Validate and atomically activate a sibling registry file.

    Relative manifest paths retain their meaning because candidate and active
    files must share a directory. The prior active file is retained at
    ``<active>.previous``. This does not restart MCP; the result says so.
    """
    candidate_lexical = _absolute_path_without_symlinks(candidate_path)
    active_lexical = _absolute_path_without_symlinks(active_path)
    previous_lexical = active_lexical.with_name(active_lexical.name + ".previous")
    _reject_symlink_path(previous_lexical)
    candidate, active = candidate_lexical.resolve(), active_lexical.resolve()
    previous = previous_lexical.resolve()
    if candidate.parent != active.parent:
        raise GraphLifecycleError("candidate registry must be a sibling of the active registry")
    if candidate == active:
        raise GraphLifecycleError("registry activation paths must be distinct regular files")
    try:
        candidate_registry = GraphRegistry.load(candidate)
    except Exception as exc:
        raise GraphLifecycleError(f"candidate registry validation failed: {exc}") from None
    try:
        backend_validator(candidate_registry)
    except Exception:
        raise GraphLifecycleError("candidate backend validation failed") from None
    payload = candidate.read_bytes()
    old_payload = active.read_bytes() if active.exists() else None
    if old_payload is not None:
        _atomic_write(previous, old_payload)
    _atomic_write(active, payload)
    return {
        "active_registry": str(active),
        "previous_registry": str(previous) if old_payload is not None else None,
        "generation_count": len(candidate_registry.graphs),
        "mcp_restart_required": True,
        "mcp_restart_performed": False,
    }


def rollback_registry(
    active_path: str | Path,
    *,
    previous_path: str | Path | None = None,
    backend_validator: Callable[[GraphRegistry], None] = validate_registry_backends,
) -> dict[str, object]:
    """Restore a previously validated registry after validating its backends."""
    active_lexical = _absolute_path_without_symlinks(active_path)
    active = active_lexical.resolve()
    previous_lexical = (
        _absolute_path_without_symlinks(previous_path)
        if previous_path
        else active_lexical.with_name(active_lexical.name + ".previous")
    )
    _reject_symlink_path(previous_lexical)
    previous = previous_lexical.resolve()
    if active.parent != previous.parent:
        raise GraphLifecycleError("rollback registry paths must be sibling regular files")
    try:
        previous_registry = GraphRegistry.load(previous)
    except Exception as exc:
        raise GraphLifecycleError(f"rollback registry validation failed: {exc}") from None
    try:
        backend_validator(previous_registry)
    except Exception:
        raise GraphLifecycleError("rollback backend validation failed") from None
    payload = previous.read_bytes()
    _atomic_write(active, payload)
    return {
        "active_registry": str(active),
        "restored_graphs": len(previous_registry.graphs),
        "mcp_restart_required": True,
        "mcp_restart_performed": False,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _absolute_path_without_symlinks(path: str | Path) -> Path:
    """Normalize lexically and reject symlinks before resolving any destination."""
    raw = Path(os.path.abspath(os.fspath(path)))
    _reject_symlink_path(raw)
    return raw


def _reject_symlink_path(path: Path) -> None:
    """Reject a symlink in any existing path component, including the leaf."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                raise GraphLifecycleError("registry paths must not contain symlinks")
        except OSError:
            raise GraphLifecycleError("registry paths could not be inspected safely") from None


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise GraphRegistryError(f"required environment variable {name} is not set")
    return value
