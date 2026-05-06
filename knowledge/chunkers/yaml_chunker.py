"""YAML chunker — PyYAML-based with path-driven dispatch.

Uses PyYAML's ``compose_all`` to get a node tree with ``start_mark`` /
``end_mark`` character offsets. We skip tree-sitter-yaml here because
PyYAML is already a dep, the node traversal is simpler than tree-sitter's
cursor API, and we need the tree anyway for sanitizer layer 2 (sensitive-
key value replacement).

Dispatch by path (the repo-conventions the plan calls out):

* ``**/tasks/*.yml|yaml``    — Ansible task file: one chunk per top-level
  sequence item (a task mapping). Name = the task's ``name:`` scalar.
* ``**/handlers/*.yml|yaml`` — Same shape as tasks, kind ``ansible_handler``.
* ``**/charts/*/templates/*.yaml|yml`` — Helm template. Contains Go-template
  syntax (``{{ .Values.foo }}``) that isn't valid YAML, so we emit the whole
  file as one chunk WITHOUT parsing. No sanitizer L2 pass (no YAML tree).
* ``values.yaml`` / ``values.yml`` — Helm values: one ``helm_values_section``
  chunk per top-level key.
* K8s manifests (has ``kind:`` + ``metadata.name:`` keys) — whole-doc chunk
  named ``Kind/name`` (e.g., ``Deployment/my-app``).
* Anything else — generic YAML: one chunk per top-level mapping key, or
  whole-doc if the root isn't a mapping.

Sanitizer layer 2 runs per chunk: the YAML node tree rooted at the chunk's
span is walked for sensitive keys; scalar values under those keys are
replaced with ``CHANGE_ME`` before the chunk text is returned to the indexer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..sanitizer import scrub_yaml_sensitive_values
from .base import BaseChunker, Chunk


class YamlChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        kind_hint = _classify_path(file_path)

        # Helm templates aren't valid YAML — skip parsing, emit whole file.
        if kind_hint == "helm_template":
            return [_whole_file_chunk("helm_template", text, name=_safe_name(file_path))]

        try:
            docs = list(yaml.compose_all(text))
        except yaml.YAMLError:
            # Best effort: broken YAML still indexable as a single text blob.
            return [_whole_file_chunk("yaml_doc", text, name=_safe_name(file_path))]

        chunks: list[Chunk] = []
        for doc in docs:
            if doc is None:
                continue
            chunks.extend(_chunk_document(doc, text, kind_hint))

        return chunks


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


def _classify_path(file_path: Path | None) -> str:
    """Return the YAML flavor hint for a path. ``generic_or_k8s`` means
    "decide after parsing based on content" (K8s manifests have a distinctive
    kind+metadata shape, everything else is generic)."""
    if file_path is None:
        return "generic_or_k8s"

    parts = file_path.parts

    parent = file_path.parent.name
    if parent == "tasks":
        return "ansible_tasks"
    if parent == "handlers":
        return "ansible_handlers"

    # Helm template: /charts/<chart>/templates/<file>.yaml
    for i in range(len(parts) - 2):
        if parts[i] == "charts" and parts[i + 2] == "templates":
            return "helm_template"

    if file_path.name in ("values.yaml", "values.yml"):
        return "helm_values"

    return "generic_or_k8s"


# ---------------------------------------------------------------------------
# Per-document chunking
# ---------------------------------------------------------------------------


def _chunk_document(node: Any, full_text: str, kind_hint: str) -> list[Chunk]:
    """Emit chunks for one YAML document, applying the kind-specific rules."""
    cls = type(node).__name__

    if kind_hint in ("ansible_tasks", "ansible_handlers"):
        if cls != "SequenceNode":
            # A malformed tasks file — fall back to whole-doc chunk.
            return [_node_chunk("yaml_doc", node, full_text)]
        chunk_kind = "ansible_task" if kind_hint == "ansible_tasks" else "ansible_handler"
        out = []
        for i, item in enumerate(node.value):
            if type(item).__name__ != "MappingNode":
                continue
            name = _extract_mapping_string(item, "name")
            out.append(_node_chunk(chunk_kind, item, full_text, name=name, sibling_order=i))
        return out

    if kind_hint == "helm_values":
        if cls != "MappingNode":
            return [_node_chunk("helm_values_section", node, full_text)]
        return [
            _top_key_chunk("helm_values_section", key_node, val_node, full_text, i)
            for i, (key_node, val_node) in enumerate(node.value)
        ]

    # generic_or_k8s: decide by content
    if cls == "MappingNode" and _looks_like_k8s(node):
        k8s_kind = _extract_mapping_string(node, "kind")
        k8s_name = _extract_k8s_name(node)
        display = (
            f"{k8s_kind}/{k8s_name}" if (k8s_kind and k8s_name) else (k8s_kind or k8s_name)
        )
        return [_node_chunk("yaml_doc", node, full_text, name=display)]

    # Plain YAML mapping — one chunk per top-level key.
    if cls == "MappingNode":
        return [
            _top_key_chunk("yaml_doc", key_node, val_node, full_text, i)
            for i, (key_node, val_node) in enumerate(node.value)
        ]

    # Anything else (sequence, scalar) — one whole-doc chunk.
    return [_node_chunk("yaml_doc", node, full_text)]


# ---------------------------------------------------------------------------
# Chunk construction helpers
# ---------------------------------------------------------------------------


def _node_chunk(
    kind: str,
    node: Any,
    full_text: str,
    *,
    name: str | None = None,
    sibling_order: int | None = None,
) -> Chunk:
    """Make a chunk covering the full span of ``node``, with layer-2 scrub."""
    start_idx = node.start_mark.index
    end_idx = node.end_mark.index
    raw_text = full_text[start_idx:end_idx]
    scrubbed = scrub_yaml_sensitive_values(raw_text, node, start_idx)

    return Chunk(
        kind=kind,
        name=name,
        qualified_name=name,
        start_line=node.start_mark.line + 1,
        end_line=node.end_mark.line + 1,
        start_byte=_char_to_byte(full_text, start_idx),
        end_byte=_char_to_byte(full_text, end_idx),
        text=scrubbed,
        sibling_order=sibling_order,
    )


def _top_key_chunk(
    kind: str,
    key_node: Any,
    val_node: Any,
    full_text: str,
    sibling_order: int,
) -> Chunk:
    """Chunk spanning a key-value pair at the top level of a mapping.

    Starts at the key's start, ends at the value's end — so the chunk
    reads naturally as ``key: value-block``.
    """
    start_idx = key_node.start_mark.index
    end_idx = val_node.end_mark.index
    raw_text = full_text[start_idx:end_idx]
    # Layer-2 scrub: rooted at val_node to catch sensitive keys inside
    # the value, and separately check the top key itself.
    from ..sanitizer import is_sensitive_key, CHANGE_ME

    scrubbed = scrub_yaml_sensitive_values(raw_text, val_node, start_idx)

    # Top-key self-check: if THIS key is sensitive and val is scalar, scrub.
    key_val = getattr(key_node, "value", None)
    if (
        isinstance(key_val, str)
        and is_sensitive_key(key_val)
        and type(val_node).__name__ == "ScalarNode"
    ):
        v_start = val_node.start_mark.index - start_idx
        v_end = val_node.end_mark.index - start_idx
        if 0 <= v_start < v_end <= len(scrubbed):
            scrubbed = scrubbed[:v_start] + CHANGE_ME + scrubbed[v_end:]

    name = str(key_val) if isinstance(key_val, str) else None
    return Chunk(
        kind=kind,
        name=name,
        qualified_name=name,
        start_line=key_node.start_mark.line + 1,
        end_line=val_node.end_mark.line + 1,
        start_byte=_char_to_byte(full_text, start_idx),
        end_byte=_char_to_byte(full_text, end_idx),
        text=scrubbed,
        sibling_order=sibling_order,
    )


def _whole_file_chunk(kind: str, text: str, *, name: str | None) -> Chunk:
    """One chunk covering the entire file. Used for Helm templates and
    broken YAML fallback. No sanitizer L2 (no tree available)."""
    lines = text.count("\n")
    return Chunk(
        kind=kind,
        name=name,
        qualified_name=name,
        start_line=1,
        end_line=max(1, lines + 1),
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        text=text,
    )


# ---------------------------------------------------------------------------
# Node-tree utilities
# ---------------------------------------------------------------------------


def _extract_mapping_string(mapping_node: Any, key: str) -> str | None:
    """Given a MappingNode, return the scalar value of ``key`` or None."""
    for k_node, v_node in mapping_node.value:
        if (
            type(k_node).__name__ == "ScalarNode"
            and str(k_node.value) == key
            and type(v_node).__name__ == "ScalarNode"
        ):
            return str(v_node.value)
    return None


def _extract_k8s_name(mapping_node: Any) -> str | None:
    """Get the ``metadata.name`` scalar from a K8s manifest mapping, or None."""
    for k_node, v_node in mapping_node.value:
        if (
            type(k_node).__name__ == "ScalarNode"
            and str(k_node.value) == "metadata"
            and type(v_node).__name__ == "MappingNode"
        ):
            return _extract_mapping_string(v_node, "name")
    return None


def _looks_like_k8s(mapping_node: Any) -> bool:
    has_kind = False
    has_metadata = False
    for k_node, _ in mapping_node.value:
        if type(k_node).__name__ != "ScalarNode":
            continue
        k = str(k_node.value)
        if k == "kind":
            has_kind = True
        elif k == "metadata":
            has_metadata = True
    return has_kind and has_metadata


def _safe_name(file_path: Path | None) -> str | None:
    return file_path.name if file_path is not None else None


def _char_to_byte(text: str, char_index: int) -> int:
    """Convert a character offset into ``text`` to its byte offset.

    PyYAML's ``start_mark.index`` / ``end_mark.index`` are character
    offsets, but the schema stores byte offsets (consistent across all
    languages, required for raw-byte re-slicing in ``knowledge get --raw``).
    For pure-ASCII YAML (common) this is identity; for UTF-8 with non-ASCII
    content the conversion is exact.
    """
    if char_index <= 0:
        return 0
    return len(text[:char_index].encode("utf-8"))
