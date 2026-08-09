from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codekg.queries.code import (
    _GENERIC_CODE_TERMS,
    _MODULE_GENERIC_WEIGHT,
    _MODULE_WEIGHT,
    _OWNER_GENERIC_WEIGHT,
    _OWNER_WEIGHT,
    _PACKAGE_GENERIC_WEIGHT,
    _PACKAGE_ROOTS,
    _PACKAGE_WEIGHT,
)

pytestmark = pytest.mark.unit

TUNING_LOG_PATH = Path(__file__).resolve().parents[2] / "RANKING_TUNING_LOG.md"


def _current_hash() -> str:
    return hashlib.sha256(repr(sorted(_GENERIC_CODE_TERMS)).encode()).hexdigest()


def _current_role_weights_hash() -> str:
    weights = (
        _OWNER_WEIGHT,
        _OWNER_GENERIC_WEIGHT,
        _MODULE_WEIGHT,
        _MODULE_GENERIC_WEIGHT,
        _PACKAGE_WEIGHT,
        _PACKAGE_GENERIC_WEIGHT,
        tuple(sorted(_PACKAGE_ROOTS)),
    )
    return hashlib.sha256(repr(weights).encode()).hexdigest()


def test_generic_code_terms_hash_is_logged_in_tuning_log() -> None:
    """A11: `_GENERIC_CODE_TERMS` must not change without a logged justification.

    If this fails, either RANKING_TUNING_LOG.md needs a new dated entry
    recording the new frozen set and its hash (per its own template), or the
    change to `_GENERIC_CODE_TERMS` was unintentional.
    """
    log_text = TUNING_LOG_PATH.read_text(encoding="utf-8")
    current_hash = _current_hash()
    assert current_hash in log_text, (
        f"_GENERIC_CODE_TERMS hash {current_hash} (set={sorted(_GENERIC_CODE_TERMS)}) "
        f"is not recorded in {TUNING_LOG_PATH}. Add a dated entry before merging."
    )


def test_qname_role_weights_hash_is_logged_in_tuning_log() -> None:
    """B2: the owner/module/package role weights (and `_PACKAGE_ROOTS`, itself
    a corpus-fitted ranking parameter) must not change without a logged,
    re-swept justification -- a silent tweak here is the exact failure mode
    A11 exists to prevent, just on the weight tuple instead of the term list.

    If this fails, either RANKING_TUNING_LOG.md needs a new dated entry
    (re-run evaluation/replay_b2.py to find the new zero-regression ceiling),
    or the change was unintentional.
    """
    log_text = TUNING_LOG_PATH.read_text(encoding="utf-8")
    current_hash = _current_role_weights_hash()
    assert current_hash in log_text, (
        f"qname role weight hash {current_hash} "
        f"(owner={_OWNER_WEIGHT}/{_OWNER_GENERIC_WEIGHT}, "
        f"module={_MODULE_WEIGHT}/{_MODULE_GENERIC_WEIGHT}, "
        f"package={_PACKAGE_WEIGHT}/{_PACKAGE_GENERIC_WEIGHT}, "
        f"package_roots={sorted(_PACKAGE_ROOTS)}) is not recorded in "
        f"{TUNING_LOG_PATH}. Add a dated entry before merging."
    )
