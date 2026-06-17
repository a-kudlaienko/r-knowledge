"""Local failure buffer for user-authored writes when the shared DB is down.

On ``shared_postgresql``, a dropped/refused connection must not lose a
``knowledge decide`` (or direct ``history add``) or dump a traceback. Instead
the entry is appended here as one JSON line under
``~/.knowledge/stage/<slug>/outbox.jsonl`` (``paths.outbox_file``) and replayed
by :func:`drain` on the next reachable command.

Design notes:

* **Failure buffer, not a workflow.** Distinct from the history *stage*
  (``sess-*.jsonl``), which is an intentional file-first accumulation flushed
  at session end. The outbox only fills when a write actually couldn't reach
  the DB — on SQLite it never fills (``db.offline_errors()`` is empty there).
* **Embeddings are recomputed at drain**, not stored — keeps the buffer small
  and avoids model/version skew; the local model is cheap.
* **project_id is resolved at drain** from the entry's ``root`` via
  ``projects.get_or_create_project`` (the buffered id from a different host /
  fresh DB would be meaningless).
* **Drain is crash-safe**: the file is atomically *claimed* by rename, replayed
  one transaction per entry; if the DB drops again mid-drain the remainder is
  re-queued; an entry that can never apply (poison) goes to a ``.deadletter``
  sibling so it never blocks the queue or is silently lost.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import db, decisions, history, paths, projects
from .sanitizer import scrub_text

# Accepted entry kinds → the module whose ``add`` replays them.
_KINDS = ("decision", "history")


def append(kind: str, root: Path, payload: dict[str, Any]) -> Path:
    """Buffer one user-authored write. Returns the outbox path it landed in.

    ``payload`` is the keyword args for the target ``add`` MINUS ``project_id``
    (re-resolved from ``root`` at drain). fsync'd — durability is the whole
    point when the DB is unreachable.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown outbox kind {kind!r}")
    # Scrub secrets from the free-text memory fields before writing to disk.
    # Structural fields (kind, ts, root, files_touched list, session_id, author,
    # supersedes, override_reason) are left intact — only prose that can carry
    # embedded secrets is touched.  Done on a shallow copy so the caller's dict
    # is not mutated.
    payload = dict(payload)
    if kind == "history":
        for field in ("short_summary", "long_summary"):
            if isinstance(payload.get(field), str):
                payload[field] = scrub_text(payload[field])
    elif kind == "decision":
        for field in ("topic", "decision", "rationale"):
            if isinstance(payload.get(field), str):
                payload[field] = scrub_text(payload[field])
    path = paths.outbox_file(root)
    entry = {"kind": kind, "ts": time.time(), "root": str(root), "payload": payload}
    line = json.dumps(entry, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return path


def pending_count(root: Path) -> int:
    """How many entries are currently buffered for ``root`` (0 if none)."""
    path = paths.outbox_file(root)
    if not path.exists():
        return 0
    entries, _ = _parse_lines(path.read_text("utf-8"))
    return len(entries)


def drain(conn, root: Path) -> int:
    """Replay buffered entries for ``root`` against ``conn``. Returns the count
    applied. No-op (returns 0) when nothing is buffered.

    Safe to call at the top of any command that already holds a live
    connection — that's how a backlog auto-syncs on the next reachable run.
    """
    path = paths.outbox_file(root)
    if not path.exists():
        return 0
    # Atomically claim the file so a concurrent drain can't double-apply, and
    # so offline writes during the drain land in a fresh outbox we merge into.
    claim = path.with_name(f"outbox.draining-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        os.rename(path, claim)
    except (FileNotFoundError, OSError):
        return 0

    entries, _ = _parse_lines(Path(claim).read_text("utf-8"))
    offline = db.offline_errors()
    applied = 0
    requeue: list[dict] = []
    deadletter: list[dict] = []

    for i, entry in enumerate(entries):
        try:
            _apply(conn, entry)
            applied += 1
        except offline as exc:  # DB went away again mid-drain — keep the rest
            requeue.extend(entries[i:])
            print(
                f"note: shared DB unreachable while syncing "
                f"({len(requeue)} entr{'y' if len(requeue) == 1 else 'ies'} "
                f"still buffered): {exc}",
            )
            break
        except Exception as exc:  # noqa: BLE001 — poison entry, never blocks the queue
            deadletter.append({"entry": entry, "error": repr(exc)})

    if requeue:
        _append_lines(path, requeue)
    if deadletter:
        _append_lines(
            path.with_name("outbox.deadletter.jsonl"), deadletter, raw=True
        )
    try:
        os.remove(claim)
    except OSError:
        pass
    return applied


def _apply(conn, entry: dict) -> None:
    """Replay one buffered entry into the DB."""
    root = Path(entry["root"])
    proj = projects.get_or_create_project(conn, root)
    payload = entry["payload"]
    kind = entry["kind"]
    if kind == "decision":
        decisions.add(conn, project_id=proj.id, **payload)
    elif kind == "history":
        history.add(conn, project_id=proj.id, **payload)
    else:
        raise ValueError(f"unknown outbox kind {kind!r}")


def _parse_lines(raw: str) -> tuple[list[dict], int]:
    """Parse JSONL into valid entries + a skip-count of malformed lines."""
    entries: list[dict] = []
    skipped = 0
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (
            not isinstance(obj, dict)
            or obj.get("kind") not in _KINDS
            or not isinstance(obj.get("payload"), dict)
            or not isinstance(obj.get("root"), str)
        ):
            skipped += 1
            continue
        entries.append(obj)
    return entries, skipped


def _append_lines(path: Path, objs: list[dict], *, raw: bool = False) -> None:
    """Append JSON lines, fsync'd. ``raw`` writes objs as-is (deadletter)."""
    with open(path, "a", encoding="utf-8") as f:
        for obj in objs:
            f.write(json.dumps(obj, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
