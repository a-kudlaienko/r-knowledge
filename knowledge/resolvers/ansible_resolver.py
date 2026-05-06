"""Ansible resolver — playbooks, tasks, roles, custom modules.

Fires for any YAML file in an Ansible-shaped layout (see
``yaml_classifier``). Uses PyYAML to parse; on parse errors the file is
skipped (returns ``[]``) — a malformed ansible file is rare enough not
to warrant recovery.

Edges emitted:

* ``ansible_import_playbook`` — ``import_playbook: other.yml``.
* ``ansible_include_tasks``   — ``include_tasks:`` / ``ansible.builtin.include_tasks:``.
* ``ansible_import_tasks``    — ``import_tasks:`` / ``ansible.builtin.import_tasks:``.
* ``ansible_include_role``    — ``include_role: { name: foo }`` /
                                ``ansible.builtin.include_role:``.
                                ``tasks_from:`` (if present) is recorded
                                in ``symbol`` so the edge can point at the
                                specific task file within the role.
* ``ansible_import_role``     — ``import_role:`` (same shape as include).
* ``ansible_role_entry``      — each entry in a play's ``roles:`` list.
* ``ansible_vars_file``       — ``vars_files: [x.yml, y.yml]``.
* ``ansible_include_vars``    — ``include_vars: file.yml`` (or the
                                 ``file:`` nested form).
* ``ansible_module``          — a task's top-level module key that
                                 matches a file in the project's custom
                                 modules (library/, plugins/*). The
                                 ``raw`` is the module name as written;
                                 resolution happens in ``relations``
                                 where the name→file_id map is built.

Dynamic paths containing ``{{ … }}`` get ``kind='unresolved'`` so the
LLM sees something is there even when we can't figure out the target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from .base import BaseResolver, Edge


# FQCN aliases — Ansible lets you call the builtin modules with any of
# these names and the semantics are identical. We normalize by stripping
# the namespace before matching.
_FQCN_ALIASES = {
    "ansible.builtin.include_tasks": "include_tasks",
    "ansible.builtin.import_tasks":  "import_tasks",
    "ansible.builtin.include_role":  "include_role",
    "ansible.builtin.import_role":   "import_role",
    "ansible.builtin.include_vars":  "include_vars",
    "ansible.builtin.import_playbook": "import_playbook",
}


class AnsibleResolver(BaseResolver):
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        try:
            docs = list(yaml.safe_load_all(source_bytes))
        except yaml.YAMLError:
            return []

        edges: list[Edge] = []
        for doc in docs:
            if doc is None:
                continue
            # A playbook is a list-of-plays; a tasks file is a list-of-tasks;
            # a handlers file is a list-of-handlers; vars files are a dict.
            if isinstance(doc, list):
                self._walk_list(doc, edges)
            elif isinstance(doc, dict):
                # Single-play file (uncommon but legal) or a vars file.
                self._walk_play_or_task(doc, edges)
        return edges

    # ---- Walk helpers -------------------------------------------------

    def _walk_list(self, items: list, edges: list[Edge]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            self._walk_play_or_task(item, edges)

    def _walk_play_or_task(self, mapping: dict, edges: list[Edge]) -> None:
        """A mapping might be a play (has ``hosts`` / ``tasks`` / ``roles``)
        or a task (has ``name`` + a module key). We handle both by looking
        at each recognized key; unrelated keys are ignored.
        """
        # Play-level sections — each emits its own edge kind.
        for roles_entry in _list(mapping.get("roles")):
            name = _role_name(roles_entry)
            if name:
                edges.append(
                    Edge(
                        kind="ansible_role_entry",
                        raw=name,
                        symbol=None,
                        line=0,
                    )
                )

        for vf in _list(mapping.get("vars_files")):
            # Can be a string, or a list of strings (nested), or a dict
            # with ``file:``. Ansible is loose about this.
            for raw in _flatten_path_or_list(vf):
                if raw:
                    edges.append(_path_edge("ansible_vars_file", raw))

        # pre_tasks, tasks, post_tasks, handlers — each is a list of
        # task-shaped mappings. Same walker for all.
        for section in ("pre_tasks", "tasks", "post_tasks", "handlers", "block",
                        "rescue", "always"):
            self._walk_list(_list(mapping.get(section)), edges)

        # import_playbook lives on the play-dict directly.
        raw = mapping.get("import_playbook") or mapping.get(
            "ansible.builtin.import_playbook"
        )
        if isinstance(raw, str):
            edges.append(_path_edge("ansible_import_playbook", raw))

        # Plays have orchestration keys like ``hosts``, ``tasks``,
        # ``roles``, ``pre_tasks`` — they are not tasks and their keys
        # must not be misread as module invocations. Only call the task
        # dispatcher on mappings that don't look like a play.
        if not _is_play_mapping(mapping):
            self._handle_task_module(mapping, edges)

    # ---- Task dispatch ------------------------------------------------

    def _handle_task_module(self, task: dict, edges: list[Edge]) -> None:
        """Inspect a task mapping's module key. Emit the right edge kind.

        The set of known "task meta" keys is filtered out; the remaining
        single key is the module name. Custom-module matching against
        library/plugins happens downstream in ``relations``.
        """
        # Explicit include/import forms. Handle both bare and FQCN.
        for key in list(task.keys()):
            normalized = _FQCN_ALIASES.get(key, key)
            if normalized == "include_tasks":
                self._emit_include_tasks(
                    task[key], edges, kind="ansible_include_tasks"
                )
                return
            if normalized == "import_tasks":
                self._emit_include_tasks(
                    task[key], edges, kind="ansible_import_tasks"
                )
                return
            if normalized == "include_role":
                self._emit_role_include(
                    task[key], edges, kind="ansible_include_role"
                )
                return
            if normalized == "import_role":
                self._emit_role_include(
                    task[key], edges, kind="ansible_import_role"
                )
                return
            if normalized == "include_vars":
                self._emit_include_vars(task[key], edges)
                return

        # Otherwise find the single "module" key and record its name.
        # Resolution to a library/ file happens in relations.py.
        module_name = _pick_module_key(task)
        if module_name is None:
            return
        # Strip FQCN namespace for custom-module matching: a project
        # module named ``foo_bar`` might be referenced as ``foo_bar`` or
        # ``mycollection.foo_bar`` — take the last segment.
        short = module_name.split(".")[-1]
        edges.append(
            Edge(
                kind="ansible_module",
                raw=short,
                symbol=None,
                line=0,
            )
        )

    # ---- Emit helpers --------------------------------------------------

    def _emit_include_tasks(self, value, edges: list[Edge], kind: str) -> None:
        """``include_tasks: file.yml`` OR the dict form
        ``include_tasks: { file: x.yml, apply: {...} }``.
        """
        raw = None
        if isinstance(value, str):
            raw = value
        elif isinstance(value, dict):
            raw = value.get("file") or value.get("name")
            if not isinstance(raw, str):
                raw = None
        if not raw:
            return
        edges.append(_path_edge(kind, raw))

    def _emit_role_include(self, value, edges: list[Edge], kind: str) -> None:
        """``include_role: { name: foo, tasks_from: install.yml }``.

        Emits ONE edge with ``raw=<role_name>`` and, if a specific
        ``tasks_from`` is given, ``symbol=<tasks_from>``. The role-name
        resolver in ``relations`` uses ``tasks_from`` to pick the right
        task file inside the role (defaults to ``tasks/main.yml``).
        """
        name = None
        tasks_from = None
        if isinstance(value, dict):
            name = value.get("name")
            tasks_from = value.get("tasks_from")
        elif isinstance(value, str):
            name = value
        if not isinstance(name, str) or not name:
            return
        # Preserve the natural include_role/import_role kind even when
        # the role name carries Jinja — variable substitution in
        # ``relations.insert_edges`` will try to resolve it; if the
        # user hasn't set the var yet, the edge lands with NULL target
        # and the CLI shows it as ``parametric``.
        edges.append(
            Edge(
                kind=kind,
                raw=name,
                symbol=tasks_from if isinstance(tasks_from, str) else None,
                line=0,
            )
        )

    def _emit_include_vars(self, value, edges: list[Edge]) -> None:
        raw = None
        if isinstance(value, str):
            raw = value
        elif isinstance(value, dict):
            raw = value.get("file") or value.get("name") or value.get("dir")
            if not isinstance(raw, str):
                raw = None
        if raw:
            edges.append(_path_edge("ansible_include_vars", raw))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Keys we know aren't module invocations. Anything else on a task is a
# candidate module name.
_TASK_META_KEYS = {
    "name", "when", "with_items", "with_dict", "with_fileglob",
    "with_nested", "loop", "loop_control", "until", "retries", "delay",
    "tags", "register", "changed_when", "failed_when", "ignore_errors",
    "notify", "become", "become_user", "become_method", "become_flags",
    "delegate_to", "delegate_facts", "run_once", "no_log", "timeout",
    "poll", "async", "vars", "environment", "check_mode", "connection",
    "diff", "port", "throttle", "retries", "module_defaults",
    "any_errors_fatal", "args", "block", "rescue", "always", "listen",
    # ``remote_user`` / ``local_action`` are used on plays; leave as meta.
    "remote_user", "local_action",
}


def _pick_module_key(task: dict) -> str | None:
    """Return the task's module-name key, or None.

    Multiple module-shaped keys on one task is invalid Ansible — we take
    the first non-meta key we find.
    """
    for key in task.keys():
        if not isinstance(key, str):
            continue
        if key in _TASK_META_KEYS:
            continue
        if key.startswith("ansible."):  # FQCN of a meta/include handled above
            continue
        return key
    return None


# Keys that identify a mapping as a PLAY (as opposed to a task). Any of
# these present → treat the mapping as a play and skip task-module
# extraction on its top-level keys. ``import_playbook`` is a play-level
# directive that's handled separately.
_PLAY_MARKER_KEYS = frozenset({
    "hosts",
    "tasks",
    "pre_tasks",
    "post_tasks",
    "roles",
    "import_playbook",
    "ansible.builtin.import_playbook",
})


def _is_play_mapping(m: dict) -> bool:
    return any(k in m for k in _PLAY_MARKER_KEYS)


def _role_name(entry) -> str | None:
    """Extract the role name from a ``roles:`` entry.

    Supports the short string form and the long dict form:
        - foo
        - { role: foo, tags: [...] }
        - { name: foo, ... }
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name = entry.get("role") or entry.get("name")
        if isinstance(name, str):
            return name
    return None


def _path_edge(kind: str, raw: str) -> Edge:
    """Build one path-carrying edge, preserving the natural kind.

    Phase 2 emitted ``kind='unresolved'`` for paths containing Jinja
    (``{{ var }}``) templates, which lost the information about what
    the edge *was* (include_tasks vs include_role vs vars_file). Phase
    3 keeps the kind intact — ``relations.insert_edges`` attempts
    variable substitution against the project's ``project_variables``
    table; if vars aren't set yet, the edge lands with NULL target and
    the CLI displays it as ``parametric`` (waiting for variables) —
    distinct from ``external`` (stdlib / third-party) and
    ``unresolved`` (syntactically irrecoverable, e.g. a Python
    ``import_module`` with a non-literal arg).
    """
    return Edge(kind=kind, raw=raw, symbol=None, line=0)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _flatten_path_or_list(value) -> Iterable[str]:
    """vars_files entries can be strings, nested lists, or dicts with
    ``file:``. Produce a flat iterable of string paths.
    """
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _flatten_path_or_list(item)
        return
    if isinstance(value, dict):
        file_val = value.get("file")
        if isinstance(file_val, str):
            yield file_val
