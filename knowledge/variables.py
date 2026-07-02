"""Per-project variable substitution for file-edge resolution.

An Ansible repo often has a handful of variables (``deploy_env``,
``app_version``, ``region``, …) that make include paths like
``_tasks/{{ deploy_env }}/…`` resolvable. Without knowing those,
the resolver leaves edges as ``kind='unresolved'``. This module
stores the variables in SQLite
(one row per (project, scope, name)), substitutes them at edge-
resolution time, and re-runs resolution on existing edges when a user
changes the table.

Separation of concerns:

* ``variables.py`` owns the CRUD, the substitution syntax, and the
  after-the-fact re-resolution pass (``apply_variables``).
* ``relations.py`` owns the during-build resolution pipeline, pulls
  the vars map out of ``FileIndex.variables`` and calls ``substitute``.
* ``cli.py`` owns the ``knowledge vars`` subcommands and calls
  ``apply_variables`` after every mutation so users never wonder
  whether a change took effect.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import NamedTuple

from . import db
from .db import Connection


# Valid scope values. ``all`` is a catch-all merged into every domain-
# scoped lookup; scope-specific names win on collision. Keeping the set
# closed makes typos in ``knowledge vars set <scope> …`` detectable at
# the CLI edge.
VALID_SCOPES: frozenset[str] = frozenset({"ansible", "terraform", "helm", "all"})


# ``{{ name }}`` / ``{{ name | filter | filter2 }}``. Only the NAME is
# captured; filters are ignored (we can't execute arbitrary Jinja, and
# for the path-resolution use case the filters almost always don't
# change what file we're looking at). Anchors on ``\w`` so object/attr
# access like ``{{ foo.bar }}`` or ``{{ foo[0] }}`` doesn't match —
# those are almost never used for static path construction.
_JINJA_VAR_RE = re.compile(
    r"\{\{-?\s*([a-zA-Z_]\w*)(?:\s*\|[^}]+?)?\s*-?\}\}"
)

# ``${var.name}`` — Terraform's interpolation for ``variable`` blocks.
# Also captures bare ``${name}`` if present (some config files use it),
# though we normalize to the ``var.`` form inside the regex to avoid
# false positives on shell variables. Keep the ``var.`` prefix mandatory;
# users can always set ``knowledge vars set terraform env prod`` and
# write ``${var.env}`` in their .tf files.
_TF_VAR_RE = re.compile(
    r"\$\{var\.([a-zA-Z_]\w*)\}"
)

# Syntax names match the ``scope`` enum so callers can write
# ``substitute(raw, vars_map, "jinja")`` without a side table.
_SYNTAX_REGEX = {
    "jinja":     _JINJA_VAR_RE,
    "terraform": _TF_VAR_RE,
}


class Variable(NamedTuple):
    """One row of project_variables, exposed to the CLI for listing."""

    scope: str
    name: str
    value: str
    updated_at: float
    source: str


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def set_many(
    conn: Connection,
    project_id: int,
    scope: str,
    pairs: dict[str, str],
) -> int:
    """Insert or update every ``(scope, name) → value`` pair. Returns count.

    One transaction for the whole batch so partial failures don't leave
    the table half-updated. Uses ``ON CONFLICT`` to update values in
    place without deleting first — preserves the ``created_at`` on
    existing rows (only ``updated_at`` bumps).
    """
    _validate_scope(scope)
    if not pairs:
        return 0
    now = time.time()
    with db.transaction(conn):
        for name, value in pairs.items():
            _validate_name(name)
            if not isinstance(value, str):
                value = str(value)
            db.execute(
                conn,
                "INSERT INTO project_variables("
                "project_id, scope, name, value, source, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'manual', ?, ?) "
                "ON CONFLICT(project_id, scope, name) DO UPDATE SET "
                "value = excluded.value, source = 'manual', "
                "updated_at = excluded.updated_at",
                (project_id, scope, name, value, now, now),
            )
    return len(pairs)


# Source label format: 'manual' for explicit `vars set`, or
# 'auto:<origin>' for indexer-driven inserts (e.g. 'auto:group_vars',
# 'auto:host_vars'). The 'auto:' prefix is the contract `set_auto` uses
# to decide whether a row is safe to overwrite.
_AUTO_SOURCE_PREFIX = "auto:"


def set_auto(
    conn: Connection,
    project_id: int,
    scope: str,
    pairs: dict[str, str],
    source: str,
) -> int:
    """Insert/update auto-discovered variables; never stomp manual ones.

    Used by the indexer when reading ``group_vars/all*`` / ``host_vars/*``.
    Every auto write is guarded by ``WHERE source LIKE 'auto:%'`` on the
    UPSERT update path, so a row that was previously set with
    ``knowledge vars set …`` (``source='manual'``) is left untouched.

    Both SQLite (UPSERT WHERE on DO UPDATE) and PostgreSQL support this
    syntax. Returns the number of (name, value) pairs the caller asked
    to apply — NOT the number actually written, which is harder to
    determine portably (sqlite's changes() vs psycopg's rowcount diverge
    when ON CONFLICT skips the row). Callers should only check this for
    "did we attempt anything?" not "how many really moved".
    """
    _validate_scope(scope)
    if not source.startswith(_AUTO_SOURCE_PREFIX):
        raise ValueError(
            f"set_auto source must start with {_AUTO_SOURCE_PREFIX!r}; "
            f"got {source!r}"
        )
    if not pairs:
        return 0
    now = time.time()
    with db.transaction(conn):
        for name, value in pairs.items():
            _validate_name(name)
            if not isinstance(value, str):
                value = str(value)
            db.execute(
                conn,
                "INSERT INTO project_variables("
                "project_id, scope, name, value, source, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, scope, name) DO UPDATE SET "
                "value = excluded.value, source = excluded.source, "
                "updated_at = excluded.updated_at "
                "WHERE project_variables.source LIKE 'auto:%'",
                (project_id, scope, name, value, source, now, now),
            )
    return len(pairs)


def delete_stale_auto(
    conn: Connection,
    project_id: int,
    source: str,
    kept_names: set[str],
) -> int:
    """Drop auto rows for ``source`` whose name is no longer in ``kept_names``.

    Manual rows are left alone (the WHERE clause only matches the exact
    auto-source label). Run this after ``set_auto`` so a key removed from
    a YAML file disappears on the next index pass.

    Returns the number of rows deleted.
    """
    _validate_scope_or_raise = None  # placeholder so linter stays quiet
    del _validate_scope_or_raise
    if not source.startswith(_AUTO_SOURCE_PREFIX):
        raise ValueError(
            f"delete_stale_auto source must start with "
            f"{_AUTO_SOURCE_PREFIX!r}; got {source!r}"
        )
    # No names → wipe every auto row for this source. Useful for "force
    # re-discover" semantics when the YAML files all disappeared.
    if not kept_names:
        return db.execute(
            conn,
            "DELETE FROM project_variables "
            "WHERE project_id = ? AND source = ?",
            (project_id, source),
        )
    placeholders = ",".join("?" for _ in kept_names)
    params: list[object] = [project_id, source]
    params.extend(kept_names)
    return db.execute(
        conn,
        f"DELETE FROM project_variables "
        f"WHERE project_id = ? AND source = ? "
        f"AND name NOT IN ({placeholders})",
        tuple(params),
    )


def unset(
    conn: Connection,
    project_id: int,
    scope: str,
    name: str,
) -> bool:
    """Delete one row. Returns True if a row existed."""
    _validate_scope(scope)
    deleted = db.execute(
        conn,
        "DELETE FROM project_variables "
        "WHERE project_id = ? AND scope = ? AND name = ?",
        (project_id, scope, name),
    )
    return deleted > 0


def unset_scope(
    conn: Connection,
    project_id: int,
    scope: str,
) -> int:
    """Delete every row in ``scope``. Returns deleted count."""
    _validate_scope(scope)
    return db.execute(
        conn,
        "DELETE FROM project_variables WHERE project_id = ? AND scope = ?",
        (project_id, scope),
    )


def list_vars(
    conn: Connection,
    project_id: int,
    scope: str | None = None,
) -> list[Variable]:
    """Return all variables for the project, optionally filtered by scope.

    Ordered by (scope, name) for stable CLI output.
    """
    if scope is not None:
        _validate_scope(scope)
        rows = db.fetch_all(
            conn,
            "SELECT scope, name, value, updated_at, source "
            "FROM project_variables "
            "WHERE project_id = ? AND scope = ? ORDER BY name",
            (project_id, scope),
        )
    else:
        rows = db.fetch_all(
            conn,
            "SELECT scope, name, value, updated_at, source "
            "FROM project_variables "
            "WHERE project_id = ? ORDER BY scope, name",
            (project_id,),
        )
    return [Variable(*r) for r in rows]


def unset_auto_all(
    conn: Connection,
    project_id: int,
) -> int:
    """Delete every auto row for the project. Manual rows untouched.

    Backs ``knowledge vars unset --auto``. The next ``build``/``update``
    will re-populate from the on-disk YAML files (if any).
    """
    return db.execute(
        conn,
        "DELETE FROM project_variables "
        "WHERE project_id = ? AND source LIKE 'auto:%'",
        (project_id,),
    )


def import_json(
    conn: Connection,
    project_id: int,
    scope: str,
    json_path: Path,
) -> int:
    """Load a JSON ``{name: value}`` object and set all entries in ``scope``.

    Nested / non-string values are coerced to strings (or rejected — we
    choose to coerce, matching how Ansible's extra-vars handles numeric
    and boolean scalars). Returns the number of keys imported.
    """
    text = json_path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"{json_path}: expected a JSON object at the top level, "
            f"got {type(data).__name__}"
        )
    pairs: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue  # JSON keys are always strings in Python's parser
        pairs[k] = str(v) if not isinstance(v, str) else v
    return set_many(conn, project_id, scope, pairs)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def resolve_scoped_vars(
    variables: dict[str, dict[str, str]],
    scope: str,
) -> dict[str, str]:
    """Merge ``all`` with the requested scope. Scope-specific wins on collision.

    Callers provide ``variables`` pre-grouped by scope (e.g. from
    ``FileIndex.variables`` which is ``{scope: {name: value}}``).
    Returning a flat ``{name: value}`` view keeps the substitute loop
    simple.
    """
    merged = dict(variables.get("all", {}))
    merged.update(variables.get(scope, {}))
    return merged


def substitute(
    raw: str,
    vars_map: dict[str, str],
    syntax: str,
) -> tuple[str, bool]:
    """Replace every ``{{ name }}`` / ``${var.name}`` whose ``name`` exists
    in ``vars_map``. Return ``(new_raw, fully_substituted)``.

    ``fully_substituted`` is ``False`` when at least one template marker
    in the original text refers to a name not in ``vars_map`` — the
    caller should NOT attempt file resolution in that case (the
    substituted string still has unresolved markers and would be
    nonsensical to look up).
    """
    regex = _SYNTAX_REGEX.get(syntax)
    if regex is None:
        raise ValueError(f"unknown substitution syntax: {syntax!r}")

    missing: list[str] = []

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        if name in vars_map:
            return vars_map[name]
        missing.append(name)
        return m.group(0)  # leave intact so we can tell we missed one

    new_raw = regex.sub(_replace, raw)
    return new_raw, not missing


def has_template_markers(raw: str) -> bool:
    """True if ``raw`` carries any Jinja or Terraform variable syntax.

    Cheap check for the ``apply_variables`` pass — we only want to touch
    edges whose text could benefit from substitution.
    """
    if not raw:
        return False
    # ``{{`` catches Jinja; the Terraform form starts with ``${var.``.
    # Both are substrings so a single ``in`` check each is enough — the
    # regex only runs once we've decided to attempt substitution.
    return "{{" in raw or "${var." in raw


# ---------------------------------------------------------------------------
# Apply (re-resolve existing edges after variables change)
# ---------------------------------------------------------------------------


def apply_variables(
    conn: Connection,
    project_id: int,
    root: Path,
) -> tuple[int, int]:
    """Re-resolve every edge whose raw carries template markers.

    Returns ``(updated, still_parametric)`` where ``updated`` is the
    number of rows whose ``target_file_id`` changed (NULL → not NULL,
    NULL → different NULL, or NULL → NULL that used to have a different
    shape) and ``still_parametric`` is the number that remain NULL
    because not every variable is known.

    Intentionally does NOT touch edges without template markers —
    stdlib/external edges shouldn't suddenly resolve because the user
    set an unrelated variable.

    Legacy rows with ``kind='unresolved'`` for Ansible Jinja paths (from
    DBs built before the Phase 3 resolver change) are handled by looking
    at the raw content instead of the stored kind. If substitution
    succeeds and the raw resembles an include/vars/role reference, we
    still need the original kind to know which resolver to re-run.
    Policy: for ``kind='unresolved'`` rows where the raw has Jinja, we
    best-effort re-classify by inspecting the raw with
    ``_guess_ansible_kind`` (path-shaped → include_tasks; bare-name
    shape → nothing, can't tell). Pre-Phase-3 DBs get partial recovery;
    a full ``knowledge build`` gives a clean slate.
    """
    # Local import: avoids the import-cycle risk that would otherwise
    # exist between ``relations`` and ``variables`` at module load time.
    from . import relations

    index = relations.FileIndex.load(conn, project_id, root)
    # Pass ``conn`` so ``prepare`` loads ``index.variables`` from the
    # project_variables table — the whole point of this call. An empty
    # pending buffer is fine (helm_templates stays empty; we don't
    # re-resolve helm_includes during the apply pass).
    index.prepare([], conn=conn)

    rows = db.fetch_all(
        conn,
        "SELECT id, source_file_id, kind, raw, symbol, target_file_id "
        "FROM file_edges WHERE project_id = ?",
        (project_id,),
    )

    # Pre-load source-file rel_paths for the edges we might touch.
    # An edge is "touchable" if its raw OR symbol carries templates —
    # Ansible include_role stashes ``tasks_from`` in symbol and both
    # need variable substitution to resolve the nested task file.
    candidate_rows = [
        r for r in rows
        if has_template_markers(r[3])
        or (r[4] is not None and has_template_markers(r[4]))
    ]
    if not candidate_rows:
        return (0, 0)

    src_ids = {r[1] for r in candidate_rows}
    placeholders = ",".join("?" * len(src_ids))
    src_rel_by_id: dict[int, str] = {
        fid: rel
        for fid, rel in db.fetch_all(
            conn,
            f"SELECT id, rel_path FROM files WHERE id IN ({placeholders})",
            tuple(src_ids),
        )
    }

    updated = 0
    still_parametric = 0
    resolver_fn = relations._resolve_yaml  # Ansible/Helm/etc. share it
    tf_resolver_fn = relations._resolve_terraform

    for edge_id, src_id, kind, raw, symbol, old_target in candidate_rows:
        source_rel = src_rel_by_id.get(src_id)
        if source_rel is None:
            continue

        # Pick syntax + scope from kind. Legacy ``unresolved`` rows don't
        # carry their domain on their sleeve — use _guess_ansible_kind to
        # recover; if we can't tell, default to ansible (the common case).
        effective_kind = kind
        if kind == "unresolved":
            effective_kind = _guess_ansible_kind(raw) or "ansible_include_tasks"

        syntax, scope = _syntax_and_scope_for_kind(effective_kind)
        if syntax is None:
            continue

        scoped = resolve_scoped_vars(index.variables, scope)
        new_raw, raw_ok = substitute(raw, scoped, syntax)
        new_symbol = symbol
        sym_ok = True
        if symbol is not None and has_template_markers(symbol):
            new_symbol, sym_ok = substitute(symbol, scoped, syntax)
        fully = raw_ok and sym_ok

        new_target: int | None
        new_kind = kind
        if fully:
            # Build a throwaway Edge to reuse the existing resolver fn.
            from .resolvers import Edge as _Edge

            fake = _Edge(
                kind=effective_kind,
                raw=new_raw,
                symbol=new_symbol,
                line=0,
            )
            if effective_kind.startswith("tf_"):
                new_target = tf_resolver_fn(fake, index, source_rel)
            else:
                new_target = resolver_fn(fake, index, source_rel)
            # If we recovered a legacy ``unresolved`` row and it resolved,
            # upgrade its kind too — otherwise the CLI would keep showing
            # it as ``unresolved`` even though we found a target.
            if kind == "unresolved" and new_target is not None:
                new_kind = effective_kind
        else:
            new_target = None

        if new_target == old_target and new_kind == kind:
            # Nothing changed; skip the UPDATE to avoid a write.
            if new_target is None:
                still_parametric += 1
            continue

        db.execute(
            conn,
            "UPDATE file_edges SET target_file_id = ?, kind = ? WHERE id = ?",
            (new_target, new_kind, edge_id),
        )
        if new_target is not None:
            updated += 1
        else:
            still_parametric += 1

    return updated, still_parametric


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _syntax_and_scope_for_kind(kind: str) -> tuple[str | None, str]:
    """Return ``(syntax, scope)`` for an edge kind, or ``(None, ...)`` if
    substitution doesn't apply to this kind.

    Scope is used to pick the right sub-dict out of
    ``FileIndex.variables`` (plus ``all``).
    """
    if kind.startswith("ansible_"):
        return ("jinja", "ansible")
    if kind.startswith("helm_"):
        return ("jinja", "helm")
    if kind.startswith("tf_"):
        return ("terraform", "terraform")
    return (None, "")


# A minimal heuristic to recover the original kind from a legacy
# ``unresolved`` row's raw text. Only used for pre-Phase-3 DBs; new
# extractions keep the kind intact and never need this.
def _guess_ansible_kind(raw: str) -> str | None:
    """Return a best-guess Ansible kind for a legacy unresolved raw.

    Path-shaped raws (contain a ``/`` or end in ``.yml``/``.yaml``) are
    treated as include_tasks references. Anything else returns None
    (caller will fall back to its default).
    """
    if "/" in raw or raw.endswith((".yml", ".yaml")):
        return "ansible_include_tasks"
    return None


def _validate_scope(scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}: expected one of {sorted(VALID_SCOPES)}"
        )


def _validate_name(name: str) -> None:
    if not name or not isinstance(name, str):
        raise ValueError(f"variable name must be a non-empty string, got {name!r}")
    # Mirror the regex's character class so we can't store a name the
    # substitution pass would never match. Quietly rejecting obvious
    # typos at set time is better than silent no-op at resolve time.
    if not re.fullmatch(r"[a-zA-Z_]\w*", name):
        raise ValueError(
            f"variable name {name!r} must match [a-zA-Z_]\\w* "
            "(Jinja/Terraform identifier rules)"
        )
