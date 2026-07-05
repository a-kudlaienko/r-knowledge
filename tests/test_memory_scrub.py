"""Regression tests: user-authored memory text is scrubbed before storage.

Covers three insertion paths:
  1. history.add()          — single entry, used by CLI and outbox drain
  2. history._insert_entries() — batch path used by ingest_stage / ingest_stage_dir
  3. decisions.add()        — decision + rationale + topic
  4. outbox.append()        — offline buffer written to disk before DB is reachable

The real sentence-transformer is expensive to load in unit tests, so we patch
``knowledge.embedder.get_embedder`` with a lightweight stub that returns a
zero-vector of the correct dimension. SQLite is opened in a temp file so no
shared state pollutes the run.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GITHUB_PAT = "ghp_" + "A" * 36          # matches github_pat pattern
CONNSTR = "postgres://user:s3cr3t@host/db"  # contains no SECRET_PATTERN — tested via add() prose


@pytest.fixture()
def tmp_db():
    """Yield an open SQLite connection backed by a temp file, then close it."""
    import knowledge.db as db_mod

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)

    conn = db_mod.connect_sqlite(db_path)
    yield conn
    try:
        conn.close()
    except Exception:
        pass
    db_path.unlink(missing_ok=True)


@pytest.fixture()
def project_id(tmp_db):
    """Create a throwaway project row, return its id."""
    import knowledge.projects as projects_mod

    root = Path("/tmp/test-project-scrub")
    proj = projects_mod.get_or_create_project(tmp_db, root)
    return proj.id


@pytest.fixture()
def stub_embedder():
    """Patch get_embedder() so tests never load the 130 MB model.

    history/decisions bind ``from .embedder import get_embedder`` at import
    time, so patching only ``knowledge.embedder.get_embedder`` silently
    misses whenever those modules were already imported by an earlier test
    file (order-dependent pollution). Patch every use-site alias too.
    """
    import knowledge.config as cfg_mod

    dim = cfg_mod.EMBEDDING_DIM
    stub = MagicMock()
    stub.encode.side_effect = lambda texts: np.zeros((len(texts), dim), dtype=np.float32)

    with patch("knowledge.embedder.get_embedder", return_value=stub), \
         patch("knowledge.decisions.get_embedder", return_value=stub), \
         patch("knowledge.history.get_embedder", return_value=stub):
        yield stub


# ---------------------------------------------------------------------------
# history.add() — single-entry path
# ---------------------------------------------------------------------------

def test_history_add_scrubs_short_and_long(tmp_db, project_id, stub_embedder):
    import knowledge.history as history_mod

    short = f"Fixed the outbox; token was {GITHUB_PAT}"
    long_ = f"Details: used {GITHUB_PAT} to auth; now rotated"

    entry_id = history_mod.add(
        tmp_db, project_id, short_summary=short, long_summary=long_
    )

    row = tmp_db.execute(
        "SELECT short_summary, long_summary FROM history WHERE id = ?", (entry_id,)
    ).fetchone()

    assert GITHUB_PAT not in row[0], "short_summary must not contain the raw token"
    assert GITHUB_PAT not in row[1], "long_summary must not contain the raw token"
    assert "CHANGE_ME" in row[0]
    assert "CHANGE_ME" in row[1]

    # Embedding must have been called on the *scrubbed* text, not the raw text.
    encoded_arg = stub_embedder.encode.call_args[0][0][0]
    assert GITHUB_PAT not in encoded_arg, "embed input must be scrubbed"


# ---------------------------------------------------------------------------
# history._insert_entries() — batch path (used by ingest_stage)
# ---------------------------------------------------------------------------

def test_history_insert_entries_batch_scrubs(tmp_db, project_id, stub_embedder):
    import knowledge.history as history_mod

    entries = [
        {"short": f"Deploy succeeded with token {GITHUB_PAT}", "long": "Long text"},
        {"short": "Normal entry", "long": f"Long text with {GITHUB_PAT} inside"},
    ]

    history_mod._insert_entries(tmp_db, entries, project_id)

    rows = tmp_db.execute(
        "SELECT short_summary, long_summary FROM history ORDER BY id"
    ).fetchall()

    for short_stored, long_stored in rows:
        assert GITHUB_PAT not in short_stored
        assert GITHUB_PAT not in long_stored

    # The batch encode call must not have received raw tokens either.
    for call in stub_embedder.encode.call_args_list:
        for text in call[0][0]:
            assert GITHUB_PAT not in text, f"raw token leaked to embedder: {text!r}"


# ---------------------------------------------------------------------------
# decisions.add() — topic / decision / rationale
# ---------------------------------------------------------------------------

def test_decisions_add_scrubs_all_text_fields(tmp_db, project_id, stub_embedder):
    import knowledge.decisions as decisions_mod

    topic = f"auth-{GITHUB_PAT}"
    decision = f"rotate token; was {GITHUB_PAT}"
    rationale = f"leaked in log: {GITHUB_PAT}"

    entry_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic=topic,
        decision=decision,
        rationale=rationale,
    )

    row = tmp_db.execute(
        "SELECT topic, decision, rationale FROM decisions WHERE id = ?", (entry_id,)
    ).fetchone()

    assert GITHUB_PAT not in row[0], "topic must be scrubbed"
    assert GITHUB_PAT not in row[1], "decision must be scrubbed"
    assert GITHUB_PAT not in row[2], "rationale must be scrubbed"
    assert "CHANGE_ME" in row[0]
    assert "CHANGE_ME" in row[1]
    assert "CHANGE_ME" in row[2]

    # Embed input must also be scrubbed.
    embed_text = stub_embedder.encode.call_args[0][0][0]
    assert GITHUB_PAT not in embed_text


def test_decisions_add_none_rationale_is_safe(tmp_db, project_id, stub_embedder):
    """Rationale=None must not raise; None passes through as NULL."""
    import knowledge.decisions as decisions_mod

    entry_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic="cache-strategy",
        decision="wipe on any chunk change",
        rationale=None,
    )

    row = tmp_db.execute(
        "SELECT rationale FROM decisions WHERE id = ?", (entry_id,)
    ).fetchone()
    assert row[0] is None


# ---------------------------------------------------------------------------
# outbox.append() — offline buffer written to disk
# ---------------------------------------------------------------------------

def test_outbox_append_scrubs_history_payload(tmp_path):
    import knowledge.outbox as outbox_mod

    root = tmp_path / "proj"
    root.mkdir()

    with patch("knowledge.paths.outbox_file", return_value=tmp_path / "outbox.jsonl"):
        outbox_mod.append(
            "history",
            root,
            {
                "short_summary": f"Fixed issue with token {GITHUB_PAT}",
                "long_summary": f"Token {GITHUB_PAT} was hardcoded",
                "session_id": "s-abc",
                "tags": None,
            },
        )

    raw = (tmp_path / "outbox.jsonl").read_text()
    assert GITHUB_PAT not in raw, "raw token must not appear in the outbox file"
    payload = json.loads(raw)["payload"]
    assert "CHANGE_ME" in payload["short_summary"]
    assert "CHANGE_ME" in payload["long_summary"]


def test_outbox_append_scrubs_decision_payload(tmp_path):
    import knowledge.outbox as outbox_mod

    root = tmp_path / "proj"
    root.mkdir()

    with patch("knowledge.paths.outbox_file", return_value=tmp_path / "outbox.jsonl"):
        outbox_mod.append(
            "decision",
            root,
            {
                "topic": f"token-rotation-{GITHUB_PAT}",
                "decision": f"rotate {GITHUB_PAT} monthly",
                "rationale": f"token {GITHUB_PAT} was leaked in logs",
                "session_id": "s-xyz",
            },
        )

    raw = (tmp_path / "outbox.jsonl").read_text()
    assert GITHUB_PAT not in raw, "raw token must not appear in the outbox file"
    payload = json.loads(raw)["payload"]
    assert "CHANGE_ME" in payload["topic"]
    assert "CHANGE_ME" in payload["decision"]
    assert "CHANGE_ME" in payload["rationale"]


def test_outbox_append_does_not_mutate_caller_dict(tmp_path):
    """append() must not alter the caller's payload dict."""
    import knowledge.outbox as outbox_mod

    root = tmp_path / "proj"
    root.mkdir()

    original = {
        "short_summary": f"token {GITHUB_PAT} here",
        "long_summary": "no secret",
    }
    caller_copy = dict(original)

    with patch("knowledge.paths.outbox_file", return_value=tmp_path / "outbox.jsonl"):
        outbox_mod.append("history", root, original)

    assert original == caller_copy, "append() must not mutate the caller's payload dict"


