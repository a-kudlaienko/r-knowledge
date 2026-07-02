"""Auto-discovery of Ansible inventory variables.

Reads the conventional ``group_vars/all.{yml,yaml,/}`` and ``host_vars/*``
locations next to the project root, every ``ansible.cfg`` directory, and
every ``inventory =`` directory, then returns flat ``{name: value}`` maps
ready to feed into ``project_variables`` via :func:`variables.set_auto`.

Why this exists: parametric ``include_tasks: "{{ deploy_env }}/main.yml"``
edges stay unresolved until someone runs ``knowledge vars set ansible
deploy_env=…``. For repos that already keep these values in standard
inventory layout, the indexer can pick them up automatically — one less
manual step before the dependency graph stops dotting through templates.

Precedence (lowest → highest, per the official Ansible docs):

  1. Inventory ``group_vars/all``
  2. Playbook ``group_vars/all``
  3. Inventory ``host_vars/*``
  4. Playbook ``host_vars/*``

We collapse 1+2 into a single ``group_vars`` map and 3+4 into a single
``host_vars`` map. Both maps share scope ``ansible``; the caller stores
them under separate ``source`` labels so :func:`variables.delete_stale_auto`
can clean up each independently when YAML files change. host_vars
overrides group_vars at the resolver level by being inserted later
(higher precedence).

Out of scope on purpose:
- ``group_vars/<group>.yml`` for groups other than ``all`` (we don't
  parse inventory to know which hosts belong to which groups).
- Role ``defaults/main.yml`` / ``vars/main.yml`` (would need per-role
  variable namespaces; current ``scope`` enum doesn't model that).
- ``vars_files:`` chasing inside playbooks.
- ansible-vault decryption (we detect the prefix and skip with a warning).
- Jinja interpretation of values (``deploy_env: "{{ env }}"`` stays literal).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import yaml

from . import gitignore
from .relations import _join_rel
from .variables import _validate_name


_VAULT_HEADER = "$ANSIBLE_VAULT"

_ALL_FILE_NAMES: tuple[str, ...] = ("all.yml", "all.yaml")


def load_inventory_vars(
    root: Path,
    ansible_cfgs: list[tuple[str, dict[str, str]]],
    *,
    on_warning=None,
) -> dict[str, dict[str, str]]:
    """Discover and parse ``group_vars/all*`` and ``host_vars/*`` files.

    ``ansible_cfgs`` comes from :func:`relations._find_ansible_cfgs` —
    a list of ``(cfg_dir_rel, defaults_dict)`` tuples. We use the
    ``inventory =`` value (if any) to extend the search to inventory
    directories, and treat the cfg directory itself as a "playbook
    location" (one tier up in precedence).

    Returns a dict with two entries: ``"group_vars"`` and ``"host_vars"``.
    Each maps variable name → string value. Same key in both means the
    host_vars value wins downstream (the caller inserts host_vars after
    group_vars).

    Warnings (vault files, parse errors, multi-document YAML) are
    routed to ``on_warning`` if provided, otherwise printed to stderr
    once per occurrence. The build is never aborted by a YAML problem
    in an inventory file.
    """
    warn = on_warning or _stderr_warn
    spec = gitignore.load_specs(root)

    # Classify roots into the two precedence tiers. Use lists (not sets)
    # to preserve cfg-discovery order; inventory-tier writes happen
    # before playbook-tier writes so playbook wins on collision.
    inventory_roots: list[Path] = []
    playbook_roots: list[Path] = [root]
    for cfg_dir_rel, cfg_values in ansible_cfgs:
        cfg_dir = (root / cfg_dir_rel).resolve() if cfg_dir_rel else root
        if cfg_dir not in playbook_roots and _is_inside(cfg_dir, root):
            playbook_roots.append(cfg_dir)
        inv = cfg_values.get("inventory", "").strip()
        if not inv:
            continue
        # Inventory may be colon-separated (rare) — Ansible itself only
        # supports one in modern releases; take the first.
        first = inv.split(":", 1)[0].strip()
        if not first:
            continue
        inv_path = (root / _join_rel(cfg_dir_rel, first)).resolve()
        if (
            inv_path.is_dir()
            and _is_inside(inv_path, root)
            and inv_path not in inventory_roots
        ):
            inventory_roots.append(inv_path)

    group_vars: dict[str, str] = {}
    host_vars: dict[str, str] = {}

    # Layer A: inventory group_vars/all
    for r in inventory_roots:
        _merge_group_vars_all(r, root, spec, group_vars, warn)
    # Layer B: playbook group_vars/all (overwrites A on collision)
    for r in playbook_roots:
        _merge_group_vars_all(r, root, spec, group_vars, warn)
    # Layer C: inventory host_vars/*
    for r in inventory_roots:
        _merge_host_vars(r, root, spec, host_vars, warn)
    # Layer D: playbook host_vars/* (overwrites C)
    for r in playbook_roots:
        _merge_host_vars(r, root, spec, host_vars, warn)

    return {"group_vars": group_vars, "host_vars": host_vars}


def _merge_group_vars_all(
    base: Path,
    root: Path,
    spec,
    accum: dict[str, str],
    warn,
) -> None:
    """Pick up ``<base>/group_vars/all.yml``, ``all.yaml``, or ``all/*.{yml,yaml}``.

    Last-writer-wins by sorted relative path within the layer, so
    ``app.yml`` then ``db.yml`` is deterministic across rebuilds.
    """
    gv_dir = base / "group_vars"
    if not gv_dir.is_dir():
        return
    for name in _ALL_FILE_NAMES:
        f = gv_dir / name
        if f.is_file() and not _is_gitignored(f, root, spec):
            _absorb_yaml_file(f, accum, warn)
    all_dir = gv_dir / "all"
    if all_dir.is_dir():
        for f in sorted(_iter_yaml_files(all_dir)):
            if not _is_gitignored(f, root, spec):
                _absorb_yaml_file(f, accum, warn)


def _merge_host_vars(
    base: Path,
    root: Path,
    spec,
    accum: dict[str, str],
    warn,
) -> None:
    """Pick up every ``*.{yml,yaml}`` under ``<base>/host_vars/``.

    Two layouts: a flat ``host_vars/web1.yml`` file or a directory
    ``host_vars/web1/<file>.yml``. We walk both. Sorted by relative
    path to keep the merge deterministic.
    """
    hv_dir = base / "host_vars"
    if not hv_dir.is_dir():
        return
    candidates: list[Path] = []
    for entry in hv_dir.iterdir():
        if entry.is_file() and entry.suffix in (".yml", ".yaml"):
            candidates.append(entry)
        elif entry.is_dir():
            candidates.extend(_iter_yaml_files(entry))
    for f in sorted(candidates):
        if not _is_gitignored(f, root, spec):
            _absorb_yaml_file(f, accum, warn)


def _iter_yaml_files(d: Path) -> Iterable[Path]:
    """Yield every ``*.yml`` / ``*.yaml`` under ``d`` (recursive)."""
    for f in d.rglob("*"):
        if f.is_file() and f.suffix in (".yml", ".yaml"):
            yield f


def _absorb_yaml_file(
    path: Path,
    accum: dict[str, str],
    warn,
) -> None:
    """Parse one YAML file and merge its top-level mapping into ``accum``.

    Skips lists/dicts as values (those rarely appear in templated path
    positions). Coerces scalars to ``str``. ``None`` becomes ``""``.
    Detects ansible-vault headers and skips with a warning. Multi-doc
    YAML uses only the first mapping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        warn(f"could not read {path}: {exc}")
        return
    if text.lstrip().startswith(_VAULT_HEADER):
        warn(f"skipping vault-encrypted file {path}")
        return
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        warn(f"YAML parse error in {path}: {exc}")
        return
    if not docs:
        return
    if len(docs) > 1:
        warn(
            f"{path}: multi-document YAML; only the first document is used"
        )
    doc = docs[0]
    if doc is None:
        return  # empty file
    if not isinstance(doc, dict):
        warn(f"{path}: top-level YAML is not a mapping; skipping")
        return
    for key, value in doc.items():
        if not isinstance(key, str):
            continue
        try:
            _validate_name(key)
        except ValueError:
            # Variable names that the substitution regex couldn't match
            # anyway; silently skip (e.g. dotted keys, unicode names).
            continue
        coerced = _coerce_scalar(value)
        if coerced is None:
            continue  # list/dict — out of scope
        accum[key] = coerced


def _coerce_scalar(value) -> str | None:
    """Return ``str(value)`` for scalars; ``None`` for non-scalars."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # bool MUST be checked before int — bool is a subclass of int.
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _is_inside(path: Path, root: Path) -> bool:
    """True iff ``path`` is the same as or under ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_gitignored(path: Path, root: Path, spec) -> bool:
    """Apply the project's gitignore rules to a discovered file."""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    return gitignore.is_ignored(spec, rel)


def _stderr_warn(msg: str) -> None:
    print(f"warning: ansible_vars: {msg}", file=sys.stderr)
