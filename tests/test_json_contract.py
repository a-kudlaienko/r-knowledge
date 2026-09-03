"""Phase 1b: `--format {text,json}` on the 8 read verbs that had NO JSON
option before this change (`why`, `map`, `brief`, `resume`, `stats`, `get`,
`path`, `projects`).

Two conventions already coexist in this CLI (see `knowledge/jsonout.py`):
bare `--json` (older verbs) and `--format {text,json}` (newer verbs). This
file only adds the second convention to the 8 verbs that previously had
neither — it does not touch `relations` or any verb that already emits
JSON.

Covers, per verb:
  1. `--format json` parses as JSON, `"ok": true`, expected top-level keys.
  2. Default (no `--format`) prose is unchanged (still plain text, not JSON).
  3. `--format json` writes exactly one line to stdout.
  4. Each converted `KnowledgeError` site: correct exit code + JSON envelope
     (and prose-on-stderr in text mode) — `why`/`invalid_depth via map`/
     `get`/`path`'s `chunk_not_found`, `chunk_path_escapes_root`,
     `chunk_read_failed`.

No embedder needed for any of these verbs — they read cartography/search/
projects tables directly. Fixtures follow the repo-wide pattern (no
`conftest.py`; each file redirects `KNOWLEDGE_HOME` via a local fixture,
per `tests/test_decisions_patch_delete.py` / `tests/test_bulk_helpers_sqlite.py`).
"""

from __future__ import annotations

import json
import time

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


def _insert_file(conn, project_id: int, rel_path: str, *, size: int) -> int:
    """Minimal `files` row (same column set as tests/test_bulk_helpers_sqlite.py)."""
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
    start_byte: int = 0,
    end_byte: int,
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
    """One project, one indexed file ('pkg/mod.py'), one chunk inside it.

    Returns (root: Path, proj: projects.Project, chunk_id: int).
    """
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(_SRC)

    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)
        file_id = _insert_file(conn, proj.id, "pkg/mod.py", size=len(_SRC))
        chunk_id = _insert_chunk(
            conn, proj.id, file_id, stored=_SRC, end_byte=len(_SRC.encode())
        )
    return root, proj, chunk_id


def _one_line(out: str) -> str:
    """Assert `out` is exactly one non-empty line and return it."""
    lines = out.splitlines()
    assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {out!r}"
    return lines[0]


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------


