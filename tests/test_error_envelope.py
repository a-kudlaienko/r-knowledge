"""Tests for the Phase 1a error contract: `knowledge/jsonout.py` plus the
top-level exception handler in `cli.main()` (see `knowledge decide` for this
item — the architectural review that any exception besides `db.offline_errors()`
reached the calling agent as a raw Python traceback).

Covers:
  1. `error_payload` — exact envelope shape, `remedy` omitted when None.
  2. `KnowledgeError` — carries code/message/remedy/exit_code (with defaults).
  3. `main()` maps a raised `KnowledgeError` to its `exit_code`, prose on
     stderr by default, the JSON envelope on stdout when the invoked verb's
     own `--json` flag is set.
  4. `main()` maps an unanticipated exception to exit 70 / `internal_error`,
     same prose-vs-JSON split.
  5. `KNOWLEDGE_TRACEBACK=1` re-raises instead of swallowing into 70 —
     the developer escape hatch.
  6. The pre-existing `db.offline_errors()` -> exit 4 path is untouched
     (checked BEFORE `except Exception`, so it can never be relabeled as
     `internal_error`).
  7. `wants_json` — the single detector for both conventions the CLI has
     accreted (`--json` store_true, and `--format {text,json}`) — and that
     both `main()`'s exception handlers honor `--format json`, not just
     `--json` (the defect this file was extended to guard against).

No DB/embedder needed: `_DISPATCH["build"]` / `_DISPATCH["status"]` /
`_DISPATCH["decisions"]` are monkeypatched to raise on demand, so
`cli.main()` runs its real argparse + exception-handling path without ever
reaching the real command bodies. `build` has no `--json`/`--format` flag
at all (exercises the "neither attribute" default); `status` has `--json`
(exercises that convention without inventing a new flag, per Phase 1a's ban
on adding `--json` to verbs); `decisions` has `--format {text,json}`
instead of `--json` (exercises the second convention, same ban respected).
"""
from __future__ import annotations

import json

import pytest

from knowledge import cli, db
from knowledge.jsonout import KnowledgeError, error_payload, wants_json


# ---------------------------------------------------------------------------
# 1. error_payload
# ---------------------------------------------------------------------------


def test_error_payload_full_shape():
    payload = error_payload("bad_arg", "the --top-k value is invalid", remedy="knowledge search --top-k 10 ...", exit_code=2)
    assert payload == {
        "ok": False,
        "code": "bad_arg",
        "message": "the --top-k value is invalid",
        "remedy": "knowledge search --top-k 10 ...",
        "exit": 2,
    }


def test_error_payload_omits_remedy_when_none():
    payload = error_payload("project_not_indexed", "no index found for this project")
    assert "remedy" not in payload
    assert payload == {
        "ok": False,
        "code": "project_not_indexed",
        "message": "no index found for this project",
        "exit": 1,
    }


def test_error_payload_is_json_serializable():
    payload = error_payload("x", "y", remedy="z", exit_code=3)
    # Round-trips cleanly; ensure_ascii=False is `emit`'s job, not the payload's.
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# 2. KnowledgeError
# ---------------------------------------------------------------------------


def test_knowledge_error_carries_attributes():
    exc = KnowledgeError("project_not_indexed", "no index found", remedy="knowledge build", exit_code=2)
    assert exc.code == "project_not_indexed"
    assert exc.message == "no index found"
    assert exc.remedy == "knowledge build"
    assert exc.exit_code == 2
    assert str(exc) == "no index found"


def test_knowledge_error_defaults():
    exc = KnowledgeError("some_code", "some message")
    assert exc.remedy is None
    assert exc.exit_code == 1


# ---------------------------------------------------------------------------
# 3. main() maps KnowledgeError
# ---------------------------------------------------------------------------


def test_main_knowledge_error_prose_on_stderr(monkeypatch, capsys):
    def _raiser(args):
        raise KnowledgeError("project_not_indexed", "no index found for this project", remedy="knowledge build", exit_code=2)

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: no index found for this project" in captured.err
    assert "  try: knowledge build" in captured.err


def test_main_knowledge_error_no_remedy_omits_try_line(monkeypatch, capsys):
    def _raiser(args):
        raise KnowledgeError("some_code", "something went wrong")

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 1

    captured = capsys.readouterr()
    assert "error: something went wrong" in captured.err
    assert "try:" not in captured.err


