"""Wrapper around ``pathspec`` for .gitignore + .knowledgeignore handling.

Merges, in priority order (later wins on conflicts):

1. ``config.EXCLUDE_GLOBS`` — never-index floor.
2. Project-root ``.gitignore`` (if present).
3. Project-root ``.knowledgeignore`` (escape hatch for "don't index, but
   don't add to git ignores" — e.g., generated docs).
4. Nested ``<subdir>/.gitignore`` / ``<subdir>/.knowledgeignore`` — each
   pattern is re-rooted under its directory (git semantics) so a secret file
   ignored only by a subdirectory's ``.gitignore`` is still excluded from the
   index (M6 — security: such a file would otherwise be scanned, and on a
   shared backend replicated to teammates).
"""

from __future__ import annotations

import os
from pathlib import Path

from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

from . import config

_IGNORE_FILES = (".gitignore", ".knowledgeignore")


def load_specs(project_root: Path) -> PathSpec:
    patterns: list[str] = list(config.EXCLUDE_GLOBS)

    # Root-level ignore files apply verbatim (already repo-root-relative).
    for name in _IGNORE_FILES:
        path = project_root / name
        if path.exists():
            # ``splitlines`` strips trailing newlines; pathspec handles
            # blanks and ``#`` comments itself.
            patterns.extend(
                path.read_text(encoding="utf-8", errors="replace").splitlines()
            )

    patterns.extend(_nested_ignore_patterns(project_root))
    return PathSpec.from_lines(GitWildMatchPattern, patterns)


def _nested_ignore_patterns(project_root: Path) -> list[str]:
    """Collect re-rooted patterns from every subdirectory ignore file.

    Walks the tree (pruning the never-index floor so we don't descend into
    ``node_modules`` / ``.git`` to find ignore files) and rewrites each
    pattern so it's relative to the repo root instead of its own directory.
    """
    floor = PathSpec.from_lines(GitWildMatchPattern, config.EXCLUDE_GLOBS)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = Path(dirpath).relative_to(project_root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
            # Root ignore files were already loaded verbatim above.
            skip_here = set(_IGNORE_FILES)
        else:
            skip_here = set()
        # Prune floor-excluded subdirs so the walk stays cheap.
        dirnames[:] = [
            d for d in dirnames
            if not floor.match_file(f"{rel_dir}/{d}/" if rel_dir else f"{d}/")
        ]
        for name in _IGNORE_FILES:
            if name in skip_here or name not in filenames:
                continue
            text = (Path(dirpath) / name).read_text(
                encoding="utf-8", errors="replace"
            )
            for line in text.splitlines():
                rerooted = _reroot_pattern(line, rel_dir)
                if rerooted is not None:
                    out.append(rerooted)
    return out


def _reroot_pattern(line: str, rel_dir: str) -> str | None:
    """Rewrite one ignore pattern so it applies relative to the repo root.

    ``rel_dir`` is the posix path (no trailing slash) of the directory the
    ignore file lives in. Returns ``None`` for blanks/comments. Mirrors git:
    a pattern containing a non-trailing slash is anchored to ``rel_dir``; an
    unanchored pattern matches at any depth *below* ``rel_dir`` (``**/``).
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if not rel_dir:
        return raw  # already root-relative

    neg = ""
    if raw.startswith("!"):
        neg, raw = "!", raw[1:]
    anchored = raw.startswith("/") or ("/" in raw.rstrip("/"))
    body = raw.lstrip("/")
    if anchored:
        return f"{neg}{rel_dir}/{body}"
    # Unanchored: matches in rel_dir and any nested subdir. ``**`` in
    # gitwildmatch spans zero-or-more segments, so this covers both.
    return f"{neg}{rel_dir}/**/{body}"


def is_ignored(spec: PathSpec, rel_path: str) -> bool:
    return spec.match_file(rel_path)
