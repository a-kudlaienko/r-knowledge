"""Command-line entry point.

``pyproject.toml`` registers this module as the ``knowledge`` console_script.
Each subcommand is one ``cmd_*`` function that takes parsed argparse ``args``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

# tree-sitter-languages 1.10.x calls Language(path, name), which tree-sitter
# deprecated in 0.21. We pin to that combo (see tree-sitter-abi-pin memory),
# so the warning fires on every invocation — silence it at the CLI entry.
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message=r".*Language\(path, name\) is deprecated.*",
)

from . import db, paths, projects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledge",
        description="Local semantic code search (multi-repo, SQLite-backed).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # build
    p_build = sub.add_parser("build", help="Full index of the current project")
    p_build.add_argument("--name", help="Override project name (default: git root dir name)")

    # update
    sub.add_parser("update", help="Incremental reindex (default for routine use)")

    # status
    p_status = sub.add_parser("status", help="Report registered/fresh/stale/missing")
    p_status.add_argument("--json", action="store_true", help="Machine-readable output")

    # search
    p_search = sub.add_parser("search", help="Semantic search")
    p_search.add_argument("query", help="Natural-language or code-fragment query")
    p_search.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_search.add_argument("--all-projects", action="store_true", help="Search across all projects")
    p_search.add_argument("--kind", help="Filter by chunk kind (function, resource, …)")
    p_search.add_argument("--lang", help="Filter by language")
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--with-siblings", action="store_true")
    p_search.add_argument("--json", action="store_true")

    # get
    p_get = sub.add_parser("get", help="Fetch a chunk by id")
    p_get.add_argument("chunk_id", type=int)
    p_get.add_argument("--with-siblings", action="store_true")
    p_get.add_argument("--raw", action="store_true", help="Re-slice original bytes from disk")

    # path
    p_path = sub.add_parser("path", help="Print file_path:start_line-end_line for a chunk")
    p_path.add_argument("chunk_id", type=int)

    # projects
    sub.add_parser("projects", help="List registered projects")

    # stats
    p_stats = sub.add_parser("stats", help="DB + project statistics")
    p_stats.add_argument("--project", help="Scope stats to one project")

    # forget
    p_forget = sub.add_parser("forget", help="Delete a project and all its chunks")
    p_forget.add_argument("project", help="Project name or absolute path")

    # history (nested subcommands)
    p_history = sub.add_parser("history", help="Work-summary storage (RAG memory)")
    p_h_sub = p_history.add_subparsers(dest="history_cmd", required=True)

    p_h_add = p_h_sub.add_parser("add", help="Add one entry directly")
    p_h_add.add_argument("--short", required=True, help="One-line summary (~160 chars)")
    p_h_add.add_argument("--long", required=True, help="Detailed summary with file refs + rationale")
    p_h_add.add_argument("--tags", help="Optional comma-separated tags")
    p_h_add.add_argument("--session-id", help="Optional session identifier")

    p_h_ingest = p_h_sub.add_parser("ingest", help="Flush staged JSONL entries into SQLite")
    p_h_ingest.add_argument(
        "--stage-file",
        help="Path to the JSONL stage (default: ~/.knowledge/stage/pending.jsonl)",
    )

    p_h_recent = p_h_sub.add_parser("recent", help="Recent entries (no semantic search)")
    p_h_recent.add_argument("--days", type=int, help="Only entries from the last N days")
    p_h_recent.add_argument("--limit", type=int, default=20)
    p_h_recent.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_h_recent.add_argument("--all-projects", action="store_true")
    p_h_recent.add_argument("--json", action="store_true")

    p_h_search = p_h_sub.add_parser("search", help="Semantic search over short summaries")
    p_h_search.add_argument("query")
    p_h_search.add_argument("--top-k", type=int, default=10)
    p_h_search.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_h_search.add_argument("--all-projects", action="store_true")
    p_h_search.add_argument("--json", action="store_true")

    p_h_get = p_h_sub.add_parser("get", help="Fetch full entry by id")
    p_h_get.add_argument("history_id", type=int)
    p_h_get.add_argument("--json", action="store_true")

    # relations
    p_rel = sub.add_parser(
        "relations",
        help="Dependency graph (file-to-file imports). LLM-friendly JSON output.",
    )
    p_rel.add_argument(
        "target",
        help="File path (relative to repo root, absolute, or relative to cwd), "
             "OR the literal word 'stats' for an edge-count summary.",
    )
    p_rel.add_argument(
        "--direction",
        choices=("forward", "reverse", "both"),
        default="both",
        help="Direction of edges to include (default: both). Ignored for 'stats'.",
    )
    p_rel.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Transitive depth (default 1 = direct edges only). Ignored for 'stats'.",
    )
    p_rel.add_argument(
        "--kinds",
        help="Comma-separated kinds to include "
             "(import, from_import, require, dynamic_import, external, unresolved)",
    )
    p_rel.add_argument(
        "--project",
        help="Scope to a specific project (name or abs path)",
    )
    p_rel.add_argument("--all-projects", action="store_true", help="Stats across all projects")
    p_rel.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (default: compact, optimal for LLM consumption)",
    )

    # vars — per-project variable table for Jinja / Terraform path
    # substitution during edge resolution. See knowledge/variables.py.
    p_vars = sub.add_parser(
        "vars",
        help="Per-project variable table (Jinja + Terraform substitution).",
    )
    p_v_sub = p_vars.add_subparsers(dest="vars_cmd", required=True)

    p_v_set = p_v_sub.add_parser(
        "set",
        help="Set one or more variables for the current project. "
             "Auto-applies to existing edges.",
    )
    p_v_set.add_argument(
        "scope",
        choices=sorted(("ansible", "terraform", "helm", "all")),
        help="Domain the variables apply to (or 'all' as a catch-all).",
    )
    p_v_set.add_argument(
        "pairs",
        nargs="+",
        metavar="K=V",
        help="One or more name=value pairs (use quotes for values with spaces).",
    )
    p_v_set.add_argument("--project", help="Scope to a specific project (name or abs path)")

    p_v_unset = p_v_sub.add_parser(
        "unset",
        help="Remove a variable (or --all to clear a whole scope).",
    )
    p_v_unset.add_argument(
        "scope",
        choices=sorted(("ansible", "terraform", "helm", "all")),
    )
    p_v_unset.add_argument("name", nargs="?", help="Variable name (omit with --all)")
    p_v_unset.add_argument(
        "--all",
        dest="unset_all",
        action="store_true",
        help="Clear every variable in the given scope",
    )
    p_v_unset.add_argument("--project", help="Scope to a specific project (name or abs path)")

    p_v_list = p_v_sub.add_parser(
        "list",
        help="List variables for the current project.",
    )
    p_v_list.add_argument(
        "--scope",
        choices=sorted(("ansible", "terraform", "helm", "all")),
        help="Filter by scope",
    )
    p_v_list.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_v_list.add_argument("--json", action="store_true")

    p_v_import = p_v_sub.add_parser(
        "import",
        help="Bulk set from a JSON object file. Auto-applies.",
    )
    p_v_import.add_argument(
        "scope",
        choices=sorted(("ansible", "terraform", "helm", "all")),
    )
    p_v_import.add_argument("file", help="Path to a JSON file with {name: value} entries")
    p_v_import.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # graph — HTML visualization of the dependency graph.
    p_graph = sub.add_parser(
        "graph",
        help="Render the project's file-dependency graph as a static HTML "
             "(vis-network). One project per run.",
    )
    p_graph.add_argument(
        "--output",
        "-o",
        help="Path to write the HTML file (default: ./relations_graph.html)",
    )
    p_graph.add_argument(
        "--project",
        help="Scope to a specific project (name or abs path). Defaults to "
             "the current git root.",
    )
    p_graph.add_argument(
        "--include-external",
        action="store_true",
        help="Include stdlib / third-party edges as synthetic gray nodes.",
    )
    p_graph.add_argument(
        "--include-parametric",
        action="store_true",
        help="Include edges waiting for variables as synthetic yellow nodes.",
    )
    p_graph.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Include kind='unresolved' edges (non-literal dynamic imports).",
    )
    p_graph.add_argument(
        "--open",
        action="store_true",
        help="Launch the default web browser on the rendered file.",
    )

    # install-skill
    p_install = sub.add_parser(
        "install-skill",
        help="Copy SKILL.md into .claude/skills/knowledge/ (project or user)",
    )
    p_install.add_argument(
        "--user",
        action="store_true",
        help="Install to ~/.claude/skills/knowledge/ instead of cwd",
    )
    p_install.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink the source instead of copying (auto-updates on git pull)",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing SKILL.md at the target",
    )

    # install-hooks
    p_hooks = sub.add_parser(
        "install-hooks",
        help="Register Stop + PreCompact + SessionEnd hooks for auto-ingest",
    )
    p_hooks.add_argument(
        "--user",
        action="store_true",
        help="Install to ~/.claude/settings.json instead of cwd",
    )
    p_hooks.add_argument(
        "--absolute",
        action="store_true",
        help="Use the absolute path to `knowledge` (robust against PATH quirks on GUI launches)",
    )

    args = parser.parse_args(argv)
    return _DISPATCH[args.cmd](args)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    from . import indexer

    root = projects.current_project_root()
    proposed_name = args.name or root.name
    print(f"building index for: {root}", flush=True)

    with db.connect() as conn:
        # Rebuild in place? If a row already exists at this exact root, it's
        # not a collision — just a re-build of the same project. Skip the
        # collision check so the user isn't prompted on every rebuild.
        existing_here = conn.execute(
            "SELECT 1 FROM projects WHERE root_path = ? LIMIT 1",
            (str(root.resolve()),),
        ).fetchone()

        resolved_name = proposed_name
        ids_to_forget: list[int] = []

        if not existing_here:
            matches = projects.list_projects_by_name(conn, proposed_name)
            if matches:
                if not sys.stdin.isatty():
                    print(
                        f"error: short name '{proposed_name}' already used at "
                        f"{len(matches)} other location"
                        f"{'s' if len(matches) != 1 else ''}:",
                        file=sys.stderr,
                    )
                    for m in matches:
                        print(f"  - {m.root_path}", file=sys.stderr)
                    print(
                        "re-run with --name <custom> to pick a different name, "
                        "or run interactively to choose replace/suffix.",
                        file=sys.stderr,
                    )
                    return 1

                suffix_candidate = projects.next_free_suffix(conn, proposed_name)
                choice = _prompt_collision_resolution(
                    proposed_name, root, matches, suffix_candidate
                )
                if choice is None:
                    print("aborted.", file=sys.stderr)
                    return 1
                resolved_name, ids_to_forget = choice

        for old_id in ids_to_forget:
            projects.forget_project(conn, old_id)

        if resolved_name != proposed_name:
            print(f"registering as: {resolved_name}", flush=True)

        t0 = time.time()
        project_id, files, chunks = indexer.build_project(
            conn, root, name_override=resolved_name
        )
    elapsed = time.time() - t0
    print(f"done: {files} files, {chunks} chunks in {elapsed:.1f}s (project_id={project_id})")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from . import indexer

    root = projects.current_project_root()
    print(f"updating index for: {root}", flush=True)
    t0 = time.time()
    with db.connect() as conn:
        project_id, files_visited, chunks_embedded = indexer.update_project(
            conn, root, name_override=None
        )
    elapsed = time.time() - t0
    print(
        f"done: {files_visited} files visited, {chunks_embedded} chunks "
        f"re-embedded in {elapsed:.1f}s (project_id={project_id})"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Exit codes: 0 = fresh, 1 = stale, 2 = missing. Skill branches on these.

    Staleness check: stat every file the DB knows about and compare mtime
    to its stored value; return stale on the first mismatch or missing
    file. Early-exit keeps the common "everything's fine" path fast.

    New files added to the project since the last index aren't detected
    here (that would require a full ``walk_project``). Callers that want
    strict detection can run ``knowledge update`` anyway — it's cheap when
    nothing has changed.
    """
    t0 = time.time()
    with db.connect() as conn:
        proj = projects.resolve_project(conn, None)
        if proj is None:
            elapsed_ms = (time.time() - t0) * 1000
            payload = {
                "state": "missing",
                "project": None,
                "elapsed_ms": round(elapsed_ms, 1),
            }
            if args.json:
                print(json.dumps(payload))
            else:
                print("state: missing")
                print(f"check_time: {elapsed_ms:.1f}ms")
            return 2

        is_stale = _project_is_stale(conn, proj)
        state = "stale" if is_stale else "fresh"
        exit_code = 1 if is_stale else 0

    elapsed_ms = (time.time() - t0) * 1000
    payload = {
        "state": state,
        "project": proj.name,
        "root": str(proj.root_path),
        "last_update": proj.last_update,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"state: {state}")
        print(f"project: {proj.name} ({proj.root_path})")
        if proj.last_update:
            from datetime import datetime
            dt = datetime.fromtimestamp(proj.last_update)
            print(f"last_update: {dt.isoformat(sep=' ', timespec='seconds')}")
        print(f"check_time: {elapsed_ms:.1f}ms")
    return exit_code