def test_main_knowledge_error_json_on_stdout(monkeypatch, capsys):
    def _raiser(args):
        raise KnowledgeError("project_not_indexed", "no index found", remedy="knowledge build", exit_code=2)

    monkeypatch.setitem(cli._DISPATCH, "status", _raiser)
    rc = cli.main(["status", "--json"])
    assert rc == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "ok": False,
        "code": "project_not_indexed",
        "message": "no index found",
        "remedy": "knowledge build",
        "exit": 2,
    }


def test_main_knowledge_error_format_json_on_stdout(monkeypatch, capsys):
    """The defect this file was extended to guard against: `decisions` (and
    six other verbs) request machine-readable output via `--format json`,
    not `--json`. Before the fix, `getattr(args, "json", False)` missed this
    entirely and the caller got prose on stderr despite asking for JSON."""

    def _raiser(args):
        raise KnowledgeError("project_not_indexed", "no index found", remedy="knowledge build", exit_code=2)

    monkeypatch.setitem(cli._DISPATCH, "decisions", _raiser)
    rc = cli.main(["decisions", "--format", "json"])
    assert rc == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "ok": False,
        "code": "project_not_indexed",
        "message": "no index found",
        "remedy": "knowledge build",
        "exit": 2,
    }


# ---------------------------------------------------------------------------
# 4. main() maps an unanticipated exception -> 70 / internal_error
# ---------------------------------------------------------------------------


def test_main_unexpected_exception_prose_on_stderr(monkeypatch, capsys):
    def _raiser(args):
        raise ValueError("boom")

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 70

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: internal error: ValueError: boom" in captured.err
    assert "set KNOWLEDGE_TRACEBACK=1 to see the traceback" in captured.err


def test_main_unexpected_exception_json_on_stdout(monkeypatch, capsys):
    def _raiser(args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cli._DISPATCH, "status", _raiser)
    rc = cli.main(["status", "--json"])
    assert rc == 70

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["code"] == "internal_error"
    assert payload["message"] == "RuntimeError: kaboom"
    assert payload["remedy"] == "re-run with KNOWLEDGE_TRACEBACK=1 for a traceback"
    assert payload["exit"] == 70


def test_main_unexpected_exception_format_json_on_stdout(monkeypatch, capsys):
    """Same defect as the `KnowledgeError` case above, but for the
    last-resort `except Exception` clause: a `--format json` verb must get
    the structured envelope, not prose on stderr."""

    def _raiser(args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(cli._DISPATCH, "decisions", _raiser)
    rc = cli.main(["decisions", "--format", "json"])
    assert rc == 70

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["code"] == "internal_error"
    assert payload["message"] == "RuntimeError: kaboom"
    assert payload["remedy"] == "re-run with KNOWLEDGE_TRACEBACK=1 for a traceback"
    assert payload["exit"] == 70


# ---------------------------------------------------------------------------
# 5. KNOWLEDGE_TRACEBACK=1 escape hatch
# ---------------------------------------------------------------------------


def test_knowledge_traceback_env_reraises_instead_of_70(monkeypatch, capsys):
    def _raiser(args):
        raise ValueError("boom")

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    monkeypatch.setenv("KNOWLEDGE_TRACEBACK", "1")

    with pytest.raises(ValueError, match="boom"):
        cli.main(["build"])

    # Nothing structured was printed -- the real traceback is Python's job now.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_knowledge_traceback_unset_does_not_reraise(monkeypatch):
    """Sanity check for the sibling test above: without the env var, the
    same exception is swallowed into the exit-70 envelope, not re-raised."""

    def _raiser(args):
        raise ValueError("boom")

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    monkeypatch.delenv("KNOWLEDGE_TRACEBACK", raising=False)

    assert cli.main(["build"]) == 70


# ---------------------------------------------------------------------------
# 6. db.offline_errors() -> exit 4 path is untouched
# ---------------------------------------------------------------------------


class _FakeOfflineError(Exception):
    """Stand-in for a backend's real connection-loss exception type."""


def test_offline_errors_path_still_exits_4_with_original_message(monkeypatch, capsys):
    monkeypatch.setattr(db, "offline_errors", lambda: (_FakeOfflineError,))

    def _raiser(args):
        raise _FakeOfflineError("connection refused")

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "error: shared index unreachable (PostgreSQL is down or "
        "unconfigured). Reads need the DB; any writes are buffered locally "
        "and sync on the next reachable run.\n" == captured.err
    )


