from contextlib import contextmanager
from threading import BoundedSemaphore

import pytest

from backend import database
from backend.store import StoreError


def test_connection_guard_preserves_transaction_and_releases_on_failure(monkeypatch):
    gate = BoundedSemaphore(1)
    monkeypatch.setattr(database, "_CONNECTIONS", gate)
    events = []

    @contextmanager
    def fake_connect(conninfo, **kwargs):
        assert conninfo == "synthetic"
        assert kwargs == {"connect_timeout": 3}
        events.append("enter")
        try:
            yield "connection"
        finally:
            events.append("exit")

    monkeypatch.setattr(database.psycopg, "connect", fake_connect)
    with pytest.raises(ValueError, match="test failure"):
        with database.connect("synthetic", connect_timeout=3) as connection:
            assert connection == "connection"
            assert gate.acquire(blocking=False) is False
            raise ValueError("test failure")
    assert events == ["enter", "exit"]
    assert gate.acquire(blocking=False)
    gate.release()


def test_connection_failure_releases_capacity(monkeypatch):
    gate = BoundedSemaphore(1)
    monkeypatch.setattr(database, "_CONNECTIONS", gate)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic connection failure")

    monkeypatch.setattr(database.psycopg, "connect", fail)
    with pytest.raises(RuntimeError, match="synthetic connection failure"):
        with database.connect():
            pytest.fail("A failed connection must never be yielded.")
    assert gate.acquire(blocking=False)
    gate.release()


def test_connection_overload_is_explicit_service_unavailable(monkeypatch):
    class Full:
        def acquire(self, *, timeout):
            assert timeout == 5
            return False

        def release(self):
            pytest.fail("No capacity was acquired.")

    monkeypatch.setattr(database, "_CONNECTIONS", Full())
    with pytest.raises(StoreError) as caught:
        with database.connect():
            pytest.fail("No connection can be opened on overload.")
    assert caught.value.status == 503
    assert caught.value.code == "database_busy"