def _project_is_stale(conn, proj) -> bool:
    """Return True if any tracked file has a newer mtime than we stored
    or is missing from disk. Early-exits on first stale finding."""
    from . import config as _config

    rows = conn.execute(
        "SELECT rel_path, mtime FROM files WHERE project_id = ?",
        (proj.id,),
    ).fetchall()

    root = proj.root_path
    grace = _config.STALE_GRACE_SECONDS
    for rel, stored_mtime in rows:
        abs_path = root / rel
        try:
            current = abs_path.stat().st_mtime
        except OSError:
            return True  # file gone → stale
        if current > stored_mtime + grace:
            return True
    return False


def cmd_search(args: argparse.Namespace) -> int:
    from . import search as search_mod

    with db.connect() as conn:
        project_id: int | None = None
        if not args.all_projects:
            try:
                proj = projects.resolve_project(conn, args.project)
            except projects.AmbiguousProjectName as exc:
                _print_ambiguous(exc)
                return 1
            if proj is None:
                where = args.project or str(projects.current_project_root())
                print(
                    f"error: project not registered: {where}\n"
                    "run 'knowledge build' from the repo root first.",
                    file=sys.stderr,
                )
                return 1
            project_id = proj.id

        results = search_mod.search(
            conn,
            query=args.query,
            project_id=project_id,
            kind=args.kind,
            lang=args.lang,
            top_k=args.top_k,
        )

    if args.json:
        print(json.dumps([r._asdict() for r in results], indent=2, default=str))
    else:
        _print_results_pretty(results)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    from . import search as search_mod

    with db.connect() as conn:
        if args.with_siblings:
            family = search_mod.get_family(conn, args.chunk_id)
            if not family:
                print(f"error: no chunk with id {args.chunk_id}", file=sys.stderr)
                return 1
            return _emit_family(family, raw=args.raw)

        row = search_mod.get_chunk(conn, args.chunk_id)
    if row is None:
        print(f"error: no chunk with id {args.chunk_id}", file=sys.stderr)
        return 1

    (cid, kind, name, qname, sl, el, sb, eb, stored, rel_path, project_root,
     _parent_id) = row
    abs_path = Path(project_root) / rel_path

    if args.raw:
        try:
            with open(abs_path, "rb") as f:
                f.seek(sb)
                data = f.read(eb - sb)
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            if not data.endswith(b"\n"):
                sys.stdout.write("\n")
        except OSError as exc:
            print(f"error: cannot read {abs_path}: {exc}", file=sys.stderr)
            return 1
    else:
        header = f"chunk {cid}: {kind} {qname or name or ''} ({rel_path}:{sl}-{el})"
        print(header)
        print("-" * len(header))
        print(stored)
    return 0


