"""Dockerfile chunker — regex-based stage split.

One ``dockerfile_stage`` chunk per ``FROM <image> [AS <stage_name>]``.
Stages without an explicit ``AS`` name get ``stage_N`` (zero-indexed).

No tree-sitter dependency — Dockerfile top-level syntax is small enough
that regex is sufficient, and ``tree-sitter-dockerfile`` availability in
the bundle is spotty across versions.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseChunker, Chunk


_FROM_RE = re.compile(
    r"(?mi)^FROM\s+\S+(?:\s+AS\s+(\S+))?",
)


class DockerfileChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        matches = list(_FROM_RE.finditer(text))
        if not matches:
            return [_whole_file_chunk(text, file_path)]

        out: list[Chunk] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            stage_name = m.group(1) or f"stage_{i}"
            out.append(
                Chunk(
                    kind="dockerfile_stage",
                    name=stage_name,
                    qualified_name=stage_name,
                    start_line=text.count("\n", 0, start) + 1,
                    end_line=text.count("\n", 0, end) + 1,
                    start_byte=len(text[:start].encode("utf-8")),
                    end_byte=len(text[:end].encode("utf-8")),
                    text=text[start:end],
                    sibling_order=i,
                )
            )
        return out


def _whole_file_chunk(text: str, file_path: Path | None) -> Chunk:
    name = file_path.name if file_path is not None else None
    return Chunk(
        kind="dockerfile_stage",
        name=name,
        qualified_name=name,
        start_line=1,
        end_line=max(1, text.count("\n") + 1),
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
    )
