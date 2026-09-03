"""Tests for `knowledge doctor` (`knowledge/doctor.py` + the `doctor` CLI verb).

Read-only health report: 7 independent checks (backend, schema_version,
freshness, embedding_model, hooks, git_head_coherence, counts). Style
follows tests/test_error_envelope.py (exception-contract assertions) and
tests/test_json_contract.py (isolated_db/seeded_project fixtures, `_one_line`).

Covers:
  1. Each check function in isolation: a healthy case and a failing case.
  2. `_run_check`/`run()`: a check that raises internally is reported as a
     single `fail` result, and every OTHER check still completes normally
     — the "still works on a broken system" property this module exists
     for.
  3. Missing/ambiguous project -> a clean CheckResult, never a crash.
  4. `--format json` emits exactly one parseable line with the documented
     `{"ok", "checks", "summary"}` shape; default prose emits one line per
     check.
  5. Exit code: 0 when every check is ok/warn, 1 when any check failed.
  6. `doctor` never writes: `db.execute`/`db.execute_returning_id` (the
     only two write paths `knowledge/db.py` exposes) are monkeypatched to
     raise, and a full `run()` still completes with no check reporting
     that sentinel failure.

No embedder needed — none of the seven checks touch chunk embeddings.
`check_hooks()`/`check_git_head_coherence()` read `$HOME`/cwd/git, so
`isolated_db` here also redirects `HOME` and cwd into `tmp_path` (beyond
just `KNOWLEDGE_HOME`) — without that, these tests would read the real
machine's `~/.claude/settings.json` and the real repo's git HEAD.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest

from knowledge import cli, config, db, doctor, paths, projects


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Redirect KNOWLEDGE_HOME, $HOME, and cwd.

    `check_hooks()` reads `~/.claude/settings.json` and
    `<cwd>/.claude/settings.json`; `check_git_head_coherence()` shells out
    to `git rev-parse HEAD` relative to the resolved project (falling back
    to cwd when nothing resolves). All three must stay inside tmp_path or
    these tests would read (never write — doctor is read-only) whatever
    happens to be on the machine actually running them.
    """
    home = tmp_path / "knowledge-home"
    home.mkdir()
    monkeypatch.setenv("KNOWLEDGE_HOME", str(home))

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    fake_cwd = tmp_path / "fake-cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    yield home


def _insert_file(conn, project_id: int, rel_path: str, *, size: int) -> int:
    """Minimal `files` row (same column set as tests/test_json_contract.py)."""
    now = time.time()
    return db.execute_returning_id(
        conn,
        "INSERT INTO files(project_id, rel_path, content_hash, mtime, size, lang, last_scanned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, rel_path, "hash-" + rel_path, now, size, "python", now),
    )


def _insert_chunk(
    conn,
    project_id: int,
    file_id: int,
    *,
    name: str = "fn",
    stored: str,
    end_byte: int,
    start_byte: int = 0,
    start_line: int = 1,
    end_line: int = 2,
) -> int:
    """Minimal `chunks` row covering every NOT NULL column."""
    return db.execute_returning_id(
        conn,
        "INSERT INTO chunks(project_id, file_id, kind, name, qualified_name, "
        "start_line, end_line, start_byte, end_byte, char_count, content_hash, "
        "stored_text, embedded_text) VALUES (?, ?, 'function', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id, file_id, name, name, start_line, end_line,
            start_byte, end_byte, len(stored), f"chash-{name}", stored, stored,
        ),
    )


_SRC = "def fn():\n    pass\n"


@pytest.fixture()
def seeded_project(isolated_db, tmp_path):
    """One project, one indexed file ('pkg/mod.py'), one chunk inside it,
    with `file_count`/`chunk_count` refreshed — i.e. the state a real
    `knowledge build` would leave behind (in sync, not drifted).

    Returns (root: Path, proj: projects.Project, chunk_id: int).
    """
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(_SRC)

    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)
        file_id = _insert_file(conn, proj.id, "pkg/mod.py", size=len(_SRC))
        chunk_id = _insert_chunk(conn, proj.id, file_id, stored=_SRC, end_byte=len(_SRC.encode()))
        projects.update_counts(conn, proj.id)
        proj = projects.resolve_project(conn, str(root))
    return root, proj, chunk_id


