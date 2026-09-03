"""Regression tests for unreadable-file handling during build/update.

Before this change, ``OSError`` on ``read_bytes()``/``stat()`` (permission
denied, a broken symlink, a file that vanished mid-scan) was swallowed with a
bare ``continue`` at two sites in ``indexer.py`` — the file silently never
made it into the index and nothing told the user. These tests prove:

1. The file is still SKIPPED, not fatal — build/update succeed either way.
2. A one-line warning is printed (via the existing ``verbose`` mechanism, no
   ``cli.py`` change) naming the count and reason.
3. A clean repo (nothing skipped) prints no such warning — no false positives.
4. The pure ``_SkipTracker`` accumulator formats and caps correctly in
   isolation, independent of any filesystem/DB interaction.

Both silent-skip sites are covered: the incremental walk loop directly inside
``update_project``, and ``_scan_file`` (used by ``_build_project_bulk``, the
path behind ``build_project``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowledge import db, indexer


# ---------------------------------------------------------------------------
# Fixtures / helpers — mirrors tests/test_indexer_chunk_writes.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Redirect KNOWLEDGE_HOME to a tmp dir so we get a fresh sqlite DB."""
    home = tmp_path / "knowledge-home"
    home.mkdir()
    monkeypatch.setenv("KNOWLEDGE_HOME", str(home))
    yield home


def _git_init(repo: Path) -> None:
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _rel_paths(conn, project_id) -> set[str]:
    rows = db.fetch_all(
        conn, "SELECT rel_path FROM files WHERE project_id = ?", (project_id,)
    )
    return {r[0] for r in rows}


# chmod(0o000) is a no-op for root (permission bits are ignored), which would
# make the permission-denied assertions below fail for an unrelated reason.
_RUNNING_AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


# ---------------------------------------------------------------------------
# 1. Pure _SkipTracker — format, categorization, path cap. No FS/DB needed.
# ---------------------------------------------------------------------------


def test_skip_tracker_empty_emits_no_warning():
    tracker = indexer._SkipTracker()
    assert tracker.total == 0
    assert tracker.warning() is None


def test_skip_tracker_distinguishes_permission_vs_missing():
    tracker = indexer._SkipTracker()
    tracker.record("a.py", PermissionError())
    tracker.record("b.py", PermissionError())
    tracker.record("c.py", FileNotFoundError())
    assert tracker.total == 3
    assert tracker.permission_denied == 2
    assert tracker.missing == 1
    assert tracker.warning() == (
        "warning: skipped 3 unreadable files "
        "(permission denied: 2, missing: 1): a.py, b.py, c.py"
    )


def test_skip_tracker_singular_file_wording():
    tracker = indexer._SkipTracker()
    tracker.record("only.py", PermissionError())
    assert tracker.warning() == (
        "warning: skipped 1 unreadable file (permission denied: 1): only.py"
    )


def test_skip_tracker_other_oserror_bucket():
    tracker = indexer._SkipTracker()
    tracker.record("weird.py", OSError("I/O error"))
    assert tracker.other == 1
    assert tracker.warning() == (
        "warning: skipped 1 unreadable file (other: 1): weird.py"
    )


def test_skip_tracker_caps_listed_paths_at_five():
    tracker = indexer._SkipTracker()
    for i in range(8):
        tracker.record(f"f{i}.py", PermissionError())
    assert tracker.total == 8
    msg = tracker.warning()
    assert msg == (
        "warning: skipped 8 unreadable files (permission denied: 8): "
        "f0.py, f1.py, f2.py, f3.py, f4.py (and 3 more)"
    )


# ---------------------------------------------------------------------------
# 2. build_project (-> _build_project_bulk -> _scan_file) end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_RUNNING_AS_ROOT, reason="chmod 000 is a no-op for root")
def test_build_project_skips_unreadable_file_and_warns(
    isolated_db, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "good.py", "def ok():\n    return 1\n")
    bad = repo / "secret.py"
    _write(bad, "def hidden():\n    return 2\n")
    bad.chmod(0o000)
    _git_init(repo)

    try:
        with db.connect() as conn:
            project_id, files_indexed, _ = indexer.build_project(
                conn, repo, verbose=True
            )
            rel_paths = _rel_paths(conn, project_id)
    finally:
        bad.chmod(0o644)  # restore so tmp_path teardown can remove it cleanly

    # Build succeeds; the unreadable file is absent, the readable one isn't.
    assert files_indexed == 1
    assert "good.py" in rel_paths
    assert "secret.py" not in rel_paths

    captured = capsys.readouterr()
    assert (
        "warning: skipped 1 unreadable file (permission denied: 1): secret.py"
        in captured.out
    )


def test_build_project_skips_broken_symlink_and_warns(
    isolated_db, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "good.py", "def ok():\n    return 1\n")
    broken = repo / "dangling.py"
    try:
        broken.symlink_to(repo / "does_not_exist.py")
    except OSError:
        pytest.skip("platform without symlink support")
    _git_init(repo)

    with db.connect() as conn:
        project_id, files_indexed, _ = indexer.build_project(
            conn, repo, verbose=True
        )
        rel_paths = _rel_paths(conn, project_id)

    assert files_indexed == 1
    assert "good.py" in rel_paths
    assert "dangling.py" not in rel_paths

    captured = capsys.readouterr()
    assert (
        "warning: skipped 1 unreadable file (missing: 1): dangling.py"
        in captured.out
    )


def test_build_project_clean_repo_emits_no_warning(isolated_db, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "good.py", "def ok():\n    return 1\n")
    _write(repo / "also_good.py", "def also():\n    return 2\n")
    _git_init(repo)

    with db.connect() as conn:
        indexer.build_project(conn, repo, verbose=True)

    captured = capsys.readouterr()
    assert "warning: skipped" not in captured.out


# ---------------------------------------------------------------------------
# 3. update_project's own walk-loop OSError site (separate from _scan_file)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_RUNNING_AS_ROOT, reason="chmod 000 is a no-op for root")
def test_update_project_skips_new_unreadable_file_and_warns(
    isolated_db, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "good.py", "def ok():\n    return 1\n")
    _git_init(repo)

    with db.connect() as conn:
        indexer.build_project(conn, repo, verbose=False)

    # A new file lands after the initial build, unreadable from the start —
    # exercises update_project's own read_bytes()/stat() try/except, not
    # _scan_file (that's build-only).
    bad = repo / "secret.py"
    _write(bad, "def hidden():\n    return 2\n")
    bad.chmod(0o000)

    try:
        with db.connect() as conn:
            project_id, files_visited, _ = indexer.update_project(
                conn, repo, verbose=True
            )
            rel_paths = _rel_paths(conn, project_id)
    finally:
        bad.chmod(0o644)

    assert "secret.py" not in rel_paths
    assert "good.py" in rel_paths

    captured = capsys.readouterr()
    assert (
        "warning: skipped 1 unreadable file (permission denied: 1): secret.py"
        in captured.out
    )


def test_update_project_clean_incremental_emits_no_warning(
    isolated_db, tmp_path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "good.py", "def ok():\n    return 1\n")
    _git_init(repo)

    with db.connect() as conn:
        indexer.build_project(conn, repo, verbose=False)

    _write(repo / "another.py", "def more():\n    return 3\n")

    with db.connect() as conn:
        indexer.update_project(conn, repo, verbose=True)

    captured = capsys.readouterr()
    assert "warning: skipped" not in captured.out
