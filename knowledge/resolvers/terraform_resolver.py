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
* ``tf_decl``          — metadata edge for a declaration in THIS file:
                         ``variable "X" {}`` → ``raw="var.X"``,
                         ``locals { X = ... }`` → ``raw="local.X"``,
                         ``module "X" {}`` → ``raw="module.X"``.
                         Consumed by ``FileIndex.prepare`` to build the
                         ``tf_decls`` side-map; dropped before persistence.
* ``tf_var_ref``       — a ``var.X`` reference. Resolves to the file in
                         the same terraform-root directory that declares
                         ``variable "X"``.
* ``tf_local_ref``     — a ``local.X`` reference. Same scoping rule.
* ``tf_module_ref``    — a ``module.X.attr`` reference (reading a module
                         output). Resolves to the file that declared
                         ``module "X" {}`` in the same directory.

Terraform merges every ``.tf`` file in one directory into a single
compilation unit — that's why same-dir scoping is the right rule for
refs. ``.tfvars`` files (plain ``NAME = VALUE`` assignments at top
level) emit ``tf_var_ref`` per assignment, linking a values file to
the ``variables.tf`` it parameterizes.

Non-goals:

* ``terraform_remote_state`` — cross-state references. None observed in
  the target repo; deferrable.
* Backend-config file refs (e.g. ``backend "s3" { ... }``) — Terraform
  settings, not file deps.
* Provider ``source = "hashicorp/…"``. These live inside
  ``terraform { required_providers { … } }`` and point at the registry;
  not a project file — always external, and not useful to clutter edges.
* Resource / data / provider references. ``aws_instance.web.id`` could
  be linked to wherever ``resource "aws_instance" "web" {}`` lives, but
  that's an order of magnitude more edges and hasn't been asked for.

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

# HCL identifier: starts with letter/underscore, then letters/digits/
# underscores/hyphens. ASCII-only; Terraform allows Unicode but real
# code doesn't use it, and restricting to ASCII keeps the regex tight
# and the false-positive rate low when scanning masked text.
_IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"

# Declaration headers. Anchored at start-of-line (with optional leading
# whitespace) so ``variable "x"`` in prose or heredoc text doesn't match.
_VARIABLE_HEADER_RE = re.compile(
    rf'(?m)^[\t ]*variable[\t ]+"({_IDENT})"[\t ]*\{{'
)
_LOCALS_HEADER_RE = re.compile(
    r"(?m)^[\t ]*locals[\t ]*\{"
)

# Reference expressions. No line-anchor — references appear mid-expression.
# Negative-lookbehind rejects ``foo.var.X`` / ``mod.local.X`` so a bare
# ``foo.var`` attribute isn't confused for a top-level reference.
_VAR_REF_RE = re.compile(rf"(?<![.\w])var\.({_IDENT})")
_LOCAL_REF_RE = re.compile(rf"(?<![.\w])local\.({_IDENT})")
# Module refs need TWO segments: ``module.NAME.OUTPUT``. We only keep
# the module name; the output suffix is just consumed so we advance
# past it. Skip bare ``module.NAME`` (no dot after) — that's only seen
# in declaration headers, already matched by _MODULE_HEADER_RE.
_MODULE_REF_RE = re.compile(rf"(?<![.\w])module\.({_IDENT})\.{_IDENT}")

