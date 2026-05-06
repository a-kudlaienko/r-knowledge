"""Reversible whitespace compression for stored/embedded text.

Runs of 3+ whitespace characters are replaced with ``^N^`` markers where
``N`` is the character count. This reduces DB size 10-30% on typical
indented code while preserving enough structure that the embedding model
still sees block boundaries. Runs of 1-2 whitespace chars pass through.

Note: the compression discards *which* whitespace characters were present
(tabs vs spaces vs newlines) — decompression always yields spaces. When
byte-exact output matters, callers retrieve the original from disk using
the chunk's ``(file_path, start_byte, end_byte)`` offsets instead.
"""

from __future__ import annotations

import re

_COMPRESS_RE = re.compile(r"\s{3,}")
_DECOMPRESS_RE = re.compile(r"\^(\d+)\^")


def compress(text: str) -> str:
    return _COMPRESS_RE.sub(lambda m: f"^{len(m.group(0))}^", text)


def decompress(text: str) -> str:
    return _DECOMPRESS_RE.sub(lambda m: " " * int(m.group(1)), text)
