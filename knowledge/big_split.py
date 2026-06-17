"""Oversized-chunk split into ``big_parent`` + ordered ``big_subchunk`` rows.

When a chunk's text exceeds ``config.MAX_CHARS``, we replace it with:

1. A ``big_parent`` row that keeps the **full byte range** of the original
   (so ``knowledge get --with-siblings --raw`` can re-slice the exact
   original from disk) but whose ``stored_text`` / ``embedded_text`` is a
   generated **summary** — first line (signature/heading) plus a one-line
   header per subchunk. One holistic embedding that answers "is this
   function/doc/block roughly about X?".

2. N ``big_subchunk`` rows, each covering a slice of the body. Each has
   its own embedding of its actual content — so "where EXACTLY is the
   embed loop" finds a subchunk, while "is this thing about searching"
   finds the parent.

Parent/child link: ``parent_idx`` points to the parent's position *within
the list returned by this module*. The indexer translates that local
offset to the corresponding global offset when it flattens chunker output.

Split strategy for M5 is **line-boundary**: accumulate lines into the
current window until adding the next line would exceed ``MAX_CHARS``,
then emit and start a new window. Language-agnostic, correct, and good
enough — AST-aware subchunking (Python statements, YAML mapping keys,
markdown secondary headings) is a future polish, not in scope here.
"""

from __future__ import annotations

from dataclasses import replace

from . import config
from .chunkers.base import Chunk
from .sanitizer import scrub_text


def split_if_oversized(chunk: Chunk, max_chars: int | None = None) -> list[Chunk]:
    """Return ``[chunk]`` unchanged if under the limit, else
    ``[big_parent, big_subchunk_0, big_subchunk_1, ...]``.

    Subchunks carry ``parent_idx = 0`` (pointing to the parent inside
    this returned list). The indexer translates that to a real
    ``parent_id`` when it inserts rows in order.
    """
    limit = max_chars if max_chars is not None else config.MAX_CHARS
    if len(chunk.text) <= limit:
        return [chunk]

    # Security (H5): scrub BEFORE splitting. A multi-line secret (e.g. a PEM
    # private key) can be longer than one window; if we split first and let the
    # indexer scrub each subchunk independently, the BEGIN line lands in one
    # subchunk and the END line in the next, so neither matches the
    # ``-----BEGIN ... -----END-----`` regex and the key material leaks into
    # the stored text + embeddings. Scrubbing the full text up front guarantees
    # no window can carry a partial secret. We split a clone carrying the
    # scrubbed text but the SAME byte offsets, so the parent keeps the original
    # full byte span (``--raw`` reassembly via the parent is unaffected; only
    # per-subchunk byte offsets shift, and only for files that actually held a
    # secret — those bytes are re-read from disk by ``--raw`` anyway).
    scrubbed = scrub_text(chunk.text)
    split_source = chunk if scrubbed == chunk.text else replace(chunk, text=scrubbed)

    sub_chunks = _line_window_split(split_source, limit)
    if len(sub_chunks) <= 1:
        # Single logical line is already longer than the limit (generated
        # dict, minified code, etc.). No meaningful split possible — keep
        # the original intact; the indexer scrubs it with full context.
        return [chunk]

    parent = _make_big_parent(split_source, sub_chunks)
    for i, sc in enumerate(sub_chunks):
        sc.parent_idx = 0       # position of parent in the returned list
        sc.sibling_order = i
    return [parent, *sub_chunks]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line_window_split(chunk: Chunk, max_chars: int) -> list[Chunk]:
    """Group the chunk's lines into windows ≤ max_chars."""
    lines = chunk.text.splitlines(keepends=True)
    out: list[Chunk] = []

    buf: list[str] = []
    buf_len = 0
    window_start_line = chunk.start_line
    window_start_byte = chunk.start_byte

    for line in lines:
        line_len = len(line)
        # Flush the current buffer before adding a line that would overflow,
        # unless the buffer is empty (one huge line — include it anyway so
        # we don't lose content; ``split_if_oversized`` treats a single-
        # subchunk result as "no useful split" and falls back to original).
        if buf and buf_len + line_len > max_chars:
            out.append(_mk_sub(buf, window_start_line, window_start_byte))
            window_start_line += sum(s.count("\n") for s in buf)
            window_start_byte += sum(len(s.encode("utf-8")) for s in buf)
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += line_len

    if buf:
        out.append(_mk_sub(buf, window_start_line, window_start_byte))

    return out


def _mk_sub(buf: list[str], start_line: int, start_byte: int) -> Chunk:
    text = "".join(buf)
    return Chunk(
        kind="big_subchunk",
        name=None,
        qualified_name=None,
        start_line=start_line,
        end_line=start_line + text.count("\n"),
        start_byte=start_byte,
        end_byte=start_byte + len(text.encode("utf-8")),
        text=text,
    )


def _make_big_parent(original: Chunk, subs: list[Chunk]) -> Chunk:
    """Summary = original's first line + one-line header per subchunk.

    Byte offsets on the parent stay pointed at the FULL original span so
    ``knowledge get --with-siblings --raw`` reassembles from disk exactly.
    """
    first_line = original.text.split("\n", 1)[0].strip()
    summary_lines = [first_line] if first_line else []
    for i, sc in enumerate(subs):
        sc_first = sc.text.split("\n", 1)[0].strip()
        if len(sc_first) > 100:
            sc_first = sc_first[:100] + "…"
        summary_lines.append(f"  [part {i}] {sc_first}")

    summary_text = "\n".join(summary_lines)

    metadata = dict(original.metadata) if original.metadata else {}
    metadata["original_kind"] = original.kind
    metadata["subchunk_count"] = len(subs)

    return Chunk(
        kind="big_parent",
        name=original.name,
        qualified_name=original.qualified_name,
        start_line=original.start_line,
        end_line=original.end_line,
        start_byte=original.start_byte,
        end_byte=original.end_byte,
        text=summary_text,
        metadata=metadata,
        parent_idx=original.parent_idx,           # preserve if already nested
        sibling_order=original.sibling_order,
    )
