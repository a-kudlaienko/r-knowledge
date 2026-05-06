"""Markdown chunker — split on H1 / H2 headings.

Each section (heading + everything up to the next H1/H2) becomes one
``markdown_section`` chunk named by the heading text. A preamble chunk
covers anything above the first heading.

Deeper headings (H3+) stay inside their parent section. For long docs
M5's big-chunk split will break oversized sections into sub-chunks along
secondary heading boundaries; that's outside M4's scope.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseChunker, Chunk


_HEADING_RE = re.compile(r"(?m)^(#{1,2})\s+(.*?)\s*$")


class MarkdownChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        matches = list(_HEADING_RE.finditer(text))

        if not matches:
            return [_whole_file_chunk(text, file_path)]

        chunks: list[Chunk] = []
        order = 0

        # Preamble — anything before the first heading
        if matches[0].start() > 0:
            chunks.append(_section_chunk(text, 0, matches[0].start(), "preamble", order))
            order += 1

        for i, m in enumerate(matches):
            heading = m.group(2).strip() or f"section_{i}"
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunks.append(_section_chunk(text, start, end, heading, order))
            order += 1

        return chunks


def _section_chunk(text: str, start: int, end: int, name: str, order: int) -> Chunk:
    return Chunk(
        kind="markdown_section",
        name=name,
        qualified_name=name,
        start_line=text.count("\n", 0, start) + 1,
        end_line=text.count("\n", 0, end) + 1,
        start_byte=len(text[:start].encode("utf-8")),
        end_byte=len(text[:end].encode("utf-8")),
        text=text[start:end],
        sibling_order=order,
    )


def _whole_file_chunk(text: str, file_path: Path | None) -> Chunk:
    name = file_path.name if file_path is not None else None
    return Chunk(
        kind="markdown_section",
        name=name or "document",
        qualified_name=name or "document",
        start_line=1,
        end_line=max(1, text.count("\n") + 1),
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
    )