# Top-level ``NAME = ...`` assignment for .tfvars files. Anchored at
# start-of-line so nested block assignments don't match. Terminates at
# the ``=`` so a long RHS doesn't need to be parsed.
_TFVARS_ASSIGN_RE = re.compile(
    rf"(?m)^[\t ]*({_IDENT})[\t ]*="
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

        # .tfvars files are top-level ``NAME = VALUE`` assignments with no
        # surrounding block. Each assignment sets a variable's value,
        # which means the file references that variable declaration. Emit
        # one tf_var_ref per assignment; skip the rest of the resolver
        # passes (no module/variable/locals blocks in a .tfvars file).
        if file_path is not None and _is_tfvars(file_path):
            for m in _TFVARS_ASSIGN_RE.finditer(masked):
                name = m.group(1)
                line = _offset_to_line(text, m.start())
                edges.append(Edge(
                    kind="tf_var_ref",
                    raw=f"var.{name}",
                    symbol=None,
                    line=line,
                ))
            return edges

        # Declarations. Strings are not masked (they could contain
        # heredoc-embedded HCL that would false-match), but the header
        # regexes are start-of-line-anchored which keeps false matches
        # rare. Emit a tf_decl edge per declaration; FileIndex.prepare
        # folds these into the per-directory name→file map.
        for m in _VARIABLE_HEADER_RE.finditer(masked):
            name = m.group(1)
            line = _offset_to_line(text, m.start())
            edges.append(Edge(
                kind="tf_decl", raw=f"var.{name}", symbol=None, line=line
            ))

        for m in _LOCALS_HEADER_RE.finditer(masked):
            body_start = m.end()
            body_end = _find_matching_close_brace(masked, body_start)
            if body_end is None:
                continue
            block_body = masked[body_start:body_end]
            # Top-level assignments inside the locals block. Nested map
            # keys (``foo = { bar = 1 }``) are skipped because their line
            # indent is inside an already-opened brace — we take only
            # depth-0 keys via a tiny walker.
            for name, name_off in _top_level_assignments(block_body):
                line = _offset_to_line(text, body_start + name_off)
                edges.append(Edge(
                    kind="tf_decl", raw=f"local.{name}",
                    symbol=None, line=line,
                ))

        # Module declarations also exist as headers; _MODULE_HEADER_RE
        # already found them once above for the source= walk. Re-run it
        # to emit the decl edge (the first walk only emitted tf_module
        # for the source line).
        for m in _MODULE_HEADER_RE.finditer(masked):
            name = m.group(1)
            line = _offset_to_line(text, m.start())
            edges.append(Edge(
                kind="tf_decl", raw=f"module.{name}", symbol=None, line=line
            ))

        # References. Scan masked-with-strings-too text so a ``"var.x"``
        # string literal doesn't emit a spurious edge. Heredoc bodies
        # that interpolate ``${var.x}`` for real ARE references — but
        # they sit inside quoted-string-like syntax; masking strings
        # drops them. That's the safer trade: losing a few real heredoc
        # refs is better than flooding the graph with prose false
        # positives. De-dupe by (kind, name) per file so the same var
        # referenced 20 times doesn't produce 20 edges to the same target.
        ref_masked = _mask_strings(masked)
        seen: set[tuple[str, str]] = set()
        for rx, kind, prefix in (
            (_VAR_REF_RE, "tf_var_ref", "var"),
            (_LOCAL_REF_RE, "tf_local_ref", "local"),
            (_MODULE_REF_RE, "tf_module_ref", "module"),
        ):
            for m in rx.finditer(ref_masked):
                name = m.group(1)
                key = (kind, name)
                if key in seen:
                    continue
                seen.add(key)
                raw = f"{prefix}.{name}"
                line = _offset_to_line(text, m.start())
                edges.append(Edge(kind=kind, raw=raw, symbol=None, line=line))

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


def _is_tfvars(file_path: Path) -> bool:
    """True for .tfvars / .auto.tfvars files.

    We key off the extension — ``suffix`` is always ``.tfvars`` for both
    forms (``foo.auto.tfvars`` has suffix ``.tfvars`` under pathlib).
    """
    return file_path.suffix == ".tfvars"


def _top_level_assignments(block_body: str) -> list[tuple[str, int]]:
    """Return ``(name, offset_in_block)`` for every depth-0 ``NAME = ...``
    assignment inside a ``locals { ... }`` block body.

    Skips nested-block assignments (``foo = { bar = ... }`` — the ``bar``
    key is not a top-level local). Uses a small brace-depth walker so
    we stay on depth 0 only. Strings and comments are assumed to already
    be masked in the caller's text.
    """
    out: list[tuple[str, int]] = []
    depth = 0
    i = 0
    n = len(block_body)
    while i < n:
        c = block_body[i]
        if c == '"':
            # Walk through string; caller may have masked this but a
            # heredoc body wouldn't be, so be defensive.
            j = i + 1
            while j < n:
                if block_body[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if block_body[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0 and (c.isalpha() or c == "_"):
            # Maybe the start of an identifier at depth 0. Match against
            # the top-level assignment regex anchored here.
            m = re.match(rf"({_IDENT})[\t ]*=", block_body[i:])
            if m is not None:
                out.append((m.group(1), i))
                # Jump past the identifier+``=`` so we don't re-match.
                i += m.end()
                continue
        i += 1
    return out


def _mask_strings(text: str) -> str:
    """Return ``text`` with quoted-string contents replaced by same-length
    whitespace. Preserves byte offsets and line counts.

    Used before reference-regex scanning so that literals like
    ``"var.foo"`` inside an error message don't emit phantom edges.
    Heredoc bodies are NOT masked here — they're often where real
    ``${var.x}`` interpolations live, and handling them properly would
    need a heredoc-aware scanner (future work).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            out.append('"')
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    # Preserve backslash + escapee as spaces so the
                    # escape sequence length is unchanged.
                    out.append("  ")
                    j += 2
                    continue
                if ch == '"':
                    out.append('"')
                    j += 1
                    break
                out.append("\n" if ch == "\n" else " ")
                j += 1
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)
