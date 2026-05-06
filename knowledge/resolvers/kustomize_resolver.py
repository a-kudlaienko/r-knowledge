"""Kustomize resolver — ``kustomization.yaml`` file-reference extraction.

Only fires for files named ``kustomization.yaml`` / ``kustomization.yml``
(see ``yaml_classifier.classify_yaml_path``). Plain K8s manifests
without a kustomization parent get no resolver — the CLI falls back to
a sibling-files listing for those.

Edges emitted:

* ``kustomize_resource``    — each entry in ``resources:``. Paths are
                              relative to the kustomization file's dir;
                              ``bases:`` (legacy) is treated the same.
* ``kustomize_component``   — ``components:`` entries (newer kustomize).
* ``kustomize_patch``       — ``patchesStrategicMerge:`` items and
                              ``patches: [{path: ...}]`` items.
* ``kustomize_generator``   — ``configMapGenerator: [{files: [...]}]``
                              and ``secretGenerator: [{files: [...]}]``.

Remote bases (``github.com/owner/repo/…``, git URLs) get emitted with
their raw string — resolution fails and they land external. That's the
right signal (the target isn't a file in this project).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .base import BaseResolver, Edge


class KustomizeResolver(BaseResolver):
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        try:
            doc = yaml.safe_load(source_bytes)
        except yaml.YAMLError:
            return []
        if not isinstance(doc, dict):
            return []

        edges: list[Edge] = []

        # resources + bases: flat list of strings (paths or remote URLs).
        for key, kind in (("resources", "kustomize_resource"),
                          ("bases", "kustomize_resource"),
                          ("components", "kustomize_component")):
            for raw in _str_list(doc.get(key)):
                edges.append(Edge(kind=kind, raw=raw, symbol=None, line=0))

        # patchesStrategicMerge: ["path.yaml", ...]
        for raw in _str_list(doc.get("patchesStrategicMerge")):
            edges.append(
                Edge(kind="kustomize_patch", raw=raw, symbol=None, line=0)
            )

        # patches: [{path: "file.yaml", target: {...}}, ...] — newer form.
        for item in _list(doc.get("patches")):
            if isinstance(item, dict):
                path = item.get("path")
                if isinstance(path, str) and path:
                    edges.append(
                        Edge(kind="kustomize_patch", raw=path, symbol=None, line=0)
                    )
            elif isinstance(item, str):
                # Some kustomize forms allow a bare string.
                edges.append(
                    Edge(kind="kustomize_patch", raw=item, symbol=None, line=0)
                )

        # configMapGenerator / secretGenerator — look at ``files:`` entries
        # which have the form ``key=path`` or just ``path``. ``envs:`` and
        # ``literals:`` don't reference files.
        for gen_key in ("configMapGenerator", "secretGenerator"):
            for item in _list(doc.get(gen_key)):
                if not isinstance(item, dict):
                    continue
                for file_ref in _str_list(item.get("files")):
                    # `key=path` form — split at first `=`.
                    path = file_ref.split("=", 1)[-1] if "=" in file_ref else file_ref
                    edges.append(
                        Edge(
                            kind="kustomize_generator",
                            raw=path,
                            symbol=None,
                            line=0,
                        )
                    )

        return edges


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _str_list(value) -> list[str]:
    return [v for v in _list(value) if isinstance(v, str) and v]
