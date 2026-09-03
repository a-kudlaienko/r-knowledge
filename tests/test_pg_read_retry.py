"""Tests for the bounded PG read-retry (Task 2).

Writes already have a durable fallback on a dropped PG connection —
``knowledge/outbox.py`` buffers the write locally and replays it later.
Reads (``ask``/``find``/``resume``/...) had nothing: one transient blip on
``db.connect()`` or a SELECT went straight to exit 4. These tests cover:

1. ``knowledge.backends.postgres.with_read_retry`` — the generic helper —
   directly, per its exact contract (attempt counts, exhaustion, which
   exception classes retry, SQLite-parity inertness on an empty tuple).
2. ``PostgresBackend.connect()`` retries the low-level ``psycopg.connect()``
   call itself.
3. ``db.fetch_one`` / ``db.fetch_all`` retry only on the PostgreSQL branch;
   the SQLite branch never calls the retry helper at all.
4. The WRITE paths (``db.execute`` / ``db.execute_returning_id``) never
   touch the retry helper — a transient blip on a write must keep flowing
   to the caller's existing ``except db.offline_errors():`` -> outbox path
   unmodified, not get silently double-applied by a retry.

No live PostgreSQL is used — ``psycopg.OperationalError`` / ``InterfaceError``
/ ``ProgrammingError`` are real exception classes from the installed
``psycopg`` package (see tests/test_pg_connect_rtt.py for the same
no-live-PG-needed assumption); everything else here is a hand-rolled fake or
a monkeypatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest

import knowledge.backends.postgres as pg_mod
import knowledge.db as db_mod
from knowledge.backends.sqlite import SqliteBackend


def _no_sleep(monkeypatch):
    """Replace the module-local ``time`` reference so tests don't pay the
    real backoff delay. Rebinding ``pg_mod.time`` (not the global ``time``
    module) keeps the stub scoped to this module only."""
    monkeypatch.setattr(pg_mod, "time", SimpleNamespace(sleep=lambda _s: None))


# ---------------------------------------------------------------------------
# 1. with_read_retry() — the generic helper
# ---------------------------------------------------------------------------


def test_retries_once_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection reset")
        return "ok"

    result = pg_mod.with_read_retry(
        flaky, (psycopg.OperationalError, psycopg.InterfaceError)
    )
    assert result == "ok"
    assert len(calls) == 2


def test_exhausts_after_two_attempts_and_propagates(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def always_fails():
        calls.append(1)
        raise psycopg.OperationalError("still down")

    with pytest.raises(psycopg.OperationalError):
        pg_mod.with_read_retry(
            always_fails, (psycopg.OperationalError, psycopg.InterfaceError)
        )
    # Exactly 2 attempts total (1 retry) — never more, per the deliberate cap.
    assert len(calls) == 2


def test_non_transient_error_is_not_retried(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def bad_query():
        calls.append(1)
        raise psycopg.ProgrammingError("syntax error")

    with pytest.raises(psycopg.ProgrammingError):
        pg_mod.with_read_retry(
            bad_query, (psycopg.OperationalError, psycopg.InterfaceError)
        )
    # A deterministic error must fire exactly once — retrying it is pure
    # added latency for the identical failure.
    assert len(calls) == 1


def test_empty_error_types_is_structurally_inert(monkeypatch):
    """SQLite's connection_error_types() is () — handing that tuple to
    with_read_retry must degrade to a single, unretried call."""
    _no_sleep(monkeypatch)
    calls = []

    def always_fails():
        calls.append(1)
        raise psycopg.OperationalError("would normally retry")

    with pytest.raises(psycopg.OperationalError):
        pg_mod.with_read_retry(always_fails, ())
    assert len(calls) == 1


def test_backoff_sleep_used_between_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(pg_mod, "time", SimpleNamespace(sleep=sleeps.append))
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.OperationalError("blip")
        return "ok"

    pg_mod.with_read_retry(flaky, (psycopg.OperationalError,))
    assert sleeps == [pg_mod._READ_RETRY_BACKOFF_SECONDS]


def test_sqlite_backend_connection_error_types_is_empty():
    assert SqliteBackend().connection_error_types() == ()


# ---------------------------------------------------------------------------
# 2. PostgresBackend.connect() retries the low-level psycopg.connect() call
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal stand-in for a psycopg connection object."""


def _stub_connect_backend(monkeypatch, connect_fn):
    """Wire PostgresBackend.connect() so only the retry-around-psycopg logic
    is exercised. DSN parsing / gssencmode kwargs / TypeInfo registration are
    stubbed out — covered separately by tests/test_pg_connect_rtt.py."""
    monkeypatch.setattr(psycopg, "connect", connect_fn)
    monkeypatch.setattr(pg_mod, "_register_pgvector_types", lambda *a, **k: None)
    monkeypatch.setattr(
        "knowledge.settings.resolve_pg_dsn",
        lambda s: "postgresql://u:p@host:5432/db",
    )
    settings = SimpleNamespace(mode="shared_postgresql")
    return pg_mod.PostgresBackend(settings)


