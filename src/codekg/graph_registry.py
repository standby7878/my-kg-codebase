"""Strict versioned registry for immutable, independently served CodeKGs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


class GraphRegistryError(ValueError):
    """Invalid graph registry, generation, or graph selection."""


@dataclass(frozen=True)
class GraphGeneration:
    graph_id: str
    kind: str
    generation_id: str
    manifest_path: Path
    corpus_path: Path
    manifest: Mapping[str, Any] = field(repr=False, compare=False)
    snapshots: tuple[Mapping[str, Any], ...] = field(repr=False)


@dataclass(frozen=True)
class GraphSpec:
    id: str
    kind: str
    generation_manifest: Path
    endpoint_env: str
    credential_env_prefix: str
    generation: GraphGeneration
    zvec_path: Path | None = None


@dataclass(frozen=True)
class GraphContext:
    """Symbolic bridge context; ``visible_extensions`` contains graph snapshot aliases."""

    id: str
    application_graph: str
    database_graph: str
    application_database: str
    visible_extensions: tuple[str, ...]
    search_path: tuple[str, ...]


@dataclass(frozen=True)
class EntityRef:
    graph_id: str
    generation_id: str
    local_key: str


class GraphHandle:
    """Request-scoped exact generation with lazily allocated backend clients."""

    def __init__(self, spec: GraphSpec, *, zvec_path: Path | None = None):
        self.spec = spec
        self.graph_id = spec.id
        self.generation_id = spec.generation.generation_id
        self.generation = spec.generation
        self.corpus_path = spec.generation.corpus_path
        self.zvec_path = zvec_path
        self._client = None
        self._catalog = None
        self._lock = threading.Lock()

    @property
    def client(self):
        return self._ensure_client(2.0)

    def _ensure_client(self, timeout_seconds: float):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                from codekg.neo4j_client import Neo4jClient

                uri = _required_env(self.spec.endpoint_env)
                prefix = self.spec.credential_env_prefix
                password = _required_env(f"{prefix}_PASSWORD")
                username = os.environ.get(f"{prefix}_USERNAME", "neo4j")
                database = os.environ.get(f"{prefix}_DATABASE", "neo4j")
                client = Neo4jClient(
                    uri=uri,
                    username=username,
                    password=password,
                    database=database,
                    connection_timeout_seconds=min(2.0, timeout_seconds),
                    max_transaction_retry_time_seconds=0.0,
                )
                try:
                    self._verify_marker(client, timeout_seconds=timeout_seconds)
                except Exception:
                    client.close()
                    raise
                self._client = client
        return self._client

    def verify_generation(self, *, timeout_seconds: float = 2.0) -> None:
        """Revalidate the pinned backend marker at a request boundary."""
        if self._client is None:
            self._ensure_client(timeout_seconds)
        else:
            self._verify_marker(self._client, timeout_seconds=timeout_seconds)

    def _verify_marker(self, client, *, timeout_seconds: float = 2.0) -> None:
        rows = client.execute_read(
            "MATCH (m:CodeKGGeneration) RETURN m.graph_id AS graph_id, "
            "m.generation_id AS generation_id LIMIT 2",
            max_rows=2,
            timeout_seconds=min(2.0, timeout_seconds),
            operation="verify_graph_generation",
        )
        if (
            len(rows) != 1
            or rows[0].get("graph_id") != self.graph_id
            or rows[0].get("generation_id") != self.generation_id
        ):
            raise GraphRegistryError(
                f"Neo4j generation marker mismatch for graph {self.graph_id!r}"
            )

    @property
    def catalog(self):
        if self._catalog is None:
            from codekg.graph_catalog import GraphCatalog

            self._catalog = GraphCatalog(self)
        return self._catalog

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def ref(self, local_key: str) -> EntityRef:
        if not isinstance(local_key, str) or not local_key:
            raise ValueError("local_key must be a non-empty string")
        return EntityRef(self.graph_id, self.generation_id, local_key)


class GraphRegistry:
    """Validated immutable registry; metadata reads never connect to Neo4j."""

    def __init__(
        self,
        path: Path,
        graphs: dict[str, GraphSpec],
        contexts: dict[str, GraphContext],
        default: str | None,
    ):
        self.path = path
        self.graphs = MappingProxyType(dict(graphs))
        self.contexts = MappingProxyType(dict(contexts))
        self.default_application_graph = default

    @classmethod
    def load(cls, path: str | Path | None = None) -> GraphRegistry:
        raw_path = path or os.environ.get("CODEKG_GRAPH_REGISTRY")
        if not raw_path:
            raise GraphRegistryError(
                "graph registry path is required (or set CODEKG_GRAPH_REGISTRY)"
            )
        registry_path = Path(raw_path).expanduser().resolve()
        try:
            data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise GraphRegistryError(f"cannot read graph registry: {exc}") from exc
        _keys(
            data, {"schema_version", "default_application_graph", "graphs", "contexts"}, "registry"
        )
        if isinstance(data.get("schema_version"), bool) or data.get("schema_version") != 1:
            raise GraphRegistryError("registry schema_version must be 1")
        raw_graphs = data.get("graphs")
        raw_contexts = data.get("contexts", [])
        if not isinstance(raw_graphs, list) or not raw_graphs:
            raise GraphRegistryError("registry must define at least one [[graphs]] entry")
        if len(raw_graphs) > 32:
            raise GraphRegistryError("registry supports at most 32 graph handles per process")
        if not isinstance(raw_contexts, list):
            raise GraphRegistryError("contexts must be an array of tables")
        if len(raw_contexts) > 256:
            raise GraphRegistryError("registry supports at most 256 bridge contexts")
        graphs: dict[str, GraphSpec] = {}
        for i, entry in enumerate(raw_graphs):
            where = f"graphs[{i}]"
            _keys(
                entry,
                {
                    "id",
                    "kind",
                    "generation_manifest",
                    "endpoint_env",
                    "credential_env_prefix",
                    "zvec_path",
                },
                where,
            )
            gid, kind = entry.get("id"), entry.get("kind")
            if not _graph_id(gid) or kind not in {"application", "database"}:
                raise GraphRegistryError(
                    f"{where} requires a valid id and kind application/database"
                )
            if gid in graphs:
                raise GraphRegistryError(f"duplicate graph id: {gid}")
            manifest = _path_field(entry, "generation_manifest", registry_path.parent, where)
            endpoint_env = _env_name(entry.get("endpoint_env"), f"{where}.endpoint_env")
            prefix = _env_name(entry.get("credential_env_prefix"), f"{where}.credential_env_prefix")
            generation = _load_generation(gid, kind, manifest)
            zvec_path = (
                _path_field(entry, "zvec_path", manifest.parent, where)
                if entry.get("zvec_path")
                else None
            )
            if zvec_path is not None and kind != "application":
                raise GraphRegistryError(
                    f"{where}.zvec_path is supported only for application graphs"
                )
            graphs[gid] = GraphSpec(
                gid, kind, manifest, endpoint_env, prefix, generation, zvec_path
            )
        default = data.get("default_application_graph")
        application_graphs = [graph.id for graph in graphs.values() if graph.kind == "application"]
        if (application_graphs or default is not None) and (
            default not in graphs or graphs[default].kind != "application"
        ):
            raise GraphRegistryError(
                "default_application_graph must name a registered application graph"
            )
        contexts: dict[str, GraphContext] = {}
        for i, entry in enumerate(raw_contexts):
            where = f"contexts[{i}]"
            _keys(
                entry,
                {
                    "id",
                    "application_graph",
                    "database_graph",
                    "application_database",
                    "visible_extensions",
                    "search_path",
                },
                where,
            )
            cid = entry.get("id")
            app, db = entry.get("application_graph"), entry.get("database_graph")
            if not _graph_id(cid) or cid in contexts:
                raise GraphRegistryError(f"{where}.id must be valid and unique")
            if app not in graphs or graphs[app].kind != "application":
                raise GraphRegistryError(
                    f"{where}.application_graph must name an application graph"
                )
            if db not in graphs or graphs[db].kind != "database":
                raise GraphRegistryError(f"{where}.database_graph must name a database graph")
            appdb = entry.get("application_database", "primary")
            extensions = _string_tuple(
                entry.get("visible_extensions", []), f"{where}.visible_extensions"
            )
            search_path = _string_tuple(
                entry.get("search_path", ["public", "pg_catalog"]), f"{where}.search_path"
            )
            available = {s.get("alias") for s in graphs[db].generation.snapshots}
            missing = set(extensions) - available
            if missing:
                raise GraphRegistryError(
                    f"{where} selects extensions absent from database generation: {sorted(missing)}"
                )
            snapshot_by_alias = {s["alias"]: s for s in graphs[db].generation.snapshots}
            non_extensions = [
                name for name in extensions if snapshot_by_alias[name]["role"] != "extension"
            ]
            if non_extensions:
                raise GraphRegistryError(
                    f"{where}.visible_extensions must select extension snapshots only"
                )
            selected = set(extensions)
            pending = list(extensions)
            pg_alias = next(
                s["alias"] for s in graphs[db].generation.snapshots if s["role"] == "postgres"
            )
            while pending:
                current = pending.pop()
                for dependency in snapshot_by_alias[current].get("dependencies", []):
                    if dependency != pg_alias and dependency not in selected:
                        raise GraphRegistryError(
                            f"{where} omits materialized extension dependency {dependency!r}"
                        )
            if not isinstance(appdb, str) or not appdb.strip():
                raise GraphRegistryError(f"{where}.application_database must be non-empty")
            contexts[cid] = GraphContext(cid, app, db, appdb, extensions, search_path)
        return cls(registry_path, graphs, contexts, default)

    def list_graphs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": g.id,
                "kind": g.kind,
                "generation_id": g.generation.generation_id,
                "snapshots": tuple(
                    {"alias": s.get("alias"), "role": s.get("role"), "revision": s.get("revision")}
                    for s in g.generation.snapshots
                ),
            }
            for g in self.graphs.values()
        ]

    def handle(self, graph_id: str | None = None, *, zvec_path: Path | None = None) -> GraphHandle:
        selected = graph_id or self.default_application_graph
        if selected is None:
            raise GraphRegistryError("graph_id is required without a default application graph")
        try:
            spec = self.graphs[selected]
            return GraphHandle(
                spec, zvec_path=zvec_path if zvec_path is not None else spec.zvec_path
            )
        except KeyError as exc:
            raise GraphRegistryError(f"unknown graph id: {selected}") from exc


def _load_generation(graph_id: str, kind: str, path: Path) -> GraphGeneration:
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphRegistryError(f"cannot read generation manifest {path}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != "codekg-corpus"
        or isinstance(manifest.get("version"), bool)
        or manifest.get("version") != 1
    ):
        raise GraphRegistryError(f"{path} is not a supported codekg-corpus generation manifest")
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise GraphRegistryError(f"{path} has no snapshot identities")
    aliases, revisions = set(), set()
    pg_count = 0
    roles = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not all(
            isinstance(snapshot.get(k), str) and snapshot[k] for k in ("alias", "role", "revision")
        ):
            raise GraphRegistryError(f"{path} contains malformed snapshot identity")
        if snapshot["alias"] in aliases:
            raise GraphRegistryError(f"{path} contains duplicate snapshot aliases")
        aliases.add(snapshot["alias"])
        revisions.add(snapshot["revision"])
        roles.add(snapshot["role"])
        if snapshot["role"] not in {"application", "postgres", "extension"}:
            raise GraphRegistryError(f"{path} contains invalid snapshot role")
        if snapshot["role"] == "postgres":
            pg_count += 1
        dependencies = snapshot.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dep, str) or not dep for dep in dependencies
        ):
            raise GraphRegistryError(f"{path} contains malformed snapshot dependencies")
    if any(
        dep not in aliases for snapshot in snapshots for dep in snapshot.get("dependencies", [])
    ):
        raise GraphRegistryError(f"{path} contains a dependency on an absent snapshot")
    if kind == "database" and pg_count != 1:
        raise GraphRegistryError(
            f"database graph {graph_id!r} must contain exactly one PostgreSQL revision"
        )
    if kind == "application" and roles != {"application"}:
        raise GraphRegistryError(
            f"application graph {graph_id!r} may contain application snapshots only"
        )
    if kind == "application" and any(
        dep not in {s["alias"] for s in snapshots if s["role"] == "application"}
        for snapshot in snapshots
        for dep in snapshot.get("dependencies", [])
    ):
        raise GraphRegistryError(
            "application graph dependencies must remain within application snapshots"
        )
    if kind == "database" and roles - {"postgres", "extension"}:
        raise GraphRegistryError(
            f"database graph {graph_id!r} may contain PostgreSQL and extension snapshots only"
        )
    if kind == "database":
        pg_snapshot = next(s for s in snapshots if s["role"] == "postgres")
        pg_alias = pg_snapshot["alias"]
        if pg_snapshot.get("dependencies", []):
            raise GraphRegistryError("PostgreSQL snapshot cannot depend on another snapshot")
        by_alias = {s["alias"]: s for s in snapshots}
        for snapshot in snapshots:
            if snapshot["role"] != "extension":
                continue
            pending, seen = list(snapshot.get("dependencies", [])), set()
            while pending:
                dep = pending.pop()
                if dep in seen:
                    continue
                seen.add(dep)
                pending.extend(by_alias[dep].get("dependencies", []))
            if pg_alias not in seen:
                raise GraphRegistryError(
                    f"extension {snapshot['alias']!r} must depend on the PostgreSQL snapshot"
                )
    base = path.parent
    registry_rel = manifest.get("registry")
    if not isinstance(registry_rel, str) or not registry_rel:
        raise GraphRegistryError(f"{path} does not identify corpus.sqlite")
    corpus = (base / registry_rel).resolve()
    if corpus.name != "corpus.sqlite" or not corpus.is_file():
        raise GraphRegistryError(f"generation corpus registry is missing: {corpus}")
    if not Path(registry_rel).is_absolute() and not corpus.is_relative_to(base.resolve()):
        raise GraphRegistryError("generation registry path escapes its manifest directory")
    try:
        connection = sqlite3.connect(f"file:{corpus.as_posix()}?mode=ro&immutable=1", uri=True)
        stored = dict(
            connection.execute("SELECT key,value FROM metadata WHERE key LIKE '%.revision'")
        )
        connection.close()
    except sqlite3.Error as exc:
        raise GraphRegistryError(f"cannot validate corpus snapshot revisions: {exc}") from exc
    expected = {f"{s['alias']}.revision": s["revision"] for s in snapshots}
    if stored != expected:
        raise GraphRegistryError(
            f"{path} snapshot revisions do not match its corpus.sqlite registry"
        )
    # The exact manifest bytes and source revision vector identify this exported generation.
    digest = hashlib.sha256(
        raw
        + json.dumps(
            sorted((s["alias"], s["revision"]) for s in snapshots), separators=(",", ":")
        ).encode()
    ).hexdigest()
    generation_id = f"{graph_id}:{digest[:32]}"
    immutable_manifest = _immutable(manifest)
    immutable_snapshots = tuple(_immutable(snapshot) for snapshot in snapshots)
    return GraphGeneration(
        graph_id, kind, generation_id, path, corpus, immutable_manifest, immutable_snapshots
    )


def _immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(item) for item in value)
    return value


def _keys(value: Any, allowed: set[str], where: str) -> None:
    if not isinstance(value, dict):
        raise GraphRegistryError(f"{where} must be a TOML table")
    unknown = set(value) - allowed
    if unknown:
        raise GraphRegistryError(f"unknown {where} key(s): {', '.join(sorted(unknown))}")


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value


def _graph_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value, flags=re.ASCII) is not None
    )


def _path_field(entry: dict, key: str, parent: Path, where: str) -> Path:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise GraphRegistryError(f"{where}.{key} must be a path")
    path = Path(value)
    return (parent / path).resolve() if not path.is_absolute() else path.resolve()


def _env_name(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value, flags=re.ASCII) is None
    ):
        raise GraphRegistryError(f"{where} must be an environment-variable name")
    return value


def _string_tuple(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GraphRegistryError(f"{where} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise GraphRegistryError(f"{where} entries must be unique")
    return tuple(value)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise GraphRegistryError(f"required environment variable {name} is not set")
    return value
