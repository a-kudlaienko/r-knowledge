"""Database connection pooling for the billing service."""

import sqlite3

DEFAULT_POOL_SIZE = 10


def connect_db(path: str) -> sqlite3.Connection:
    """Open a SQLite connection to the billing database."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class ConnectionPool:
    """Reusable pool of database connections for billing queries."""

    def __init__(self, path: str, size: int = DEFAULT_POOL_SIZE):
        self.path = path
        self.size = size
        self._pool = []

    def acquire(self) -> sqlite3.Connection:
        if self._pool:
            return self._pool.pop()
        return connect_db(self.path)

    def release(self, conn: sqlite3.Connection) -> None:
        if len(self._pool) < self.size:
            self._pool.append(conn)
        else:
            conn.close()