# ---------------------------------------------------------------------------
# 7. wants_json — single detector for both `--json` and `--format json`
# ---------------------------------------------------------------------------


class _Args:
    """Minimal stand-in for `argparse.Namespace` — only sets the attributes
    a real parsed-args object would have for a given verb, so `wants_json`'s
    `getattr(..., default)` fallbacks are actually exercised."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_wants_json_true_for_json_flag():
    assert wants_json(_Args(json=True)) is True


def test_wants_json_false_for_json_flag_false():
    assert wants_json(_Args(json=False)) is False


def test_wants_json_true_for_format_json():
    assert wants_json(_Args(format="json")) is True


def test_wants_json_false_for_format_text():
    assert wants_json(_Args(format="text")) is False


def test_wants_json_false_when_neither_attribute_present():
    """`build` has neither `--json` nor `--format` — both `getattr` calls
    fall back to their defaults."""
    assert wants_json(_Args()) is False


# ---------------------------------------------------------------------------
# 8. Secret scrubbing — `exc.message`/`str(exc)` can carry a mis-pasted DSN
# or token (settings.py's `!r` interpolation is the confirmed carrier; see
# settings.py:265-270's storage.mode error), and both exception handlers in
# `main()` must scrub before the payload reaches stdout JSON *or* stderr
# prose. Four paths total: {KnowledgeError, Exception} x {JSON, prose}.
# ---------------------------------------------------------------------------

_DSN_WITH_PASSWORD = "postgresql://alice:sup3rs3cret@db.internal:5432/knowledge"
_GITHUB_PAT = "ghp_" + "A" * 36


def test_main_knowledge_error_json_scrubs_dsn_password(monkeypatch, capsys):
    def _raiser(args):
        raise KnowledgeError(
            "settings_error",
            f"storage.mode must be 'sqlite' or 'shared_postgresql' (got {_DSN_WITH_PASSWORD!r})",
        )

    monkeypatch.setitem(cli._DISPATCH, "status", _raiser)
    rc = cli.main(["status", "--json"])
    assert rc == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "sup3rs3cret" not in payload["message"]
    assert "alice" not in payload["message"]
    # Diagnostic context must survive the scrub — otherwise the payload is
    # useless for debugging, not just secret-free.
    assert "storage.mode" in payload["message"]


def test_main_knowledge_error_prose_scrubs_dsn_password(monkeypatch, capsys):
    def _raiser(args):
        raise KnowledgeError(
            "settings_error",
            f"storage.mode must be 'sqlite' or 'shared_postgresql' (got {_DSN_WITH_PASSWORD!r})",
        )

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 1

    captured = capsys.readouterr()
    assert "sup3rs3cret" not in captured.err
    assert "alice" not in captured.err
    assert "storage.mode" in captured.err


def test_main_unexpected_exception_json_scrubs_dsn_password(monkeypatch, capsys):
    def _raiser(args):
        raise ValueError(
            f"storage.mode must be 'sqlite' or 'shared_postgresql' (got {_DSN_WITH_PASSWORD!r})"
        )

    monkeypatch.setitem(cli._DISPATCH, "decisions", _raiser)
    rc = cli.main(["decisions", "--format", "json"])
    assert rc == 70

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "sup3rs3cret" not in payload["message"]
    assert "alice" not in payload["message"]
    assert "storage.mode" in payload["message"]


def test_main_unexpected_exception_prose_scrubs_dsn_password(monkeypatch, capsys):
    def _raiser(args):
        raise ValueError(
            f"storage.mode must be 'sqlite' or 'shared_postgresql' (got {_DSN_WITH_PASSWORD!r})"
        )

    monkeypatch.setitem(cli._DISPATCH, "build", _raiser)
    rc = cli.main(["build"])
    assert rc == 70

    captured = capsys.readouterr()
    assert "sup3rs3cret" not in captured.err
    assert "alice" not in captured.err
    assert "storage.mode" in captured.err


def test_main_unexpected_exception_scrubs_github_pat(monkeypatch, capsys):
    """Second secret-pattern class covered (github_pat, not just dsn_with_credentials)."""

    def _raiser(args):
        raise ValueError(f"bad token {_GITHUB_PAT}")

    monkeypatch.setitem(cli._DISPATCH, "decisions", _raiser)
    rc = cli.main(["decisions", "--format", "json"])
    assert rc == 70

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert _GITHUB_PAT not in payload["message"]
    assert "CHANGE_ME" in payload["message"]
