"""JavaScript / TypeScript chunker — tree-sitter.

M4 emits flat top-level chunks:

* ``function`` — ``function foo() {}`` and ``const foo = () => {}`` /
  ``const foo = function() {}`` arrow-or-func assignments at the top level.
* ``class``    — ``class Foo {}`` (full body; methods inside stay embedded).
* ``module_level`` — imports + top-level expressions grouped as one chunk.

``export`` wrappers are peeled so ``export function foo()`` or
``export const foo = () => {}`` still get named chunks instead of landing
in ``module_level``. Hierarchical method chunks (class → method children)
land in M5 alongside the general big-chunk split.

The TypeScript variant just swaps the parser — tree-sitter's TS grammar
accepts the JS node types we walk, so the extraction logic is shared.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk


class JavaScriptChunker(BaseChunker):
    PARSER_NAME = "javascript"

    def __init__(self) -> None:
        self._parser = get_parser(self.PARSER_NAME)

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        chunks: list[Chunk] = []
        module_stmt_nodes: list = []

        for node in root.children:
            inner = self._peel_export(node)
            extracted = self._classify_top_level(inner, source_bytes)
            if extracted is None:
                module_stmt_nodes.append(node)
                continue
            kind, name = extracted
            # Use the OUTER node for byte range so an ``export`` keyword is
            # included when present — gives search results cleaner context.
            chunks.append(self._make_chunk(node, source_bytes, kind, name))

        if module_stmt_nodes:
            chunks.insert(0, self._module_level_chunk(module_stmt_nodes, source_bytes))
        return chunks

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _peel_export(node):
        """``export_statement`` → underlying declaration, else return as-is."""
        if node.type == "export_statement":
            for child in node.named_children:
                if child.type in (
                    "function_declaration",
                    "class_declaration",
                    "lexical_declaration",
                    "variable_declaration",
                ):
                    return child
        return node

    def _classify_top_level(self, node, source_bytes: bytes):
        """Return ``(kind, name)`` for a chunkable top-level node, else None."""
        if node.type == "function_declaration":
            return ("function", self._child_text(node, "name", source_bytes))
        if node.type == "class_declaration":
            return ("class", self._child_text(node, "name", source_bytes))
        if node.type in ("lexical_declaration", "variable_declaration"):
            # const/let/var FOO = arrow_or_function
            name = self._arrow_assignment_name(node, source_bytes)
            if name is not None:
                return ("function", name)
        return None

    @staticmethod
    def _arrow_assignment_name(decl_node, source_bytes: bytes) -> str | None:
        for child in decl_node.named_children:
            if child.type != "variable_declarator":
                continue
            value = child.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression", "function"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    return source_bytes[name_node.start_byte : name_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
        return None

    @staticmethod
    def _child_text(node, field: str, source_bytes: bytes) -> str | None:
        n = node.child_by_field_name(field)
        if n is None:
            return None
        return source_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _make_chunk(node, source_bytes: bytes, kind: str, name: str | None) -> Chunk:
        text_bytes = source_bytes[node.start_byte : node.end_byte]
        return Chunk(
            kind=kind,
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


class TypeScriptChunker(JavaScriptChunker):
    PARSER_NAME = "typescript"