def _one_line(out: str) -> str:
    """Assert `out` is exactly one non-empty line and return it."""
    lines = out.splitlines()
    assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {out!r}"
    return lines[0]


# ---------------------------------------------------------------------------
# 1. check_backend
# ---------------------------------------------------------------------------


def test_check_backend_healthy(isolated_db):
    with db.connect():
        pass  # bootstrap the sqlite schema
    result = doctor.check_backend()
    assert result.name == "backend"
    assert result.status == "ok"
    assert "sqlite ok" in result.detail


def test_check_backend_settings_error_is_fail(isolated_db, monkeypatch):
    from knowledge import settings as settings_mod

    def _raise(*_a, **_kw):
        raise settings_mod.SettingsError("bad config")

    monkeypatch.setattr(settings_mod, "load_settings", _raise)
    result = doctor.check_backend()
    assert result.status == "fail"
    assert "bad config" in result.detail
    assert result.remedy == "knowledge config show"


# ---------------------------------------------------------------------------
# 2. check_schema_version
# ---------------------------------------------------------------------------


def test_check_schema_version_healthy(isolated_db):
    with db.connect():
        pass
    result = doctor.check_schema_version()
    assert result.status == "ok"
    assert config.SCHEMA_VERSION in result.detail


def test_check_schema_version_mismatch_is_fail(isolated_db):
    with db.connect() as conn:
        db.set_meta(conn, "schema_version", "999")
    result = doctor.check_schema_version()
    assert result.status == "fail"
    assert "999" in result.detail
    assert config.SCHEMA_VERSION in result.detail
    assert result.remedy == "knowledge build"


# ---------------------------------------------------------------------------
# 3. check_freshness
# ---------------------------------------------------------------------------


def test_check_freshness_healthy(seeded_project):
    root, _proj, _cid = seeded_project
    result = doctor.check_freshness(str(root))
    assert result.status == "ok"
    assert "fresh" in result.detail


def test_check_freshness_stale_is_warn(seeded_project):
    root, _proj, _cid = seeded_project
    # Bump the on-disk mtime well past the file's stored mtime + grace.
    future = time.time() + 3600
    import os

    os.utime(root / "pkg" / "mod.py", (future, future))

    result = doctor.check_freshness(str(root))
    assert result.status == "warn"
    assert "stale" in result.detail
    assert result.remedy == "knowledge update"


def test_check_freshness_missing_project_is_fail_not_crash(isolated_db):
    result = doctor.check_freshness("no-such-project")
    assert result.status == "fail"
    assert "not registered" in result.detail
    assert result.remedy == "knowledge build"


# ---------------------------------------------------------------------------
# 4. check_embedding_model
# ---------------------------------------------------------------------------


def test_check_embedding_model_not_cached_is_warn(isolated_db):
    result = doctor.check_embedding_model()
    assert result.status == "warn"
    assert config.MODEL in result.detail
    assert "download" in result.detail


def test_check_embedding_model_cached_is_ok(isolated_db):
    model_slug = config.MODEL.replace("/", "--")
    (paths.models_dir() / f"models--{model_slug}").mkdir(parents=True)
    result = doctor.check_embedding_model()
    assert result.status == "ok"
    assert "cached at" in result.detail


def test_check_embedding_model_settings_error_is_fail(isolated_db, monkeypatch):
    from knowledge import settings as settings_mod

    def _raise(*_a, **_kw):
        raise settings_mod.SettingsError("bad config")

    monkeypatch.setattr(settings_mod, "load_settings", _raise)
    result = doctor.check_embedding_model()
    assert result.status == "fail"
    assert "bad config" in result.detail


# ---------------------------------------------------------------------------
# 5. check_hooks
# ---------------------------------------------------------------------------


def test_check_hooks_missing_reindex_hook_is_fail(isolated_db):
    result = doctor.check_hooks()
    assert result.status == "fail"
    assert "MISSING" in result.detail
    assert result.remedy == "knowledge install-hooks"


def test_check_hooks_installed_is_ok(isolated_db):
    rc = cli.cmd_install_hooks(argparse.Namespace(user=True, absolute=False))
    assert rc == 0
    result = doctor.check_hooks()
    assert result.status == "ok"
    assert "knowledge history ingest: ok (user)" in result.detail
    assert "knowledge update: ok (user)" in result.detail


