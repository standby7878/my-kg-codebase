from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from codekg.sql_config import SqlConfig, load_sql_config

pytestmark = pytest.mark.unit


def test_sql_defaults_are_disabled_and_immutable() -> None:
    config = SqlConfig()
    assert config.enabled is False
    assert config.database == "management"
    assert config.include == ("**/*.sql",)
    with pytest.raises(FrozenInstanceError):
        config.enabled = True  # type: ignore[misc]


def test_sql_config_loads_and_matches_recursive_globs(tmp_path) -> None:
    (tmp_path / "codekg.toml").write_text(
        """
[sql]
enabled = true
database = "management"
include = ["database/**/*.sql"]
exclude = ["database/generated/**"]
default_schema = "public"
search_path = ["tenant", "public"]
""",
        encoding="utf-8",
    )
    config = load_sql_config(tmp_path)
    assert config.enabled is True
    assert config.matches("database/schema.sql")
    assert config.matches("database/tenant/schema.sql")
    assert not config.matches("database/generated/schema.sql")
    assert not config.matches("python/module.py")


def test_sql_config_rejects_unknown_or_invalid_values(tmp_path) -> None:
    (tmp_path / "codekg.toml").write_text("[sql]\nmanagement = 'wrong'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load_sql_config(tmp_path)

    with pytest.raises(ValueError):
        SqlConfig(dialect="mysql")
