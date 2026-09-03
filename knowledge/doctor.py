"""Pure check logic for ``knowledge doctor`` — no printing, no writes.

"Is my index healthy and complete?" has no single existing answer: it's
scattered across ``status`` (freshness), ``db ping`` (connectivity),
``config check-env`` (PG env), ``daemon status`` (running/pid/model). This
module answers it in one place with seven independent, read-only checks:

  1. backend            — can we connect to the configured storage backend?
  2. schema_version     — stored vs compiled; a mismatch forces a rebuild.
  3. freshness          — fresh/stale/missing for the resolved project.
  4. embedding_model     — resolved model name; is it already cached locally?
  5. hooks              — are the history-ingest + re-index hooks registered?
  6. git_head_coherence — what we can (and can't) say about index-vs-HEAD.
  7. counts             — file/chunk counts + one cheap orphan-row check.

Design notes:

* Every ``check_*`` function is pure: it takes plain arguments, returns a
  :class:`CheckResult`, and never prints. ``knowledge/cli.py`` owns all
  rendering (prose and ``--format json``), matching how ``resume.py`` /
  ``consolidate.py`` separate their pure ``build()`` from the ``cmd_*``
  handler that renders it.
* :func:`run` wraps every check in :func:`_run_check`, which converts *any*
  exception into a single failing :class:`CheckResult` for that check only.
  A bug in one check (or in a dependency it calls) must never abort the
  whole report — "still produces a complete report on a broken system" is
  this module's entire reason to exist.
* Nothing here calls ``db.execute``/``db.execute_returning_id`` (the only
  write paths ``knowledge/db.py`` exposes) or any settings/hook-writing
  helper. Every check is a pure read.
* ``probe_backend``/``_probe_sqlite``/``_probe_postgres``/``BackendProbe``
  are the same connectivity probe ``knowledge db ping`` uses — extracted
  here so the two verbs share one implementation instead of two that could
  silently drift apart. ``knowledge/cli.py``'s ``_ping_sqlite``/
  ``_ping_postgres`` are thin formatters over these.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, db, paths, projects


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome. ``status`` is one of "ok" / "warn" / "fail"."""

    name: str
    status: str
    detail: str
    remedy: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate of every check, in the order they were run."""

    checks: tuple[CheckResult, ...]

    @property
    def summary(self) -> dict[str, int]:
        out = {"ok": 0, "warn": 0, "fail": 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        """Warnings don't fail the report — only an explicit "fail" does."""
        return self.summary["fail"] == 0

    @property
    def exit_code(self) -> int:
        # 0 = all ok (warnings allowed), 1 = at least one check failed.
        # Exit codes 2/3/4/70 already have fixed meanings elsewhere in this
        # CLI and 5 is reserved for a future `gate` verb — doctor must not
        # invent a new one.
        return 0 if self.ok else 1


@dataclass(frozen=True)
class BackendProbe:
    """Raw facts from one connection attempt to the configured backend.

    Shared by ``knowledge db ping`` (formats these into its pre-existing,
    unchanged prose) and the "backend" check below — one source of truth
    for "how do we test connectivity" so the two verbs can't drift apart.
    """

    mode: str  # "sqlite" | "shared_postgresql"
    ok: bool
    error: str | None = None
    error_kind: str | None = None  # "dependency_missing" | "dsn" | "connection" | None

    # sqlite-only
    db_path: str | None = None
    schema_version: str | None = None
    chunk_count: int | None = None

    # shared_postgresql-only
    pg_version: str | None = None
    pgvector_version: str | None = None
    db_name: str | None = None
    db_user: str | None = None
    schema_ok: bool | None = None
    embeddings_ok: bool | None = None
    project_count: int | None = None


def probe_backend(settings) -> BackendProbe:
    """Dispatch to the right probe for ``settings.mode``."""
    if settings.mode == "sqlite":
        return _probe_sqlite()
    if settings.mode == "shared_postgresql":
        return _probe_postgres(settings)
    return BackendProbe(mode=settings.mode, ok=False, error=f"unknown storage mode {settings.mode!r}")


