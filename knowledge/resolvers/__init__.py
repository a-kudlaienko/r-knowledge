"""Resolver registry.

``dispatch_resolver(lang, file_path)`` returns a list of cached resolver
instances (possibly empty) to run over a file.

Phase 1 covered Python + JS/TS via language tag alone. Phase 2 adds
domains whose YAML files all look the same to ``EXT_TO_LANG`` but route
to different resolvers by path convention: Ansible, Helm, GitHub Actions,
Kustomize. The ``file_path`` argument is consulted for ``lang='yaml'``
to pick the right resolver; other languages ignore it.

Most (lang, flavor) combos resolve to exactly one resolver — but some
deserve two. ArgoCD Application manifests commonly live inside a Helm
chart's ``templates/`` dir; on those files we run the Helm resolver
(for ``{{ include }}`` / ``{{ define }}``) AND the ArgoCD resolver (for
``spec.source.path`` chart-to-chart references). Multi-resolver support
keeps each resolver focused instead of overloading one with cross-
domain concerns.
"""

from __future__ import annotations

from pathlib import Path

from .ansible_resolver import AnsibleResolver
from .argocd_resolver import ArgoCDResolver
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

# YAML flavors (from ``classify_yaml_path``) → tuple of resolver classes
# to run in order. Edges from all resolvers are concatenated. Order is
# cosmetic (edges carry their own ``kind`` for routing at resolution).
_YAML_RESOLVERS: dict[str, tuple[type[BaseResolver], ...]] = {
    "ansible":       (AnsibleResolver,),
    "helm_chart":    (HelmResolver,),
    # ArgoCD Application manifests very often live inside a Helm
    # chart's templates/ dir. Run both so we capture Helm includes AND
    # cross-chart ``spec.source.path`` references in one pass.
    "helm_template": (HelmResolver, ArgoCDResolver),
    "gha_workflow":  (GitHubActionsResolver,),
    "gha_action":    (GitHubActionsResolver,),
    "kustomize":     (KustomizeResolver,),
}

_INSTANCES: dict[str, BaseResolver] = {}


def dispatch_resolver(
    lang: str,
    file_path: Path | None = None,
) -> list[BaseResolver]:
    """Return a list of cached resolvers to run on ``(lang, file_path)``.

    Empty list means "no resolver available" — same semantics as the
    old ``None`` return. Languages that map to a single resolver
    (``python``, ``hcl``, …) ignore ``file_path``. YAML routes through
    ``classify_yaml_path``; unclassified YAML (plain k8s manifests,
    values files, etc.) returns ``[]`` so those files get no edges and
    the CLI falls back to its sibling-files view.
    """
    cls = _SIMPLE_RESOLVERS.get(lang)
    if cls is not None:
        return [_get_instance(cls, lang)]

    if lang == "yaml":
        if file_path is None:
            return []
        flavor = classify_yaml_path(file_path)
        if flavor is None:
            return []
        classes = _YAML_RESOLVERS.get(flavor)
        if not classes:
            return []
        # Key the cache by (flavor, resolver class) so ansible+helm
        # don't share one instance (they have distinct state / config)
        # and multi-resolver flavors get each class cached separately.
        return [
            _get_instance(c, f"yaml:{flavor}:{c.__name__}")
            for c in classes
        ]

    return []


def _get_instance(cls: type[BaseResolver], key: str) -> BaseResolver:
    inst = _INSTANCES.get(key)
    if inst is None:
        inst = cls()
        _INSTANCES[key] = inst
    return inst


__all__ = ["Edge", "BaseResolver", "dispatch_resolver", "classify_yaml_path"]