def _emit_family(family: list, *, raw: bool) -> int:
    """Render a big_parent's family (parent + ordered big_subchunks).

    ``family[0]`` is the parent (``ORDER BY CASE … THEN -1``). ``--raw``
    re-slices the parent's full byte span from disk — that IS the exact
    original. Without ``--raw``, we print the parent summary followed by
    each subchunk's stored (sanitized + whitespace-compressed) text.
    """
    from .whitespace import decompress

    parent = family[0]
    (_, kind, name, _sl, _el, sb, eb, _stored, rel_path, project_root,
     _sib) = parent
    abs_path = Path(project_root) / rel_path

    if raw:
        # Parent's byte range covers the whole original — one disk read
        # reassembles exactly what was on disk at build time.
        try:
            with open(abs_path, "rb") as f:
                f.seek(sb)
                data = f.read(eb - sb)
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            if not data.endswith(b"\n"):
                sys.stdout.write("\n")
        except OSError as exc:
            print(f"error: cannot read {abs_path}: {exc}", file=sys.stderr)
            return 1
        return 0

    header = f"chunk {parent[0]}: {kind} {name or ''} ({rel_path}:{parent[3]}-{parent[4]})"
    print(header)
    print("=" * len(header))
    print(decompress(parent[7]))
    if len(family) > 1:
        print()
        print(f"--- {len(family) - 1} subchunk(s) ---")
        for sub in family[1:]:
            (sub_id, _k, _n, sub_sl, sub_el, _sb, _eb, sub_stored,
             _rp, _pr, sib) = sub
            print(f"\n[{sib}] chunk {sub_id} (lines {sub_sl}-{sub_el}):")
            print(decompress(sub_stored))
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    from . import search as search_mod

    with db.connect() as conn:
        row = search_mod.get_chunk(conn, args.chunk_id)
    if row is None:
        print(f"error: no chunk with id {args.chunk_id}", file=sys.stderr)
        return 1
    (_cid, _kind, _name, _qname, sl, el, _sb, _eb, _stored, rel_path, project_root,
     _parent_id) = row
    print(f"{Path(project_root) / rel_path}:{sl}-{el}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = projects.list_projects(conn)
    if not rows:
        print("No projects registered. Run `knowledge build` from a repo root.")
        return 0
    # Tabular, no external deps.
    header = f"{'NAME':<24} {'FILES':>6} {'CHUNKS':>7}  ROOT"
    print(header)
    print("-" * len(header))
    for p in rows:
        print(f"{p.name:<24} {p.file_count:>6} {p.chunk_count:>7}  {p.root_path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = projects.list_projects(conn)
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"DB:          {paths.db_path()}")
    print(f"DB size:     {_format_size(paths.db_path())}")
    print(f"Projects:    {len(rows)}")
    print(f"Files:       {total_files}")
    print(f"Chunks:      {total_chunks}")
    if rows:
        print("")
        for p in rows:
            print(
                f"  - {p.name:<20}  files={p.file_count:<5} "
                f"chunks={p.chunk_count:<6} {p.root_path}"
            )
    else:
        print("\nNo projects registered. Run `knowledge build` from a repo root.")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            proj = projects.resolve_project(conn, args.project)
        except projects.AmbiguousProjectName as exc:
            if not sys.stdin.isatty():
                _print_ambiguous(exc)
                return 1
            chosen = _prompt_forget_selection(exc)
            if not chosen:
                print("aborted.", file=sys.stderr)
                return 1
            for p in chosen:
                projects.forget_project(conn, p.id)
                print(f"forgot: {p.name} ({p.root_path})")
            return 0
        if proj is None:
            print(f"error: project not found: {args.project}", file=sys.stderr)
            return 1
        projects.forget_project(conn, proj.id)
    print(f"forgot: {proj.name} ({proj.root_path})")
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    """Copy (or symlink) the bundled SKILL.md into the project or user skills dir."""
    import shutil

    src = Path(__file__).resolve().parent.parent / "skill-template" / "SKILL.md"
    if not src.is_file():
        print(
            f"error: skill template not found at {src}\n"
            "expected the repo-knowledge repo layout (editable install).",
            file=sys.stderr,
        )
        return 1

    if args.user:
        target_dir = Path.home() / ".claude" / "skills" / "knowledge"
    else:
        target_dir = Path.cwd() / ".claude" / "skills" / "knowledge"
    target = target_dir / "SKILL.md"

    if target.exists() or target.is_symlink():
        if not args.force:
            print(
                f"error: {target} already exists. Re-run with --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        target.unlink()

    target_dir.mkdir(parents=True, exist_ok=True)

    if args.symlink:
        target.symlink_to(src)
        mode = f"symlinked → {src}"
    else:
        shutil.copyfile(src, target)
        mode = "copied"

    print(f"installed skill: {target}  ({mode})")
    print()
    print("next steps:")
    if args.user:
        print("  the `/knowledge` skill is now available in every project.")
    else:
        print("  the `/knowledge` skill is now available in this project.")
        print("  commit .claude/skills/knowledge/SKILL.md if you want teammates to share it.")
    print("  from a repo root, run `knowledge build` once to index the code.")
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    """Register PreCompact + SessionEnd hooks that auto-flush staged history."""
    if args.user:
        settings_path = Path.home() / ".claude" / "settings.json"
        scope = "user"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"
        scope = "project"

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"error: failed to parse {settings_path}: {exc}\n"
                "fix the JSON syntax and re-run, or delete the file to start clean.",
                file=sys.stderr,
            )
            return 1
        if not isinstance(settings, dict):
            print(
                f"error: {settings_path} is not a JSON object (got {type(settings).__name__})",
                file=sys.stderr,
            )
            return 1
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(
            f"error: 'hooks' in {settings_path} is not an object",
            file=sys.stderr,
        )
        return 1

    hook_cmd = _resolve_hook_command(absolute=args.absolute)
    # Stop fires after every assistant turn → SQLite gets each turn's staged
    # entries while the session is still live. PreCompact + SessionEnd are
    # the safety net for anything written after the last Stop, or for
    # abrupt terminations where SessionEnd may still land before the
    # process dies.
    events = ("Stop", "PreCompact", "SessionEnd")
    added: list[str] = []
    already: list[str] = []
    upgraded: list[str] = []

    for event in events:
        event_list = hooks.setdefault(event, [])
        if not isinstance(event_list, list):
            print(
                f"error: hooks.{event} in {settings_path} is not an array",
                file=sys.stderr,
            )
            return 1
        existing_cmd = _find_history_ingest_command(event_list)
        if existing_cmd == hook_cmd:
            already.append(event)
        elif existing_cmd is not None:
            _replace_history_ingest_command(event_list, hook_cmd)
            upgraded.append(f"{event} ({existing_cmd} → {hook_cmd})")
        else:
            event_list.append(
                {"hooks": [{"type": "command", "command": hook_cmd}]}
            )
            added.append(event)

    if added or upgraded:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )

    print(f"scope: {scope}  ({settings_path})")
    print(f"command: {hook_cmd}")
    if added:
        print(f"added hook for: {', '.join(added)}")
    if upgraded:
        print("upgraded:")
        for u in upgraded:
            print(f"  {u}")
    if already:
        print(f"already registered for: {', '.join(already)}  (no change)")
    print()
    print("to verify:")
    print(
        "  1. append a JSON entry to ~/.knowledge/stage/pending.jsonl in a "
        "running session"
    )
    print("  2. run /compact (or let auto-compact fire)")
    print("  3. `knowledge history recent --limit 1` — the entry should be there")
    return 0


