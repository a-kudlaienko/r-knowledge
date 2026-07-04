"""JSON chunker — tree-sitter-json, with sanitizer layer 2.

Emission rules:

* Root ``object`` → one ``json_object`` chunk per top-level key (the
  chunk text spans the key-value pair, named by the key).
* Root ``array`` or scalar → single whole-file chunk.

Sanitizer layer 2 walks the pair subtree: when a ``pair`` whose key is
sensitive-named has a string value, the string (including its surrounding
quotes) is replaced with ``"CHANGE_ME"`` — preserving JSON validity. Non-
string sensitive values (numbers, booleans) are left alone; they usually
aren't secrets, and rewriting them to a string would invalidate the JSON.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from ..sanitizer import CHANGE_ME, is_sensitive_key, scrub_text
from .base import BaseChunker, Chunk


class JsonChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("json")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root_value = self._find_root_value(tree.root_node)
        if root_value is None:
            return []

        if root_value.type == "object":
            out: list[Chunk] = []
            order = 0
            for child in root_value.named_children:
                if child.type == "pair":
                    out.append(self._pair_chunk(child, source_bytes, order))
                    order += 1
            return out

        # Root is an array / scalar — emit whole-file chunk with full scrubbing.
        return [self._whole_chunk(root_value, source_bytes)]

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _find_root_value(doc_node):
        """Tree-sitter-json wraps the real root in a ``document`` node."""
        for child in doc_node.named_children:
            if child.type in ("object", "array", "string", "number", "true", "false", "null"):
                return child
        return None

    def _pair_chunk(self, pair_node, source_bytes: bytes, sibling_order: int) -> Chunk:
        key_node = pair_node.child_by_field_name("key")
        key_text = self._key_string(key_node, source_bytes) if key_node else None

        chunk_start = pair_node.start_byte
        chunk_end = pair_node.end_byte
        raw_text = source_bytes[chunk_start:chunk_end].decode("utf-8", errors="replace")

        spans: list[tuple[int, int, str]] = []
        self._collect_sensitive_spans(pair_node, source_bytes, chunk_start, spans)
        sanitized = _apply_replacements(raw_text, spans)

        return Chunk(
            kind="json_object",
            name=key_text,
            qualified_name=key_text,
            start_line=pair_node.start_point[0] + 1,
            end_line=pair_node.end_point[0] + 1,
            start_byte=chunk_start,
            end_byte=chunk_end,
            text=sanitized,
            sibling_order=sibling_order,
        )

    def _whole_chunk(self, root_value, source_bytes: bytes) -> Chunk:
        """Emit a single chunk for array/scalar roots.

        Applies the same two-layer sanitisation as ``_pair_chunk``:
        * Layer 2 (key-based): walks the subtree via ``_collect_sensitive_spans``
          so that array elements like ``[{"password": "S3cr3t"}]`` are scrubbed.
        * Layer 1 (regex): ``scrub_text`` catches inline secrets not covered by
          the key-based walk (e.g. raw tokens in string values).
        """
        chunk_start = root_value.start_byte
        chunk_end = root_value.end_byte
        raw_text = source_bytes[chunk_start:chunk_end].decode("utf-8", errors="replace")

        spans: list[tuple[int, int, str]] = []
        self._collect_sensitive_spans(root_value, source_bytes, chunk_start, spans)
        sanitized = _apply_replacements(raw_text, spans)
        sanitized = scrub_text(sanitized)  # layer-1 pass as final safety net

        return Chunk(
            kind="json_object",
            name=None,
            qualified_name=None,
            start_line=root_value.start_point[0] + 1,
            end_line=root_value.end_point[0] + 1,
            start_byte=chunk_start,
            end_byte=chunk_end,
            text=sanitized,
        )

    def _collect_sensitive_spans(
        self,
        node,
        source_bytes: bytes,
        chunk_start: int,
        spans: list[tuple[int, int, str]],
    ) -> None:
        """Walk the subtree for pairs whose key is sensitive + value is string."""
        if node.type == "pair":
            key = node.child_by_field_name("key")
            val = node.child_by_field_name("value")
            if key is not None and val is not None:
                key_clean = self._key_string(key, source_bytes) or ""
                if is_sensitive_key(key_clean) and val.type == "string":
                    spans.append(
                        (
                            val.start_byte - chunk_start,
                            val.end_byte - chunk_start,
                            f'"{CHANGE_ME}"',
                        )
                    )
                    return  # don't recurse into the value we just replaced
        for child in node.children:
            self._collect_sensitive_spans(child, source_bytes, chunk_start, spans)

    @staticmethod
    def _key_string(key_node, source_bytes: bytes) -> str:
        """JSON keys are always ``"quoted strings"`` — strip the quotes."""
        raw = source_bytes[key_node.start_byte : key_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        return raw.strip('"').strip("'")


def _apply_replacements(text: str, spans: list[tuple[int, int, str]]) -> str:
    """Apply ``(start, end, replacement)`` edits; end-to-start so offsets hold."""
    if not spans:
        return text
    spans_sorted = sorted(spans, key=lambda s: -s[0])
    out = text
    for s, e, repl in spans_sorted:
        if 0 <= s < e <= len(out):
            out = out[:s] + repl + out[e:]
    return out
