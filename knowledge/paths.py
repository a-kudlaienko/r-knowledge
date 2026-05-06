"""Filesystem locations.

Everything lives under ``~/.knowledge/`` by default. Override via the
``KNOWLEDGE_HOME`` env var (useful for tests and isolated runs).
"""

from __future__ import annotations

import os
from pathlib import Path


def user_dir() -> Path:
    """Return ``~/.knowledge/`` (creating it on first access)."""
    override = os.environ.get("KNOWLEDGE_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    """Single SQLite DB shared by all projects."""
    return user_dir() / "index.sqlite"


def models_dir() -> Path:
    """sentence-transformers cache."""
    p = user_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    """Global user config (cache budget, model override)."""
    return user_dir() / "config.json"


def stage_dir() -> Path:
    """Scratch dir for staged work-summaries awaiting ingest."""
    p = user_dir() / "stage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stage_path() -> Path:
    """Default staged-summaries file. JSONL, one entry per line.

    ``knowledge history ingest`` reads this path, embeds + inserts each
    entry, and truncates the file on SQL success. On failure the file is
    left intact so the caller can keep appending.
    """
    return stage_dir() / "pending.jsonl"
