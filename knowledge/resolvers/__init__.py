"""Resolver registry.

``dispatch_resolver(lang, file_path)`` returns a cached resolver instance,
or ``None`` if no resolver is implemented for that (lang, path) combo.

Phase 1 covered Python + JS/TS via language tag alone. Phase 2 adds
domains whose YAML files all look the same to ``EXT_TO_LANG`` but route
to different resolvers by path convention: Ansible, Helm, GitHub Actions,
Kustomize. The ``file_path`` argument is consulted for ``lang='yaml'``
to pick the right resolver; other languages ignore it.

The pattern mirrors ``chunkers/yaml_chunker.py``'s internal path-based
flavor detection — one shared classifier keeps both in sync.
"""

from __future__ import annotations

from pathlib import Path

from .ansible_resolver import AnsibleResolver
from .base import BaseResolver, Edge
from .github_actions_resolver import GitHubActionsResolver
from .helm_resolver import HelmResolver
from .javascript_resolver import JavaScriptResolver, TypeScriptResolver
from .kustomize_resolver import KustomizeResolver
from .python_resolver import PythonResolver
from .terraform_resolver import TerraformResolver
from .yaml_classifier import classify_yaml_path

# Non-YAML languages: one resolver per lang tag.
_SIMPLE_RESOLVERS: dict[str, type[BaseResolver]] = {
    "python":     PythonResolver,
    "javascript": JavaScriptResolver,
    "typescript": TypeScriptResolver,
    "hcl":        TerraformResolver,
}

# YAML flavors (from ``classify_yaml_path``) → resolver class.
_YAML_RESOLVERS: dict[str, type[BaseResolver]] = {
    "ansible":       AnsibleResolver,
    "helm_chart":    HelmResolver,
    "helm_template": HelmResolver,
    "gha_workflow":  GitHubActionsResolver,
    "gha_action":    GitHubActionsResolver,
    "kustomize":     KustomizeResolver,
}

_INSTANCES: dict[str, BaseResolver] = {}


def dispatch_resolver(
    lang: str,
    file_path: Path | None = None,
) -> BaseResolver | None:
    """Return a cached resolver for ``(lang, file_path)``, or None.

    Languages that map to a single resolver (``python``, ``hcl``, …)
    ignore ``file_path``. YAML routes through ``classify_yaml_path`` to
    pick between ansible/helm/gha/kustomize — and returns ``None`` for
    plain-k8s / values-only / unclassified YAML so those files get no
    edges (the CLI falls back to a sibling-files view for them).
    """
    cls = _SIMPLE_RESOLVERS.get(lang)
    if cls is not None:
        return _get_instance(cls, lang)

    if lang == "yaml":
        if file_path is None:
            return None
        flavor = classify_yaml_path(file_path)
        if flavor is None:
            return None
        cls = _YAML_RESOLVERS.get(flavor)
        if cls is None:
            return None
        # Key the cache by the resolver class name so ansible+helm don't
        # share one instance (they have distinct state / config).
        return _get_instance(cls, f"yaml:{flavor}")

    return None


def _get_instance(cls: type[BaseResolver], key: str) -> BaseResolver:
    inst = _INSTANCES.get(key)
    if inst is None:
        inst = cls()
        _INSTANCES[key] = inst
    return inst


__all__ = ["Edge", "BaseResolver", "dispatch_resolver", "classify_yaml_path"]
