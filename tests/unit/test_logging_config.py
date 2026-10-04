from __future__ import annotations

import logging

import pytest

from codekg.logging_config import DEFAULT_LOG_LEVEL, debug_event, log_level

pytestmark = pytest.mark.unit


def test_log_level_defaults_to_debug(monkeypatch) -> None:
    monkeypatch.delenv("CODEKG_LOG_LEVEL", raising=False)

    assert DEFAULT_LOG_LEVEL == "DEBUG"
    assert log_level() == logging.DEBUG


def test_log_level_invalid_value_falls_back_to_debug(monkeypatch) -> None:
    monkeypatch.setenv("CODEKG_LOG_LEVEL", "not-a-level")

    assert log_level() == logging.DEBUG


def test_log_level_non_level_logging_attribute_falls_back_to_debug(monkeypatch) -> None:
    monkeypatch.setenv("CODEKG_LOG_LEVEL", "BASIC_FORMAT")

    assert log_level() == logging.DEBUG


def test_debug_event_is_structured(caplog) -> None:
    logger = logging.getLogger("codekg.tests.logging")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        debug_event(logger, "scanner_completed", files=2, calls=3)

    assert 'codekg_scanner_completed {"calls": 3, "files": 2}' in caplog.text


def test_disabled_debug_event_does_not_serialize(monkeypatch, caplog) -> None:
    def unexpected_serialization(*args, **kwargs):
        raise AssertionError("disabled telemetry must not serialize its fields")

    monkeypatch.setattr("codekg.logging_config.json.dumps", unexpected_serialization)
    logger = logging.getLogger("codekg.tests.disabled_logging")
    with caplog.at_level(logging.INFO, logger=logger.name):
        debug_event(logger, "ignored", value=object())
    assert not caplog.records