def _probe_sqlite() -> BackendProbe:
    """Connect, run one trivial read, close.

    Mirrors the pre-existing body of ``cli._ping_sqlite`` exactly (same
    query, same fields) — extracted here so `db ping` and `doctor` can't
    silently diverge.
    """
    try:
        with db.connect() as conn:
            schema_version = db.get_meta(conn, "schema_version")
            chunk_count = db.fetch_one(conn, "SELECT COUNT(*) FROM chunks")[0]
    except Exception as exc:  # noqa: BLE001 — APSW has many failure modes
        return BackendProbe(mode="sqlite", ok=False, error=str(exc), db_path=str(paths.db_path()))
    return BackendProbe(
        mode="sqlite",
        ok=True,
        db_path=str(paths.db_path()),
        schema_version=schema_version or "unknown",
        chunk_count=chunk_count,
    )


def _probe_postgres(settings) -> BackendProbe:
    """Connect, gather version/extension/schema facts, close.

    Mirrors the pre-existing body of ``cli._ping_postgres`` — see
    ``_probe_sqlite``.
    """
    from . import settings as settings_mod
    from .backends.postgres import PostgresBackend, _DependencyMissing

    backend = PostgresBackend(settings)
    try:
        conn = backend.connect(refresh_types=True)
    except _DependencyMissing as exc:
        return BackendProbe(mode="shared_postgresql", ok=False, error=str(exc), error_kind="dependency_missing")
    except settings_mod.DsnError as exc:
        return BackendProbe(mode="shared_postgresql", ok=False, error=str(exc), error_kind="dsn")
    except Exception as exc:  # noqa: BLE001 — psycopg has many failure modes
        return BackendProbe(mode="shared_postgresql", ok=False, error=str(exc), error_kind="connection")

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg_full = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            pgvector_version = row[0] if row else None
            cur.execute("SELECT current_database(), current_user")
            db_name, db_user = cur.fetchone()
            cur.execute(
                "SELECT to_regclass('public.projects') IS NOT NULL, "
                "       to_regclass('public.chunk_embeddings') IS NOT NULL"
            )
            schema_ok, embeddings_ok = cur.fetchone()
            project_count = 0
            if schema_ok:
                cur.execute("SELECT COUNT(*) FROM projects")
                project_count = cur.fetchone()[0]
    finally:
        conn.close()

    return BackendProbe(
        mode="shared_postgresql",
        ok=True,
        pg_version=pg_full,
        pgvector_version=pgvector_version,
        db_name=db_name,
        db_user=db_user,
        schema_ok=bool(schema_ok),
        embeddings_ok=bool(embeddings_ok),
        project_count=project_count,
    )


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _resolve_project_for_check(
    conn, project: str | None, check_name: str
) -> tuple[projects.Project | None, CheckResult | None]:
    """Resolve a project selector, turning both failure modes (unregistered,
    ambiguous) into a ready-to-return :class:`CheckResult` instead of an
    exception — shared by every check that needs exactly one project row.
    """
    try:
        proj = projects.resolve_project(conn, project)
    except projects.AmbiguousProjectName as exc:
        return None, CheckResult(
            check_name,
            "fail",
            f"ambiguous project selector: {exc}",
            remedy="pass --project with an absolute repo path",
        )
    if proj is None:
        where = project or str(projects.current_project_root())
        return None, CheckResult(
            check_name, "fail", f"project not registered: {where}", remedy="knowledge build"
        )
    return proj, None


