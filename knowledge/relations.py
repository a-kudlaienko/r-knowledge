"""File-to-file dependency graph (imports, requires, includes).

Sits between the language-specific resolvers (which produce raw ``Edge``
objects from a parse tree) and the SQL store (``file_edges`` table). One
responsibility: take edges + a project's file table and turn them into
persisted rows with ``target_file_id`` resolved where possible.

Design invariants
-----------------

* **Two-phase during build/update.** Resolvers run per-file while the
  indexer walks the project, but resolution needs the *whole* file table.
  Callers collect raw ``Edge`` lists in a buffer, then invoke
  :func:`insert_edges` for each buffered batch after the walk finishes.
  That guarantees forward references resolve correctly even when the
  importing file is processed before the imported file.
* **Resolvers stay pure.** They don't know about file ids or project
  roots. Resolution logic lives here so the two Python+JS rules can
  reference a single :class:`FileIndex`.
* **Wipe-before-insert.** :func:`insert_edges` starts by deleting the
  source file's existing outbound edges. Idempotent for re-indexing
  changed files; a no-op on freshly inserted files. No UNIQUE dance.
* **NULL target_file_id has two meanings**, distinguished by ``kind``:

  * ``kind='unresolved'`` — resolver marked the edge unresolvable
    (templated / variable dynamic import). The ``raw`` field holds the
    original expression text.
  * any other kind with ``target_file_id IS NULL`` — resolver attempted
    to locate a project file but failed → stdlib / third-party /
    external. The CLI reports this as ``kind='external'``.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import db as _db
from .db import Connection
from .resolvers import Edge, dispatch_resolver

# Column order for file_edges rows returned by resolve_edges:
#   (project_id, source_file_id, target_file_id, kind, raw, symbol, line)


# Extensions a bare JS/TS import specifier is allowed to resolve to. Must
# match what the scanner actually indexes — if the scanner doesn't know
# about an extension, the file has no row in ``files`` and resolution
# silently misses. Keep in sync with ``config.EXT_TO_LANG``.
_JS_RESOLVE_EXTS: tuple[str, ...] = (".js", ".jsx", ".ts", ".tsx")
_JS_KNOWN_EXTS: tuple[str, ...] = _JS_RESOLVE_EXTS + (".json", ".mjs", ".cjs")

# Default Ansible roles path (spec: colon-separated list of dirs).
# ``ansible.cfg`` ``[defaults] roles_path`` overrides.
_DEFAULT_ROLES_PATH: tuple[str, ...] = ("roles",)

# Default Ansible library (custom modules) and action_plugins paths.
# Both can be overridden via ``ansible.cfg``. Filter / lookup / callback /
# connection / vars plugins are also custom-code dirs, but they're used
# from Jinja2 expressions we don't parse in Phase 2 — only library and
# action plugins are invoked via task YAML keys.
_DEFAULT_LIBRARY_PATHS: tuple[str, ...] = ("library",)
_DEFAULT_ACTION_PLUGIN_PATHS: tuple[str, ...] = ("action_plugins",)


# ---------------------------------------------------------------------------
# File index (in-memory snapshot of one project's files table)
# ---------------------------------------------------------------------------


@dataclass
class FileIndex:
    """Project files loaded into memory for O(1) resolution lookups.

    Cheap to build (~1 query + a dict comprehension) and reused for every
    edge of every file in one build/update pass. Callers load once at the
    end of the walk and pass it to :func:`insert_edges` per file.

    Phase 2 adds side-maps used by Ansible and Helm resolution. They're
    populated lazily by :meth:`prepare` which takes the pending-edges
    buffer so domain-specific info gathered during extraction (e.g.
    Helm ``{{- define -}}`` sites) can be folded in before resolution.
    """

    project_id: int
    root: Path
    by_path: dict[str, int]

    # --- Phase 2 side-maps (populated by prepare()) ---

    # Ansible roles_path as a list of project-relative dirs (posix).
    # Read from ``ansible.cfg``'s ``[defaults] roles_path`` at project
    # root, with fallback to ``roles/``.
    ansible_roles_path: list[str] = field(default_factory=list)

    # Custom-module name → file_id. Built by scanning the paths declared
    # in ``ansible.cfg`` ``library =`` and ``action_plugins =`` (with
    # defaults). Keyed on the module name (= file stem) so a task with
    # ``foo_bar:`` gets matched without knowing the file path.
    ansible_modules: dict[str, int] = field(default_factory=dict)

    # Per-chart Helm ``{{- define "name" -}}`` blocks. The outer key is
    # the chart root (directory that contains Chart.yaml, as a project-
    # relative posix path); inner key is the template name; value is the
    # file_id of the file declaring that ``define``. Chart-scoped rather
    # than global because two charts can legitimately define the same
    # name (``mychart.labels`` is the convention).
    helm_templates: dict[str, dict[str, int]] = field(default_factory=dict)

    # Project variables grouped by scope. Populated in ``prepare()`` from
    # the ``project_variables`` table. Shape: ``{scope: {name: value}}``
    # where scope ∈ {"ansible", "terraform", "helm", "all"}. Consulted
    # during resolution when an edge's ``raw`` carries ``{{ name }}`` or
    # ``${var.name}`` — see ``variables.substitute``.
    variables: dict[str, dict[str, str]] = field(default_factory=dict)

    # Terraform declarations, scoped by directory (the terraform-root
    # boundary). Outer key = directory posix path (``terraform/live/.../dev``,
    # ``""`` for project root); inner key = declaration string
    # (``var.NAME`` / ``local.NAME`` / ``module.NAME``); value = file_id.
    # Populated in prepare() from the tf_decl edges resolvers emit during
    # extraction. Consulted by _resolve_terraform to target var/local/
    # module references to their declaration file.
    tf_decls: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: Connection, project_id: int, root: Path) -> "FileIndex":
        rows = _db.fetch_all(
            conn,
            "SELECT id, rel_path FROM files WHERE project_id = ?",
            (project_id,),
        )
        return cls(
            project_id=project_id,
            root=root,
            by_path={r[1]: r[0] for r in rows},
        )

    def prepare(
        self,
        pending: list[tuple[int, str, str, list[Edge]]],
        conn: Connection | None = None,
    ) -> None:
        """Populate side-maps. Call once after :meth:`load`, before any
        resolution.

        ``pending`` is the buffer of raw (unresolved) edges the indexer
        accumulated during the walk. The Helm side-map is built from it
        (resolver emits ``helm_define`` edges that we consume here and
        drop from persistence). Ansible-module and roles-path side-maps
        come from scanning ``by_path`` and reading ``ansible.cfg``.

        Ansible configs aren't always at the project root — it's common
        to have ``<root>/ansible/ansible.cfg`` with paths relative to
        the ``ansible/`` dir, or multiple configs in a monorepo. We
        walk the disk for ``ansible.cfg`` files and interpret each
        one's paths relative to its own directory, then merge the
        results.
        """
        cfgs = _find_ansible_cfgs(self.root)
        roles_paths: list[str] = []
        module_paths: list[str] = []
        for cfg_dir_rel, cfg_values in cfgs:
            rp = _split_colon_list(cfg_values.get("roles_path"), ())
            for p in rp:
                roles_paths.append(_join_rel(cfg_dir_rel, p))
            lib = _split_colon_list(cfg_values.get("library"), ())
            for p in lib:
                module_paths.append(_join_rel(cfg_dir_rel, p))
            ap = _split_colon_list(cfg_values.get("action_plugins"), ())
            for p in ap:
                module_paths.append(_join_rel(cfg_dir_rel, p))

        # Fallback defaults: plain ``roles/`` and ``library/`` at project
        # root for configs that omit the keys or repos with no cfg.
        if not roles_paths:
            roles_paths = list(_DEFAULT_ROLES_PATH)
        if not module_paths:
            module_paths = list(_DEFAULT_LIBRARY_PATHS) + list(
                _DEFAULT_ACTION_PLUGIN_PATHS
            )

        # Dedupe while preserving order.
        self.ansible_roles_path = _dedupe(roles_paths)
        self.ansible_modules = _scan_ansible_modules(
            self.by_path, _dedupe(module_paths)
        )

        # Helm: per-chart template-name → file_id map, built from the
        # ``helm_define`` edges the resolver produced during extraction.
        self.helm_templates = _collect_helm_defines(pending)

        # Terraform: per-directory declaration-name → file_id map, built
        # from the tf_decl edges the resolver emitted. Directory scope
        # mirrors Terraform's compilation-unit rule (every .tf in one
        # directory shares a single namespace).
        self.tf_decls = _collect_tf_decls(pending)

        # Project variables (Jinja / Terraform substitution source).
        # ``conn`` is optional so callers outside the build/update flow
        # (e.g. unit tests) can skip loading. An empty dict disables
        # substitution silently, which is the correct no-op behavior.
        if conn is not None:
            self.variables = _load_project_variables(conn, self.project_id)

    def find(self, rel_path: str) -> int | None:
        return self.by_path.get(rel_path)


# ---------------------------------------------------------------------------
# Core write API
# ---------------------------------------------------------------------------


def wipe_file(conn: Connection, source_file_id: int) -> None:
    """Delete all outbound edges originating from ``source_file_id``.

    Used by the indexer on a file that's about to be re-extracted. Called
    implicitly by :func:`insert_edges` — callers normally don't invoke
    this directly.
    """
    _db.execute(
        conn,
        "DELETE FROM file_edges WHERE source_file_id = ?",
        (source_file_id,),
    )


def resolve_edges(
    index: "FileIndex",
    source_file_id: int,
    source_rel: str,
    lang: str,
    edges: Iterable[Edge],
) -> list[tuple]:
    """Resolve a file's edges to ``file_edges`` row tuples — PURE, no DB.

    Returns rows in column order::

        (project_id, source_file_id, target_file_id, kind, raw, symbol, line)

    Applies the same drop/resolve rules as :func:`insert_edges`:

    * ``helm_define`` / ``tf_decl`` edges are dropped (metadata only).
    * ``unresolved`` kind or no resolver → ``target_file_id = None``.
    * Calls ``resolver_fn(e, index, source_rel)`` for resolvable edges.
    * Falls back to :func:`_try_substitute_and_resolve` when the resolver
      misses on an edge whose ``raw`` carries a template expression.
    * ``ansible_module`` edges that remain unresolved after all fallbacks
      are dropped (builtins / stdlib noise).

    ``project_id`` is taken from ``index.project_id``.  The function never
    touches the database.
    """
    edges = list(edges)
    if not edges:
        return []

    resolver_fn = _resolver_for(lang)
    rows: list[tuple] = []

    for e in edges:
        # ``helm_define`` is metadata consumed by FileIndex.prepare, not a
        # real dependency.  ``tf_decl`` is the Terraform analogue — both
        # consumed by prepare(), never written to file_edges.
        if e.kind in ("helm_define", "tf_decl"):
            continue

        if e.kind == "unresolved" or resolver_fn is None:
            target_id = None
        else:
            target_id = resolver_fn(e, index, source_rel)

        # Variable substitution fallback.
        if target_id is None and resolver_fn is not None and e.kind != "unresolved":
            target_id = _try_substitute_and_resolve(
                e, index, source_rel, resolver_fn
            )

        # Unresolved ansible_module = builtin / stdlib noise — drop.
        if e.kind == "ansible_module" and target_id is None:
            continue

        rows.append(
            (
                index.project_id,
                source_file_id,
                target_id,
                e.kind,
                e.raw,
                e.symbol,
                e.line,
            )
        )
    return rows


def extract_edges(
    raw_bytes: bytes,
    abs_path: Path,
    lang: str,
) -> list[Edge]:
    """Run the language resolver on one file. Returns [] if no resolver.

    Thin wrapper to centralize the ``dispatch_resolver`` call so indexer
    code stays symmetric with ``dispatch_chunker``. The dispatcher may
    consult ``abs_path`` to pick among multiple YAML flavors (Ansible,
    Helm, GitHub Actions, Kustomize) — the resolver itself still gets
    the path too for any per-file context it needs. Resolver errors
    bubble up; callers can catch to skip one bad file without failing
    the whole batch.
    """
    resolvers = dispatch_resolver(lang, abs_path)
    if not resolvers:
        return []
    edges: list[Edge] = []
    for resolver in resolvers:
        edges.extend(resolver.extract(raw_bytes, abs_path))
    return edges


def insert_edges(
    conn: Connection,
    index: FileIndex,
    source_file_id: int,
    source_rel: str,
    lang: str,
    edges: Iterable[Edge],
) -> int:
    """Resolve + persist a file's edges. Wipes prior edges first.

    Returns the number of rows inserted. Delegates resolution to the pure
    :func:`resolve_edges` helper, then writes each row individually.
    Edges with ``kind='unresolved'`` bypass resolution and land with
    ``target_file_id=NULL`` preserving their ``raw`` expression.
    Everything else runs through the language's resolver; misses land with
    ``target_file_id=NULL`` and are reported as ``external`` at query time.
    """
    wipe_file(conn, source_file_id)
    rows = resolve_edges(index, source_file_id, source_rel, lang, edges)
    for row in rows:
        _db.execute(
            conn,
            "INSERT INTO file_edges("
            "project_id, source_file_id, target_file_id, kind, raw, symbol, line"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Language-specific resolution
# ---------------------------------------------------------------------------


def _resolver_for(lang: str):
    """Return the per-edge resolver function for a language.

    One entry per edge ``kind`` produced by resolvers in that language,
    or one function per-language that switches on ``edge.kind``. The
    second form is cleaner for Phase 2 where several kinds share the
    source-relative-path resolution rule.
    """
    if lang == "python":
        return _resolve_python
    if lang in ("javascript", "typescript"):
        return _resolve_js
    if lang == "hcl":
        return _resolve_terraform
    if lang == "yaml":
        # YAML flavors all share one dispatcher that picks on edge.kind.
        return _resolve_yaml
    return None


def _resolve_python(edge: Edge, index: FileIndex, source_rel: str) -> int | None:
    """Try candidate rel_paths for a Python edge; return the first match.

    Candidates are generated per the import style (absolute vs relative)
    and the edge kind (plain ``import`` vs ``from_import``, which gets a
    two-interpretation treatment — ``from X import Y`` might mean "Y is
    a submodule file" or "Y is a name in X.py").
    """
    for c in _python_candidates(edge, source_rel):
        fid = index.find(c)
        if fid is not None:
            return fid
    return None


def _python_candidates(edge: Edge, source_rel: str) -> list[str]:
    raw = edge.raw
    if not raw:
        return []

    # Count leading dots. For Python, N leading dots = level N relative
    # import. Level 1 = same package. Level 2 = parent package. Etc.
    leading = 0
    for ch in raw:
        if ch != ".":
            break
        leading += 1
    rest = raw[leading:]  # may be "" for ``from . import x``

    source_dir = source_rel.rsplit("/", 1)[0] if "/" in source_rel else ""
    source_parts = source_dir.split("/") if source_dir else []

    if leading > 0:
        base = source_parts[:]
        # Pop (leading - 1) levels. ``from .foo`` stays in the source's
        # package dir; ``from ..foo`` goes up one; etc.
        for _ in range(leading - 1):
            if not base:
                return []  # went above project root — unresolvable
            base.pop()
    else:
        base = []

    module_parts = [p for p in rest.split(".") if p] if rest else []
    module_path = base + module_parts

    candidates: list[str] = []
    # ``from_import`` with a concrete symbol is ambiguous between two
    # interpretations: submodule file, or name inside the module file.
    # Try both (submodule first — if a name-import and a submodule
    # coexist, the submodule is what Python's import machinery prefers).
    if edge.kind == "from_import" and edge.symbol and edge.symbol != "*":
        sym_path = module_path + [edge.symbol]
        if sym_path:
            candidates.append("/".join(sym_path) + ".py")
            candidates.append("/".join(sym_path) + "/__init__.py")
        if module_path:
            candidates.append("/".join(module_path) + ".py")
            candidates.append("/".join(module_path) + "/__init__.py")
    else:
        if module_path:
            candidates.append("/".join(module_path) + ".py")
            candidates.append("/".join(module_path) + "/__init__.py")

    return candidates


def _resolve_js(edge: Edge, index: FileIndex, source_rel: str) -> int | None:
    for c in _js_candidates(edge, source_rel):
        fid = index.find(c)
        if fid is not None:
            return fid
    return None


# ---------------------------------------------------------------------------
# Terraform resolution
# ---------------------------------------------------------------------------


def _resolve_terraform(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    """Resolve Terraform edges.

    ``tf_module`` / ``tf_templatefile`` / ``tf_file`` share a path-based
    rule: the ``raw`` string, after stripping an optional
    ``${path.module}/`` prefix, is either absolute (URL, registry,
    ``git::`` — never a project file) or a POSIX path relative to the
    source file's directory. Modules resolve to a **directory** in the
    files table, which we approximate by looking for a sibling ``.tf``
    file at that path (any .tf in the module dir works — we pick the
    canonical ``main.tf`` first).

    ``tf_var_ref`` / ``tf_local_ref`` / ``tf_module_ref`` resolve through
    ``index.tf_decls[source_dir]`` — a name→file_id lookup scoped to
    the terraform root (directory). Misses land external.
    """
    raw = edge.raw
    if not raw:
        return None

    # Symbol-ref resolution via the directory-scoped decl map. Cheap
    # dict lookup — no path munging.
    if edge.kind in ("tf_var_ref", "tf_local_ref", "tf_module_ref"):
        source_dir = source_rel.rsplit("/", 1)[0] if "/" in source_rel else ""
        return index.tf_decls.get(source_dir, {}).get(raw)

    # ``${path.module}/...`` is the conventional prefix for "relative to
    # this file's module directory" in Terraform. Strip it; what remains
    # is an ordinary relative path.
    if raw.startswith("${path.module}/"):
        raw = raw[len("${path.module}/"):]
    elif raw.startswith("${path.root}/"):
        # Root-relative (rarer). Drop to project root.
        return _resolve_path_from_root(raw[len("${path.root}/"):], index, edge.kind)

    # Remote sources: registry (no leading slash, contains slash and ``/``
    # as namespace separator like ``hashicorp/vpc``) or explicit remote
    # prefixes. Without a leading ``.`` or ``/`` they're never project
    # files. Registry-style ``foo/bar`` addresses are indistinguishable
    # from a relative dir named ``foo/bar`` — we treat anything not
    # starting with ``.`` or ``/`` as remote for tf_module.
    if edge.kind == "tf_module":
        if not (raw.startswith(".") or raw.startswith("/")):
            return None
    else:
        # templatefile/file: no leading "./" is common for
        # ``templatefile("./tpl/foo.tftpl")`` — but bare paths like
        # ``templatefile("tpl/foo.tftpl")`` are also valid. Always
        # interpret as project-relative (relative to source dir).
        pass

    rel_target = _resolve_relative(source_rel, raw)
    if rel_target is None:
        return None

    if edge.kind == "tf_module":
        # A module source is a directory. Pick any .tf inside it as the
        # representative target — main.tf is the convention. Fall back
        # to any .tf under that dir that exists in the files table.
        return _pick_tf_in_dir(index, rel_target)

    # templatefile / file — raw is a file, use the resolved rel path as-is.
    return index.find(rel_target)


def _pick_tf_in_dir(index: FileIndex, dir_rel: str) -> int | None:
    """Return a representative file_id for a Terraform module directory.

    Tries ``<dir>/main.tf`` first (Terraform convention), then any other
    ``.tf`` file in the directory in alphabetical order. Returns None
    if the directory isn't populated in the files table — could be that
    the module sits outside the project, or the scanner skipped it.
    """
    prefix = dir_rel.rstrip("/") + "/"
    main = prefix + "main.tf"
    fid = index.find(main)
    if fid is not None:
        return fid
    # Fallback: any .tf under the module dir.
    candidates = sorted(
        p for p in index.by_path
        if p.startswith(prefix) and p.endswith(".tf")
    )
    if candidates:
        return index.by_path[candidates[0]]
    return None


def _resolve_path_from_root(
    raw: str, index: FileIndex, kind: str
) -> int | None:
    """Resolve ``raw`` as a project-root-relative path."""
    normalized = _normalize_posix(raw.lstrip("/"))
    if normalized is None:
        return None
    if kind == "tf_module":
        return _pick_tf_in_dir(index, normalized)
    return index.find(normalized)


# ---------------------------------------------------------------------------
# YAML dispatch (Ansible / Helm / GHA / Kustomize share one resolver fn)
# ---------------------------------------------------------------------------


def _resolve_yaml(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    """Pick the right per-kind resolver for YAML edges."""
    kind = edge.kind
    if kind.startswith("ansible_"):
        return _resolve_ansible(edge, index, source_rel)
    if kind.startswith("helm_"):
        return _resolve_helm(edge, index, source_rel)
    if kind.startswith("argocd_"):
        return _resolve_argocd(edge, index, source_rel)
    if kind.startswith("gha_"):
        return _resolve_gha(edge, index, source_rel)
    if kind.startswith("kustomize_"):
        return _resolve_kustomize(edge, index, source_rel)
    return None


# ---------------------------------------------------------------------------
# Ansible resolution
# ---------------------------------------------------------------------------


def _resolve_ansible(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    kind = edge.kind
    raw = edge.raw
    if not raw:
        return None

    # include_tasks / import_tasks / vars_files / include_vars /
    # import_playbook — path-relative to the source file's directory.
    if kind in (
        "ansible_include_tasks",
        "ansible_import_tasks",
        "ansible_vars_file",
        "ansible_include_vars",
        "ansible_import_playbook",
    ):
        rel_target = _resolve_relative(source_rel, raw)
        if rel_target is None:
            return None
        return index.find(rel_target)

    # Role references — resolved through roles_path. ``raw`` is the role
    # name (may contain slashes for the roles/argocd layout).
    # ``symbol`` is the optional ``tasks_from`` value.
    if kind in ("ansible_include_role", "ansible_import_role", "ansible_role_entry"):
        return _resolve_ansible_role(
            role_name=raw, tasks_from=edge.symbol, index=index
        )

    # Custom module: look up in the ansible_modules side-map.
    if kind == "ansible_module":
        return index.ansible_modules.get(raw)

    return None


def _resolve_ansible_role(
    role_name: str,
    tasks_from: str | None,
    index: FileIndex,
) -> int | None:
    """Resolve a role reference to its entry-point task file.

    For each configured ``roles_path`` dir, try
    ``<roles_path>/<role_name>/tasks/<tasks_from or main.yml>``. Role
    names with slashes (the ``roles/myrole`` layout) resolve to a nested
    path under roles_path naturally.
    """
    task_file = tasks_from if tasks_from else "main.yml"
    # Ansible also accepts main.yaml.
    variants = [task_file]
    if task_file.endswith(".yml"):
        variants.append(task_file[:-4] + ".yaml")
    elif task_file.endswith(".yaml"):
        variants.append(task_file[:-5] + ".yml")
    elif not task_file.endswith((".yml", ".yaml")):
        variants = [task_file + ".yml", task_file + ".yaml"]

    for roles_dir in index.ansible_roles_path:
        base = f"{roles_dir.strip('/')}/{role_name.strip('/')}"
        for variant in variants:
            candidate = f"{base}/tasks/{variant}"
            fid = index.find(candidate)
            if fid is not None:
                return fid
    return None


# ---------------------------------------------------------------------------
# Helm resolution
# ---------------------------------------------------------------------------


def _resolve_helm(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    kind = edge.kind
    if kind == "helm_define":
        # helm_define edges are never persisted; they're consumed by
        # FileIndex.prepare. If one reaches resolution, skip silently.
        return None
    if kind == "helm_dependency":
        # Three encodings from the resolver (see helm_resolver.py):
        #   raw=""       → no repository field; look in parent's charts/<name>/.
        #   raw=<path>   → file:// repository; path is relative to Chart.yaml.
        #   raw=<other>  → http(s)://, oci://, @alias — external.
        raw = edge.raw
        name = edge.symbol
        if raw and ("://" in raw or raw.startswith("@")):
            return None  # remote — external
        source_dir = source_rel.rsplit("/", 1)[0] if "/" in source_rel else ""
        # Case 1: no repository. Helm unpacks at <parent>/charts/<name>/.
        if not raw:
            return _find_chart_yaml(
                _join_dir(source_dir, f"charts/{name}"), index
            ) if name else None
        # Case 2: file:// path. Primary target is the path itself; fall
        # back to charts/<name> because `helm dep update` copies the
        # source into charts/<name> and some repos only commit the copy.
        subchart_dir = _resolve_relative(source_rel, raw)
        if subchart_dir is not None:
            fid = _find_chart_yaml(subchart_dir, index)
            if fid is not None:
                return fid
        if name:
            return _find_chart_yaml(
                _join_dir(source_dir, f"charts/{name}"), index
            )
        return None
    if kind == "helm_include":
        # Template-name → file, scoped to the source file's chart.
        chart_root = _find_helm_chart_root(source_rel, index)
        if chart_root is None:
            return None
        chart_map = index.helm_templates.get(chart_root)
        if chart_map is None:
            return None
        return chart_map.get(edge.raw)
    return None


# ---------------------------------------------------------------------------
# ArgoCD resolution
# ---------------------------------------------------------------------------


def _resolve_argocd(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    """Resolve ``argocd_app_source`` edges to a chart / kustomization /
    raw file inside this repo.

    The ``raw`` value is taken from ``spec.source.path`` / each
    ``spec.sources[*].path`` — a repo-relative directory path. Primary
    target is the chart or kustomization manifest at that directory;
    fall back to a direct file match for paths that point at an
    explicit file. Parametric paths (``charts/{{ .Values.x }}``) resolve
    to ``None`` — they'd need Helm runtime values to disambiguate.
    """
    raw = edge.raw
    if not raw:
        return None
    # Helm or Ansible template markers mean the path isn't literally
    # usable. Ansible-style ``${var.x}`` isn't expected in Application
    # specs but we still guard against it defensively.
    if "{{" in raw or "${" in raw:
        return None
    # Strip leading ``./`` and trailing slash for a canonical key.
    path = raw[2:] if raw.startswith("./") else raw
    path = path.rstrip("/")
    if not path:
        return None
    # ArgoCD App ``path:`` is always repo-root-relative, not source-
    # file-relative. Probe chart / kustomization manifests first, then
    # fall through to a direct file match.
    for name in (
        "Chart.yaml", "Chart.yml",
        "kustomization.yaml", "kustomization.yml",
    ):
        fid = index.find(f"{path}/{name}")
        if fid is not None:
            return fid
    return index.find(path)


def _find_chart_yaml(dir_path: str, index: FileIndex) -> int | None:
    """Return the file_id for ``<dir_path>/Chart.yaml`` or ``Chart.yml``."""
    for name in ("Chart.yaml", "Chart.yml"):
        candidate = f"{dir_path}/{name}" if dir_path else name
        fid = index.find(candidate)
        if fid is not None:
            return fid
    return None


def _join_dir(base: str, rel: str) -> str:
    """Join two project-relative POSIX dir paths, tolerating empty base."""
    return f"{base}/{rel}" if base else rel


def _find_helm_chart_root(source_rel: str, index: FileIndex) -> str | None:
    """Return the project-relative dir containing Chart.yaml for a file,
    or None if the file isn't inside a chart. Walks up from the source
    file's directory until a Chart.yaml is found.
    """
    parts = source_rel.split("/")
    # Strip the filename.
    parts = parts[:-1]
    while parts:
        candidate = "/".join(parts) + "/Chart.yaml"
        if candidate in index.by_path:
            return "/".join(parts)
        candidate_yml = "/".join(parts) + "/Chart.yml"
        if candidate_yml in index.by_path:
            return "/".join(parts)
        parts.pop()
    return None


# ---------------------------------------------------------------------------
# GitHub Actions resolution
# ---------------------------------------------------------------------------


def _resolve_gha(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    raw = edge.raw
    if not raw:
        return None
    if edge.kind == "gha_uses_external":
        return None  # always external by construction

    # ``lstrip("./")`` would strip BOTH ``.`` and ``/`` characters in
    # any order — turning ``./.github/x`` into ``github/x``. Use
    # prefix removal so we drop the leading ``./`` only.
    if raw.startswith("./"):
        path = raw[2:]
    else:
        path = raw
    if "@" in path:
        path = path.split("@", 1)[0]

    if edge.kind == "gha_uses_workflow":
        return index.find(path)

    if edge.kind == "gha_uses_action":
        # ``./.github/actions/<name>[/subpath]`` — resolves to
        # ``<that-path>/action.yml``.
        for candidate in (
            f"{path.rstrip('/')}/action.yml",
            f"{path.rstrip('/')}/action.yaml",
        ):
            fid = index.find(candidate)
            if fid is not None:
                return fid
    return None


# ---------------------------------------------------------------------------
# Kustomize resolution
# ---------------------------------------------------------------------------


def _resolve_kustomize(
    edge: Edge, index: FileIndex, source_rel: str
) -> int | None:
    raw = edge.raw
    if not raw:
        return None
    # Remote bases/resources (github.com/…, git::https://…) — external.
    if (
        "://" in raw
        or raw.startswith("github.com/")
        or raw.startswith("git@")
        or raw.startswith("git::")
    ):
        return None

    resolved = _resolve_relative(source_rel, raw)
    if resolved is None:
        return None

    # A kustomize ``resources:``/``bases:``/``components:`` entry can be
    # either a direct file or a directory containing a nested
    # kustomization.yaml. Check both.
    fid = index.find(resolved)
    if fid is not None:
        return fid
    for candidate in (
        f"{resolved}/kustomization.yaml",
        f"{resolved}/kustomization.yml",
    ):
        fid = index.find(candidate)
        if fid is not None:
            return fid
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_relative(source_rel: str, raw_path: str) -> str | None:
    """Resolve ``raw_path`` against the directory of ``source_rel``.

    Returns the normalized project-relative posix path, or None if
    resolution walks above the project root. Handles leading ``./``,
    ``../``, and absolute-within-project ``/foo`` (treated as root-
    relative — not really valid in most configs but some tools emit it).
    """
    if raw_path.startswith("/"):
        return _normalize_posix(raw_path.lstrip("/"))

    source_dir = source_rel.rsplit("/", 1)[0] if "/" in source_rel else ""
    combined = f"{source_dir}/{raw_path}" if source_dir else raw_path
    return _normalize_posix(combined)


def _normalize_posix(path: str) -> str | None:
    """Collapse ``./`` and ``../`` segments. Returns None on ``..`` escape."""
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


# ---------------------------------------------------------------------------
# FileIndex.prepare() helpers
# ---------------------------------------------------------------------------


def _find_ansible_cfgs(root: Path) -> list[tuple[str, dict[str, str]]]:
    """Return every ``ansible.cfg`` found in the project, paired with its
    [defaults] section values and the directory the cfg lives in
    (project-relative posix path, "" for the root).

    Ansible's own resolution order picks one cfg and interprets paths
    relative to that cfg's directory. We're more permissive: we collect
    every cfg we find and merge their path lists. That way a monorepo
    with ``ansible/ansible.cfg`` AND a separate ``tools/ansible.cfg``
    resolves role references under both trees.

    Walks with a shallow bound and skips obvious non-ansible dirs so big
    repos don't pay for a full crawl. The ``knowledge build`` flow
    already walks the tree for files; we could piggy-back on that but
    this is cheap enough (a dozen stat calls) to keep self-contained.
    """
    skip_dirs = {
        ".git", ".terraform", "_local", ".local", "node_modules",
        "__pycache__", ".venv", "venv", "dist", "build",
    }
    found: list[tuple[str, dict[str, str]]] = []

    def walk(dir_path: Path, depth: int) -> None:
        if depth > 6:
            return
        try:
            entries = list(dir_path.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and entry.name == "ansible.cfg":
                values = _parse_cfg_defaults(entry)
                if values:
                    rel_dir = (
                        entry.parent.relative_to(root).as_posix()
                        if entry.parent != root
                        else ""
                    )
                    found.append((rel_dir, values))
            elif entry.is_dir() and entry.name not in skip_dirs:
                walk(entry, depth + 1)

    walk(root, 0)
    return found


def _parse_cfg_defaults(cfg_path: Path) -> dict[str, str]:
    """Return the ``[defaults]`` section of one cfg, or ``{}`` on any parse
    issue. ``configparser`` is lenient enough for most ansible.cfg files.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(cfg_path, encoding="utf-8")
    except configparser.Error:
        return {}
    if "defaults" not in parser:
        return {}
    return {k: v for k, v in parser["defaults"].items()}


def _join_rel(base_rel: str, path: str) -> str:
    """Join a cfg's project-relative dir to a roles/library path string.

    Strips an optional ``./`` prefix from ``path`` and normalizes
    separators to posix. Result is also project-relative posix.
    """
    p = path.strip().lstrip("./")
    if not base_rel:
        return p
    if not p:
        return base_rel
    return f"{base_rel}/{p}"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _split_colon_list(
    value: str | None, default: tuple[str, ...]
) -> list[str]:
    """Split ``a:b:c`` (or ``a,b,c``) into a list. Fall back to ``default``
    when value is missing. Whitespace and empty segments stripped.
    """
    if not value:
        return list(default)
    # Ansible roles_path uses colons; some configs use commas. Support both.
    pieces = [p.strip() for p in value.replace(",", ":").split(":")]
    cleaned = [p for p in pieces if p]
    return cleaned or list(default)


def _scan_ansible_modules(
    by_path: dict[str, int],
    module_dirs: list[str],
) -> dict[str, int]:
    """Build the name→file_id map for custom Ansible modules.

    For each configured dir (``library/``, ``action_plugins/``, …), find
    ``.py`` files in ``by_path`` and key them by stem. Non-module files
    (``__init__.py``, anything starting with ``_``) are skipped. Later
    dirs override earlier ones when names collide (rare).
    """
    result: dict[str, int] = {}
    for module_dir in module_dirs:
        prefix = module_dir.strip("/") + "/"
        for rel_path, file_id in by_path.items():
            if not rel_path.startswith(prefix):
                continue
            if not rel_path.endswith(".py"):
                continue
            name = rel_path.rsplit("/", 1)[-1][:-3]  # strip ".py"
            if not name or name.startswith("_"):
                continue
            result[name] = file_id
    return result


def _try_substitute_and_resolve(
    edge: Edge,
    index: FileIndex,
    source_rel: str,
    resolver_fn,
) -> int | None:
    """Attempt variable substitution on an edge whose initial resolution
    missed. Returns the resolved target_file_id or None if substitution
    didn't fully cover the template (still parametric) or the resolver
    still fails on the substituted path.

    Scope/syntax mapping lives in ``variables``; we re-import it here
    lazily to avoid a hard import cycle (variables imports relations
    inside its ``apply_variables`` function).
    """
    from .variables import (
        has_template_markers,
        resolve_scoped_vars,
        substitute,
        _syntax_and_scope_for_kind,
    )

    raw_has = has_template_markers(edge.raw)
    symbol_has = edge.symbol is not None and has_template_markers(edge.symbol)
    if not (raw_has or symbol_has):
        return None
    syntax, scope = _syntax_and_scope_for_kind(edge.kind)
    if syntax is None:
        return None
    scoped = resolve_scoped_vars(index.variables, scope)
    if not scoped:
        return None

    new_raw = edge.raw
    new_symbol = edge.symbol
    fully = True
    if raw_has:
        new_raw, raw_ok = substitute(edge.raw, scoped, syntax)
        fully = fully and raw_ok
    if symbol_has and edge.symbol is not None:
        # ``tasks_from`` on an include_role lives in symbol and can
        # carry Jinja too (``{{ mg_step | regex_replace(...) }}``).
        # Resolving the role_name but not tasks_from would produce a
        # wrong target, so require BOTH to fully substitute.
        new_symbol, sym_ok = substitute(edge.symbol, scoped, syntax)
        fully = fully and sym_ok
    if not fully:
        return None
    # Rebuild the edge with the substituted raw and hand it back to the
    # resolver. Keep kind/line intact so the resolver's per-kind branch
    # routes correctly.
    substituted = Edge(
        kind=edge.kind,
        raw=new_raw,
        symbol=new_symbol,
        line=edge.line,
    )
    return resolver_fn(substituted, index, source_rel)


def _load_project_variables(
    conn: Connection, project_id: int
) -> dict[str, dict[str, str]]:
    """Load ``project_variables`` rows into the ``{scope: {name: value}}``
    shape ``FileIndex.variables`` expects. Empty when the table has no
    rows for this project — callers treat empty as "no substitution".
    """
    rows = _db.fetch_all(
        conn,
        "SELECT scope, name, value FROM project_variables "
        "WHERE project_id = ?",
        (project_id,),
    )
    out: dict[str, dict[str, str]] = {}
    for scope, name, value in rows:
        out.setdefault(scope, {})[name] = value
    return out


def _collect_tf_decls(
    pending: list[tuple[int, str, str, list[Edge]]],
) -> dict[str, dict[str, int]]:
    """Fold ``tf_decl`` edges into a per-directory name→file_id map.

    Terraform's scope is the directory (a root module or child module),
    so we key on the source file's dirname. Multiple files in the same
    dir can declare different vars/locals/modules; one ``variable "X"``
    should only exist once per dir but the map is last-write-wins if
    it doesn't (matching Terraform's own error-tolerant parse behavior).
    """
    result: dict[str, dict[str, int]] = {}
    for file_id, rel_path, _lang, edges in pending:
        dir_key = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        for e in edges:
            if e.kind != "tf_decl":
                continue
            if not e.raw:
                continue
            result.setdefault(dir_key, {})[e.raw] = file_id
    return result


def _collect_helm_defines(
    pending: list[tuple[int, str, str, list[Edge]]],
) -> dict[str, dict[str, int]]:
    """Fold ``helm_define`` edges from ``pending`` into a chart-scoped
    name→file_id map.

    Chart root per edge is computed from the source file's rel_path by
    walking up to the nearest ``templates/`` ancestor and using that
    ancestor's parent as the chart root. Two defines with the same name
    in the same chart → the last one wins (Helm's runtime semantics
    allow this and there's no canonical tiebreaker).
    """
    result: dict[str, dict[str, int]] = {}
    for file_id, rel_path, _lang, edges in pending:
        chart_root = _chart_root_from_template_path(rel_path)
        if chart_root is None:
            continue
        for e in edges:
            if e.kind != "helm_define":
                continue
            result.setdefault(chart_root, {})[e.raw] = file_id
    return result


def _chart_root_from_template_path(rel_path: str) -> str | None:
    """For a ``foo/bar/templates/baz.tpl``-shaped path, return ``foo/bar``.

    Returns None if the path has no ``templates/`` ancestor (shouldn't
    happen for files classified as ``helm_template`` but we're defensive).
    """
    parts = rel_path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "templates":
            if i == 0:
                return ""  # templates/ at root — odd but allowed
            return "/".join(parts[:i])
    return None


def _js_candidates(edge: Edge, source_rel: str) -> list[str]:
    raw = edge.raw
    if not raw:
        return []
    # Bare specifier (no leading . or /) → package import, not a project
    # file. Phase 1 treats these as external; tsconfig paths aliases are
    # a Phase 2 item.
    if not (raw.startswith(".") or raw.startswith("/")):
        return []

    source_dir = source_rel.rsplit("/", 1)[0] if "/" in source_rel else ""
    source_parts = source_dir.split("/") if source_dir else []

    # Absolute-path specifiers ("/foo/bar") are rare in real code and
    # usually refer to a bundler root alias. Treat like a bare specifier
    # for now — external.
    if raw.startswith("/"):
        return []

    # Resolve ../ and ./ segments against the source file's directory.
    target_parts = source_parts[:]
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if target_parts:
                target_parts.pop()
            else:
                return []  # above project root
        else:
            target_parts.append(part)

    if not target_parts:
        return []
    base = "/".join(target_parts)
    last = target_parts[-1]

    # Explicit extension? Try exact first. ``import x from './foo.json'``
    # is a real pattern (bundlers allow it).
    if any(last.endswith(ext) for ext in _JS_KNOWN_EXTS):
        return [base]

    candidates: list[str] = []
    for ext in _JS_RESOLVE_EXTS:
        candidates.append(base + ext)
    for ext in _JS_RESOLVE_EXTS:
        candidates.append(base + "/index" + ext)
    return candidates


# ---------------------------------------------------------------------------
# Read API (CLI consumers)
# ---------------------------------------------------------------------------


@dataclass
class EdgeRow:
    """One resolved edge as the CLI presents it. JSON-friendly."""

    source_file_id: int
    source_rel: str
    target_file_id: int | None
    target_rel: str | None
    kind: str
    raw: str
    symbol: str | None
    line: int | None


def get_forward(
    conn: Connection,
    file_id: int,
    depth: int = 1,
    kinds: set[str] | None = None,
) -> list[EdgeRow]:
    """Outbound edges from ``file_id``.

    ``depth=1`` returns only the direct outbound set. ``depth>1`` follows
    resolved edges transitively, with a seen-set to bound cycles. External
    and unresolved edges contribute to depth 1 only — they have no
    ``target_file_id`` to follow.
    """
    if depth < 1:
        return []
    results: list[EdgeRow] = []
    seen: set[int] = {file_id}
    frontier: list[int] = [file_id]
    for _ in range(depth):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = _db.fetch_all(
            conn,
            f"""
            SELECT e.source_file_id, sf.rel_path,
                   e.target_file_id,  tf.rel_path,
                   e.kind, e.raw, e.symbol, e.line
            FROM file_edges e
            JOIN files sf        ON sf.id = e.source_file_id
            LEFT JOIN files tf   ON tf.id = e.target_file_id
            WHERE e.source_file_id IN ({placeholders})
            ORDER BY sf.rel_path, e.line, e.raw
            """,
            tuple(frontier),
        )

        next_frontier: list[int] = []
        for r in rows:
            edge = _row_to_edgerow(r)
            if kinds is not None and edge.kind not in kinds:
                continue
            results.append(edge)
            if edge.target_file_id is not None and edge.target_file_id not in seen:
                seen.add(edge.target_file_id)
                next_frontier.append(edge.target_file_id)
        frontier = next_frontier
    return results


def get_reverse(
    conn: Connection,
    file_id: int,
    depth: int = 1,
    kinds: set[str] | None = None,
) -> list[EdgeRow]:
    """Inbound edges into ``file_id`` (who imports this file).

    Mirrors :func:`get_forward` but walks the reverse graph. Only resolved
    edges participate (an external or unresolved edge has no file source
    to report — well, actually the source is always a real file, so the
    distinction here is: every edge pointing INTO ``file_id`` has a
    concrete source; we recurse by looking at who imports the sources).
    """
    if depth < 1:
        return []
    results: list[EdgeRow] = []
    seen: set[int] = {file_id}
    frontier: list[int] = [file_id]
    for _ in range(depth):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = _db.fetch_all(
            conn,
            f"""
            SELECT e.source_file_id, sf.rel_path,
                   e.target_file_id,  tf.rel_path,
                   e.kind, e.raw, e.symbol, e.line
            FROM file_edges e
            JOIN files sf        ON sf.id = e.source_file_id
            LEFT JOIN files tf   ON tf.id = e.target_file_id
            WHERE e.target_file_id IN ({placeholders})
            ORDER BY sf.rel_path, e.line
            """,
            tuple(frontier),
        )

        next_frontier: list[int] = []
        for r in rows:
            edge = _row_to_edgerow(r)
            if kinds is not None and edge.kind not in kinds:
                continue
            results.append(edge)
            if edge.source_file_id not in seen:
                seen.add(edge.source_file_id)
                next_frontier.append(edge.source_file_id)
        frontier = next_frontier
    return results


def stats(conn: Connection, project_id: int | None = None) -> dict:
    """Aggregate counts for sanity-checking the graph.

    ``project_id=None`` is cross-project (mainly useful on a workstation
    with one project). Output is plain dict so CLI JSON-encodes directly.

    The ``parametric`` bucket is new in Phase 3: edges whose ``raw``
    carries unsatisfied Jinja / Terraform template markers. Before Phase
    3 these hid under ``unresolved`` (resolver returned the edge that
    way) or ``external`` (resolver returned NULL). Surfacing them
    distinctly makes it obvious how many edges a fresh set of
    ``knowledge vars set`` could unlock.
    """
    proj_clause = ""
    params: tuple = ()
    if project_id is not None:
        proj_clause = "WHERE project_id = ?"
        params = (project_id,)

    total = _db.fetch_one(
        conn,
        f"SELECT COUNT(*) FROM file_edges {proj_clause}",
        params,
    )[0]
    resolved = _db.fetch_one(
        conn,
        f"SELECT COUNT(*) FROM file_edges "
        f"{proj_clause} {'AND' if proj_clause else 'WHERE'} "
        f"target_file_id IS NOT NULL",
        params,
    )[0]
    # Parametric = NULL target AND kind != 'unresolved' AND raw carries
    # template markers. Done in SQL via ``LIKE`` rather than the
    # more precise ``has_template_markers`` regex — good enough for
    # counting and avoids pulling every row into Python.
    parametric = _db.fetch_one(
        conn,
        f"SELECT COUNT(*) FROM file_edges "
        f"{proj_clause} {'AND' if proj_clause else 'WHERE'} "
        f"target_file_id IS NULL AND kind != 'unresolved' "
        f"AND (raw LIKE '%{{{{%' OR raw LIKE '%${{var.%')",
        params,
    )[0]
    by_kind_rows = _db.fetch_all(
        conn,
        f"SELECT kind, COUNT(*) FROM file_edges {proj_clause} GROUP BY kind",
        params,
    )

    by_kind = {k: c for k, c in by_kind_rows}
    unresolved = by_kind.get("unresolved", 0)
    # external = NULL target, not unresolved, no template markers.
    # (resolver tried and genuinely missed — stdlib / third-party / remote).
    external = total - resolved - unresolved - parametric
    return {
        "edges": total,
        "resolved": resolved,
        "external": external,
        "parametric": parametric,
        "unresolved": unresolved,
        "by_kind": by_kind,
    }


def find_file_id(
    conn: Connection,
    project_id: int,
    rel_path: str,
) -> int | None:
    """Look up a file's id by its project-relative path. None if missing."""
    row = _db.fetch_one(
        conn,
        "SELECT id FROM files WHERE project_id = ? AND rel_path = ?",
        (project_id, rel_path),
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_edgerow(r) -> EdgeRow:
    return EdgeRow(
        source_file_id=r[0],
        source_rel=r[1],
        target_file_id=r[2],
        target_rel=r[3],
        kind=r[4],
        raw=r[5],
        symbol=r[6],
        line=r[7],
    )