_HOOK_CMD_SUFFIX = "knowledge history ingest"


def _resolve_hook_command(absolute: bool) -> str:
    """Return the shell command string written into settings.json."""
    if not absolute:
        return _HOOK_CMD_SUFFIX
    import shutil
    resolved = shutil.which("knowledge")
    if resolved is None:
        print(
            "warning: --absolute requested but 'knowledge' not on PATH; "
            "falling back to bare command.",
            file=sys.stderr,
        )
        return _HOOK_CMD_SUFFIX
    return f"{resolved} history ingest"


def _is_history_ingest_cmd(cmd: str) -> bool:
    """Loose match: treat any command ending in 'knowledge history ingest'
    as ours, so upgrading bare→absolute (or vice-versa) replaces in place
    instead of duplicating.
    """
    return cmd.endswith(_HOOK_CMD_SUFFIX)


def _find_history_ingest_command(event_list: list) -> str | None:
    """Return the existing ingest-command string if present, else None."""
    for matcher in event_list:
        if not isinstance(matcher, dict):
            continue
        inner = matcher.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if isinstance(cmd, str) and _is_history_ingest_cmd(cmd):
                return cmd
    return None


def _replace_history_ingest_command(event_list: list, new_cmd: str) -> None:
    """Rewrite the first matching ingest command in-place."""
    for matcher in event_list:
        if not isinstance(matcher, dict):
            continue
        inner = matcher.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if isinstance(cmd, str) and _is_history_ingest_cmd(cmd):
                h["command"] = new_cmd
                return


