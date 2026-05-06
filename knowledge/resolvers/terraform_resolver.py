"""Terraform / HCL dependency resolver — regex + brace-depth tracking.

No tree-sitter here for the same reason ``hcl_chunker.py`` avoids it:
the bundled grammar is unreliable across releases and HCL's import-y
surface is regular enough to parse with a small state machine.

Edges emitted (file-to-file):

* ``tf_module``        — ``module "name" { source = "…" }``. Local relative
                         sources resolve against the importing file's dir;
                         anything else (registry, ``git::``, ``tfr://``) is
                         passed through as ``raw`` and ends up external.
* ``tf_templatefile``  — ``templatefile("path", {...})`` calls. Local-only;
                         we keep the raw path even when it starts with
                         ``${path.module}/…`` so the resolver can strip that.
* ``tf_file``          — ``file("path")`` / ``filebase64("path")``. Same
                         resolution rules as templatefile.

Non-goals for Phase 2:

* ``terraform_remote_state`` — cross-state references. None observed in
  the target repo; deferrable.
* Backend-config file refs (e.g. ``backend "s3" { ... }``) — Terraform
  settings, not file deps.
* Provider ``source = "hashicorp/…"``. These live inside
  ``terraform { required_providers { … } }`` and point at the registry;
  not a project file — always external, and not useful to clutter edges.

Resolution of the ``raw`` path → ``target_file_id`` lives in
``relations._resolve_terraform``; this resolver stays pure.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseResolver, Edge

# Match ``module "label" {`` at any indentation. We only need the label
# to know it's a module block — the real payload is the body we scan
# next for ``source = "..."``. ``re.MULTILINE`` + start-of-line anchoring
# keeps us from matching something like ``# module "foo" {`` inside a
# comment that happens to start at column 0, but the body-level comment
# stripping below handles the rest.
_MODULE_HEADER_RE = re.compile(
    r"""
    ^[\t ]*                          # line-leading whitespace only (no content)
    module
    [\t ]+
    "([^"]+)"                        # the module label (group 1)
    [\t ]*\{                         # opening brace
    """,
    re.VERBOSE | re.MULTILINE,
)

# ``source = "..."`` or ``source = "\${path.module}/…"``. We don't
# interpolate — we take the literal string and let resolution decide.
_SOURCE_ASSIGN_RE = re.compile(
    r'(?m)^[\t ]*source[\t ]*=[\t ]*"([^"]+)"'
)

# ``templatefile("path", { … })``. Allow optional whitespace and either
# quoted path form (double). Single-quoted strings aren't valid HCL so
# we skip that form.
_TEMPLATEFILE_RE = re.compile(
    r'templatefile\s*\(\s*"([^"]+)"\s*,'
)

# ``file("path")`` and ``filebase64("path")``. Match either, single group.
_FILE_CALL_RE = re.compile(
    r'\b(?:file|filebase64)\s*\(\s*"([^"]+)"\s*\)'
)


class TerraformResolver(BaseResolver):
    """One-pass scan: strip comments/strings for module-header matching,
    then walk the source text to emit the three edge kinds.
    """

    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        text = source_bytes.decode("utf-8", errors="replace")
        # Comments could contain syntax that looks like a module/function
        # call. Strip them to a masked version where they're replaced by
        # equal-length whitespace — preserves byte offsets (so line
        # numbers from the ORIGINAL text still line up for Edge.line).
        masked = _mask_comments(text)

        edges: list[Edge] = []

        # Modules: find header, then scan forward for ``source = "..."``
        # up to the matching closing brace. Brace-depth tracker keeps us
        # inside the one module block we just entered.
        for m in _MODULE_HEADER_RE.finditer(masked):
            body_start = m.end()
            body_end = _find_matching_close_brace(masked, body_start)
            if body_end is None:
                continue
            block_body = masked[body_start:body_end]
            source_match = _SOURCE_ASSIGN_RE.search(block_body)
            if source_match is None:
                continue
            raw = source_match.group(1)
            # Report the line where ``source = ...`` lives (not the block
            # header) — more useful for jumping to the definition.
            line = _offset_to_line(text, body_start + source_match.start())
            edges.append(Edge(kind="tf_module", raw=raw, symbol=None, line=line))

        # templatefile("path", ...) — scan whole file (calls can appear
        # in resource bodies, locals, data sources).
        for m in _TEMPLATEFILE_RE.finditer(masked):
            raw = m.group(1)
            line = _offset_to_line(text, m.start())
            edges.append(
                Edge(kind="tf_templatefile", raw=raw, symbol=None, line=line)
            )

        # file() / filebase64() calls. Skip commented-out ones (already
        # masked). Could match inside strings — HCL's heredoc + quoted
        # strings would false-positive here, but the ``file(`` prefix is
        # rarely appearing inside literal text, and false positives land
        # as external anyway (no harm done).
        for m in _FILE_CALL_RE.finditer(masked):
            raw = m.group(1)
            line = _offset_to_line(text, m.start())
            edges.append(Edge(kind="tf_file", raw=raw, symbol=None, line=line))

        return edges


# ---------------------------------------------------------------------------
# Byte-preserving comment and string masking
# ---------------------------------------------------------------------------


def _mask_comments(text: str) -> str:
    """Return a copy of ``text`` with comments replaced by same-length
    whitespace. Preserves offsets so ``Edge.line`` aligns with the
    original source. Three comment forms:

    * ``# ...`` to end of line
    * ``// ...`` to end of line
    * ``/* ... */`` block

    Quoted strings are preserved as-is. Heredoc (``<<EOF``…``EOF``) is
    not specially handled — if a heredoc contains ``# ...`` lines we
    may over-mask; HCL heredocs with embedded comment-looking syntax
    are uncommon in Terraform files.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Start of a quoted string — copy through the closing quote,
        # respecting backslash escapes.
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue

        # Line comments: # or //
        if c == "#" or (c == "/" and i + 1 < n and text[i + 1] == "/"):
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue

        # Block comment /* ... */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                out.append(" " * (n - i))
                i = n
                continue
            end += 2
            # Preserve newlines so line-number arithmetic stays correct.
            masked = "".join(ch if ch == "\n" else " " for ch in text[i:end])
            out.append(masked)
            i = end
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _find_matching_close_brace(text: str, start: int) -> int | None:
    """Return the index of the ``}`` that matches the ``{`` immediately
    preceding ``start`` (i.e. the opening brace was the last character
    consumed by our regex). Returns None if unmatched."""
    depth = 1
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Skip the string in full — masked text still has its quoted
            # content, so we need to walk it here too.
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _offset_to_line(text: str, offset: int) -> int:
    """1-based line number for a character offset in ``text``.

    Used instead of re-computing from scratch for each edge; the caller
    passes offsets already within range. A scan of the prefix is O(N)
    per call, which is fine for per-file resolver latency.
    """
    if offset <= 0:
        return 1
    return text.count("\n", 0, offset) + 1