def test_connect_retries_once_on_operational_error(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def flaky_connect(dsn, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection refused")
        return _FakeConn()

    backend = _stub_connect_backend(monkeypatch, flaky_connect)
    conn = backend.connect()
    assert isinstance(conn, _FakeConn)
    assert len(calls) == 2


def test_connect_exhausts_after_two_attempts(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def always_fails(dsn, **kwargs):
        calls.append(kwargs)
        raise psycopg.OperationalError("still down")

    backend = _stub_connect_backend(monkeypatch, always_fails)
    with pytest.raises(psycopg.OperationalError):
        backend.connect()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 3. db.fetch_one / db.fetch_all — PG branch retries, SQLite branch inert
# ---------------------------------------------------------------------------


class _FakeCursorCtx:
    """``with conn.cursor() as cur:`` context manager for _FakePgConn."""

    def __init__(self, parent, row):
        self._parent = parent
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params):
        # Fail count is tracked on the CONNECTION (persists across the
        # fresh `conn.cursor()` each retry attempt opens), not the cursor.
        if self._parent.cursor_calls <= self._parent.fail_times:
            raise psycopg.OperationalError("blip")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row]


class _FakePgConn:
    def __init__(self, fail_times: int, row):
        self.fail_times = fail_times
        self.row = row
        self.cursor_calls = 0

    def cursor(self):
        self.cursor_calls += 1
        return _FakeCursorCtx(self, self.row)


def _pg_offline_errors():
    return (psycopg.OperationalError, psycopg.InterfaceError)


def test_fetch_one_retries_on_pg_branch(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(db_mod, "current_mode", lambda: "postgresql")
    monkeypatch.setattr(db_mod, "offline_errors", _pg_offline_errors)

    conn = _FakePgConn(fail_times=1, row=(1, "hello"))
    result = db_mod.fetch_one(conn, "SELECT * FROM t WHERE id = ?", (1,))
    assert result == (1, "hello")
    assert conn.cursor_calls == 2


def test_fetch_all_exhausted_retry_propagates_and_caps_at_two_attempts(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(db_mod, "current_mode", lambda: "postgresql")
    monkeypatch.setattr(db_mod, "offline_errors", _pg_offline_errors)

    conn = _FakePgConn(fail_times=99, row=(1,))
    with pytest.raises(psycopg.OperationalError):
        db_mod.fetch_all(conn, "SELECT * FROM t", ())
    # Exactly 2 attempts — the real contract, not just "it eventually raised".
    assert conn.cursor_calls == 2


def test_fetch_one_does_not_retry_programming_error(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(db_mod, "current_mode", lambda: "postgresql")
    monkeypatch.setattr(db_mod, "offline_errors", _pg_offline_errors)

    class _BoomCursorCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql, params):
            raise psycopg.ProgrammingError("syntax error")

        def fetchone(self):
            raise AssertionError("unreachable")

    class _Conn:
        def __init__(self):
            self.cursor_calls = 0

        def cursor(self):
            self.cursor_calls += 1
            return _BoomCursorCtx()

    conn = _Conn()
    with pytest.raises(psycopg.ProgrammingError):
        db_mod.fetch_one(conn, "SELECT * FROM t WHERE id = ?", (1,))
    assert conn.cursor_calls == 1  # deterministic error, not retried


def test_fetch_one_sqlite_branch_never_engages_retry_helper(monkeypatch):
    """The retry path must not be MERELY unlikely to fire on SQLite — the
    ``if current_mode() == "postgresql"`` guard means it is never even
    reached. Prove it by making the helper explode if touched."""
    monkeypatch.setattr(db_mod, "current_mode", lambda: "sqlite")

    def _boom(*a, **k):
        raise AssertionError("with_read_retry must not run on the SQLite path")

    monkeypatch.setattr(pg_mod, "with_read_retry", _boom)

    class _FakeSqliteCursor:
        def fetchone(self):
            return (42,)

    class _FakeSqliteConn:
        def execute(self, sql, params):
            return _FakeSqliteCursor()

    result = db_mod.fetch_one(_FakeSqliteConn(), "SELECT 1", ())
    assert result == (42,)


# ---------------------------------------------------------------------------
# 4. Write paths never touch the retry helper — never retry writes
# ---------------------------------------------------------------------------


def test_execute_write_path_never_uses_retry_helper(monkeypatch):
    """db.execute() is the WRITE path. If it ever called with_read_retry, a
    transient blip mid-UPDATE could double-apply the write on retry — far
    worse than the current behavior of failing once and buffering to the
    outbox. Guard by making the helper explode if it's ever touched."""
    monkeypatch.setattr(db_mod, "current_mode", lambda: "postgresql")

    def _boom(*a, **k):
        raise AssertionError("write path must never call with_read_retry")

    monkeypatch.setattr(pg_mod, "with_read_retry", _boom)

    class _Cur:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql, params):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    n = db_mod.execute(_Conn(), "UPDATE t SET x = ? WHERE id = ?", (1, 2))
    assert n == 1


def test_execute_returning_id_write_path_never_uses_retry_helper(monkeypatch):
    monkeypatch.setattr(db_mod, "current_mode", lambda: "postgresql")

    def _boom(*a, **k):
        raise AssertionError("write path must never call with_read_retry")

    monkeypatch.setattr(pg_mod, "with_read_retry", _boom)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql, params):
            return None

        def fetchone(self):
            return (99,)

    class _Conn:
        def cursor(self):
            return _Cur()

    new_id = db_mod.execute_returning_id(_Conn(), "INSERT INTO t(x) VALUES (?)", (1,))
    assert new_id == 99