def cmd_history(args: argparse.Namespace) -> int:
    """Dispatcher for the nested `knowledge history ...` subcommands."""
    return _HISTORY_DISPATCH[args.history_cmd](args)


def cmd_history_add(args: argparse.Namespace) -> int:
    from . import history

    with db.connect() as conn:
        root = projects.current_project_root()
        proj = projects.get_or_create_project(conn, root)
        hid = history.add(
            conn,
            project_id=proj.id,
            short_summary=args.short,
            long_summary=args.long,
            session_id=args.session_id,
            tags=args.tags,
        )
    print(f"added history entry id={hid} in project '{proj.name}' ({proj.root_path})")
    return 0


def cmd_history_ingest(args: argparse.Namespace) -> int:
    from . import history

    stage = (
        Path(args.stage_file).expanduser() if args.stage_file else paths.stage_path()
    )
    # Empty-stage fast path — no DB touch. Critical for user-scoped hooks
    # that fire on every session: if we called get_or_create_project here,
    # every random repo the user opens would accrete an empty project row.
    if not stage.exists() or not stage.read_text(encoding="utf-8").strip():
        print(f"stage is empty: {stage}")
        return 0
    with db.connect() as conn:
        root = projects.current_project_root()
        proj = projects.get_or_create_project(conn, root)
        ingested, skipped = history.ingest_stage(conn, stage, proj.id)

    if ingested > 0:
        print(
            f"ingested: {ingested} into '{proj.name}' ({proj.root_path})"
        )
        print(f"stage file truncated: {stage}")
    if skipped > 0:
        print(f"skipped (malformed): {skipped}", file=sys.stderr)
    return 0


def cmd_history_recent(args: argparse.Namespace) -> int:
    from . import history

    with db.connect() as conn:
        project_id: int | None = None
        if not args.all_projects:
            try:
                proj = projects.resolve_project(conn, args.project)
            except projects.AmbiguousProjectName as exc:
                _print_ambiguous(exc)
                return 1
            if proj is None:
                where = args.project or str(projects.current_project_root())
                print(
                    f"error: no project registered for: {where}\n"
                    "run 'knowledge build' first, or add history with "
                    "'knowledge history add' which auto-creates the row.",
                    file=sys.stderr,
                )
                return 1
            project_id = proj.id
        entries = history.recent(
            conn,
            project_id=project_id,
            days=args.days,
            limit=args.limit,
        )

    if args.json:
        print(json.dumps([e._asdict() for e in entries], indent=2, default=str))
    else:
        _print_history_list(entries)
    return 0


def cmd_history_search(args: argparse.Namespace) -> int:
    from . import history

    with db.connect() as conn:
        project_id: int | None = None
        if not args.all_projects:
            try:
                proj = projects.resolve_project(conn, args.project)
            except projects.AmbiguousProjectName as exc:
                _print_ambiguous(exc)
                return 1
            if proj is None:
                where = args.project or str(projects.current_project_root())
                print(
                    f"error: no project registered for: {where}",
                    file=sys.stderr,
                )
                return 1
            project_id = proj.id
        results = history.search(
            conn,
            query=args.query,
            project_id=project_id,
            top_k=args.top_k,
        )

    if args.json:
        print(
            json.dumps(
                [{"entry": e._asdict(), "distance": d} for e, d in results],
                indent=2,
                default=str,
            )
        )
    else:
        _print_history_search(results)
    return 0


def cmd_history_get(args: argparse.Namespace) -> int:
    from . import history

    with db.connect() as conn:
        entry = history.get(conn, args.history_id)
    if entry is None:
        print(f"error: no history entry with id {args.history_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(entry._asdict(), indent=2, default=str))
    else:
        _print_history_full(entry)
    return 0


_HISTORY_DISPATCH = {
    "add": cmd_history_add,
    "ingest": cmd_history_ingest,
    "recent": cmd_history_recent,
    "search": cmd_history_search,
    "get": cmd_history_get,
}


def cmd_relations(args: argparse.Namespace) -> int:
    """Dispatcher: overloaded positional ``target`` routes to stats or file.

    ``knowledge relations stats`` and ``knowledge relations <file>`` share
    one subparser to keep the CLI shallow. The literal word ``stats`` is
    reserved — users with a file literally named ``stats`` must pass
    ``./stats`` or an absolute path.
    """
    if args.target == "stats":
        return _cmd_relations_stats(args)
    return _cmd_relations_file(args)


def _cmd_relations_file(args: argparse.Namespace) -> int:
    from . import relations as rel_mod

    kinds: set[str] | None = None
    if args.kinds:
        kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}

    with db.connect() as conn:
        try:
            proj = projects.resolve_project(conn, args.project)
        except projects.AmbiguousProjectName as exc:
            _print_ambiguous(exc)
            return 1
        if proj is None:
            where = args.project or str(projects.current_project_root())
            print(
                f"error: project not registered: {where}\n"
                "run 'knowledge build' from the repo root first.",
                file=sys.stderr,
            )
            return 1

        rel_path = _resolve_relations_target(args.target, proj.root_path)
        file_id = rel_mod.find_file_id(conn, proj.id, rel_path)
        if file_id is None:
            print(
                f"error: file not indexed: {rel_path}\n"
                "check the path, or run 'knowledge update' if the file is new.",
                file=sys.stderr,
            )
            return 1

        forward: list = []
        reverse: list = []
        if args.direction in ("forward", "both"):
            forward = rel_mod.get_forward(conn, file_id, args.depth, kinds)
        if args.direction in ("reverse", "both"):
            reverse = rel_mod.get_reverse(conn, file_id, args.depth, kinds)

        # Plain-manifest fallback: if this file has no outbound edges
        # (no resolver fired — plain k8s YAML, a Markdown file, etc.),
        # emit sibling files as a weak "they live in the same folder"
        # signal. Still cheap (one indexed lookup), scoped to the
        # forward view, and clearly labeled so the LLM knows it's a
        # folder-grouping hint, not a real dep.
        siblings: list = []
        if (
            args.direction in ("forward", "both")
            and not forward
            and not reverse
        ):
            siblings = _sibling_files(conn, proj.id, rel_path)

    payload: dict = {
        "file": rel_path,
        "project": proj.name,
    }
    if args.direction in ("forward", "both"):
        payload["forward"] = [_edgerow_to_dict(e, direction="forward") for e in forward]
    if args.direction in ("reverse", "both"):
        payload["reverse"] = [_edgerow_to_dict(e, direction="reverse") for e in reverse]
    if siblings:
        payload["siblings"] = siblings
        payload["siblings_note"] = (
            "no resolver for this file type; listing files in the same directory "
            "as a weak folder-grouping hint."
        )

    if args.pretty:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(json.dumps(payload, separators=(",", ":"), default=str))
    return 0


