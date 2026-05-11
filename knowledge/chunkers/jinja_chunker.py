"""Jinja2 chunker — regex-based top-level block/macro/call/filter detection.

No tree-sitter: no robust Jinja grammar exists in the ``tree-sitter-languages``
bundle, and ``jinja2.lexer`` is overkill for our needs. Regex handles the
common case (top-level ``{% block name %}...{% endblock %}`` and friends).

Limitations accepted:

* Nested blocks of the SAME kind trip the naive matcher (the first
  ``{% endblock %}`` closes the outermost ``{% block %}``). In infra
  templates nested same-kind blocks are rare; when they occur, the first
  chunk is still useful and the inner span is covered indirectly.
* ``{% raw %}`` passthrough and complex whitespace modifiers aren't
  specially handled — a regex match inside a raw block would split
  incorrectly, but again this is rare in infra templates.

Fallback: if no ``{% block|macro|call|filter %}`` is found, the whole file
becomes one ``jinja_block`` chunk.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseChunker, Chunk


_OPEN_RE = re.compile(r"\{%-?\s*(block|macro|call|filter)\s+(\w+)")


class JinjaChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        opens = list(_OPEN_RE.finditer(text))
        if not opens:
            return [_whole_file_chunk(text, file_path)]

        chunks: list[Chunk] = []
        for i, m in enumerate(opens):
            kind_word = m.group(1)
            name = m.group(2)
            start = m.start()

            end_re = re.compile(rf"\{{%-?\s*end{kind_word}\s*-?%\}}")
            end_match = end_re.search(text, m.end())
            if end_match is None:
                continue  # unclosed — skip this one
            end = end_match.end()

            chunks.append(
                Chunk(
                    kind=f"jinja_{kind_word}",
                    name=name,
                    qualified_name=name,
                    start_line=text.count("\n", 0, start) + 1,
                    end_line=text.count("\n", 0, end) + 1,
                    start_byte=len(text[:start].encode("utf-8")),
                    end_byte=len(text[:end].encode("utf-8")),
                    text=text[start:end],
                    sibling_order=i,
                )
            )

        if not chunks:
            return [_whole_file_chunk(text, file_path)]
        return chunks


def _whole_file_chunk(text: str, file_path: Path | None) -> Chunk:
    name = file_path.name if file_path is not None else None
    return Chunk(
        kind="jinja_block",
        name=name,
        qualified_name=name,
        start_line=1,
        end_line=max(1, text.count("\n") + 1),
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
    )
