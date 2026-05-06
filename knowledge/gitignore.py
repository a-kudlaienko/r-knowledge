"""Wrapper around ``pathspec`` for .gitignore + .knowledgeignore handling.

Merges, in priority order (later wins on conflicts):

1. ``config.EXCLUDE_GLOBS`` — never-index floor.
2. Project-root ``.gitignore`` (if present).
3. Project-root ``.knowledgeignore`` (escape hatch for "don't index, but
   don't add to git ignores" — e.g., generated docs).

Nested ``.gitignore`` files at subdirectory level land in M6. For M2 the
root-level file covers the common case and keeps scanning fast.
"""

from __future__ import annotations

from pathlib import Path

from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

from . import config


def load_specs(project_root: Path) -> PathSpec:
    patterns: list[str] = list(config.EXCLUDE_GLOBS)

    for name in (".gitignore", ".knowledgeignore"):
        path = project_root / name
        if path.exists():
            # ``splitlines`` strips trailing newlines; pathspec handles
            # blanks and ``#`` comments itself.
            patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())

    return PathSpec.from_lines(GitWildMatchPattern, patterns)


def is_ignored(spec: PathSpec, rel_path: str) -> bool:
    return spec.match_file(rel_path)