def _format_epoch(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_backend() -> CheckResult:
    """1. Backend reachability — mode + can we connect?"""
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        return CheckResult("backend", "fail", f"config error: {exc}", remedy="knowledge config show")

    probe = probe_backend(s)
    if not probe.ok:
        return CheckResult("backend", "fail", f"{probe.mode}: {probe.error}", remedy="knowledge db ping")

    if probe.mode == "sqlite":
        return CheckResult(
            "backend",
            "ok",
            f"sqlite ok — schema_version={probe.schema_version}, "
            f"{probe.chunk_count} chunk(s) at {probe.db_path}",
        )

    # shared_postgresql: the connection itself succeeded, but the extension
    # or schema might still be missing. `db ping` reports that as prose
    # without failing (existing behavior, unchanged); `doctor` treats it as
    # a real problem since the tool is unusable without both.
    missing = []
    if not probe.pgvector_version:
        missing.append("pgvector extension")
    if not (probe.schema_ok and probe.embeddings_ok):
        missing.append("schema")
    if missing:
        return CheckResult(
            "backend",
            "fail",
            f"shared_postgresql connected as {probe.db_user}@{probe.db_name} but missing: {', '.join(missing)}",
            remedy="knowledge db init-postgres",
        )
    return CheckResult(
        "backend",
        "ok",
        f"shared_postgresql ok — {probe.db_name} as {probe.db_user}, "
        f"pgvector {probe.pgvector_version}, {probe.project_count} project(s)",
    )


def check_schema_version() -> CheckResult:
    """2. Stored ``meta.schema_version`` vs the compiled ``config.SCHEMA_VERSION``.

    A mismatch means every chunk in the DB was produced by a different
    chunker/schema generation — ``knowledge build`` is a forced full
    rebuild in that case, not a minor drift.
    """
    with db.connect() as conn:
        stored = db.get_meta(conn, "schema_version")

    if stored is None:
        return CheckResult("schema_version", "warn", "no schema_version recorded yet", remedy="knowledge build")
    if stored != config.SCHEMA_VERSION:
        return CheckResult(
            "schema_version",
            "fail",
            f"stored={stored} compiled={config.SCHEMA_VERSION} — mismatch forces a rebuild",
            remedy="knowledge build",
        )
    return CheckResult("schema_version", "ok", f"{stored} (matches compiled)")


def check_freshness(project: str | None) -> CheckResult:
    """3. fresh/stale/missing for the resolved project.

    Reuses ``cli._project_is_stale`` — the exact mtime-based staleness rule
    ``knowledge status`` uses — rather than reimplementing the hashing.
    """
    from . import cli as cli_mod

    with db.connect() as conn:
        proj, failure = _resolve_project_for_check(conn, project, "freshness")
        if failure is not None:
            return failure
        is_stale = cli_mod._project_is_stale(conn, proj)

    if is_stale:
        return CheckResult("freshness", "warn", f"{proj.name} is stale", remedy="knowledge update")
    return CheckResult("freshness", "ok", f"{proj.name} is fresh")


def check_embedding_model() -> CheckResult:
    """4. Resolved embedding model name, and whether it's already cached
    locally — never downloads it to find out.

    Model resolution mirrors ``embedder.py``'s ``_ensure_loaded()``: a user
    override (``settings.embedding_model``) wins, else the built-in default
    (``config.MODEL``). The on-disk cache-dir naming
    (``models--<org>--<name>``) is duplicated here rather than imported
    from ``embedder.py`` on purpose: this keeps this read-only check from
    ever importing sentence-transformers/torch, a multi-second heavyweight
    import it has no business paying. If that naming ever changes, update
    both places.
    """
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        return CheckResult("embedding_model", "fail", f"config error: {exc}")

    model_name = (s.embedding_model or "").strip() or config.MODEL
    model_slug = model_name.replace("/", "--")
    model_dir = paths.models_dir() / f"models--{model_slug}"

    if model_dir.exists():
        return CheckResult("embedding_model", "ok", f"{model_name} — cached at {model_dir}")
    return CheckResult(
        "embedding_model", "warn", f"{model_name} — not cached yet; the next query downloads ~130MB"
    )


def check_hooks() -> CheckResult:
    """5. Are the history-ingest hooks AND the PostToolUse re-index hook
    registered, in either settings.json scope?

    ``_HOOK_SPECS`` (in ``cli.py``) is the installer's own source of truth
    for what "installed" means — reused here via ``_find_hook_command`` so
    this check can never drift from what ``install-hooks`` actually writes.
    A missing re-index hook is treated as a real failure (the index quietly
    rots after every edit); a missing history-ingest hook is a warning.
    """
    from . import cli as cli_mod

    scopes = {
        "user": Path.home() / ".claude" / "settings.json",
        "project": Path.cwd() / ".claude" / "settings.json",
    }
    parsed = {name: _read_settings_json(path) for name, path in scopes.items()}

    parts: list[str] = []
    any_missing = False
    reindex_missing = False
    for spec in cli_mod._HOOK_SPECS:
        found_scope = None
        for scope_name, settings in parsed.items():
            if settings is None:
                continue
            hooks = settings.get("hooks")
            if not isinstance(hooks, dict):
                continue
            for event in spec.events:
                event_list = hooks.get(event, [])
                if isinstance(event_list, list) and cli_mod._find_hook_command(event_list, spec.signature):
                    found_scope = scope_name
                    break
            if found_scope:
                break

        is_reindex = "PostToolUse" in spec.events
        if found_scope:
            parts.append(f"{spec.signature}: ok ({found_scope})")
        else:
            any_missing = True
            if is_reindex:
                reindex_missing = True
                parts.append(f"{spec.signature}: MISSING (index will rot after edits)")
            else:
                parts.append(f"{spec.signature}: MISSING")

    detail = "; ".join(parts)
    if reindex_missing:
        return CheckResult("hooks", "fail", detail, remedy="knowledge install-hooks")
    if any_missing:
        return CheckResult("hooks", "warn", detail, remedy="knowledge install-hooks")
    return CheckResult("hooks", "ok", detail)


def _read_settings_json(path: Path) -> dict | None:
    """Best-effort parse; ``None`` on missing/unreadable/malformed.

    A broken settings.json must not crash the health check that's supposed
    to catch exactly this kind of thing.
    """
    if not path.exists():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_git_head_coherence(project: str | None) -> CheckResult:
    """6. Report what we can about "does the index reflect the current
    checkout" — and say plainly what we can't.

    No table anywhere stores "this project's index was built while git was
    at commit X":

    * ``query_cache`` IS keyed on ``head_sha``, but it's a per-*query*,
      1-hour-TTL answer cache (see ``knowledge/query_cache.py``) — a hit
      there means one past ``ask`` call was cached at that HEAD, not that
      the whole index reflects it. Using it as a build marker here would
      imply a comparison this system cannot actually make.
    * ``projects.last_build``/``last_update`` are plain epoch timestamps,
      with no git SHA attached.

    Persisting a real per-project HEAD is out of scope here on purpose: it
    would need a new column, which means a ``SCHEMA_VERSION`` bump forcing
    every user through a full rebuild, just for a diagnostic nicety.

    So: report current HEAD + last index activity, and say the comparison
    itself can't be made, honestly, instead of implying one. This always
    reports "warn" — it is fundamentally informational, never a pass/fail
    signal on its own; ``freshness`` is the closest available signal for
    "is this index stale".
    """
    from . import query_cache as query_cache_mod

    with db.connect() as conn:
        try:
            proj = projects.resolve_project(conn, project)
        except projects.AmbiguousProjectName:
            proj = None

    # HEAD is computed from the resolved PROJECT's root when we have one —
    # cwd is only a fallback for the "nothing registered" case, since a
    # caller can legitimately run `doctor --project <other-repo>` from
    # somewhere else entirely.
    root = proj.root_path if proj is not None else projects.current_project_root()
    head = query_cache_mod.get_head_sha(root)
    head_display = head[:12] if head else "unknown (not a git repo, or git unavailable)"

    if proj is None:
        return CheckResult(
            "git_head_coherence",
            "warn",
            f"HEAD={head_display}; no per-project HEAD is stored anywhere "
            "(query_cache is query-scoped with a 1h TTL, not a build marker; "
            "last_build/last_update carry no git SHA) — nothing registered "
            "to compare against, and no true comparison would be possible "
            "even if there were",
        )

    last = proj.last_update or proj.last_build
    last_display = _format_epoch(last) if last else "never"
    return CheckResult(
        "git_head_coherence",
        "warn",
        f"HEAD={head_display}; last index activity={last_display} — no "
        "per-project HEAD is stored, so this is not a true comparison; "
        "see 'freshness' for the closest available signal",
    )


def check_counts(project: str | None) -> CheckResult:
    """7. Files/chunks for the project, plus cheap invariant checks:
    orphaned chunks (``file_id`` with no surviving ``files`` row), and the
    cached ``projects.file_count``/``chunk_count`` denormals drifting from
    the actual ``COUNT(*)``.

    Both queries are scoped by ``project_id`` and backed by
    ``idx_chunks_project`` (see ``knowledge/db.py``'s schema) — proportional
    to one project's row count, not a full-table scan.
    """
    with db.connect() as conn:
        proj, failure = _resolve_project_for_check(conn, project, "counts")
        if failure is not None:
            return failure

        actual_files = db.fetch_one(conn, "SELECT COUNT(*) FROM files WHERE project_id = ?", (proj.id,))[0]
        actual_chunks = db.fetch_one(conn, "SELECT COUNT(*) FROM chunks WHERE project_id = ?", (proj.id,))[0]
        orphans = db.fetch_one(
            conn,
            "SELECT COUNT(*) FROM chunks c LEFT JOIN files f ON f.id = c.file_id "
            "WHERE c.project_id = ? AND f.id IS NULL",
            (proj.id,),
        )[0]

    problems: list[str] = []
    if orphans:
        problems.append(f"{orphans} orphaned chunk(s) (file_id has no surviving files row)")
    if proj.file_count != actual_files:
        problems.append(f"cached file_count={proj.file_count} != actual {actual_files}")
    if proj.chunk_count != actual_chunks:
        problems.append(f"cached chunk_count={proj.chunk_count} != actual {actual_chunks}")

    detail = f"{proj.name}: {actual_files} file(s), {actual_chunks} chunk(s)"
    if problems:
        return CheckResult("counts", "fail", f"{detail} — {'; '.join(problems)}", remedy="knowledge build")
    if actual_files == 0 and actual_chunks == 0:
        return CheckResult("counts", "warn", f"{detail} — empty index", remedy="knowledge build")
    return CheckResult("counts", "ok", detail)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_check(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """Execute one check, converting ANY exception into a `fail` result.

    This is the entire mechanism behind "never crash": a bug in one check
    (or in a dependency it calls) becomes a single failing line instead of
    an exception that would abort ``run()`` and silently drop every check
    after it — producing a complete report even when the system under
    inspection is broken is this module's whole purpose.
    """
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — deliberately catches everything
        return CheckResult(name, "fail", f"{type(exc).__name__}: {exc}")
    if not isinstance(result, CheckResult):
        # Defensive: a buggy check body returning the wrong type must still
        # surface as a report line, not an AttributeError deeper in cli.py.
        return CheckResult(name, "fail", f"check returned {type(result).__name__}, not CheckResult")
    return result


def run(project: str | None = None) -> DoctorReport:
    """Run every check independently and return the aggregate report.

    Order matches the numbered list above: backend -> schema -> freshness
    -> embedding model -> hooks -> git/HEAD -> counts.
    """
    checks = (
        _run_check("backend", check_backend),
        _run_check("schema_version", check_schema_version),
        _run_check("freshness", lambda: check_freshness(project)),
        _run_check("embedding_model", check_embedding_model),
        _run_check("hooks", check_hooks),
        _run_check("git_head_coherence", lambda: check_git_head_coherence(project)),
        _run_check("counts", lambda: check_counts(project)),
    )
    return DoctorReport(checks=checks)