# ---------------------------------------------------------------------------
# decisions.add() — kind='fact' + context (Item H)
# ---------------------------------------------------------------------------

def test_decisions_add_fact_kind_and_context_round_trip(tmp_db, project_id, stub_embedder):
    """kind='fact' + context stores kind, folds context into rationale with
    labels, and embeds topic :: decision :: context (not just topic :: decision).
    """
    import knowledge.decisions as decisions_mod

    entry_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic="pg-types-cache-stale-oid",
        decision="delete ~/.knowledge/pg_types_cache.json after DROP/CREATE EXTENSION vector",
        rationale="a fresh fetch+rewrite fixed the connect() crash",
        kind="fact",
        context='psycopg.errors.UndefinedObject: type "vector" does not exist',
    )

    row = tmp_db.execute(
        "SELECT topic, decision, rationale, kind FROM decisions WHERE id = ?",
        (entry_id,),
    ).fetchone()
    topic, decision, rationale, kind = row
    assert kind == "fact"
    assert "Symptom: psycopg.errors.UndefinedObject" in rationale
    assert "Why it works: a fresh fetch+rewrite" in rationale

    embed_text = stub_embedder.encode.call_args[0][0][0]
    assert embed_text == (
        "pg-types-cache-stale-oid :: "
        "delete ~/.knowledge/pg_types_cache.json after DROP/CREATE EXTENSION vector :: "
        'psycopg.errors.UndefinedObject: type "vector" does not exist'
    )


