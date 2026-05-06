"""Chunker ABC + the ``Chunk`` dataclass.

A chunker takes raw file bytes and yields a list of ``Chunk`` objects.
Chunks are emitted in a parent-before-children order so ``indexer`` can
resolve ``parent_idx`` references to real DB ids as it walks the list.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Chunk:
    """One logical unit of code/config, ready for storage + embedding.

    ``text`` is the raw slice — sanitization + whitespace compression are
    applied by the indexer before insert/embed, not here. Chunkers focus
    on *what* to split and *where* the bytes are; everything else is a
    shared pipeline step.
    """

    kind: str                              # function, class, module_level, resource, …
    name: str | None
    qualified_name: str | None
    start_line: int                        # 1-based for display
    end_line: int
    start_byte: int                        # file offsets (bytes, 0-based)
    end_byte: int
    text: str                              # raw slice (will be sanitized+compressed later)
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_idx: int | None = None          # index into the chunker's output list
    sibling_order: int | None = None


class BaseChunker(ABC):
    """All chunkers implement ``chunk(source_bytes, file_path) -> list[Chunk]``.

    ``file_path`` is the project-relative path to the source file. The
    YAML chunker uses it to distinguish Ansible tasks vs Helm templates
    vs plain YAML (path-based convention is how infra repos organize
    these). Chunkers that don't care may ignore it.
    """

    @abstractmethod
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        ...
