"""Python import resolver — tree-sitter-based.

Walks the tree for three syntactic shapes:

* ``import a`` / ``import a.b`` / ``import a as b`` →
  one ``Edge(kind='import', raw='a.b', symbol=None)`` per dotted name.
* ``from a.b import x, y as z`` →
  one ``Edge(kind='from_import', raw='a.b', symbol='x')`` per imported name.
  Star imports (``from a import *``) emit a single edge with ``symbol='*'``.
* ``importlib.import_module('a.b')`` with a **string-literal** argument →
  ``Edge(kind='dynamic_import', raw='a.b', symbol=None)``. Variable args
  are emitted as ``kind='unresolved'`` with the raw expression text
  preserved.

Relative imports (``from . import x``, ``from ..y import z``) are encoded
with leading dots preserved in ``raw`` (``.``, ``..y``). Resolution in
``relations.py`` interprets the dots against the source file's package
path.

This file does NOT try to resolve ``raw`` to a concrete target file — that
happens downstream where we know the project's file table. Keeping the
resolver pure (bytes → edges) keeps it unit-testable.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseResolver, Edge


class PythonResolver(BaseResolver):
    def __init__(self) -> None:
        self._parser = get_parser("python")

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
            self._handle_import(node, src, out)
            return
        if t == "import_from_statement":
            self._handle_from_import(node, src, out)
            return
        if t == "call":
            if self._handle_importlib_call(node, src, out):
                return  # don't descend — we've captured this call
        # Not an import site — recurse into children. Most branches of a
        # Python file contain no imports below top-level, but tree-sitter's
        # node types cover lots of containers (try, if, class, function —
        # people do `import foo` inside try/except and inside functions).
        for child in node.children:
            self._walk(child, src, out)

    # ---- handlers ---------------------------------------------------------

    def _handle_import(self, node, src: bytes, out: list[Edge]) -> None:
        """``import a``, ``import a.b``, ``import a as b, c as d``.

        tree-sitter-python represents each target as a ``dotted_name`` or
        an ``aliased_import`` wrapping one. ``name`` field on the outer
        node is the single-target path; for multi-target statements we
        have to iterate named children.
        """
        line = node.start_point[0] + 1
        # Multi-target import like `import a, b.c as d` — iterate aliased
        # and bare dotted_name children. Skip punctuation and the `import`
        # keyword token itself.
        found = False
        for child in node.named_children:
            if child.type == "dotted_name":
                raw = src[child.start_byte : child.end_byte].decode(
                    "utf-8", errors="replace"
                )
                out.append(Edge(kind="import", raw=raw, symbol=None, line=line))
                found = True
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if name is not None:
                    raw = src[name.start_byte : name.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    out.append(
                        Edge(kind="import", raw=raw, symbol=None, line=line)
                    )
                    found = True
        # Fallback for grammar variants: use ``name`` field (single target).
        if not found:
            name = node.child_by_field_name("name")
            if name is not None:
                raw = src[name.start_byte : name.end_byte].decode(
                    "utf-8", errors="replace"
                )
                out.append(Edge(kind="import", raw=raw, symbol=None, line=line))

    def _handle_from_import(self, node, src: bytes, out: list[Edge]) -> None:
        """``from a.b import x, y as z``, ``from . import x``, ``from .a import *``.

        Relative imports use ``relative_import`` in place of ``dotted_name``;
        we preserve the leading dots in ``raw`` so resolution can interpret
        them against the source file's package path.
        """
        line = node.start_point[0] + 1
        module_node = node.child_by_field_name("module_name")
        raw_module = self._module_name_text(module_node, src) if module_node else ""
        # ``child_by_field_name`` wraps the underlying C node in a fresh
        # Python object each call, so ``child is module_node`` fails even
        # for the same node. Compare byte offsets instead.
        module_start = module_node.start_byte if module_node is not None else -1

        # Collect imported names. tree-sitter-python uses ``name`` field for
        # each imported symbol; aliased ones are ``aliased_import`` under
        # the statement. Star-imports show up as a ``wildcard_import`` node.
        imported_symbols: list[str] = []
        saw_wildcard = False
        for child in node.named_children:
            if child.start_byte == module_start:
                continue
            if child.type == "wildcard_import":
                saw_wildcard = True
            elif child.type == "dotted_name":
                sym = src[child.start_byte : child.end_byte].decode(
                    "utf-8", errors="replace"
                )
                imported_symbols.append(sym)
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if name is not None:
                    sym = src[name.start_byte : name.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    imported_symbols.append(sym)

        if saw_wildcard:
            out.append(
                Edge(
                    kind="from_import",
                    raw=raw_module,
                    symbol="*",
                    line=line,
                )
            )
            return

        if not imported_symbols:
            # Malformed or unexpected shape — still emit one edge so the
            # module reference isn't lost.
            out.append(
                Edge(
                    kind="from_import",
                    raw=raw_module,
                    symbol=None,
                    line=line,
                )
            )
            return

        for sym in imported_symbols:
            out.append(
                Edge(
                    kind="from_import",
                    raw=raw_module,
                    symbol=sym,
                    line=line,
                )
            )

    def _handle_importlib_call(self, node, src: bytes, out: list[Edge]) -> bool:
        """Detect ``importlib.import_module(...)`` and emit a dynamic edge.

        Returns True when the call was recognized (caller should stop
        descending into this subtree to avoid double-counting).
        """
        func = node.child_by_field_name("function")
        if func is None:
            return False
        if not self._is_import_module_callee(func, src):
            return False

        args = node.child_by_field_name("arguments")
        if args is None:
            return False

        # First positional string literal is the module name. Anything else
        # (variable, f-string, expression) → unresolved.
        first_arg = None
        for child in args.named_children:
            first_arg = child
            break
        if first_arg is None:
            return True  # call recognized but empty — nothing to record

        line = node.start_point[0] + 1
        if first_arg.type == "string":
            module_name = self._string_literal_text(first_arg, src)
            if module_name is not None:
                out.append(
                    Edge(
                        kind="dynamic_import",
                        raw=module_name,
                        symbol=None,
                        line=line,
                    )
                )
                return True

        # Non-literal argument — preserve the raw expression so the LLM
        # at least knows a dynamic import exists here.
        raw_expr = src[first_arg.start_byte : first_arg.end_byte].decode(
            "utf-8", errors="replace"
        )
        out.append(
            Edge(
                kind="unresolved",
                raw=raw_expr,
                symbol=None,
                line=line,
            )
        )
        return True

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _module_name_text(node, src: bytes) -> str:
        """Extract ``raw`` from a ``dotted_name`` or ``relative_import`` node.

        ``relative_import`` keeps its leading dots (``.``, ``..``) so
        resolution can count them. A ``relative_import`` may also wrap a
        trailing ``dotted_name`` (``from ..pkg import x``) — we preserve
        the full textual span including dots.
        """
        if node is None:
            return ""
        return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _is_import_module_callee(func_node, src: bytes) -> bool:
        """True if ``func_node`` is ``importlib.import_module`` or an
        identifier ``import_module`` (for ``from importlib import import_module``).
        """
        if func_node.type == "identifier":
            text = src[func_node.start_byte : func_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            return text == "import_module"
        if func_node.type == "attribute":
            # object.attribute form — check attribute name == import_module
            attr = func_node.child_by_field_name("attribute")
            if attr is None:
                return False
            attr_text = src[attr.start_byte : attr.end_byte].decode(
                "utf-8", errors="replace"
            )
            return attr_text == "import_module"
        return False

    @staticmethod
    def _string_literal_text(string_node, src: bytes) -> str | None:
        """Return the text content of a Python ``string`` node, stripped
        of its quotes. Returns None for f-strings / concatenated / byte
        strings where the literal content isn't a plain module name.
        """
        # tree-sitter-python's ``string`` node wraps string_start,
        # string_content*, string_end. Reject f-strings (interpolation
        # child) and byte strings (b-prefix).
        raw = src[string_node.start_byte : string_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        # Reject prefixed strings other than r/u (byte strings, f-strings).
        stripped = raw.lstrip()
        if stripped[:2].lower() in ("f'", 'f"', "b'", 'b"', "rb", "br"):
            return None
        if stripped[:1].lower() == "f":
            return None
        # Collect string_content children — safest way to get the literal
        # value without re-implementing quote handling.
        parts: list[str] = []
        for child in string_node.children:
            if child.type == "string_content":
                parts.append(
                    src[child.start_byte : child.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                )
            elif child.type == "interpolation":
                return None  # f-string
        if not parts:
            # Grammar variant with no string_content child — strip quotes.
            return _strip_string_quotes(raw)
        return "".join(parts)


def _strip_string_quotes(raw: str) -> str | None:
    """Last-resort quote stripping for grammar variants without
    ``string_content`` children. Returns None if we can't identify quotes.
    """
    s = raw.lstrip()
    if s.startswith(("r'", 'r"', "u'", 'u"', "R'", 'R"', "U'", 'U"')):
        s = s[1:]
    if len(s) < 2:
        return None
    q = s[0]
    if q not in ("'", '"'):
        return None
    # triple quote?
    if s[:3] in ("'''", '"""'):
        return s[3:-3] if s.endswith(s[:3]) else None
    return s[1:-1] if s.endswith(q) else None
