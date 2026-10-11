"""Bound concurrent database connections across all stores within an API/worker process."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

import psycopg

from backend.store import StoreError

_CONNECTIONS = BoundedSemaphore(12)


@contextmanager
def connect(conninfo: str = "", **kwargs) -> Iterator[psycopg.Connection]:
    if not _CONNECTIONS.acquire(timeout=5):
        raise StoreError(
            503, "database_busy",
            "処理が混み合っています。しばらく待って再試行してください。",
        )
    try:
        with psycopg.connect(conninfo, **kwargs) as connection:
            yield connection
    finally:
        _CONNECTIONS.release()
