"""Tests for `knowledge/cli.py`'s `_resolve_project_or_raise` — the shared
`--project` resolution helper behind all 14 verbs that take a `--project`
selector.

Before this change, the helper printed prose straight to stderr and
returned `None`, so "project not registered" — the very first thing an
agent hits in any un-indexed repo — never honored a caller's
`--json`/`--format json` request, unlike every other `cmd_*` error (see
`tests/test_error_envelope.py` / `tests/test_json_contract.py` for that
contract). The fix makes it raise `KnowledgeError` like everything else,
so `main()`'s single top-level handler renders it consistently.

Covers:
  1. Not-registered selector, no JSON flag -> prose on stderr, exit 1,
     `remedy` rendered as the `  try: ...` line.
  2. Same, with `--format json` (verb: `why`) -> JSON envelope on stdout.
  3. Same, with bare `--json` (verb: `vars list`) -> JSON envelope on
     stdout. This is the second, distinct `--json` convention `wants_json`
     covers (see `knowledge/jsonout.py`), exercised on a different verb
     than (2) to prove the fix is cross-cutting, not per-verb.
  4. `AmbiguousProjectName` (two projects sharing a name) -> exit 1, and
     the candidate root paths survive in both prose and the JSON message
     (verb: `brief`) -- `_print_ambiguous`'s information content must not
     be lost when folded into `KnowledgeError.message`.

No embedder needed: none of these paths ever reach cartography/search --
`_resolve_project_or_raise` raises before any of `why`/`map`/`brief`/
`vars list` touches its real command body. Fixtures follow the repo-wide
pattern (no `conftest.py`; each file redirects `KNOWLEDGE_HOME` locally),
matching `tests/test_json_contract.py`.
"""
from __future__ import annotations

import json

import pytest

from knowledge import cli, db, projects


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Redirect KNOWLEDGE_HOME to a tmp dir so we get a fresh SQLite DB."""
    home = tmp_path / "knowledge-home"
    home.mkdir()
    monkeypatch.setenv("KNOWLEDGE_HOME", str(home))
    yield home


def _one_line(out: str) -> str:
    """Assert `out` is exactly one non-empty line and return it."""
    lines = out.splitlines()
    assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {out!r}"
    return lines[0]


# ---------------------------------------------------------------------------
# 1. Not-registered, no JSON flag -> prose on stderr
# ---------------------------------------------------------------------------


def test_not_registered_prose_on_stderr(isolated_db, tmp_path, capsys):
    ghost = tmp_path / "never-built"
    ghost.mkdir()

    rc = cli.main(["map", "--project", str(ghost)])
    assert rc == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"error: project not registered: {ghost}" in captured.err
    assert "  try: knowledge build" in captured.err


# ---------------------------------------------------------------------------
# 2. Not-registered, `--format json` (verb: `why`)
# ---------------------------------------------------------------------------


def test_not_registered_format_json_envelope(isolated_db, tmp_path, capsys):
    ghost = tmp_path / "never-built"
    ghost.mkdir()

    rc = cli.main(
        ["why", "some/file.py", "--project", str(ghost), "--format", "json"]
    )
    assert rc == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(_one_line(captured.out))
    assert payload == {
        "ok": False,
        "code": "project_not_registered",
        "message": f"project not registered: {ghost}",
        "remedy": "knowledge build",
        "exit": 1,
    }


# ---------------------------------------------------------------------------
# 3. Not-registered, bare `--json` (verb: `vars list` -- the other
#    `wants_json` convention, and a verb distinct from (2) above).
# ---------------------------------------------------------------------------


def test_not_registered_bare_json_flag_envelope(isolated_db, tmp_path, capsys):
    ghost = tmp_path / "never-built"
    ghost.mkdir()

    rc = cli.main(["vars", "list", "--project", str(ghost), "--json"])
    assert rc == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(_one_line(captured.out))
    assert payload == {
        "ok": False,
        "code": "project_not_registered",
        "message": f"project not registered: {ghost}",
        "remedy": "knowledge build",
        "exit": 1,
    }


# ---------------------------------------------------------------------------
# 4. AmbiguousProjectName -- two projects sharing a name, resolved by name
#    (non-absolute selector). Candidate root paths must survive in both
#    prose and the JSON message.
# ---------------------------------------------------------------------------


@pytest.fixture()
def duplicate_named_projects(isolated_db, tmp_path):
    """Two distinct projects registered under the same display name --
    the only way `resolve_project` raises `AmbiguousProjectName` (a
    non-absolute name selector matching more than one row).
    """
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()

    with db.connect() as conn:
        proj_a = projects.get_or_create_project(conn, root_a, name_override="dup")
        proj_b = projects.get_or_create_project(conn, root_b, name_override="dup")
    return proj_a, proj_b


def test_ambiguous_project_name_prose_on_stderr(duplicate_named_projects, capsys):
    proj_a, proj_b = duplicate_named_projects

    rc = cli.main(["brief", "--project", "dup"])
    assert rc == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: project name 'dup' is ambiguous (2 matches):" in captured.err
    assert str(proj_a.root_path) in captured.err
    assert str(proj_b.root_path) in captured.err
    assert "pass an absolute root path instead of the name to pick one." in captured.err


def test_ambiguous_project_name_json_envelope(duplicate_named_projects, capsys):
    proj_a, proj_b = duplicate_named_projects

    rc = cli.main(["brief", "--project", "dup", "--format", "json"])
    assert rc == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(_one_line(captured.out))
    assert payload["ok"] is False
    assert payload["code"] == "ambiguous_project_name"
    assert payload["exit"] == 1
    assert "remedy" not in payload  # no single fixed remedy command applies
    assert "project name 'dup' is ambiguous (2 matches):" in payload["message"]
    assert str(proj_a.root_path) in payload["message"]
    assert str(proj_b.root_path) in payload["message"]
