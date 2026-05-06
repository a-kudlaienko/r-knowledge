"""Project registry + current-repo detection.

A "project" is one indexed repo. Keyed by canonicalized absolute path, so
symlinked worktrees and relative-path variants all resolve to the same row.

APSW note: there are no ``.commit()`` calls — APSW auto-commits outside of
explicit transaction blocks. For the mutation patterns in this module that's
exactly what we want.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from .db import Connection


class Project(NamedTuple):
    id: int
    name: str
    root_path: Path
    git_remote: str | None
    created_at: float
    last_build: float | None
    last_update: float | None
    file_count: int
    chunk_count: int


class AmbiguousProjectName(Exception):
    """Raised when a project-name selector matches multiple rows.

    ``name`` is only a display label; ``root_path`` is the primary key. Two
    clones of the same repo at different paths register as distinct rows
    with the same default name. Callers must disambiguate by passing an
    absolute root path.
    """

    def __init__(self, name: str, matches: list[Project]) -> None:
        self.name = name
        self.matches = matches
        super().__init__(
            f"project name '{name}' matches {len(matches)} projects"
        )


_SELECT_COLS = (
    "id, name, root_path, git_remote, created_at, last_build, "
    "last_update, file_count, chunk_count"
)


def current_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default cwd) until we find a ``.git/`` dir.

    Falls back to the start directory if no git root is found — callers that
    require a git repo should check explicitly.
    """
    p = (start or Path.cwd()).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p


def _git_remote(root: Path) -> str | None:
    """Best-effort origin URL; ``None`` if not a git repo or no origin set."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def get_or_create_project(
    conn: Connection,
    root: Path,
    name_override: str | None = None,
) -> Project:
    """Return the project row for ``root``, creating it if missing."""
    root = root.resolve()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
        (str(root),),
    ).fetchone()
    if row:
        return _row_to_project(row)

    name = name_override or root.name
    now = time.time()
    remote = _git_remote(root)
    conn.execute(
        "INSERT INTO projects(name, root_path, git_remote, created_at) "
        "VALUES (?, ?, ?, ?)",
        (name, str(root), remote, now),
    )
    # APSW: get the autoincrement PK of the row we just inserted.
    new_id = conn.last_insert_rowid()
    return Project(
        id=new_id,
        name=name,
        root_path=root,
        git_remote=remote,
        created_at=now,
        last_build=None,
        last_update=None,
        file_count=0,
        chunk_count=0,
    )


def resolve_project(
    conn: Connection,
    selector: str | None,
) -> Project | None:
    """Resolve a project by name or absolute path. Returns None if unknown.

    When ``selector`` is None, uses the current git root (cwd-based). Does
    NOT create the project — use ``get_or_create_project`` for that.

    Raises :class:`AmbiguousProjectName` if a non-absolute ``selector``
    matches more than one project — e.g. the same repo built from two
    clones. Callers must present the colliding roots and re-query with an
    absolute path.
    """
    if selector is None:
        root = current_project_root()
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
            (str(root),),
        ).fetchone()
        return _row_to_project(row) if row else None

    p = Path(selector).expanduser()
    if p.is_absolute():
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
            (str(p.resolve()),),
        ).fetchone()
        return _row_to_project(row) if row else None

    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM projects WHERE name = ?",
        (selector,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousProjectName(
            selector, [_row_to_project(r) for r in rows]
        )
    return _row_to_project(rows[0])


def list_projects(conn: Connection) -> list[Project]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM projects ORDER BY name"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def list_projects_by_name(conn: Connection, name: str) -> list[Project]:
    """All rows sharing ``name``. Multiple rows = same-named repos at
    different roots — legal, but ambiguous for name-based selectors.
    """
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM projects WHERE name = ?",
        (name,),
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def next_free_suffix(conn: Connection, base: str) -> str:
    """Return ``f"{base}_{N}"`` with the smallest N >= 2 that's unused.

    Used at build time when the requested short name collides with an
    existing project and the user wants to keep both.
    """
    taken = {
        r[0]
        for r in conn.execute("SELECT name FROM projects").fetchall()
    }
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def forget_project(conn: Connection, project_id: int) -> None:
    """Cascade-delete a project. Files + chunks go with it (FK ON DELETE CASCADE).

    Vector rows are orphaned but harmless — they're filtered out by the JOIN
    on ``chunks.project_id`` at search time. A future ``knowledge vacuum``
    can purge them if size becomes an issue.
    """
    conn.execute(
        "DELETE FROM chunks_vec WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE project_id = ?)",
        (project_id,),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def update_counts(conn: Connection, project_id: int) -> None:
    """Refresh ``file_count`` + ``chunk_count`` denormals after mutation."""
    fc = conn.execute(
        "SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    cc = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE projects SET file_count = ?, chunk_count = ? WHERE id = ?",
        (fc, cc, project_id),
    )


def _row_to_project(row) -> Project:
    return Project(
        id=row[0],
        name=row[1],
        root_path=Path(row[2]),
        git_remote=row[3],
        created_at=row[4],
        last_build=row[5],
        last_update=row[6],
        file_count=row[7],
        chunk_count=row[8],
    )
