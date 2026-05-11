"""Helm resolver — Chart.yaml dependencies + intra-chart template includes.

Two file flavors both route here via ``yaml_classifier``:

* ``Chart.yaml``            — emit ``helm_dependency`` edges from the
                              ``dependencies:`` list. Repository kinds:

                              - ``file://…``        — local subchart, path
                                relative to Chart.yaml's dir.
                              - ``oci://``, ``http://``, ``https://``,
                                registry aliases — external.

* ``<chart>/templates/*``   — emit ``helm_include`` edges from
                              ``{{ include "name" . }}`` and
                              ``{{ template "name" . }}`` expressions.
                              The template ``name`` is not a file — it's
                              a symbol defined by ``{{ define "name" }}``
                              in some ``.tpl`` / ``.yaml`` in the same
                              chart's ``templates/`` dir. Resolution
                              happens in ``relations`` where we build a
                              per-chart name→file map in a pre-pass.

Edges for Helm templates carry ``raw=<template-name>``, and ``symbol``
is None (Helm doesn't have the "symbol from module" shape Python does).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .base import BaseResolver, Edge


# {{ include "name.template" . }} and {{ template "name.template" . }}.
# Both Helm forms — they differ operationally (``template`` is less
# composable) but resolve to the same defined template name. Quotes
# can be double or single (Go template string literals allow either).
_INCLUDE_RE = re.compile(
    r'''{{-?\s*(?:include|template)\s+(?:"([^"]+)"|'([^']+)')'''
)

# {{- define "name" -}} — template definition sites. We emit these as a
# special ``helm_define`` edge kind that relations.py consumes into the
# per-chart name→file map and then DROPS before persistence (they're
# metadata, not a real dep). Keeping them in the edge stream avoids a
# second I/O pass over template files.
_DEFINE_RE = re.compile(
    r'''{{-?\s*define\s+(?:"([^"]+)"|'([^']+)')'''
)


class HelmResolver(BaseResolver):
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        if file_path is None:
            return []

        # Chart.yaml: YAML-parseable and small.
        if file_path.name in ("Chart.yaml", "Chart.yml"):
            return self._extract_chart_yaml(source_bytes)

        # Otherwise: a template file under some ``templates/`` dir.
        return self._extract_template(source_bytes)

    # ---- Chart.yaml ----------------------------------------------------

    def _extract_chart_yaml(self, source_bytes: bytes) -> list[Edge]:
        try:
            doc = yaml.safe_load(source_bytes)
        except yaml.YAMLError:
            return []
        if not isinstance(doc, dict):
            return []

        edges: list[Edge] = []
        deps = doc.get("dependencies")
        if not isinstance(deps, list):
            return edges

        for dep in deps:
            if not isinstance(dep, dict):
                continue
            name = dep.get("name")
            if not isinstance(name, str) or not name:
                continue
            repo = dep.get("repository")
            alias = dep.get("alias")
            # Three cases, each encoded distinctly so the resolver can
            # pick the correct lookup strategy:
            #
            # 1. ``file://<path>`` — local chart at that path (relative
            #    to the Chart.yaml's dir). ``raw`` carries the path.
            # 2. Omitted/empty repository — Helm convention: the chart
            #    is unpacked at ``<parent>/charts/<name>``. Encode by
            #    leaving ``raw`` empty; resolver uses ``symbol`` (name).
            # 3. ``http(s)://``, ``oci://``, ``@alias`` — external.
            #    Keep ``raw`` = repo for display; resolver returns None.
            if isinstance(repo, str) and repo.startswith("file://"):
                edges.append(
                    Edge(
                        kind="helm_dependency",
                        raw=repo[len("file://"):].rstrip("/"),
                        symbol=name,
                        line=0,
                    )
                )
            elif not isinstance(repo, str) or not repo:
                edges.append(
                    Edge(
                        kind="helm_dependency",
                        raw="",
                        symbol=name,
                        line=0,
                    )
                )
            else:
                edges.append(
                    Edge(
                        kind="helm_dependency",
                        raw=repo,
                        symbol=alias if isinstance(alias, str) else name,
                        line=0,
                    )
                )
        return edges

    # ---- Template files ------------------------------------------------

    def _extract_template(self, source_bytes: bytes) -> list[Edge]:
        text = source_bytes.decode("utf-8", errors="replace")
        edges: list[Edge] = []
        # Collect define sites first — relations.py uses them to build
        # the per-chart name→file map, then drops them before persisting.
        for m in _DEFINE_RE.finditer(text):
            name = m.group(1) or m.group(2)
            line = text.count("\n", 0, m.start()) + 1
            edges.append(
                Edge(kind="helm_define", raw=name, symbol=None, line=line)
            )
        for m in _INCLUDE_RE.finditer(text):
            name = m.group(1) or m.group(2)
            line = text.count("\n", 0, m.start()) + 1
            edges.append(
                Edge(kind="helm_include", raw=name, symbol=None, line=line)
            )
        return edges
