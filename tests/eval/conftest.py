"""Shared plumbing for the golden-query eval harness.

The ``eval`` marker (registered in ``pyproject.toml``) is meant to behave
like ``integration``: excluded from the default run, opt-in via ``-m eval``.
Pytest's own ``-m`` deselection (``_pytest.mark.deselect_by_mark``) filters
against whatever expression the caller passed to ``-m`` — it has no notion of
"marker X is excluded unless explicitly requested". A plain ``-m "not
integration"`` run says nothing about ``eval`` at all, so without help every
``eval``-marked test would still run under it and inflate the default count.

The hook below closes that gap: if the ``-m`` expression does not mention
``eval`` as a whole word, every ``eval``-marked item is deselected before
collection finishes — using the exact same
``config.hook.pytest_deselected(items=...)`` protocol pytest's own
``deselect_by_mark`` uses (see ``_pytest/mark/__init__.py``), so tooling that
listens for that hook (e.g. ``-rs`` reporting) still sees them accounted for.
If the caller's ``-m`` expression *does* mention ``eval`` (``-m eval``,
``-m "eval and not slow"``, ``-m "not eval"``), native pytest marker
filtering already does the right thing and this hook stays out of the way.
"""

from __future__ import annotations

import re

import pytest

_EVAL_TOKEN = re.compile(r"\beval\b")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    markexpr = config.option.markexpr or ""
    if _EVAL_TOKEN.search(markexpr):
        return  # caller's -m already mentions eval; let native filtering handle it

    deselected = [item for item in items if item.get_closest_marker("eval") is not None]
    if not deselected:
        return
    remaining = [item for item in items if item not in deselected]
    config.hook.pytest_deselected(items=deselected)
    items[:] = remaining


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Redirect KNOWLEDGE_HOME to a fresh tmp dir — same pattern used by
    ``isolated_db`` in ``tests/test_vec_prefilter_scoping.py`` /
    ``tests/test_supersede_retrieval.py``. Never touches the real
    ``~/.knowledge`` or the system ``knowledge`` install."""
    home = tmp_path / "knowledge-home"
    home.mkdir()
    monkeypatch.setenv("KNOWLEDGE_HOME", str(home))
    yield home