def test_why_json_shape(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["why", str(root / "pkg" / "mod.py"), "--project", str(root), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["file"]["path"] == "pkg/mod.py"
    assert payload["file"]["lang"] == "python"
    assert "top_symbols" in payload and "inbound" in payload and "outbound" in payload


def test_why_default_prose_unchanged(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["why", str(root / "pkg" / "mod.py"), "--project", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pkg/mod.py" in out
    assert "lang=python" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_why_file_not_indexed_is_knowledge_error(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["why", str(root / "pkg" / "missing.py"), "--project", str(root)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: file not indexed: pkg/missing.py" in err
    assert "run 'knowledge update'" in err


def test_why_file_not_indexed_json_envelope(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(
        ["why", str(root / "pkg" / "missing.py"), "--project", str(root), "--format", "json"]
    )
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["code"] == "file_not_indexed"
    assert payload["exit"] == 1


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


def test_map_json_shape(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    # --depth 1: groups by the first path component ('pkg'); the default
    # depth=2 would bucket by 'pkg/mod.py' since the file has only 2 parts.
    rc = cli.main(["map", "--project", str(root), "--depth", "1", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["project"] == "proj"
    assert payload["depth"] == 1
    assert payload["subtrees"] == [
        {
            "path": "pkg",
            "files": 1,
            "dominant_lang": "python",
            "top_kinds": [{"kind": "function", "count": 1}],
            "entrypoint": None,
        }
    ]


def test_map_default_prose_unchanged(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["map", "--project", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pkg" in out
    assert "DIR" in out  # table header, prose-only
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_map_invalid_depth_is_knowledge_error(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["map", "--project", str(root), "--depth", "0"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: --depth must be >= 1" in err


def test_map_invalid_depth_json_envelope(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(
        ["map", "--project", str(root), "--depth", "0", "--format", "json"]
    )
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload == {
        "ok": False,
        "code": "invalid_depth",
        "message": "--depth must be >= 1",
        "exit": 1,
    }


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


def test_brief_json_shape(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["brief", "--project", str(root), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["project"] == "proj"
    assert payload["totals"] == {"files": 1, "chunks": 1, "edges": 0}
    assert payload["top_langs"] == [{"lang": "python", "files": 1}]


def test_brief_default_prose_unchanged(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["brief", "--project", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "files=1  chunks=1  edges=0" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_json_shape(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["resume", "--project", str(root), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["project"] == "proj"
    assert payload["totals"] == {"history_entries": 0, "decisions": 0}
    assert payload["recent_decisions"] == []
    assert payload["pending_stage"] == 0


def test_resume_default_prose_unchanged(seeded_project, capsys):
    root, _proj, _cid = seeded_project
    rc = cli.main(["resume", "--project", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# recent decisions" in out
    assert "# how to proceed" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_resume_missing_format_attr_still_defaults_to_prose(seeded_project, capsys):
    """Bare Namespace with no `format` attribute (as tests/test_supersede_retrieval.py
    calls it directly) must not raise — `getattr` fallback stays load-bearing."""
    import argparse

    root, _proj, _cid = seeded_project
    rc = cli.cmd_resume(argparse.Namespace(project=str(root)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "# recent decisions" in out


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_json_shape(seeded_project, capsys):
    root, proj, _cid = seeded_project
    rc = cli.main(["stats", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["backend"] == "sqlite"
    assert payload["totals"] == {"projects": 1, "files": 1, "chunks": 1}
    assert payload["projects"] == [
        {"name": "proj", "root": str(proj.root_path), "files": 0, "chunks": 0}
    ]


def test_stats_default_prose_unchanged(seeded_project, capsys):
    rc = cli.main(["stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Projects:    1" in out
    assert "Files:       1" in out
    assert "Chunks:      1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_json_shape_stored_text(seeded_project, capsys):
    _root, _proj, cid = seeded_project
    rc = cli.main(["get", str(cid), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload == {
        "ok": True,
        "chunk": {
            "id": cid, "path": "pkg/mod.py", "start_line": 1, "end_line": 2,
            "kind": "function", "name": "fn", "text": _SRC,
        },
    }


def test_get_default_prose_unchanged(seeded_project, capsys):
    _root, _proj, cid = seeded_project
    rc = cli.main(["get", str(cid)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"chunk {cid}: function fn (pkg/mod.py:1-2)" in out
    assert _SRC.rstrip("\n") in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_get_raw_json_shape(seeded_project, capsys):
    _root, _proj, cid = seeded_project
    rc = cli.main(["get", str(cid), "--raw", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["chunk"]["text"] == _SRC


def test_get_raw_default_prose_unchanged(seeded_project, capsys):
    _root, _proj, cid = seeded_project
    rc = cli.main(["get", str(cid), "--raw"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == _SRC


def test_get_chunk_not_found_is_knowledge_error(seeded_project, capsys):
    _root, _proj, _cid = seeded_project
    rc = cli.main(["get", "999999"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: no chunk with id 999999" in err


def test_get_chunk_not_found_json_envelope(seeded_project, capsys):
    _root, _proj, _cid = seeded_project
    rc = cli.main(["get", "999999", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload == {
        "ok": False,
        "code": "chunk_not_found",
        "message": "no chunk with id 999999",
        "exit": 1,
    }


def test_get_raw_chunk_path_escapes_root(seeded_project, capsys):
    root, proj, _cid = seeded_project
    with db.connect() as conn:
        file_id = _insert_file(conn, proj.id, "../escape.py", size=10)
        escaping_id = _insert_chunk(conn, proj.id, file_id, stored="x", end_byte=1)

    rc = cli.main(["get", str(escaping_id), "--raw", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload["code"] == "chunk_path_escapes_root"
    assert payload["exit"] == 1


def test_get_raw_chunk_read_failed(seeded_project, capsys):
    """rel_path is contained under root but the file itself is missing on disk."""
    _root, proj, _cid = seeded_project
    with db.connect() as conn:
        file_id = _insert_file(conn, proj.id, "pkg/missing.py", size=10)
        missing_id = _insert_chunk(conn, proj.id, file_id, stored="x", end_byte=1)

    rc = cli.main(["get", str(missing_id), "--raw", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload["code"] == "chunk_read_failed"
    assert payload["exit"] == 1


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------


def test_path_json_shape(seeded_project, capsys):
    root, _proj, cid = seeded_project
    rc = cli.main(["path", str(cid), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload == {
        "ok": True,
        "path": str((root / "pkg" / "mod.py").resolve()),
        "start_line": 1,
        "end_line": 2,
    }


def test_path_default_prose_unchanged(seeded_project, capsys):
    root, _proj, cid = seeded_project
    rc = cli.main(["path", str(cid)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{(root / 'pkg' / 'mod.py').resolve()}:1-2\n"
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_path_chunk_not_found_json_envelope(seeded_project, capsys):
    _root, _proj, _cid = seeded_project
    rc = cli.main(["path", "999999", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload == {
        "ok": False,
        "code": "chunk_not_found",
        "message": "no chunk with id 999999",
        "exit": 1,
    }


def test_path_chunk_path_escapes_root_json_envelope(seeded_project, capsys):
    _root, proj, _cid = seeded_project
    with db.connect() as conn:
        file_id = _insert_file(conn, proj.id, "../escape.py", size=10)
        escaping_id = _insert_chunk(conn, proj.id, file_id, stored="x", end_byte=1)

    rc = cli.main(["path", str(escaping_id), "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 1
    assert payload["code"] == "chunk_path_escapes_root"


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


def test_projects_json_shape(seeded_project, capsys):
    _root, proj, _cid = seeded_project
    rc = cli.main(["projects", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload == {
        "ok": True,
        "projects": [
            {"name": "proj", "root": str(proj.root_path), "files": 0, "chunks": 0}
        ],
    }


def test_projects_default_prose_unchanged(seeded_project, capsys):
    rc = cli.main(["projects"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NAME" in out and "FILES" in out and "CHUNKS" in out
    assert "proj" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_projects_local_sqlite_json_shape(seeded_project, capsys):
    _root, proj, _cid = seeded_project
    rc = cli.main(["projects", "--local-sqlite", "--format", "json"])
    out = _one_line(capsys.readouterr().out)
    payload = json.loads(out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["source"] == "sqlite"
    assert payload["projects"] == [
        {"name": "proj", "root": str(proj.root_path), "files": 0, "chunks": 0}
    ]


def test_projects_local_sqlite_default_prose_unchanged(seeded_project, capsys):
    rc = cli.main(["projects", "--local-sqlite"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sqlite source:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