def test_check_hooks_broken_settings_json_does_not_crash(isolated_db, monkeypatch):
    from pathlib import Path

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json")

    result = doctor.check_hooks()
    assert result.status == "fail"  # re-index hook still absent, just can't see it


# ---------------------------------------------------------------------------
# 6. check_git_head_coherence
# ---------------------------------------------------------------------------


def test_check_git_head_coherence_always_warn_with_project(seeded_project):
    root, _proj, _cid = seeded_project
    result = doctor.check_git_head_coherence(str(root))
    assert result.status == "warn"
    assert "HEAD=" in result.detail
    assert "no per-project HEAD is stored" in result.detail


def test_check_git_head_coherence_missing_project_no_crash(isolated_db):
    result = doctor.check_git_head_coherence("no-such-project")
    assert result.status == "warn"
    assert "nothing registered" in result.detail


# ---------------------------------------------------------------------------
# 7. check_counts
# ---------------------------------------------------------------------------


def test_check_counts_healthy(seeded_project):
    root, _proj, _cid = seeded_project
    result = doctor.check_counts(str(root))
    assert result.status == "ok"
    assert "1 file(s)" in result.detail
    assert "1 chunk(s)" in result.detail


def test_check_counts_cache_drift_is_fail(isolated_db, tmp_path):
    """A file/chunk inserted without the follow-up `update_counts()` call
    leaves the cached denormals at 0 while the actual rows say 1 — exactly
    the kind of drift this check exists to catch."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(_SRC)
    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)
        file_id = _insert_file(conn, proj.id, "pkg/mod.py", size=len(_SRC))
        _insert_chunk(conn, proj.id, file_id, stored=_SRC, end_byte=len(_SRC.encode()))
        # deliberately no update_counts() here

    result = doctor.check_counts(str(root))
    assert result.status == "fail"
    assert "cached file_count=0 != actual 1" in result.detail
    assert "cached chunk_count=0 != actual 1" in result.detail
    assert result.remedy == "knowledge build"


def test_check_counts_orphan_chunk_is_fail(seeded_project):
    """Simulate an orphaned chunk: a `files` row deleted while foreign-key
    enforcement is off for that one statement (the only way to produce one
    on SQLite — `ON DELETE CASCADE` would otherwise remove the chunk too)."""
    root, proj, _cid = seeded_project

    conn = db.connect()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM files WHERE project_id = ?", (proj.id,))
        # Keep the cached file_count in sync so the ONLY signal below is the
        # orphan detection, not a conflated cache-drift finding.
        conn.execute("UPDATE projects SET file_count = 0 WHERE id = ?", (proj.id,))
    finally:
        conn.close()

    result = doctor.check_counts(str(root))
    assert result.status == "fail"
    assert "orphaned chunk" in result.detail


def test_check_counts_missing_project_is_fail_not_crash(isolated_db):
    result = doctor.check_counts("no-such-project")
    assert result.status == "fail"
    assert "not registered" in result.detail


# ---------------------------------------------------------------------------
# DoctorReport / exit code
# ---------------------------------------------------------------------------


def test_exit_code_zero_when_all_ok_or_warn():
    report = doctor.DoctorReport(
        checks=(
            doctor.CheckResult("a", "ok", "..."),
            doctor.CheckResult("b", "warn", "..."),
        )
    )
    assert report.ok is True
    assert report.exit_code == 0
    assert report.summary == {"ok": 1, "warn": 1, "fail": 0}


def test_exit_code_one_when_any_fail():
    report = doctor.DoctorReport(
        checks=(
            doctor.CheckResult("a", "ok", "..."),
            doctor.CheckResult("b", "fail", "..."),
        )
    )
    assert report.ok is False
    assert report.exit_code == 1
    assert report.summary == {"ok": 1, "warn": 0, "fail": 1}


# ---------------------------------------------------------------------------
# run() / _run_check: never crash, isolates exceptions per-check
# ---------------------------------------------------------------------------


def test_run_isolates_a_raising_check_from_the_rest(seeded_project, monkeypatch):
    root, _proj, _cid = seeded_project

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "check_counts", _boom)
    report = doctor.run(str(root))

    assert len(report.checks) == 7
    by_name = {c.name: c for c in report.checks}
    assert by_name["counts"].status == "fail"
    assert "boom" in by_name["counts"].detail
    # Every other check still ran normally — none of them were skipped or
    # themselves crashed as a side effect of `check_counts` blowing up.
    assert by_name["backend"].status == "ok"
    assert by_name["schema_version"].status == "ok"
    assert by_name["freshness"].status == "ok"


def test_run_check_returning_wrong_type_is_reported_as_fail(monkeypatch):
    monkeypatch.setattr(doctor, "check_backend", lambda: "not a CheckResult")
    report = doctor.run(None)
    by_name = {c.name: c for c in report.checks}
    assert by_name["backend"].status == "fail"
    assert "not CheckResult" in by_name["backend"].detail


def test_run_missing_project_no_crash(isolated_db):
    report = doctor.run("no-such-project")
    assert isinstance(report, doctor.DoctorReport)
    assert len(report.checks) == 7
    by_name = {c.name: c for c in report.checks}
    assert by_name["freshness"].status == "fail"
    assert by_name["counts"].status == "fail"


def test_run_full_healthy_report(seeded_project):
    root, _proj, _cid = seeded_project
    rc = cli.cmd_install_hooks(argparse.Namespace(user=True, absolute=False))
    assert rc == 0

    report = doctor.run(str(root))
    assert len(report.checks) == 7
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["backend"] == "ok"
    assert statuses["schema_version"] == "ok"
    assert statuses["freshness"] == "ok"
    assert statuses["embedding_model"] == "warn"  # fresh KNOWLEDGE_HOME: never downloaded
    assert statuses["hooks"] == "ok"
    assert statuses["git_head_coherence"] == "warn"  # always warn, by design
    assert statuses["counts"] == "ok"
    assert report.exit_code == 0


# ---------------------------------------------------------------------------
# No-writes property (the one most cared about)
# ---------------------------------------------------------------------------


def test_doctor_never_writes(seeded_project, monkeypatch):
    """`db.execute`/`db.execute_returning_id` are the only two write paths
    `knowledge/db.py` exposes. Force both to raise; a full `run()` must
    still complete, and no check's detail may carry the sentinel — proof
    that none of the seven checks ever reaches either function."""
    root, _proj, _cid = seeded_project

    def _forbidden(*_a, **_kw):
        raise AssertionError("doctor must never call db.execute/db.execute_returning_id")

    monkeypatch.setattr(db, "execute", _forbidden)
    monkeypatch.setattr(db, "execute_returning_id", _forbidden)

    report = doctor.run(str(root))
    assert len(report.checks) == 7
    for c in report.checks:
        assert "must never call" not in c.detail, f"{c.name} unexpectedly wrote: {c.detail}"


# ---------------------------------------------------------------------------
# CLI wiring: --format json / prose / exit code
# ---------------------------------------------------------------------------


def test_cli_doctor_json_shape(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["doctor", "--project", str(root), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)

    assert set(payload.keys()) == {"ok", "checks", "summary"}
    assert isinstance(payload["ok"], bool)
    assert len(payload["checks"]) == 7
    for entry in payload["checks"]:
        assert {"name", "status", "detail"} <= set(entry.keys())
        assert set(entry.keys()) <= {"name", "status", "detail", "remedy"}
        assert entry["status"] in ("ok", "warn", "fail")
    assert set(payload["summary"].keys()) == {"ok", "warn", "fail"}
    assert rc in (0, 1)
    assert rc == (0 if payload["ok"] else 1)


def test_cli_doctor_default_prose_one_line_per_check(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["doctor", "--project", str(root)])
    out = capsys.readouterr().out
    check_lines = [line for line in out.splitlines() if line.startswith("[")]
    assert len(check_lines) == 7
    assert any("ok " in line or "warn" in line or "fail" in line for line in check_lines)
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert rc in (0, 1)


def test_cli_doctor_exit_code_matches_report(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    # Force a real failure: stored schema_version diverges from compiled.
    with db.connect() as conn:
        db.set_meta(conn, "schema_version", "999")

    rc = cli.main(["doctor", "--project", str(root)])
    capsys.readouterr()
    assert rc == 1


def test_cli_doctor_default_project_none_no_crash(isolated_db, capsys):
    """No --project given, cwd resolves to nothing registered — must still
    print a full report and a clean, non-zero exit, never a traceback."""
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if l.startswith("[")]) == 7
    assert rc == 1
