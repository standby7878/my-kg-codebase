"""Small Neo4j client wrappers for CodeKG.

The lazy singleton and result-shaping pattern is adapted from unify/kg-mcp's
Neo4j client, reduced to the needs of this Neo4j-only prototype.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

DEFAULT_URI = "bolt://neo4j:7687"
DEFAULT_USERNAME = "neo4j"
DEFAULT_DATABASE = "neo4j"


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
    ) -> None:
        self.uri = uri or os.getenv("NEO4J_URI", DEFAULT_URI)
        self.username = username or os.getenv("NEO4J_USERNAME", DEFAULT_USERNAME)
        self.password = password or os.getenv("NEO4J_PASSWORD")
        self.database = database or os.getenv("NEO4J_DATABASE", DEFAULT_DATABASE)
        self.transaction_timeout_seconds = (
            transaction_timeout_seconds
            if transaction_timeout_seconds is not None
            else _optional_timeout_from_environment()
        )
        if not self.password:
            raise CodeKGNeo4jError("NEO4J_PASSWORD must be set")
        self._driver = GraphDatabase.driver(self.uri, auth=(self.username, self.password))

    def verify(self) -> None:
        self._driver.verify_connectivity()

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
                return session.execute_read(work)
        except Neo4jError as exc:
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
                return session.execute_write(work)
        except Neo4jError as exc:
            raise _operation_error("write", operation, query, exc) from exc

    def close(self) -> None:
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
    statement = " ".join(query.split())[:160]
    return CodeKGNeo4jError(f"Neo4j {mode} failed during {context}: {exc}; query={statement!r}")
