"""Shell / bash chunker — tree-sitter-bash.

Top-level ``function_definition`` nodes become ``shell_function`` chunks.
Everything else at the top level (commands, variable assignments, ``set``
pragmas, top-level ``if``/``case`` blocks) aggregates into one
``module_level`` chunk so imports-style preamble is still retrievable.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk


class ShellChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("bash")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        chunks: list[Chunk] = []
        module_stmt_nodes: list = []

        for node in root.children:
            if node.type == "function_definition":
                chunks.append(self._fn_chunk(node, source_bytes))
            elif node.type == "comment":
                # Comments alone aren't worth their own chunk; they'll be
                # absorbed by module_level if adjacent to top-level code.
                module_stmt_nodes.append(node)
            else:
                module_stmt_nodes.append(node)

        if module_stmt_nodes:
            chunks.insert(0, self._module_level_chunk(module_stmt_nodes, source_bytes))
        return chunks

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _fn_chunk(node, source_bytes: bytes) -> Chunk:
        name_node = node.child_by_field_name("name")
        name = None
        if name_node is not None:
            name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )
        text_bytes = source_bytes[node.start_byte : node.end_byte]
        return Chunk(
            kind="shell_function",
            name=name,
            qualified_name=name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            text=text_bytes.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _module_level_chunk(nodes, source_bytes: bytes) -> Chunk:
        first, last = nodes[0], nodes[-1]
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
