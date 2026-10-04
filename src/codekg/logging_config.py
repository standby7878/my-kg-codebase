"""Safe, process-level logging configuration for CodeKG entry points."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

DEFAULT_LOG_LEVEL = "DEBUG"


def log_level() -> int:
    """Return the configured level, falling back safely to DEBUG."""

    configured = os.getenv("CODEKG_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, configured, None)
    return level if isinstance(level, int) else logging.DEBUG


def configure_logging() -> None:
    """Configure the process handler once; call only from executable entry points."""

    root = logging.getLogger()
    root.setLevel(log_level())
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


def debug_event(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """Emit structured lifecycle telemetry; callers must pass aggregate-safe fields only."""

    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug("codekg_%s %s", event, json.dumps(fields, sort_keys=True, default=str))
