"""Python chunker — tree-sitter-based.

M2 emits flat top-level chunks:

* ``function`` — each top-level ``def`` (decorators included).
* ``class``    — each top-level ``class`` (FULL body text, methods inside).
* ``module_level`` — all top-of-file statements that aren't defs (imports,
  constants, top-level code), as one chunk.

Hierarchical method extraction (class → method children, with the class
chunk becoming a signature-only ``big_parent``) lands in M5 alongside the
general big-chunk split. Keeping M2 flat proves the pipeline end-to-end
without bolting hierarchy logic onto the ABC before it has callers.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk


class PythonChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("python")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        chunks: list[Chunk] = []
        module_stmt_nodes: list = []

        for node in root.children:
            inner = self._def_inner(node)
            if inner is None:
                module_stmt_nodes.append(node)
                continue

            kind = "function" if inner.type == "function_definition" else "class"
            chunks.append(self._extract(node, inner, source_bytes, kind))

        if module_stmt_nodes:
            # Emit module_level covering the span from the first to the last
            # non-def top-level node. Gaps (interspersed defs) are fine — the
            # span includes them in start/end_byte, but the intervening def
            # chunks sit alongside this one; readers never see raw text here
            # re-duplicated because ``stored_text`` is from this slice only.
            chunks.insert(0, self._module_level_chunk(module_stmt_nodes, source_bytes))

        return chunks

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _def_inner(node):
        """Return the underlying function/class def node, or None.

        Handles both bare ``def``/``class`` and decorated variants
        (``decorated_definition`` wrapping the real def).
        """
        if node.type in ("function_definition", "class_definition"):
            return node
        if node.type == "decorated_definition":
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    return child
        return None

    @staticmethod
    def _extract(outer, inner, source_bytes: bytes, kind: str) -> Chunk:
        name_node = inner.child_by_field_name("name")
        name = None
        if name_node is not None:
            name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )

        text_bytes = source_bytes[outer.start_byte : outer.end_byte]
        text = text_bytes.decode("utf-8", errors="replace")

        return Chunk(
            kind=kind,
            name=name,
            qualified_name=name,  # M5 qualifies with module/class path
            start_line=outer.start_point[0] + 1,
            end_line=outer.end_point[0] + 1,
            start_byte=outer.start_byte,
            end_byte=outer.end_byte,
            text=text,
        )

    @staticmethod
    def _module_level_chunk(nodes, source_bytes: bytes) -> Chunk:
        first = nodes[0]
        last = nodes[-1]
        text_bytes = source_bytes[first.start_byte : last.end_byte]
        return Chunk(
            kind="module_level",
            name=None,
            qualified_name=None,
            start_line=first.start_point[0] + 1,
            end_line=last.end_point[0] + 1,
            start_byte=first.start_byte,
            end_byte=last.end_byte,
            text=text_bytes.decode("utf-8", errors="replace"),
        )