def _cmd_relations_stats(args: argparse.Namespace) -> int:
    from . import relations as rel_mod

    with db.connect() as conn:
        project_id: int | None = None
        project_name: str | None = None
        if not args.all_projects:
            try:
                proj = projects.resolve_project(conn, args.project)
            except projects.AmbiguousProjectName as exc:
                _print_ambiguous(exc)
                return 1
            if proj is None:
                where = args.project or str(projects.current_project_root())
                print(
                    f"error: project not registered: {where}\n"
                    "run 'knowledge build' first.",
                    file=sys.stderr,
                )
                return 1
            project_id = proj.id
            project_name = proj.name

        summary = rel_mod.stats(conn, project_id)

    payload: dict = {"project": project_name or "*"}
    payload.update(summary)
    if args.pretty:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(json.dumps(payload, separators=(",", ":"), default=str))
    return 0


def _sibling_files(conn, project_id: int, rel_path: str) -> list[dict]:
    """Return the other files in the same directory as ``rel_path``.

    Used only when a file has no edges and no resolver fired — plain k8s
    manifests, markdown, JSON configs, etc. Avoids a cross-directory
    fan-out; scope stays on the directly-adjacent files an LLM would
    consider "context" for the one it was asked about. Capped so a
    densely-populated dir (hundreds of manifests) doesn't blow up output.
    """
    dir_prefix = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    pattern = f"{dir_prefix}/%" if dir_prefix else "%"
    rows = conn.execute(
        "SELECT rel_path, lang FROM files "
        "WHERE project_id = ? AND rel_path LIKE ? AND rel_path != ? "
        "ORDER BY rel_path LIMIT 50",
        (project_id, pattern, rel_path),
    ).fetchall()
    # Exclude rows that are in SUBdirectories — pattern ``dir/%`` also
    # matches ``dir/sub/file``. Drop anything with a deeper slash count.
    target_depth = rel_path.count("/")
    out: list[dict] = []
    for r in rows:
        p, lang = r[0], r[1]
        if p.count("/") != target_depth:
            continue
        out.append({"file": p, "lang": lang})
    return out


def _resolve_relations_target(target: str, project_root: Path) -> str:
    """Normalize the user's target string to a project-relative posix path.

    Accepts absolute, cwd-relative, or already project-relative paths.
    Does NOT check that the file exists on disk — the caller looks it up
    in the files table (which is the source of truth for "indexed").
    """
    p = Path(target).expanduser()
    if p.is_absolute():
        try:
            return p.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            # Absolute path outside the project root — best-effort: treat
            # the raw input as a posix path. Lookup will miss and the
            # caller emits a clean error.
            return target
    # If it looks like "knowledge/cli.py" from cwd == project_root, that's
    # already the posix-rel form. If cwd is a subdir, relative-to-cwd
    # might need adjusting — try both.
    cwd = Path.cwd().resolve()
    abs_from_cwd = (cwd / p).resolve()
    try:
        return abs_from_cwd.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        # Fall back to raw input; lookup will miss and emit a clean error.
        return p.as_posix()


def _edgerow_to_dict(e, direction: str) -> dict:
    """Shape one EdgeRow for JSON output.

    ``direction`` controls which side we present as the "other" file:
    forward view shows the target; reverse view shows the source. Edges
    with NULL target (no matching project file) flatten to a compact
    ``{kind, raw, line}`` with no ``file`` key. Three display kinds
    signal the different reasons for NULL:

    * ``unresolved`` — resolver couldn't even try (non-literal dynamic
      import, etc.).
    * ``parametric`` — ``raw`` carries Jinja ``{{ name }}`` or Terraform
      ``${var.name}`` templates that aren't satisfied by the current
      project_variables table. Set the missing variable with
      ``knowledge vars set <scope> name=value`` and the edge will
      resolve on the next query.
    * ``external`` — all other NULL-target edges. Stdlib / third-party /
      remote module sources.
    """
    if direction == "forward":
        other_rel = e.target_rel
    else:  # reverse
        other_rel = e.source_rel
    display_kind = e.kind
    if other_rel is None:
        if e.kind == "unresolved":
            display_kind = "unresolved"
        elif _has_template_markers(e.raw):
            # raw carries Jinja / TF template syntax → user hasn't set
            # (all) the needed variables yet. Distinct from ``external``
            # which means "resolved against project but no file matched".
            display_kind = "parametric"
        else:
            display_kind = "external"

    out: dict = {"kind": display_kind, "raw": e.raw}
    if other_rel is not None:
        out["file"] = other_rel
    if e.symbol is not None:
        out["symbol"] = e.symbol
    if e.line is not None:
        out["line"] = e.line
    return out


def _has_template_markers(raw) -> bool:
    """Tiny helper: same check as ``variables.has_template_markers`` but
    inlined here to avoid importing the variables module for every
    edge during JSON output. Keeps the ``knowledge relations`` hot
    path free of the CRUD surface.
    """
    if not raw:
        return False
    return "{{" in raw or "${var." in raw


# ---------------------------------------------------------------------------
# vars — per-project variable table
# ---------------------------------------------------------------------------