def test_decisions_add_default_kind_is_decision(tmp_db, project_id, stub_embedder):
    """Plain decide() calls (no kind=, no context=) are unaffected: kind
    defaults to 'decision' and the embed text stays topic :: decision."""
    import knowledge.decisions as decisions_mod

    entry_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic="cache-strategy",
        decision="wipe on any chunk change",
    )
    row = tmp_db.execute(
        "SELECT rationale, kind FROM decisions WHERE id = ?", (entry_id,)
    ).fetchone()
    assert row[1] == "decision"
    assert row[0] is None  # no rationale, no context -> rationale stays NULL

    embed_text = stub_embedder.encode.call_args[0][0][0]
    assert embed_text == "cache-strategy :: wipe on any chunk change"


def test_decisions_add_context_is_scrubbed(tmp_db, project_id, stub_embedder):
    """context must pass through the same scrub as topic/decision/rationale,
    both in storage (folded into rationale) and in the embedded text."""
    import knowledge.decisions as decisions_mod

    entry_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic="token-leak-fix",
        decision="rotate the token",
        kind="fact",
        context=f"error log showed token {GITHUB_PAT}",
    )
    row = tmp_db.execute(
        "SELECT rationale FROM decisions WHERE id = ?", (entry_id,)
    ).fetchone()
    assert GITHUB_PAT not in row[0]
    assert "CHANGE_ME" in row[0]

    embed_text = stub_embedder.encode.call_args[0][0][0]
    assert GITHUB_PAT not in embed_text


