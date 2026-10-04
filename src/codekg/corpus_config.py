"""Strict manifest parsing for explicitly versioned source corpora."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from codekg.sql_config import SqlConfig

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class CorpusSnapshotConfig:
    alias: str
    logical_repo: str
    version: str
    role: str
    path: Path
    dependencies: tuple[str, ...] = ()
    sql_config: SqlConfig | None = None
    max_file_bytes: int = _MAX_FILE_BYTES


@dataclass(frozen=True)
class CorpusConfig:
    manifest_path: Path
    snapshots: tuple[CorpusSnapshotConfig, ...]

    @property
    def by_alias(self) -> dict[str, CorpusSnapshotConfig]:
        return {snapshot.alias: snapshot for snapshot in self.snapshots}

    def dependency_closure(self, alias: str) -> tuple[str, ...]:
        """Return the explicit transitive dependency set in manifest order."""
        by_alias = self.by_alias
        if alias not in by_alias:
            raise ValueError(f"unknown snapshot alias: {alias}")
        seen: set[str] = set()
        pending = list(by_alias[alias].dependencies)
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(by_alias[current].dependencies)
        return tuple(item.alias for item in self.snapshots if item.alias in seen)


def effective_sql_config(snapshot: CorpusSnapshotConfig) -> SqlConfig:
    """Default server/extension snapshots to SQL+template ingestion."""
    if snapshot.sql_config is not None:
        return snapshot.sql_config
    if snapshot.role in {"postgres", "extension"}:
        return SqlConfig(enabled=True, include=("**/*.sql", "**/*.sql.in"))
    return SqlConfig()


def load_corpus_config(path: str | Path) -> CorpusConfig:
    """Load a strict `[[snapshots]]` manifest relative to its own location."""
    manifest = Path(path).resolve()
    with manifest.open("rb") as source:
        document = tomllib.load(source)
    if set(document) != {"snapshots"} or not isinstance(document.get("snapshots"), list):
        raise ValueError("corpus manifest must contain only [[snapshots]] tables")
    root = manifest.parent.resolve()
    snapshots: list[CorpusSnapshotConfig] = []
    aliases: set[str] = set()
    allowed = {
        "alias",
        "logical_repo",
        "version",
        "role",
        "path",
        "dependencies",
        "sql",
        "max_file_bytes",
    }
    for index, item in enumerate(document["snapshots"]):
        where = f"snapshots[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be a TOML table")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(f"unknown {where} key(s): {', '.join(sorted(unknown))}")
        for name in ("alias", "logical_repo", "version", "role", "path"):
            if not isinstance(item.get(name), str) or not item[name].strip():
                raise ValueError(f"{where}.{name} must be a non-empty string")
        alias = item["alias"]
        if not _ALIAS.fullmatch(alias) or alias in {".", ".."}:
            raise ValueError(f"invalid snapshot alias: {alias!r}")
        if alias in aliases:
            raise ValueError(f"duplicate snapshot alias: {alias}")
        aliases.add(alias)
        if item["role"] not in {"application", "postgres", "extension"}:
            raise ValueError(f"{where}.role must be application, postgres, or extension")
        raw_path = Path(item["path"])
        snapshot_root = (raw_path if raw_path.is_absolute() else root / raw_path).resolve()
        if not snapshot_root.is_dir():
            raise ValueError(f"{where}.path is not an existing directory: {snapshot_root}")
        dependencies = item.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dep, str) or not dep for dep in dependencies
        ):
            raise ValueError(f"{where}.dependencies must be an array of aliases")
        if len(set(dependencies)) != len(dependencies) or alias in dependencies:
            raise ValueError(f"{where}.dependencies contains a duplicate or self-reference")
        if item["role"] == "postgres" and dependencies:
            raise ValueError("PostgreSQL snapshots cannot depend on other snapshots")
        sql = _sql_config(item.get("sql"), where)
        maximum = item.get("max_file_bytes", _MAX_FILE_BYTES)
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= _MAX_FILE_BYTES
        ):
            raise ValueError(f"{where}.max_file_bytes must be between 1 and {_MAX_FILE_BYTES}")
        snapshots.append(
            CorpusSnapshotConfig(
                alias,
                item["logical_repo"],
                item["version"],
                item["role"],
                snapshot_root,
                tuple(dependencies),
                sql,
                maximum,
            )
        )
    by_alias = {snapshot.alias: snapshot for snapshot in snapshots}
    for snapshot in snapshots:
        missing = set(snapshot.dependencies) - by_alias.keys()
        if missing:
            raise ValueError(
                f"snapshot {snapshot.alias} has missing dependencies: {', '.join(sorted(missing))}"
            )
        if snapshot.role == "extension" and not any(
            by_alias[d].role == "postgres" for d in snapshot.dependencies
        ):
            raise ValueError(
                f"extension snapshot {snapshot.alias} must depend on an explicit "
                "PostgreSQL snapshot"
            )
    # Multiple PostgreSQL aliases can be catalogued independently, but a
    # consumer's dependency view must not expose the same logical source twice.
    for snapshot in snapshots:
        if snapshot.role == "postgres":
            continue
        visible = (snapshot, *(by_alias[a] for a in _dependency_closure(snapshot, by_alias)))
        postgres_repos = [s.logical_repo for s in visible if s.role == "postgres"]
        if len(postgres_repos) != len(set(postgres_repos)):
            raise ValueError(
                f"snapshot {snapshot.alias} dependency closure exposes multiple aliases "
                "of the same PostgreSQL repository"
            )
    _validate_cycles(snapshots)
    return CorpusConfig(manifest, tuple(snapshots))


def _dependency_closure(snapshot: CorpusSnapshotConfig, by_alias: dict[str, CorpusSnapshotConfig]):
    seen: set[str] = set()
    pending = list(snapshot.dependencies)
    while pending:
        alias = pending.pop()
        if alias not in seen:
            seen.add(alias)
            pending.extend(by_alias[alias].dependencies)
    return tuple(alias for alias in by_alias if alias in seen)


def _sql_config(raw: object, where: str) -> SqlConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.sql must be a TOML table")
    allowed = {
        "enabled",
        "dialect",
        "database",
        "include",
        "exclude",
        "default_schema",
        "search_path",
    }
    if set(raw) - allowed:
        raise ValueError(f"unknown {where}.sql key(s): {', '.join(sorted(set(raw) - allowed))}")
    values = dict(raw)
    for name in ("include", "exclude", "search_path"):
        if name in values and isinstance(values[name], list):
            values[name] = tuple(values[name])
    try:
        return SqlConfig(**values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {where}.sql configuration: {error}") from error


def _validate_cycles(snapshots: list[CorpusSnapshotConfig]) -> None:
    by_alias = {snapshot.alias: snapshot for snapshot in snapshots}
    states: dict[str, int] = {}

    def visit(alias: str) -> None:
        if states.get(alias) == 1:
            raise ValueError(f"dependency cycle includes snapshot {alias}")
        if states.get(alias) == 2:
            return
        states[alias] = 1
        for dependency in by_alias[alias].dependencies:
            visit(dependency)
        states[alias] = 2

    for snapshot in snapshots:
        visit(snapshot.alias)