def cmd_vars(args: argparse.Namespace) -> int:
    """Dispatcher for ``knowledge vars ...``."""
    return _VARS_DISPATCH[args.vars_cmd](args)


def _cmd_vars_set(args: argparse.Namespace) -> int:
    from . import variables

    pairs = _parse_kv_pairs(args.pairs)
    if pairs is None:
        return 1

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        try:
            n = variables.set_many(conn, proj.id, args.scope, pairs)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        updated, still_parametric = variables.apply_variables(
            conn, proj.id, proj.root_path
        )

    print(f"set {n} variable(s) in scope '{args.scope}' for {proj.name}")
    _print_apply_summary(updated, still_parametric)
    return 0


def _cmd_vars_unset(args: argparse.Namespace) -> int:
    from . import variables

    if not args.unset_all and not args.name:
        print(
            "error: give a variable name, or pass --all to clear the whole scope.",
            file=sys.stderr,
        )
        return 1

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        try:
            if args.unset_all:
                removed = variables.unset_scope(conn, proj.id, args.scope)
                print(
                    f"cleared {removed} variable(s) from scope '{args.scope}' "
                    f"for {proj.name}"
                )
            else:
                existed = variables.unset(conn, proj.id, args.scope, args.name)
                if existed:
                    print(
                        f"unset '{args.name}' in scope '{args.scope}' "
                        f"for {proj.name}"
                    )
                else:
                    print(
                        f"no such variable: scope='{args.scope}' name='{args.name}'",
                        file=sys.stderr,
                    )
                    return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        updated, still_parametric = variables.apply_variables(
            conn, proj.id, proj.root_path
        )

    _print_apply_summary(updated, still_parametric)
    return 0


def _cmd_vars_list(args: argparse.Namespace) -> int:
    from . import variables

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        rows = variables.list_vars(conn, proj.id, args.scope)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "scope": v.scope,
                        "name": v.name,
                        "value": v.value,
                        "updated_at": v.updated_at,
                    }
                    for v in rows
                ],
                indent=2,
                default=str,
            )
        )
        return 0

    if not rows:
        scope_s = f" in scope '{args.scope}'" if args.scope else ""
        print(f"(no variables set{scope_s} for {proj.name})")
        return 0
    print(f"{proj.name}:")
    for v in rows:
        print(f"  [{v.scope:<9}] {v.name:<24} = {v.value}")
    return 0


def _cmd_vars_import(args: argparse.Namespace) -> int:
    from . import variables

    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        try:
            n = variables.import_json(conn, proj.id, args.scope, path)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        updated, still_parametric = variables.apply_variables(
            conn, proj.id, proj.root_path
        )

    print(
        f"imported {n} variable(s) from {path} into scope '{args.scope}' "
        f"for {proj.name}"
    )
    _print_apply_summary(updated, still_parametric)
    return 0


_VARS_DISPATCH = {
    "set": _cmd_vars_set,
    "unset": _cmd_vars_unset,
    "list": _cmd_vars_list,
    "import": _cmd_vars_import,
}


# ---------------------------------------------------------------------------
# graph — HTML visualization of the dependency graph
# ---------------------------------------------------------------------------


