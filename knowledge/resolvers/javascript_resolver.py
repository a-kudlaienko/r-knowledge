"""JavaScript / TypeScript import resolver — tree-sitter.

Walks the tree for four syntactic shapes:

* ``import x from './foo'`` / ``import {a, b} from 'pkg'`` /
  ``import * as ns from './foo'`` / bare ``import './foo'`` →
  one ``Edge(kind='import', raw=<source-string>, symbol=None)``.
  (Symbol-level tracking for named imports is Phase 2 — file-to-file
  edges are the product for Phase 1.)
* ``const x = require('./foo')`` / ``require('fs')`` →
  ``Edge(kind='require', raw=<source-string>, symbol=None)``.
* ``import('./foo')`` with a **string-literal** argument →
  ``Edge(kind='dynamic_import', raw='./foo', symbol=None)``.
* ``import(expr)`` with template literals or variables →
  ``Edge(kind='unresolved', raw=<literal-expression-text>, symbol=None)``.

The TypeScript variant subclasses this and just swaps the parser — TS's
grammar produces the same node types for imports that we walk here.

Like the Python resolver, this is pure: no DB, no file-id resolution.
``relations.py`` resolves ``raw`` (relative path, bare specifier) to a
concrete ``target_file_id`` using the project's files table.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseResolver, Edge


class JavaScriptResolver(BaseResolver):
    PARSER_NAME = "javascript"

    def __init__(self) -> None:
        self._parser = get_parser(self.PARSER_NAME)

    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        tree = self._parser.parse(source_bytes)
        edges: list[Edge] = []
        self._walk(tree.root_node, source_bytes, edges)
        return edges

    # ---- walker -----------------------------------------------------------

    def _walk(self, node, src: bytes, out: list[Edge]) -> None:
        t = node.type
        if t == "import_statement":
            self._handle_import_statement(node, src, out)
            return
        if t == "call_expression":
            # Dynamic import() is parsed as a call_expression whose function
            # is the keyword `import`. require() is a plain identifier call.
            if self._handle_call(node, src, out):
                return
        # Recurse. Imports can appear inside blocks, conditional requires
        # are common (``if (cond) require(...)``), etc.
        for child in node.children:
            self._walk(child, src, out)

    # ---- handlers ---------------------------------------------------------

    def _handle_import_statement(self, node, src: bytes, out: list[Edge]) -> None:
        """``import ... from 'mod'`` / bare ``import 'mod'`` / ``import('mod')`` form.

        The source specifier is always a ``string`` child of the import
        statement. We extract its literal text (without the quotes) and
        emit one edge per statement.
        """
        spec = self._find_source_string(node)
        if spec is None:
            return
        raw = self._string_literal_text(spec, src)
        if raw is None:
            return
        line = node.start_point[0] + 1
        out.append(Edge(kind="import", raw=raw, symbol=None, line=line))

    def _handle_call(self, node, src: bytes, out: list[Edge]) -> bool:
        """Detect ``require('x')`` and ``import('x')``.

        Returns True if this call was recognized and recorded (or recognized
        as an unresolved dynamic import), so the caller can stop recursing
        into its subtree.
        """
        func = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if func is None or args is None:
            return False

        func_text = src[func.start_byte : func.end_byte].decode(
            "utf-8", errors="replace"
        )
        is_require = func_text == "require"
        is_dynamic_import = func_text == "import" or func.type == "import"

        if not (is_require or is_dynamic_import):
            return False

        line = node.start_point[0] + 1

        # First argument: require/import take exactly one string or expr.
        first_arg = None
        for child in args.named_children:
            first_arg = child
            break
        if first_arg is None:
            return True  # recognized but empty

        if first_arg.type == "string":
            raw = self._string_literal_text(first_arg, src)
            if raw is not None:
                kind = "require" if is_require else "dynamic_import"
                out.append(Edge(kind=kind, raw=raw, symbol=None, line=line))
                return True

        # Template literal, identifier, expression → unresolved. Preserve
        # the raw text so the LLM sees that a dynamic target exists.
        raw_expr = src[first_arg.start_byte : first_arg.end_byte].decode(
            "utf-8", errors="replace"
        )
        out.append(
            Edge(kind="unresolved", raw=raw_expr, symbol=None, line=line)
        )
        return True

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _find_source_string(node):
        """Return the ``string`` child that carries the import source.

        tree-sitter-javascript puts the specifier on a ``string`` node
        somewhere among ``import_statement``'s children — usually last,
        but safer to scan.
        """
        for child in node.children:
            if child.type == "string":
                return child
        return None

    @staticmethod
    def _string_literal_text(string_node, src: bytes) -> str | None:
        """Return the unquoted string literal, or None for template strings.

        tree-sitter-javascript's ``string`` node has a ``string_fragment``
        child for the textual body. Template strings are a different
        node type (``template_string``) and we never land here for those.
        """
        parts: list[str] = []
        for child in string_node.children:
            if child.type == "string_fragment":
                parts.append(
                    src[child.start_byte : child.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                )
        if parts:
            return "".join(parts)
        # Empty string literal like ``import ''`` — valid but rare. Strip
        # quotes from the raw span.
        raw = src[string_node.start_byte : string_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            return raw[1:-1]
        return None


class TypeScriptResolver(JavaScriptResolver):
    """TypeScript uses the same node types for imports — just swap the parser."""

    PARSER_NAME = "typescript"
