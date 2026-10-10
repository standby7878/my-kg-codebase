"""Small Neo4j client wrappers for CodeKG.

The lazy singleton and result-shaping pattern is adapted from unify/kg-mcp's
Neo4j client, reduced to the needs of this Neo4j-only prototype.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

from codekg.logging_config import debug_event

DEFAULT_URI = "bolt://neo4j:7687"
DEFAULT_USERNAME = "neo4j"
DEFAULT_DATABASE = "neo4j"
logger = logging.getLogger(__name__)


class CodeKGNeo4jError(RuntimeError):
    """Raised when a Neo4j operation fails."""


class Neo4jClient:
    """Thin sync Neo4j driver wrapper with explicit read/write helpers."""

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        transaction_timeout_seconds: float | None = None,
        connection_timeout_seconds: float | None = None,
        max_transaction_retry_time_seconds: float | None = None,
        auth_enabled: bool | None = None,
    ) -> None:
        self.uri = uri or os.getenv("NEO4J_URI", DEFAULT_URI)
        self.username = username or os.getenv("NEO4J_USERNAME", DEFAULT_USERNAME)
        if auth_enabled is None:
            auth_enabled = os.getenv("NEO4J_AUTH", "").strip() != "none"
        self.auth_enabled = auth_enabled
        self.password = (
            (password if password is not None else os.getenv("NEO4J_PASSWORD"))
            if auth_enabled
            else None
        )
        self.database = database or os.getenv("NEO4J_DATABASE", DEFAULT_DATABASE)
        self.transaction_timeout_seconds = (
            transaction_timeout_seconds
            if transaction_timeout_seconds is not None
            else _optional_timeout_from_environment()
        )
        if auth_enabled and not self.password:
            raise CodeKGNeo4jError("NEO4J_PASSWORD must be set")
        driver_options = {}
        if connection_timeout_seconds is not None:
            if connection_timeout_seconds <= 0:
                raise ValueError("connection timeout must be positive")
            driver_options["connection_timeout"] = connection_timeout_seconds
            driver_options["connection_acquisition_timeout"] = connection_timeout_seconds
        if max_transaction_retry_time_seconds is not None:
            if max_transaction_retry_time_seconds < 0:
                raise ValueError("transaction retry timeout must be non-negative")
            driver_options["max_transaction_retry_time"] = max_transaction_retry_time_seconds
        if auth_enabled:
            self._driver = GraphDatabase.driver(
                self.uri, auth=(self.username, self.password), **driver_options
            )
        else:
            self._driver = GraphDatabase.driver(self.uri, **driver_options)
        debug_event(
            logger,
            "neo4j_client_created",
            database=self.database,
            timeout_configured=self.transaction_timeout_seconds is not None,
        )

    def verify(self) -> None:
        debug_event(logger, "neo4j_verify_started", database=self.database)
        self._driver.verify_connectivity()
        debug_event(logger, "neo4j_verify_completed", database=self.database)

    def execute_read(
        self,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_rows: int = 1000,
        operation: str | None = None,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        debug_event(
            logger, "neo4j_read_started", operation=operation or "unnamed", max_rows=max_rows
        )
        try:
            with self._driver.session(
                database=self.database,
                default_access_mode="READ",
            ) as session:

                def work(tx):
                    result = tx.run(query, params)
                    rows = []
                    for index, record in enumerate(result):
                        if index >= max_rows:
                            break
                        rows.append(record.data())
                    result.consume()
                    return rows

                timeout = _transaction_timeout(timeout_seconds, self.transaction_timeout_seconds)
                if timeout is not None:
                    work.timeout = timeout
                rows = session.execute_read(work)
                debug_event(
                    logger, "neo4j_read_completed", operation=operation or "unnamed", rows=len(rows)
                )
                return rows
        except Neo4jError as exc:
            debug_event(
                logger,
                "neo4j_read_failed",
                operation=operation or "unnamed",
                error_type=type(exc).__name__,
            )
            raise _operation_error("read", operation, query, exc) from exc

    def execute_write(
        self,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        operation: str | None = None,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        debug_event(logger, "neo4j_write_started", operation=operation or "unnamed")
        try:
            with self._driver.session(database=self.database) as session:

                def work(tx):
                    result = tx.run(query, params)
                    rows = [record.data() for record in result]
                    result.consume()
                    return rows

                timeout = _transaction_timeout(timeout_seconds, self.transaction_timeout_seconds)
                if timeout is not None:
                    work.timeout = timeout
                rows = session.execute_write(work)
                debug_event(
                    logger,
                    "neo4j_write_completed",
                    operation=operation or "unnamed",
                    rows=len(rows),
                )
                return rows
        except Neo4jError as exc:
            debug_event(
                logger,
                "neo4j_write_failed",
                operation=operation or "unnamed",
                error_type=type(exc).__name__,
            )
            raise _operation_error("write", operation, query, exc) from exc

    def close(self) -> None:
        debug_event(logger, "neo4j_client_closing", database=self.database)
        self._driver.close()


_client: Neo4jClient | None = None
_client_lock = threading.Lock()


def get_client() -> Neo4jClient:
    """Return the process-wide Neo4j client singleton."""

    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = Neo4jClient()
    return _client


def close_client() -> None:
    """Close the process-wide Neo4j client singleton."""

    global _client
    with _client_lock:
        if _client is None:
            return
        _client.close()
        _client = None


def _optional_timeout_from_environment() -> float | None:
    value = os.getenv("NEO4J_TRANSACTION_TIMEOUT_SECONDS")
    if value is None or not value.strip():
        return None
    try:
        timeout = float(value)
    except ValueError as exc:
        raise CodeKGNeo4jError(
            "NEO4J_TRANSACTION_TIMEOUT_SECONDS must be a positive number of seconds"
        ) from exc
    if timeout <= 0:
        raise CodeKGNeo4jError(
            "NEO4J_TRANSACTION_TIMEOUT_SECONDS must be a positive number of seconds"
        )
    return timeout


def _transaction_timeout(explicit: float | None, configured: float | None) -> float | None:
    timeout = explicit if explicit is not None else configured
    if timeout is not None and timeout <= 0:
        raise ValueError("Neo4j transaction timeout must be positive")
    return timeout


def _operation_error(
    mode: str,
    operation: str | None,
    query: str,
    exc: Neo4jError,
) -> CodeKGNeo4jError:
    context = operation or "unnamed operation"
    return CodeKGNeo4jError(f"Neo4j {mode} failed during {context}: {exc}")