def test_decisions_recent_kind_filter(tmp_db, project_id, stub_embedder):
    import knowledge.decisions as decisions_mod

    decisions_mod.add(
        tmp_db, project_id=project_id, topic="d1", decision="a decision",
    )
    decisions_mod.add(
        tmp_db, project_id=project_id, topic="f1", decision="a fact",
        kind="fact", context="some symptom",
    )

    only_facts = decisions_mod.recent(tmp_db, project_id=project_id, kind="fact")
    only_decisions = decisions_mod.recent(tmp_db, project_id=project_id, kind="decision")
    both = decisions_mod.recent(tmp_db, project_id=project_id)

    assert [d.topic for d in only_facts] == ["f1"]
    assert [d.topic for d in only_decisions] == ["d1"]
    assert {d.topic for d in both} == {"d1", "f1"}


def test_decisions_search_finds_fact_by_symptom_text(tmp_db, project_id, stub_embedder):
    """search() with kind='fact' returns only fact rows; default (no kind)
    returns both — the conflict check must see facts alongside decisions."""
    import knowledge.decisions as decisions_mod

    decisions_mod.add(
        tmp_db, project_id=project_id, topic="d1", decision="a decision",
    )
    fact_id = decisions_mod.add(
        tmp_db, project_id=project_id, topic="pg-types-cache-stale-oid",
        decision="delete the cache file", kind="fact",
        context='psycopg.errors.UndefinedObject: type "vector" does not exist',
    )

    results = decisions_mod.search(
        tmp_db, query="UndefinedObject vector does not exist",
        project_id=project_id, kind="fact",
    )
    assert [d.id for d, _dist in results] == [fact_id]

    both = decisions_mod.search(
        tmp_db, query="UndefinedObject vector does not exist", project_id=project_id,
    )
    assert {d.topic for d, _dist in both} == {"d1", "pg-types-cache-stale-oid"}


# ---------------------------------------------------------------------------
# `knowledge fact` CLI verb — end to end (CLI -> row -> embedded text)
# ---------------------------------------------------------------------------

def test_cmd_fact_end_to_end(tmp_db, project_id, stub_embedder, tmp_path, monkeypatch):
    """cmd_fact (the `knowledge fact` dispatch target) writes a kind='fact'
    row whose embedded text includes the (scrubbed) context, mirroring the
    CLI -> decisions.add() -> embedder call chain a real invocation takes."""
    import argparse

    import knowledge.cli as cli_mod
    import knowledge.projects as projects_mod

    root = tmp_path / "proj"
    root.mkdir()

    monkeypatch.setattr(projects_mod, "current_project_root", lambda *a, **kw: root)
    monkeypatch.setattr(projects_mod, "current_author", lambda *a, **kw: "Test Author")
    monkeypatch.setattr(cli_mod.db, "connect", lambda *a, **kw: tmp_db)
    monkeypatch.setattr(
        "knowledge.paths.outbox_file", lambda _root: tmp_path / "outbox.jsonl"
    )

    args = argparse.Namespace(
        topic="pg-types-cache-stale-oid",
        fact_text="delete ~/.knowledge/pg_types_cache.json after DROP/CREATE EXTENSION vector",
        context=f"psycopg error: type not found, token {GITHUB_PAT}",
        rationale="a fresh fetch+rewrite fixed the connect() crash",
        files=["knowledge/backends/postgres.py"],
        session_id=None,
        supersede=None,
        override_reason=None,
        project=None,
    )

    rc = cli_mod.cmd_fact(args)
    assert rc == 0

    row = tmp_db.execute(
        "SELECT topic, decision, rationale, kind FROM decisions "
        "WHERE topic = ? ORDER BY id DESC LIMIT 1",
        ("pg-types-cache-stale-oid",),
    ).fetchone()
    topic, decision, rationale, kind = row
    assert kind == "fact"
    assert GITHUB_PAT not in rationale
    assert "CHANGE_ME" in rationale

    embed_text = stub_embedder.encode.call_args[0][0][0]
    assert embed_text.startswith("pg-types-cache-stale-oid :: ")
    assert GITHUB_PAT not in embed_text
    assert "CHANGE_ME" in embed_text