def cmd_graph(args: argparse.Namespace) -> int:
    """Render the project's file-edge graph to a self-contained HTML.

    One project per run (``--project`` to override the cwd default).
    Output path defaults to ``./relations_graph.html``. With ``--open``
    we launch the user's default browser on the file once it's written.
    """
    from . import graph as graph_mod

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1

        html_str = graph_mod.build_graph_html(
            conn,
            project_id=proj.id,
            project_name=proj.name,
            include_external=args.include_external,
            include_parametric=args.include_parametric,
            include_unresolved=args.include_unresolved,
        )

    out_path = (
        Path(args.output).expanduser()
        if args.output
        else Path.cwd() / "relations_graph.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_str, encoding="utf-8")
    # Echo the absolute path (not the input form) so tmux copy-paste
    # or CI log grabs always give a usable path.
    abs_out = out_path.resolve()
    print(f"wrote graph: {abs_out}")

    if args.open:
        import webbrowser
        # ``file://`` scheme + absolute path is what every browser
        # accepts without a server. No shell quoting concerns.
        webbrowser.open(abs_out.as_uri())

    return 0


def _parse_kv_pairs(args_list: list[str]) -> dict[str, str] | None:
    """Parse ``NAME=VALUE`` arg forms. Prints an error and returns None on
    malformed entries (a missing ``=`` is the only failure mode).
    """
    out: dict[str, str] = {}
    for p in args_list:
        if "=" not in p:
            print(
                f"error: expected NAME=VALUE, got {p!r}. "
                f"Use quotes if the value contains spaces.",
                file=sys.stderr,
            )
            return None
        k, v = p.split("=", 1)
        k = k.strip()
        if not k:
            print(f"error: empty name in pair {p!r}", file=sys.stderr)
            return None
        out[k] = v
    return out


def _resolve_project_or_error(conn, selector):
    """Wrap ``resolve_project`` + uniform error output. Returns the
    Project on success, or None on any resolution failure (after
    emitting a message to stderr).
    """
    try:
        proj = projects.resolve_project(conn, selector)
    except projects.AmbiguousProjectName as exc:
        _print_ambiguous(exc)
        return None
    if proj is None:
        where = selector or str(projects.current_project_root())
        print(
            f"error: project not registered: {where}\n"
            "run 'knowledge build' from the repo root first.",
            file=sys.stderr,
        )
        return None
    return proj


def _print_apply_summary(updated: int, still_parametric: int) -> None:
    """Uniform one-line report after auto-apply runs."""
    if updated or still_parametric:
        bits = []
        if updated:
            bits.append(f"{updated} edge(s) newly resolved")
        if still_parametric:
            bits.append(f"{still_parametric} still parametric")
        print("auto-apply: " + ", ".join(bits))
    else:
        print("auto-apply: no edges affected")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_results_pretty(results) -> None:
    if not results:
        print("(no matches)")
        return
    # Import here so the hot --help path doesn't pay for it.
    from .whitespace import decompress

    for i, r in enumerate(results, 1):
        label = r.qualified_name or r.name or "(anon)"
        print(f"{i}. [{r.kind:>8}] {label}  ({r.rel_path}:{r.start_line}-{r.end_line})")
        print(f"   project={r.project_name}  lang={r.lang}  distance={r.distance:.3f}  id={r.chunk_id}")
        # Preview: decompress ^N^ markers, show first two non-empty lines.
        preview_lines = [ln for ln in decompress(r.preview).splitlines() if ln.strip()][:2]
        for ln in preview_lines:
            print(f"     {ln[:120]}")
        print()


def _prompt_collision_resolution(
    proposed_name: str,
    new_root: Path,
    matches: list[projects.Project],
    suffix_candidate: str,
) -> tuple[str, list[int]] | None:
    """Interactive short-name collision prompt for ``knowledge build``.

    Returns ``(final_name, ids_to_forget)`` on go-ahead, or ``None`` on abort.
    Caller must have already verified stdin is a tty — the helper itself
    just reads input and trusts that decision.
    """
    n = len(matches)
    print(
        f"\nshort name '{proposed_name}' is already registered at "
        f"{n} different location{'s' if n != 1 else ''}:",
        file=sys.stderr,
    )
    for i, m in enumerate(matches, 1):
        print(
            f"  {i}. {m.root_path}  "
            f"(files={m.file_count}, chunks={m.chunk_count})",
            file=sys.stderr,
        )
    print(f"\ntrying to build: {new_root}\n", file=sys.stderr)

    if n == 1:
        only = matches[0]
        print(
            f"  [r] replace  — forget the existing project "
            f"(deletes {only.file_count} files, {only.chunk_count} chunks) "
            f"and use '{proposed_name}' for the new build",
            file=sys.stderr,
        )
        print(
            f"  [s] suffix   — keep both; new build registered as "
            f"'{suffix_candidate}'",
            file=sys.stderr,
        )
        print("  [a] abort", file=sys.stderr)
        valid = "rsa"
    else:
        print(
            f"  [s] suffix — keep all; new build registered as "
            f"'{suffix_candidate}'",
            file=sys.stderr,
        )
        print("  [a] abort", file=sys.stderr)
        print(
            "  (to replace a specific existing project, run "
            "'knowledge forget <abs-path>' first)",
            file=sys.stderr,
        )
        valid = "sa"

    while True:
        try:
            raw = input(f"\nyour choice [{'/'.join(valid)}]: ").strip().lower()
        except EOFError:
            return None
        if not raw:
            continue
        choice = raw[0]
        if choice not in valid:
            print(f"invalid choice: {raw!r}", file=sys.stderr)
            continue
        if choice == "a":
            return None
        if choice == "s":
            return (suffix_candidate, [])
        # choice == "r" (only reachable when n == 1)
        return (proposed_name, [m.id for m in matches])


def _prompt_forget_selection(
    exc: projects.AmbiguousProjectName,
) -> list[projects.Project]:
    """Interactive picker for ``knowledge forget`` when a name is ambiguous.

    Returns the list of projects the user chose to forget, or ``[]`` on
    abort. Caller must have already verified stdin is a tty.
    """
    print(
        f"\nproject name '{exc.name}' matches {len(exc.matches)} projects:",
        file=sys.stderr,
    )
    for i, p in enumerate(exc.matches, 1):
        print(
            f"  {i}. {p.root_path}  "
            f"(files={p.file_count}, chunks={p.chunk_count})",
            file=sys.stderr,
        )
    print("", file=sys.stderr)
    print("  <number>   forget that one", file=sys.stderr)
    print("  all        forget all of them", file=sys.stderr)
    print("  q          abort", file=sys.stderr)

    while True:
        try:
            raw = input("\nyour choice: ").strip().lower()
        except EOFError:
            return []
        if not raw or raw in ("q", "quit", "abort"):
            return []
        if raw in ("all", "*"):
            return list(exc.matches)
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(exc.matches):
                return [exc.matches[n - 1]]
        print(f"invalid choice: {raw!r}", file=sys.stderr)


def _print_ambiguous(exc: projects.AmbiguousProjectName) -> None:
    print(
        f"error: project name '{exc.name}' is ambiguous "
        f"({len(exc.matches)} matches):",
        file=sys.stderr,
    )
    for m in exc.matches:
        print(f"  - {m.root_path}", file=sys.stderr)
    print(
        "pass an absolute root path instead of the name to pick one.",
        file=sys.stderr,
    )


def _fmt_time(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _print_history_list(entries) -> None:
    if not entries:
        print("(no history)")
        return
    for e in entries:
        tags = f"  [{e.tags}]" if e.tags else ""
        print(f"{_fmt_time(e.created_at)}  id={e.id}{tags}")
        print(f"  {e.short_summary}")


def _print_history_search(results) -> None:
    if not results:
        print("(no matches)")
        return
    for i, (e, dist) in enumerate(results, 1):
        tags = f"  [{e.tags}]" if e.tags else ""
        print(
            f"{i}. {_fmt_time(e.created_at)}  id={e.id}  "
            f"distance={dist:.3f}{tags}"
        )
        print(f"   {e.short_summary}")


def _print_history_full(e) -> None:
    tags = f"  [{e.tags}]" if e.tags else ""
    print(
        f"id={e.id}  project_id={e.project_id}  "
        f"{_fmt_time(e.created_at)}{tags}"
    )
    if e.session_id:
        print(f"session: {e.session_id}")
    print()
    print(f"SHORT: {e.short_summary}")
    print()
    print("LONG:")
    print(e.long_summary)


def _format_size(p: Path) -> str:
    if not p.exists():
        return "0 B"
    size = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


_DISPATCH = {
    "build": cmd_build,
    "update": cmd_update,
    "status": cmd_status,
    "search": cmd_search,
    "get": cmd_get,
    "path": cmd_path,
    "projects": cmd_projects,
    "stats": cmd_stats,
    "forget": cmd_forget,
    "history": cmd_history,
    "relations": cmd_relations,
    "vars": cmd_vars,
    "graph": cmd_graph,
    "install-skill": cmd_install_skill,
    "install-hooks": cmd_install_hooks,
}


if __name__ == "__main__":
    sys.exit(main())
