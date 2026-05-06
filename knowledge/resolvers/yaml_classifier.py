"""YAML flavor classifier by path convention.

One place to decide "what kind of YAML is this" so the dispatcher and
any other caller agree. Mirrors ``chunkers/yaml_chunker.py``'s internal
``_classify_path``, but extended with the flavors Phase 2 adds:

* ``ansible``        — anywhere under an ``ansible/`` dir, or under
                        ``playbooks/``, ``roles/``, ``tasks/``, ``handlers/``,
                        ``meta/``, ``vars/``, ``defaults/``, ``group_vars/``,
                        ``host_vars/``. Captures both standard Ansible repos
                        and the ``platform/``.
* ``helm_chart``     — a ``Chart.yaml`` / ``Chart.yml``.
* ``helm_template``  — any file under ``<chart>/templates/`` (including .tpl).
* ``gha_workflow``   — any file under ``.github/workflows/``.
* ``gha_action``     — an ``action.yml`` / ``action.yaml`` under
                        ``.github/actions/**``.
* ``kustomize``      — a ``kustomization.yaml`` / ``kustomization.yml``.
* ``None``           — unclassified YAML (plain k8s manifest, Helm values,
                        generic config). No resolver; no edges.

Returns the ``None`` sentinel (not raising) for the no-match case so the
dispatcher treats that as "no resolver available" — same semantics as
languages we haven't written a resolver for.
"""

from __future__ import annotations

from pathlib import Path


def classify_yaml_path(file_path: Path) -> str | None:
    name = file_path.name
    parts = file_path.parts

    # Kustomize — either top-level kustomization file.
    if name in ("kustomization.yaml", "kustomization.yml"):
        return "kustomize"

    # GitHub Actions — path-prefix based. Workflows live under
    # ``.github/workflows/`` and composite/reusable actions under
    # ``.github/actions/<name>/action.yml``. A repo that happens to have
    # ``.github`` elsewhere in its tree (rare) could false-positive — we
    # accept it rather than add more specific anchoring.
    for i in range(len(parts) - 1):
        if parts[i] == ".github":
            if i + 1 < len(parts) and parts[i + 1] == "workflows":
                return "gha_workflow"
            if i + 1 < len(parts) and parts[i + 1] == "actions":
                # An action.yml somewhere under .github/actions/**.
                if name in ("action.yml", "action.yaml"):
                    return "gha_action"
                # Files under an action dir that aren't action.yml aren't
                # the action manifest — unclassified.
                return None

    # Helm — Chart.yaml is the manifest; anything under ``templates/``
    # within a chart dir is a template. Values files are deliberately
    # not classified (they have no outbound edges).
    if name in ("Chart.yaml", "Chart.yml"):
        return "helm_chart"
    for i in range(len(parts) - 1):
        # <parent>/templates/<...>.{yaml,yml,tpl}. A chart's "templates"
        # dir is the canonical place; we don't require the parent to be
        # named "charts/" — a bare ``<chart>/templates/x.yaml`` layout
        # (no wrapping ``charts/`` dir) also resolves.
        if parts[i] == "templates" and i > 0:
            suffix = file_path.suffix.lower()
            if suffix in (".yaml", ".yml", ".tpl"):
                return "helm_template"

    # Ansible — broad net on directory names anywhere in the path.
    # ``ansible/`` as a top-level dir, or canonical role subdirs
    # (tasks, handlers, vars, defaults, meta), or playbooks/.
    # Role-wrapper layouts that use ``platform/`` instead of ``roles/``
    # are caught by the same subdir patterns (tasks/, handlers/, meta/,
    # defaults/, vars/).
    ansible_dirs = {
        "ansible",
        "playbooks",
        "roles",
        "tasks",
        "handlers",
        "meta",
        "vars",
        "defaults",
        "group_vars",
        "host_vars",
    }
    if ansible_dirs.intersection(parts):
        return "ansible"

    return None
