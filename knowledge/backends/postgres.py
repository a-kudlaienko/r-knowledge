"""PostgreSQL backend — opt-in via ``storage.mode = "shared_postgresql"``.

psycopg3 + pgvector + tsvector. Connection-per-invocation: the CLI doesn't
hold pools (re-introduce when daemon mode lands; see
``todo/01-postgresql-shared-mode.md`` "Non-goals").

Advisory locks scope every mutation to a single project so two ``knowledge
update`` runs against the same project across hosts can't corrupt the index.
The whole indexer transaction holds the lock for v1.

# TODO(shared-postgres-v2): chunk indexer txn to release the advisory lock
# between batches so concurrent ``update``s on the same project don't block
# each other on huge first builds. Trade-off: gives up build atomicity (a
# killed process leaves partial state, recovery path needed). See
# todo/01-postgresql-shared-mode.md → "Concurrency rules".

Connection setup RTT cuts (see ``knowledge decide`` id=188):

* ``gssencmode=disable`` is passed to ``psycopg.connect`` whenever the DSN
  doesn't already specify one (and ``PGGSSENCMODE`` isn't set in the
  environment) — skips a GSSAPI negotiation round trip against servers that
  don't speak it. Any explicit setting (DSN or env var) always wins.
* pgvector's ``register_vector(conn)`` does 4 ``TypeInfo.fetch`` catalog
  queries (vector, bit, halfvec, sparsevec) — one network round trip each.
  Those OIDs are stable for the lifetime of the extension, so they're cached
  in ``~/.knowledge/pg_types_cache.json`` (keyed by
  ``sha256(host|port|dbname)``) after the first connect and reused directly
  via ``psycopg.types.TypeInfo(name, oid, array_oid)`` on every connect after
  that — turning 4 RTTs into 0.
  Stale OIDs only happen if the ``vector`` extension is dropped and
  recreated on the server (new OIDs). Recovery: delete
  ``~/.knowledge/pg_types_cache.json``, or call
  ``PostgresBackend.connect(refresh_types=True)`` once, which always
  re-fetches and rewrites the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from .. import paths

if TYPE_CHECKING:
    from ..settings import Settings

# The four pgvector types register_vector() fetches TypeInfo for. vector and
# bit are assumed present by pgvector itself (register_vector_info raises if
# vector's TypeInfo is None; register_bit_info doesn't even guard against
# None). halfvec/sparsevec are optional — older pgvector versions/extensions
# without them return None from TypeInfo.fetch and upstream skips
# registration entirely.
_PGVECTOR_TYPE_NAMES = ("vector", "bit", "halfvec", "sparsevec")
_REQUIRED_PGVECTOR_TYPES = ("vector", "bit")

# Namespace constant for ``pg_advisory_xact_lock(_LOCK_NAMESPACE, project_id)``.
# 0x6B6E6F77 == ASCII "know" — distinctive enough that conflicts with other
# tools using advisory locks on the same database are extremely unlikely
# while still fitting in the 32-bit lock-key first slot.
_LOCK_NAMESPACE = 0x6B6E6F77


class _DependencyMissing(RuntimeError):
    """Raised when psycopg/pgvector aren't installed.

    Lets callers print a helpful ``pip install repo-knowledge[postgres]``
    hint instead of an opaque ``ModuleNotFoundError``.
    """


def _require_psycopg():
    try:
        import psycopg  # type: ignore

        return psycopg
    except ImportError as exc:  # pragma: no cover - dep guard
        raise _DependencyMissing(
            "psycopg is not installed. shared_postgresql mode requires "
            "the optional 'postgres' extra: "
            "pip install -e '.[postgres]'"
        ) from exc


def _require_pgvector():
    try:
        import pgvector.psycopg  # type: ignore  # noqa: F401

        return True
    except ImportError as exc:  # pragma: no cover - dep guard
        raise _DependencyMissing(
            "pgvector is not installed. shared_postgresql mode requires "
            "pgvector for vector(384) marshalling: "
            "pip install -e '.[postgres]'"
        ) from exc


def _pg_types_cache_key(host: str, port: int, dbname: str) -> str:
    """sha256 hex digest identifying one PostgreSQL connection target."""
    raw = f"{host}|{port}|{dbname}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_pg_types_cache() -> dict:
    """Read the whole type-OID cache file. Never raises.

    A missing, corrupted, or otherwise unreadable cache file is treated as
    empty — callers fall back to the fetch-from-server path silently, then
    rewrite the file. This file is purely a performance cache; it must never
    be able to break a connection.
    """
    path = paths.pg_types_cache_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_pg_types_cache(data: dict) -> None:
    """Atomically rewrite the type-OID cache file with 0600 permissions."""
    path = paths.pg_types_cache_path()
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(str(tmp_path), str(path))
    except OSError:
        # Best-effort: a failed cache write just means the next connect()
        # fetches from the server again. Clean up the tmp file if it landed.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _entry_is_valid(entry: Any) -> bool:
    """A cached entry must have non-null oids for the required types."""
    if not isinstance(entry, dict):
        return False
    for name in _REQUIRED_PGVECTOR_TYPES:
        value = entry.get(name)
        if not (isinstance(value, list) and len(value) == 2):
            return False
    return True


def _register_from_cache(psycopg_types_mod, conn: Any, entry: dict) -> None:
    """Reconstruct TypeInfo objects from a cached entry and register them.

    Mirrors ``pgvector.psycopg.register_vector`` exactly, minus the network
    round trips: builds ``TypeInfo(name, oid, array_oid)`` per cached type and
    calls the matching ``register_*_info`` function directly. A ``None``
    entry for halfvec/sparsevec means that type didn't exist server-side when
    the cache was written — skipped, same as upstream does when
    ``TypeInfo.fetch`` returns ``None``.
    """
    from pgvector.psycopg.bit import register_bit_info
    from pgvector.psycopg.halfvec import register_halfvec_info
    from pgvector.psycopg.sparsevec import register_sparsevec_info
    from pgvector.psycopg.vector import register_vector_info

    registrars = {
        "vector": register_vector_info,
        "bit": register_bit_info,
        "halfvec": register_halfvec_info,
        "sparsevec": register_sparsevec_info,
    }
    TypeInfo = psycopg_types_mod.TypeInfo
    for name in _PGVECTOR_TYPE_NAMES:
        oid_pair = entry.get(name)
        if oid_pair is None:
            continue
        oid, array_oid = oid_pair
        info = TypeInfo(name, oid, array_oid)
        registrars[name](conn, info)


def _register_from_server(psycopg_types_mod, conn: Any) -> dict:
    """Fetch pgvector TypeInfo from the server, register, return a cache entry.

    Exactly mirrors ``pgvector.psycopg.register_vector``'s own fetch/register
    sequence (4 ``TypeInfo.fetch`` catalog round trips) so behavior is
    identical to upstream on a cache miss.
    """
    from pgvector.psycopg.bit import register_bit_info
    from pgvector.psycopg.halfvec import register_halfvec_info
    from pgvector.psycopg.sparsevec import register_sparsevec_info
    from pgvector.psycopg.vector import register_vector_info

    TypeInfo = psycopg_types_mod.TypeInfo
    entry: dict = {}

    info = TypeInfo.fetch(conn, "vector")
    register_vector_info(conn, info)
    entry["vector"] = [info.oid, info.array_oid] if info is not None else None

    info = TypeInfo.fetch(conn, "bit")
    register_bit_info(conn, info)
    entry["bit"] = [info.oid, info.array_oid] if info is not None else None

    info = TypeInfo.fetch(conn, "halfvec")
    if info is not None:
        register_halfvec_info(conn, info)
        entry["halfvec"] = [info.oid, info.array_oid]
    else:
        entry["halfvec"] = None

    info = TypeInfo.fetch(conn, "sparsevec")
    if info is not None:
        register_sparsevec_info(conn, info)
        entry["sparsevec"] = [info.oid, info.array_oid]
    else:
        entry["sparsevec"] = None

    return entry


def _register_pgvector_types(
    conn: Any, cache_key: str, *, refresh_types: bool
) -> None:
    """Register pgvector's vector/bit/halfvec/sparsevec types on ``conn``.

    Cache hit (and not ``refresh_types``): reconstruct TypeInfo from
    ``~/.knowledge/pg_types_cache.json`` — zero extra round trips. Cache miss,
    invalid cached entry, or ``refresh_types=True``: fetch from the server
    like upstream ``register_vector`` and (re)write the cache.
    """
    import psycopg.types  # type: ignore

    cache = _load_pg_types_cache()
    entry = cache.get(cache_key) if not refresh_types else None
    if entry is not None and _entry_is_valid(entry):
        _register_from_cache(psycopg.types, conn, entry)
        return

    entry = _register_from_server(psycopg.types, conn)
    cache[cache_key] = entry
    _write_pg_types_cache(cache)


class PostgresBackend:
    """psycopg3 + pgvector adapter for the shared backend."""

    name: ClassVar[str] = "postgresql"

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings

    def connect(self, *, refresh_types: bool = False) -> Any:
        from .. import settings as settings_mod

        psycopg = _require_psycopg()
        _require_pgvector()

        dsn = settings_mod.resolve_pg_dsn(self._settings)
        conninfo = psycopg.conninfo.conninfo_to_dict(dsn)

        connect_kwargs: dict = {}
        if "gssencmode" not in conninfo and not os.environ.get("PGGSSENCMODE"):
            # Skip a GSSAPI negotiation round trip against servers that don't
            # speak it. Explicit user settings (DSN or PGGSSENCMODE) win.
            connect_kwargs["gssencmode"] = "disable"

        # autocommit=False so caller code uses ``with backend.transaction(conn):``
        # consistently with the SQLite path.
        conn = psycopg.connect(dsn, autocommit=False, **connect_kwargs)

        host = conninfo.get("host") or ""
        port = int(conninfo.get("port") or 5432)
        dbname = conninfo.get("dbname") or conninfo.get("user") or ""
        cache_key = _pg_types_cache_key(host, port, dbname)
        _register_pgvector_types(conn, cache_key, refresh_types=refresh_types)
        return conn

    @contextmanager
    def transaction(self, conn: Any):
        # psycopg3 connection ``with`` block manages a transaction boundary
        # for us — commit on clean exit, rollback on exception. Nested
        # ``transaction()`` calls become savepoints, matching APSW
        # semantics closely enough that callers don't have to care.
        with conn.transaction():
            yield

    def advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> AbstractContextManager:
        # Held for the duration of the surrounding xact — that's why this
        # MUST be called inside ``backend.transaction(conn)``. The lock
        # auto-releases on commit/rollback; no manual ``pg_advisory_unlock``
        # needed.
        return self._lock_ctx(conn, project_id, exclusive=exclusive, blocking=True)

    def connection_error_types(self) -> tuple[type[BaseException], ...]:
        # psycopg raises OperationalError for a dropped/refused connection and
        # InterfaceError when operating on an already-broken connection. Both
        # mean "PG is unreachable" → buffer the write locally and retry later.
        # Imported lazily so this is safe even before the optional extra is
        # installed (returns () so buffering simply never fires).
        try:
            psycopg = _require_psycopg()
        except _DependencyMissing:
            return ()
        return (psycopg.OperationalError, psycopg.InterfaceError)

    def try_advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> bool:
        sql = (
            "SELECT pg_try_advisory_xact_lock(%s, %s)"
            if exclusive
            else "SELECT pg_try_advisory_xact_lock_shared(%s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (_LOCK_NAMESPACE, project_id))
            row = cur.fetchone()
            return bool(row and row[0])

    @contextmanager
    def _lock_ctx(self, conn: Any, project_id: int, *, exclusive: bool, blocking: bool):
        if not blocking:
            # try_advisory_lock_project handles the non-blocking variant.
            ok = self.try_advisory_lock_project(
                conn, project_id, exclusive=exclusive
            )
            if not ok:
                raise RuntimeError(
                    f"project {project_id} is being indexed by another client; retry"
                )
            yield
            return
        sql = (
            "SELECT pg_advisory_xact_lock(%s, %s)"
            if exclusive
            else "SELECT pg_advisory_xact_lock_shared(%s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (_LOCK_NAMESPACE, project_id))
        yield
        # No release — pg_advisory_xact_* releases at txn end.

    # -----------------------------------------------------------------------
    # Schema bootstrap. Used by ``knowledge db init-postgres``.
    # -----------------------------------------------------------------------

    def apply_schema(self, conn: Any) -> list[str]:
        """Run every NNN_*.sql migration in ``knowledge.schema.postgres``.

        Returns the list of file basenames that were applied. Each file is
        idempotent (uses IF NOT EXISTS), so re-running this is safe; the
        return value is mostly for human-readable output, not state tracking.
        """

        from ..schema import postgres as schema_pkg

        applied: list[str] = []
        with conn.transaction():
            for path in schema_pkg.list_migrations():
                sql = path.read_text("utf-8")
                with conn.cursor() as cur:
                    cur.execute(sql)
                applied.append(path.name)
        return applied