def test_cmd_fact_supersedes_with_same_gate_and_author_chain(
    tmp_db, project_id, stub_embedder, tmp_path, monkeypatch
):
    """A better fact uses the decision override gate and attribution chain."""
    import argparse

    import knowledge.cli as cli_mod
    import knowledge.decisions as decisions_mod
    import knowledge.projects as projects_mod

    # Match the root used by the ``project_id`` fixture: supersede ids are
    # deliberately project-scoped, so a different synthetic root must fail.
    root = Path("/tmp/test-project-scrub")
    prior_id = decisions_mod.add(
        tmp_db,
        project_id=project_id,
        topic="cache-fix",
        decision="old fix",
        kind="fact",
        author="Prior Author",
    )

    monkeypatch.setattr(projects_mod, "current_project_root", lambda *a, **kw: root)
    monkeypatch.setattr(projects_mod, "current_author", lambda *a, **kw: "New Author")
    monkeypatch.setattr(cli_mod.db, "connect", lambda *a, **kw: tmp_db)
    monkeypatch.setattr(
        "knowledge.paths.outbox_file", lambda _root: tmp_path / "outbox.jsonl"
    )

    args = argparse.Namespace(
        topic="cache-fix",
        fact_text="better fix",
        context="same raw symptom",
        rationale="verified by the regression test",
        files=["knowledge/decisions.py"],
        session_id=None,
        supersede=prior_id,
        override_reason="the old fix was incomplete",
        project=None,
    )

    assert cli_mod.cmd_fact(args) == 0
    row = tmp_db.execute(
        "SELECT kind, author, supersedes, override_reason FROM decisions "
        "WHERE topic = ? ORDER BY id DESC LIMIT 1",
        ("cache-fix",),
    ).fetchone()
    assert row == ("fact", "New Author", prior_id, "the old fix was incomplete")


# ---------------------------------------------------------------------------
# outbox — kind/context round trip (Item H)
# ---------------------------------------------------------------------------

def test_outbox_append_scrubs_and_carries_fact_kind_and_context(tmp_path):
    import knowledge.outbox as outbox_mod

    root = tmp_path / "proj"
    root.mkdir()

    with patch("knowledge.paths.outbox_file", return_value=tmp_path / "outbox.jsonl"):
        outbox_mod.append(
            "decision",
            root,
            {
                "topic": "pg-types-cache-stale-oid",
                "decision": "delete the cache file",
                "rationale": "evidence it works",
                "context": f"error log token {GITHUB_PAT}",
                "kind": "fact",
                "session_id": "s-xyz",
            },
        )

    raw = (tmp_path / "outbox.jsonl").read_text()
    assert GITHUB_PAT not in raw
    entry = json.loads(raw)
    payload = entry["payload"]
    assert payload["kind"] == "fact"
    assert "CHANGE_ME" in payload["context"]


def test_outbox_apply_defaults_missing_kind_to_decision(tmp_db, project_id, stub_embedder):
    """Entries buffered by a pre-Item-H client have no 'kind' key at all --
    _apply() must default it to 'decision' rather than raising a TypeError
    against the current decisions.add() signature."""
    import knowledge.outbox as outbox_mod

    entry = {
        "root": str(Path("/tmp/test-project-scrub")),
        "kind": "decision",
        "payload": {
            "topic": "legacy-entry",
            "decision": "some old decision with no kind field",
            "rationale": None,
            "files_touched": None,
            "session_id": None,
            "author": "Someone",
            "supersedes": None,
            "override_reason": None,
            # NOTE: no "kind" key here on purpose -- simulates a pre-Item-H
            # buffered entry.
        },
    }
    outbox_mod._apply(tmp_db, entry)

    row = tmp_db.execute(
        "SELECT kind FROM decisions WHERE topic = ?", ("legacy-entry",)
    ).fetchone()
    assert row[0] == "decision"


# ---------------------------------------------------------------------------
# Idempotency: scrub_text itself
# ---------------------------------------------------------------------------

def test_scrub_text_idempotent():
    """Applying scrub_text twice must produce the same result as once."""
    from knowledge.sanitizer import scrub_text

    raw = f"Token: {GITHUB_PAT}"
    once = scrub_text(raw)
    twice = scrub_text(once)
    assert once == twice
