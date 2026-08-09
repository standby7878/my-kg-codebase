from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

EVALUATION_DIR = Path(__file__).resolve().parents[2] / "evaluation"
if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from benchmark_lib import _matches_any_path, paths_equivalent  # noqa: E402


class TestPathsEquivalent:
    def test_claimed_long_form_matches_gold_short_form(self) -> None:
        assert paths_equivalent("lib/sqlalchemy/engine/base.py", "base.py") is True

    def test_claimed_short_form_matches_gold_long_form(self) -> None:
        assert paths_equivalent("base.py", "lib/sqlalchemy/engine/base.py") is True

    def test_unrelated_directory_with_shared_basename_is_rejected(self) -> None:
        assert paths_equivalent("other/base.py", "engine/base.py") is False

    def test_identical_paths_match(self) -> None:
        assert paths_equivalent("src/requests/sessions.py", "src/requests/sessions.py") is True

    def test_case_insensitive_match(self) -> None:
        assert paths_equivalent("Lib/SQLAlchemy/Engine/Base.py", "base.py") is True

    def test_non_string_inputs_are_rejected(self) -> None:
        assert paths_equivalent(None, "base.py") is False
        assert paths_equivalent("base.py", None) is False
        assert paths_equivalent(123, "base.py") is False

    def test_empty_strings_are_rejected(self) -> None:
        assert paths_equivalent("", "base.py") is False
        assert paths_equivalent("base.py", "") is False

    def test_partial_component_is_not_a_suffix_match(self) -> None:
        # "gine/base.py" is not a path-component suffix of "engine/base.py".
        assert paths_equivalent("gine/base.py", "engine/base.py") is False


class TestMatchesAnyPath:
    def test_matches_when_any_accepted_path_is_equivalent(self) -> None:
        accepted = ["base.py", "lib/sqlalchemy/engine/base.py"]
        assert _matches_any_path("lib/sqlalchemy/engine/base.py", accepted) is True
        assert _matches_any_path("base.py", accepted) is True

    def test_rejects_when_no_accepted_path_matches(self) -> None:
        accepted = ["base.py", "lib/sqlalchemy/engine/base.py"]
        assert _matches_any_path("impl.py", accepted) is False
