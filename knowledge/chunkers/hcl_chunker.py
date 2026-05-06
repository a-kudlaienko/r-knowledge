"""HCL / Terraform chunker — regex + brace-tracking, no tree-sitter.

The ``tree-sitter-languages`` bundle's HCL grammar is inconsistent across
releases, so we implement a targeted parser here. HCL top-level syntax is
regular:

    block_type ["label1" ["label2"]] {
        ...
    }

and we only need to identify top-level blocks plus their ``locals {}``
entries, which a brace-depth tracker handles cleanly. Strings and all
three comment styles (``#``, ``//``, ``/* ... */``) are respected so we
don't mistake braces inside a quoted string for block delimiters.

Block kinds emitted:

* ``resource``  / ``module`` / ``variable`` / ``output`` / ``provider``
  / ``data`` — canonical Terraform blocks.
* ``terraform_config`` — the ``terraform { … }`` settings block.
* ``locals_block`` — one per ``locals {}`` (covers the whole block).
* ``locals_entry`` — one per key *inside* ``locals {}`` (fine-grained
  retrieval, which is what the plan wants).

Sanitizer layer 2 runs per chunk via ``scrub_hcl_sensitive_values`` — a
regex matching ``key = "string"`` pairs and scrubbing when the key name
is sensitive.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..sanitizer import scrub_hcl_sensitive_values
from .base import BaseChunker, Chunk


_BLOCK_HEADER_RE = re.compile(
    r"""
    (?P<type>[a-zA-Z_][\w-]*)        # block type: resource, module, locals, ...
    (?P<labels>(?:\s+"[^"]*")*)       # zero or more quoted labels
    \s*\{                             # opening brace
    """,
    re.VERBOSE,
)

_LABEL_RE = re.compile(r'"([^"]*)"')

# locals { x = 1 } — entries use `=` assignment.
# For locals_entry extraction we re-scan the body with a simpler pattern.
_LOCALS_KEY_RE = re.compile(r"(?m)^\s*([a-zA-Z_][\w-]*)\s*=")

_BLOCK_TYPE_TO_KIND = {
    "resource":  "resource",
    "module":    "module",
    "variable":  "variable",
    "output":    "output",
    "provider":  "provider",
    "data":      "data",
    "terraform": "terraform_config",
    "locals":    "locals_block",
}


class HclChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        chunks: list[Chunk] = []
        n = len(text)
        i = 0
        block_order = 0

        while i < n:
            i = _skip_ws_and_comments(text, i)
            if i >= n:
                break

            m = _BLOCK_HEADER_RE.match(text, i)
            if m is None:
                # Not a block at this position; skip to next line and retry.
                nl = text.find("\n", i)
                i = nl + 1 if nl >= 0 else n
                continue

            block_type = m.group("type")
            labels = _LABEL_RE.findall(m.group("labels") or "")
            header_end = m.end()
            open_brace = header_end - 1

            close_brace = _find_matching_brace(text, open_brace)
            if close_brace is None:
                break  # unclosed block — bail rather than emit garbage

            block_start = i
            block_end = close_brace + 1  # inclusive of '}'

            kind = _BLOCK_TYPE_TO_KIND.get(block_type)
            if kind is not None:
                name = _block_name(block_type, labels)
                chunks.append(
                    _make_chunk(text, block_start, block_end, kind, name, block_order)
                )
                block_order += 1

                # Also emit per-key locals_entry sub-chunks for locals blocks.
                if block_type == "locals":
                    chunks.extend(
                        _extract_locals_entries(text, open_brace + 1, close_brace, block_order)
                    )
                    # We don't bump block_order for children — they share the
                    # parent's conceptual sibling position. If M5 wants them
                    # rooted under the block, parent_idx will link them.

            # else: unknown top-level block kind (could be a custom DSL on
            # top of HCL) — skip silently.

            i = block_end

        return chunks


# ---------------------------------------------------------------------------
# Chunk construction
# ---------------------------------------------------------------------------


def _make_chunk(
    text: str,
    start: int,
    end: int,
    kind: str,
    name: str | None,
    sibling_order: int,
) -> Chunk:
    raw = text[start:end]
    scrubbed = scrub_hcl_sensitive_values(raw)
    return Chunk(
        kind=kind,
        name=name,
        qualified_name=name,
        start_line=_line_of(text, start),
        end_line=_line_of(text, end - 1),
        start_byte=len(text[:start].encode("utf-8")),
        end_byte=len(text[:end].encode("utf-8")),
        text=scrubbed,
        sibling_order=sibling_order,
    )


def _block_name(block_type: str, labels: list[str]) -> str | None:
    """Canonical display name for a block:

    * resource/data with two labels → ``type.name``
    * one-label blocks (module, variable, output, provider) → ``name``
    * zero-label blocks (terraform, locals) → None
    """
    if block_type in ("resource", "data") and len(labels) >= 2:
        return f"{labels[0]}.{labels[1]}"
    if labels:
        return labels[0]
    return None


def _extract_locals_entries(
    text: str, body_start: int, body_end: int, start_order: int
) -> list[Chunk]:
    """Emit one chunk per assignment inside ``locals { ... }``.

    Finds each ``name =`` at line start within the body, then slices from
    that start to the start of the next assignment (or the body end).
    """
    body = text[body_start:body_end]
    matches = list(_LOCALS_KEY_RE.finditer(body))
    if not matches:
        return []

    out: list[Chunk] = []
    for idx, m in enumerate(matches):
        entry_body_start = m.start()
        entry_body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        abs_start = body_start + entry_body_start
        abs_end = body_start + entry_body_end
        name = m.group(1)
        out.append(
            _make_chunk(text, abs_start, abs_end, "locals_entry", name, start_order + idx)
        )
    return out


# ---------------------------------------------------------------------------
# Lex helpers: skip whitespace/comments, find matching brace
# ---------------------------------------------------------------------------


def _skip_ws_and_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else n
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else n
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = end + 2 if end >= 0 else n
            continue
        return i
    return i


def _find_matching_brace(text: str, open_pos: int) -> int | None:
    """Return the index of the ``}`` that matches the ``{`` at ``open_pos``.

    Respects string literals (double quotes with backslash escapes) and all
    three HCL comment styles. Returns None for an unclosed block.
    """
    assert text[open_pos] == "{"
    n = len(text)
    i = open_pos
    depth = 0
    in_string = False

    while i < n:
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue

        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else n
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else n
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = end + 2 if end >= 0 else n
            continue

        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return None


def _line_of(text: str, index: int) -> int:
    """1-based line number for a character index."""
    if index <= 0:
        return 1
    return text.count("\n", 0, index) + 1
