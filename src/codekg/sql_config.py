"""Configuration for project SQL ingestion.

Only PostgreSQL source files are supported by the first SQL semantic
foundation.  Configuration is deliberately small and immutable so callers can
pass one validated object through discovery and parsing without sharing
mutable parser state.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class SqlConfig:
    enabled: bool = False
    dialect: str = "postgres"
    database: str = "management"
    include: tuple[str, ...] = ("**/*.sql",)
    exclude: tuple[str, ...] = ()
    default_schema: str = "public"
    search_path: tuple[str, ...] = ("public",)

    def __post_init__(self) -> None:
        if self.dialect != "postgres":
            raise ValueError("[sql].dialect must be 'postgres'")
        if not isinstance(self.enabled, bool):
            raise TypeError("[sql].enabled must be a boolean")
        for field_name in ("database", "default_schema"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"[sql].{field_name} must be a non-empty string")
        for field_name in ("include", "exclude", "search_path"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise ValueError(f"[sql].{field_name} must be a tuple of non-empty strings")
        if not self.include:
            raise ValueError("[sql].include must contain at least one pattern")

    def matches(self, relative: str | os.PathLike[str]) -> bool:
        """Return whether a project-relative path is selected for SQL parsing."""

        path = PurePosixPath(str(relative).replace(os.sep, "/"))
        if path.is_absolute() or ".." in path.parts:
            return False
        value = path.as_posix()
        return _matches_any(value, self.include) and not _matches_any(value, self.exclude)


def load_sql_config(root: str | os.PathLike[str]) -> SqlConfig:
    """Load and validate ``[sql]`` from ``codekg.toml`` below *root*.

    A missing file or section is equivalent to the safe disabled defaults.
    Unknown keys are rejected to avoid silently accepting a misspelled scope or
    search-path setting.
    """

    path = Path(root)
    if path.is_dir():
        path = path / "codekg.toml"
    if not path.exists():
        return SqlConfig()
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    section = document.get("sql", {})
    if not isinstance(section, dict):
        raise ValueError("[sql] must be a TOML table")
    allowed = {
        "enabled",
        "dialect",
        "database",
        "include",
        "exclude",
        "default_schema",
        "search_path",
    }
    unknown = set(section) - allowed
    if unknown:
        names = ", ".join(sorted(str(value) for value in unknown))
        raise ValueError(f"unknown [sql] setting(s): {names}")
    values: dict[str, Any] = dict(section)
    for name in ("include", "exclude", "search_path"):
        if name in values:
            raw = values[name]
            if isinstance(raw, str):
                values[name] = (raw,)
            elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
                values[name] = tuple(raw)
            else:
                raise ValueError(f"[sql].{name} must be a string or array of strings")
    return SqlConfig(**values)


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if _glob_match(value.split("/"), normalized.split("/")):
            return True
    return False


def _glob_match(path_parts: list[str], pattern_parts: list[str]) -> bool:
    """Match slash-separated globs where ``**`` consumes zero or more parts."""

    @cache
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern = pattern_parts[pattern_index]
        if pattern == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and _segment_match(path_parts[path_index], pattern)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _segment_match(value: str, pattern: str) -> bool:
    # Keep wildcard semantics local to a path segment; in particular, '*'
    # must never cross a slash.
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    regex = regex.replace(r"\?", "[^/]")
    return bool(re.fullmatch(regex, value))
