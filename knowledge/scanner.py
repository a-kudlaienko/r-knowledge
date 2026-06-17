"""Walk a project root, yielding indexable ``(abs_path, lang)`` pairs.

Applies ``.gitignore`` + ``.knowledgeignore`` + ``config.EXCLUDE_GLOBS``
and the ``config.EXT_TO_LANG`` extension map. Files with unrecognized
extensions are silently skipped (not an error — intentional: the index
only covers known languages).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from pathspec import PathSpec

from . import config
from .gitignore import load_specs


def classify_file(p: Path) -> str | None:
    """Return the language tag for a file, or None if unknown/unsupported."""
    if p.name in config.EXT_TO_LANG:  # e.g., exact match for "Dockerfile"
        return config.EXT_TO_LANG[p.name]
    return config.EXT_TO_LANG.get(p.suffix)


def walk_project(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, lang)`` for every indexable file.

    Ignore rules are applied at directory level (prune) and file level.
    Pruning directories is critical — without it, the scanner descends
    into ``node_modules/`` before checking, which on large repos is the
    difference between 2 seconds and 2 minutes.
    """
    spec = load_specs(root)
    root_resolved = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        rel_dir = dir_path.relative_to(root).as_posix()

        # Prune ignored subdirs in-place so os.walk skips them entirely.
        # os.walk uses the empty string for the root dir; gitignore rules
        # are relative to the root, so we join before matching.
        dirnames[:] = [
            d for d in dirnames
            if not _dir_is_ignored(spec, rel_dir, d)
        ]

        for fname in filenames:
            rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            if spec.match_file(rel_path):
                continue

            p = dir_path / fname
            # M5: a file symlink can point outside the repo (e.g. a planted
            # 'creds.conf -> ~/.aws/credentials'). Indexing it would pull
            # external secrets into the DB and, in shared mode, replicate them
            # to teammates. Permit in-repo symlinks (target stays under root);
            # skip any whose target escapes the project. (os.walk already does
            # not descend directory symlinks — followlinks defaults to False.)
            if p.is_symlink():
                try:
                    if not p.resolve().is_relative_to(root_resolved):
                        continue
                except OSError:
                    continue
            lang = classify_file(p)
            if lang is None:
                continue
            yield p, lang


def _dir_is_ignored(spec: PathSpec, rel_dir: str, name: str) -> bool:
    rel = name if rel_dir == "." else f"{rel_dir}/{name}"
    # Trailing slash nudges GitWildMatch toward directory semantics.
    return spec.match_file(f"{rel}/")
