"""Tests for `knowledge gate` (`knowledge/gate.py` + the `gate` CLI verb).

`gate` turns the prose-only "pre-change conflict check" mandated by
AGENTS.local.md into a sensor with a verdict: given a `--topic` and/or
`--files`, does prior knowledge (decisions/facts, history, exact
files_touched matches, import coupling) bear on the change?

Style follows tests/test_doctor.py (isolated_db, never-writes proof) and
tests/test_vec_prefilter_scoping.py (`_AngleEmbedder` for deterministic,
non-trivial semantic distances — `_StubEmbedder`'s identical vectors would
make every hit distance=0, which can't exercise a real "too far" case).

Covers:
  1. `GateReport.verdict`/`.exit_code` as a pure dataclass property (no DB):
     conflict from a live hit, clear with no hits, clear when the ONLY hit
     is a dead row (the subtlest requirement), conflict when a dead row's
     replacement is itself relevant, coupling never drives the verdict.
  2. Each of the 4 signals in isolation: present when seeded/near, absent
     when not/far — including proof the exact files_touched signal needs
     no embedder at all.
  3. `cmd_gate`: usage error (exit 2) when neither --topic nor --files is
     given (including all-blank input); `--format json` single-line shape;
     live clear/conflict end-to-end.
  4. `gate` never writes: `db.execute`/`db.execute_returning_id` monkeypatched
     to raise, full `cmd_gate` call must still complete.
  5. `install-hooks --with-gate`: default output unaffected, flag adds
     exactly one PreToolUse entry, idempotent on rerun.
  6. `gate --hook`: reads the PreToolUse event JSON from stdin instead of
     --topic/--files. Conflict -> exactly one
     `hookSpecificOutput.additionalContext` JSON line, exit 0. Clear ->
     empty stdout, exit 0. Never emits `permissionDecision`/`decision`
     (recursively). Malformed/empty stdin and an internal exception inside
     `gate.build` both degrade to silent exit 0. `file_path` /
     `notebook_path` / `path` fallback chain, tried in that order.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from knowledge import cli, db, decisions as decisions_mod, gate, history as history_mod, projects
from knowledge.decisions import Decision
from knowledge.history import HistoryEntry
from knowledge.jsonout import KnowledgeError


DIM = 384


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


class _AngleEmbedder:
    """Maps text to a unit vector at a controlled angle in the (x, y) plane.

    Same construction as tests/test_vec_prefilter_scoping.py's fixture of the
    same name (kept as a local copy — no conftest.py in this repo, fixtures
    are file-local by convention). Text must start with ``"a<degrees>"``;
    cosine distance to the angle-0 query is ``1 - cos(theta)``, strictly
    increasing, so "near" vs "far" is arithmetic, not luck — unlike a stub
    embedder that returns identical vectors for everything (distance 0
    always), which cannot exercise a genuine "too far to matter" case.
    """

    _ANGLE = re.compile(r"^a(\d+(?:\.\d+)?)")

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            m = self._ANGLE.match(t)
            theta = math.radians(float(m.group(1))) if m else math.pi
            v = np.zeros(DIM, dtype=np.float32)
            v[0] = math.cos(theta)
            v[1] = math.sin(theta)
            rows.append(v)
        return np.stack(rows).astype(np.float32)


@pytest.fixture()
def angle_embedder(monkeypatch):
    """Install the angle embedder into every module `gate` reaches for search."""
    embedder = _AngleEmbedder()
    for mod in (decisions_mod, history_mod):
        monkeypatch.setattr(mod, "get_embedder", lambda: embedder)
    return embedder


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _mk_project(conn, name: str) -> int:
    return projects.get_or_create_project(conn, Path(f"/tmp/{name}")).id


def _mk_decision(conn, project_id: int, angle: float, **kwargs) -> int:
    """One decision whose embedded text starts with ``a<angle>`` — see
    `_AngleEmbedder`. ``kwargs`` forwards to `decisions.add` (e.g.
    `files_touched=`, `supersedes=`)."""
    return decisions_mod.add(
        conn,
        project_id=project_id,
        topic=f"a{angle}",
        decision=f"decision at {angle}",
        **kwargs,
    )


def _mk_history(conn, project_id: int, angle: float) -> int:
    entry_id = db.execute_returning_id(
        conn,
        "INSERT INTO history(project_id, created_at, short_summary, long_summary) "
        "VALUES (?, ?, ?, ?)",
        (project_id, time.time(), f"a{angle}", f"long {angle}"),
    )
    vec = _AngleEmbedder().encode([f"a{angle}"])[0]
    db.insert_history_embedding(conn, entry_id, vec)
    return entry_id


def _mk_file(conn, project_id: int, rel_path: str) -> int:
    now = time.time()
    return db.execute_returning_id(
        conn,
        "INSERT INTO files(project_id, rel_path, content_hash, mtime, size, "
        "lang, last_scanned) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, rel_path, f"h-{rel_path}", now, 100, "python", now),
    )


def _mk_edge(
    conn, project_id: int, source_file_id: int, target_file_id: int | None, *,
    kind: str = "python_import", raw: str = "import x",
) -> None:
    db.execute(
        conn,
        "INSERT INTO file_edges(project_id, source_file_id, target_file_id, "
        "kind, raw, symbol, line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, source_file_id, target_file_id, kind, raw, None, 1),
    )


def _make_decision(**overrides) -> Decision:
    base = Decision(
        id=1, project_id=1, created_at=time.time(), topic="t", decision="d",
        rationale=None, files_touched=[], session_id=None, author=None,
        supersedes=None, override_reason=None, kind="decision",
    )
    return base._replace(**overrides)


def _make_history_entry(**overrides) -> HistoryEntry:
    base = HistoryEntry(
        id=1, project_id=1, created_at=time.time(), short_summary="s",
        long_summary="l", session_id=None, tags=None,
    )
    return base._replace(**overrides)


# ---------------------------------------------------------------------------
# 1. GateReport.verdict / .exit_code — pure, no DB
# ---------------------------------------------------------------------------


def test_verdict_conflict_on_live_decision_hit():
    d = _make_decision(id=1)
    report = gate.GateReport(
        topic="t", files=(), decision_hits=(gate.DecisionHit(d, 0.1, None),),
        history_hits=(), files_touched_hits=(), coupled_files=(),
    )
    assert report.verdict == "conflict"
    assert report.exit_code == 5


def test_verdict_clear_when_no_hits_at_all():
    report = gate.GateReport(
        topic="t", files=(), decision_hits=(), history_hits=(),
        files_touched_hits=(), coupled_files=(),
    )
    assert report.verdict == "clear"
    assert report.exit_code == 0


def test_verdict_clear_when_only_hit_is_a_dead_row():
    """The subtlest requirement: a superseded row must NOT, by itself,
    flip the verdict to conflict."""
    d = _make_decision(id=1)
    report = gate.GateReport(
        topic="t", files=(),
        decision_hits=(gate.DecisionHit(d, 0.1, superseded_by=2),),
        history_hits=(), files_touched_hits=(), coupled_files=(),
    )
    assert report.verdict == "clear"
    assert report.exit_code == 0
    # But it is still SHOWN, marked dead, pointing at its replacement.
    assert len(report.decision_hits) == 1
    assert report.decision_hits[0].superseded_by == 2
    assert report.superseded_shown == 1


def test_verdict_conflict_when_dead_rows_replacement_is_also_a_hit():
    """If the dead row's replacement is itself relevant, the replacement
    is what counts toward the verdict."""
    old = _make_decision(id=1)
    new = _make_decision(id=2)
    report = gate.GateReport(
        topic="t", files=(),
        decision_hits=(
            gate.DecisionHit(old, 0.1, superseded_by=2),
            gate.DecisionHit(new, 0.2, None),
        ),
        history_hits=(), files_touched_hits=(), coupled_files=(),
    )
    assert report.verdict == "conflict"
    assert report.exit_code == 5
    assert report.superseded_shown == 1  # only the old one is dead


def test_verdict_never_driven_by_coupling_alone():
    report = gate.GateReport(
        topic=None, files=("a.py",), decision_hits=(), history_hits=(),
        files_touched_hits=(),
        coupled_files=(gate.CoupledFile("b.py", "forward", "python_import", via="a.py"),),
    )
    assert report.verdict == "clear"
    assert report.exit_code == 0


def test_verdict_conflict_from_history_hit_alone():
    e = _make_history_entry(id=7)
    report = gate.GateReport(
        topic="t", files=(), decision_hits=(), history_hits=(gate.HistoryHit(e, 0.3),),
        files_touched_hits=(), coupled_files=(),
    )
    assert report.verdict == "conflict"
    assert report.exit_code == 5


def test_verdict_conflict_from_exact_files_touched_hit_alone():
    d = _make_decision(id=3)
    report = gate.GateReport(
        topic=None, files=("a.py",), decision_hits=(), history_hits=(),
        files_touched_hits=(gate.DecisionHit(d, None, None),), coupled_files=(),
    )
    assert report.verdict == "conflict"
    assert report.exit_code == 5


# ---------------------------------------------------------------------------
# 2. Each signal in isolation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("isolated_db", "angle_embedder")
def test_signal_decisions_semantic_present_when_near_absent_when_far():
    with db.connect() as conn:
        pid = _mk_project(conn, "proj")
        _mk_decision(conn, pid, 0.0)

        near = gate.build(conn, pid, "a0", [])
        far = gate.build(conn, pid, "a170", [])

    assert len(near.decision_hits) == 1
    assert near.verdict == "conflict"
    assert far.decision_hits == ()
    assert far.verdict == "clear"


@pytest.mark.usefixtures("isolated_db", "angle_embedder")
def test_signal_history_semantic_present_when_near_absent_when_far():
    with db.connect() as conn:
        pid = _mk_project(conn, "proj")
        _mk_history(conn, pid, 0.0)

        near = gate.build(conn, pid, "a0", [])
        far = gate.build(conn, pid, "a170", [])

    assert len(near.history_hits) == 1
    assert near.verdict == "conflict"
    assert far.history_hits == ()
    assert far.verdict == "clear"


@pytest.mark.usefixtures("isolated_db", "angle_embedder")
def test_signal_exact_files_touched_present_and_absent():
    with db.connect() as conn:
        pid = _mk_project(conn, "proj")
        _mk_decision(conn, pid, 0.0, files_touched=["src/foo.py"])

        match = gate.build(conn, pid, None, ["src/foo.py"])
        no_match = gate.build(conn, pid, None, ["src/bar.py"])

    assert len(match.files_touched_hits) == 1
    assert match.verdict == "conflict"
    assert no_match.files_touched_hits == ()
    assert no_match.verdict == "clear"


def test_signal_exact_files_touched_does_not_need_the_embedder(isolated_db, angle_embedder, monkeypatch):
    """`--files` exact matching must work independent of embedding
    similarity — prove it by poisoning the embedder AFTER seeding and
    calling gate with files only (no topic, so no search should ever run)."""
    with db.connect() as conn:
        pid = _mk_project(conn, "proj")
        _mk_decision(conn, pid, 0.0, files_touched=["src/foo.py"])

    def _boom():
        raise AssertionError("files_touched signal must not need the embedder")

    monkeypatch.setattr(decisions_mod, "get_embedder", _boom)

    with db.connect() as conn:
        report = gate.build(conn, pid, None, ["src/foo.py"])

    assert len(report.files_touched_hits) == 1
    assert report.verdict == "conflict"


@pytest.mark.usefixtures("isolated_db")
def test_signal_coupling_present_and_absent_never_drives_verdict():
    with db.connect() as conn:
        pid = _mk_project(conn, "proj")
        fa = _mk_file(conn, pid, "a.py")
        fb = _mk_file(conn, pid, "b.py")
        fc = _mk_file(conn, pid, "c.py")
        _mk_file(conn, pid, "d.py")  # isolated — no edges at all
        _mk_edge(conn, pid, fa, fb)  # a.py imports b.py
        _mk_edge(conn, pid, fc, fa)  # c.py imports a.py

        coupled = gate.build(conn, pid, None, ["a.py"])
        isolated = gate.build(conn, pid, None, ["d.py"])

    got = {(c.rel_path, c.direction) for c in coupled.coupled_files}
    assert got == {("b.py", "forward"), ("c.py", "reverse")}
    assert coupled.verdict == "clear"  # coupling alone never conflicts
    assert isolated.coupled_files == ()


# ---------------------------------------------------------------------------
# 3. cmd_gate: usage error, JSON shape, live clear/conflict
# ---------------------------------------------------------------------------


def test_cmd_gate_raises_usage_error_when_neither_topic_nor_files():
    args = argparse.Namespace(topic=None, files=None, project=None, format="text")
    with pytest.raises(KnowledgeError) as exc_info:
        cli.cmd_gate(args)
    assert exc_info.value.exit_code == 2
    assert exc_info.value.code == "gate_no_target"


def test_cmd_gate_raises_usage_error_when_topic_and_files_are_blank():
    """Blank strings (the opt-in hook's `jq` extraction coming up empty)
    must degrade to 'not provided', not a real target."""
    args = argparse.Namespace(topic="   ", files=[""], project=None, format="text")
    with pytest.raises(KnowledgeError) as exc_info:
        cli.cmd_gate(args)
    assert exc_info.value.exit_code == 2


def test_cli_main_gate_no_target_exit_code_2(isolated_db, capsys):
    rc = cli.main(["gate"])
    capsys.readouterr()
    assert rc == 2


def _one_line(out: str) -> str:
    lines = out.splitlines()
    assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {out!r}"
    return lines[0]


@pytest.fixture()
def gate_project(isolated_db, tmp_path):
    """A real, resolvable project root — needed for `--project <path>` to
    round-trip through `_resolve_project_or_raise`, unlike the fake
    `/tmp/<name>` roots the pure-signal tests above use."""
    root = tmp_path / "proj"
    root.mkdir()
    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)
    return root, proj.id


@pytest.mark.usefixtures("angle_embedder")
def test_cli_gate_json_shape_and_clear_verdict(gate_project, capsys):
    root, _pid = gate_project
    rc = cli.main(["gate", "--project", str(root), "--topic", "a170", "--format", "json"])
    payload = json.loads(_one_line(capsys.readouterr().out))

    assert payload["ok"] is True
    assert payload["verdict"] == "clear"
    assert rc == 0
    assert set(payload.keys()) >= {
        "ok", "verdict", "topic", "files", "decisions", "history",
        "coupled_files", "summary",
    }
    assert set(payload["summary"].keys()) == {"decisions", "history", "superseded_shown"}


@pytest.mark.usefixtures("angle_embedder")
def test_cli_gate_json_conflict_verdict(gate_project, capsys):
    root, pid = gate_project
    with db.connect() as conn:
        _mk_decision(conn, pid, 0.0)

    rc = cli.main(["gate", "--project", str(root), "--topic", "a0", "--format", "json"])
    payload = json.loads(_one_line(capsys.readouterr().out))

    assert payload["verdict"] == "conflict"
    assert rc == 5
    assert payload["summary"]["decisions"] == 1
    assert payload["decisions"][0]["match"] == "semantic"


@pytest.mark.usefixtures("angle_embedder")
def test_cli_gate_prose_shows_superseded_marker(gate_project, capsys):
    root, pid = gate_project
    with db.connect() as conn:
        old_id = _mk_decision(conn, pid, 0.0)
        new_id = _mk_decision(conn, pid, 1.0, supersedes=old_id)

    rc = cli.main(["gate", "--project", str(root), "--topic", "a0"])
    out = capsys.readouterr().out

    assert rc == 5  # the live replacement (angle 1, well within threshold) still conflicts
    assert f"SUPERSEDED by id={new_id}" in out
    assert "verdict: conflict" in out


# ---------------------------------------------------------------------------
# 4. Never writes
# ---------------------------------------------------------------------------


def test_gate_never_writes(isolated_db, angle_embedder, monkeypatch, tmp_path):
    """`db.execute`/`db.execute_returning_id` are the only two write paths
    `knowledge/db.py` exposes. Force both to raise; a full `cmd_gate` call
    (project resolution + all four signals) must still complete."""
    root = tmp_path / "proj"
    root.mkdir()
    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)
        pid = proj.id
        fa = _mk_file(conn, pid, "a.py")
        fb = _mk_file(conn, pid, "b.py")
        _mk_edge(conn, pid, fa, fb)
        _mk_decision(conn, pid, 0.0, files_touched=["a.py"])
        _mk_history(conn, pid, 0.0)

    def _forbidden(*_a, **_kw):
        raise AssertionError("gate must never call db.execute/db.execute_returning_id")

    monkeypatch.setattr(db, "execute", _forbidden)
    monkeypatch.setattr(db, "execute_returning_id", _forbidden)

    args = argparse.Namespace(topic="a0", files=["a.py"], project=str(root), format="json")
    rc = cli.cmd_gate(args)  # must NOT raise the AssertionError above
    assert rc == 5


# ---------------------------------------------------------------------------
# 5. install-hooks --with-gate
# ---------------------------------------------------------------------------


def _hook_args(**overrides) -> argparse.Namespace:
    base = dict(user=False, absolute=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def test_install_hooks_default_output_unaffected_by_gate_flag(tmp_path, monkeypatch, capsys):
    """No --with-gate -> byte-identical to the pre-gate behavior: no
    PreToolUse entry, same "added hook for" line as before this feature."""
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_install_hooks(_hook_args())
    assert rc == 0

    settings = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert "PreToolUse" not in settings["hooks"]

    out = capsys.readouterr().out
    assert "added hook for: Stop, PreCompact, SessionEnd, PostToolUse" in out
    assert "PreToolUse" not in out


def test_install_hooks_with_gate_adds_exactly_one_pretooluse_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_install_hooks(_hook_args(with_gate=True))
    assert rc == 0

    settings = json.loads(_settings_path(tmp_path).read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Write|Edit|NotebookEdit"
    cmd = pre[0]["hooks"][0]["command"]
    assert cmd == "knowledge gate --hook 2>/dev/null || true"

    # The other three events are completely unaffected by the flag.
    for event in ("Stop", "PreCompact", "SessionEnd", "PostToolUse"):
        assert event in settings["hooks"]


def test_install_hooks_with_gate_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.cmd_install_hooks(_hook_args(with_gate=True))
    first = _settings_path(tmp_path).read_bytes()
    capsys.readouterr()

    rc = cli.cmd_install_hooks(_hook_args(with_gate=True))
    second = _settings_path(tmp_path).read_bytes()
    out = capsys.readouterr().out

    assert rc == 0
    assert first == second
    assert "already registered for" in out
    assert "PreToolUse" in out
    assert "added hook for" not in out


# ---------------------------------------------------------------------------
# 6. gate --hook — PreToolUse entrypoint
# ---------------------------------------------------------------------------


def _feed_stdin(monkeypatch, text: str) -> None:
    """Install *text* as the process's stdin for `_cmd_gate_hook` to read —
    this is how Claude Code hands a hook the tool-call event JSON."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _assert_no_blocking_keys(obj) -> None:
    """Recursively assert neither `permissionDecision` nor `decision`
    appears anywhere in *obj* — the two keys that would let a hook block or
    auto-approve a tool call even at exit 0. `--hook` mode must be
    structurally incapable of emitting either."""
    if isinstance(obj, dict):
        assert "permissionDecision" not in obj
        assert "decision" not in obj
        for v in obj.values():
            _assert_no_blocking_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_blocking_keys(v)


@pytest.mark.usefixtures("angle_embedder")
def test_hook_conflict_emits_single_additional_context_line(gate_project, monkeypatch, capsys):
    root, pid = gate_project
    with db.connect() as conn:
        did = _mk_decision(conn, pid, 0.0, files_touched=["a.py"])
    monkeypatch.chdir(root)
    _feed_stdin(monkeypatch, json.dumps({"tool_input": {"file_path": "a.py"}}))

    rc = cli.main(["gate", "--hook"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(_one_line(out))
    hook_out = payload["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["additionalContext"]  # non-empty, human-readable
    assert f"id={did}" in hook_out["additionalContext"]
    _assert_no_blocking_keys(payload)


def test_hook_clear_prints_nothing(gate_project, monkeypatch, capsys):
    root, _pid = gate_project
    monkeypatch.chdir(root)
    _feed_stdin(monkeypatch, json.dumps({"tool_input": {"file_path": "a.py"}}))

    rc = cli.main(["gate", "--hook"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ""


@pytest.mark.parametrize("raw_stdin", ["", "not json", "{}", '{"tool_input": {}}'])
def test_hook_malformed_stdin_is_silent_and_exits_zero(isolated_db, monkeypatch, capsys, raw_stdin):
    """Empty, unparseable, or field-missing stdin must never raise and must
    never print anything — a hook is never allowed to be the reason
    something breaks."""
    _feed_stdin(monkeypatch, raw_stdin)

    rc = cli.main(["gate", "--hook"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ""


def test_hook_file_path_wins_when_present(gate_project, monkeypatch):
    """The tolerant fallback chain tries `file_path` FIRST — Write/Edit
    report it, so it must win even when `notebook_path`/`path` are also
    present in the event."""
    root, _pid = gate_project
    monkeypatch.chdir(root)
    captured: dict = {}

    def _fake_build(conn, project_id, topic, files):
        captured["files"] = list(files)
        return gate.GateReport(
            topic=topic, files=tuple(files), decision_hits=(), history_hits=(),
            files_touched_hits=(), coupled_files=(),
        )

    monkeypatch.setattr(gate, "build", _fake_build)
    _feed_stdin(monkeypatch, json.dumps({
        "tool_input": {"file_path": "a.py", "notebook_path": "b.ipynb", "path": "c.py"}
    }))

    rc = cli.main(["gate", "--hook"])
    assert rc == 0
    assert captured["files"] == ["a.py"]


def test_hook_notebook_path_used_when_file_path_absent(gate_project, monkeypatch):
    """NotebookEdit reports `notebook_path`, not `file_path`."""
    root, _pid = gate_project
    monkeypatch.chdir(root)
    captured: dict = {}

    def _fake_build(conn, project_id, topic, files):
        captured["files"] = list(files)
        return gate.GateReport(
            topic=topic, files=tuple(files), decision_hits=(), history_hits=(),
            files_touched_hits=(), coupled_files=(),
        )

    monkeypatch.setattr(gate, "build", _fake_build)
    _feed_stdin(monkeypatch, json.dumps({
        "tool_input": {"notebook_path": "b.ipynb", "path": "c.py"}
    }))

    rc = cli.main(["gate", "--hook"])
    assert rc == 0
    assert captured["files"] == ["b.ipynb"]


def test_hook_path_used_when_file_path_and_notebook_path_absent(gate_project, monkeypatch):
    """`path` is the last-resort, defensive fallback."""
    root, _pid = gate_project
    monkeypatch.chdir(root)
    captured: dict = {}

    def _fake_build(conn, project_id, topic, files):
        captured["files"] = list(files)
        return gate.GateReport(
            topic=topic, files=tuple(files), decision_hits=(), history_hits=(),
            files_touched_hits=(), coupled_files=(),
        )

    monkeypatch.setattr(gate, "build", _fake_build)
    _feed_stdin(monkeypatch, json.dumps({"tool_input": {"path": "c.py"}}))

    rc = cli.main(["gate", "--hook"])
    assert rc == 0
    assert captured["files"] == ["c.py"]


def test_hook_internal_exception_is_silent_and_exits_zero(gate_project, monkeypatch, capsys):
    """A crash inside `gate.build` (DB error, embedder blow-up, ...) must
    degrade to a silent no-op, never a traceback, never a nonzero exit."""
    root, _pid = gate_project
    monkeypatch.chdir(root)

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "build", _boom)
    _feed_stdin(monkeypatch, json.dumps({"tool_input": {"file_path": "a.py"}}))

    rc = cli.main(["gate", "--hook"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ""


def test_hook_takes_precedence_over_format(gate_project, monkeypatch, capsys):
    """--hook and --format are mutually exclusive; --hook wins when both are
    given — the hook-shaped output is produced, not the `--format json`
    envelope (`{"ok": ..., "verdict": ...}`)."""
    root, pid = gate_project
    with db.connect() as conn:
        _mk_decision(conn, pid, 0.0, files_touched=["a.py"])
    monkeypatch.chdir(root)
    _feed_stdin(monkeypatch, json.dumps({"tool_input": {"file_path": "a.py"}}))

    rc = cli.main(["gate", "--hook", "--format", "json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(_one_line(out))
    assert "hookSpecificOutput" in payload
    assert "verdict" not in payload
