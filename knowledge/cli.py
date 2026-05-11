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

    # find — exact / prefix / regex lookup by symbol name. Schema v2+.
    # No embedding: hits partial indexes idx_chunks_name / idx_chunks_qname
    # directly. O(log n), under 10ms typical.
    p_find = sub.add_parser(
        "find",
        help="Exact-name lookup by chunk name or qualified_name (no embedding).",
    )
    p_find.add_argument("name", help="Symbol name (prefix by default; see --exact/--regex)")
    p_find.add_argument("--exact", action="store_true", help="Exact match instead of prefix")
    p_find.add_argument("--regex", action="store_true", help="Python regex (overrides --exact)")
    p_find.add_argument("--kind", help="Filter by chunk kind (function, resource, …)")
    p_find.add_argument("--lang", help="Filter by language")
    p_find.add_argument("--limit", type=int, default=10)
    p_find.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_find.add_argument("--all-projects", action="store_true")
    p_find.add_argument(
        "--format",
        choices=("citations", "chunks", "json"),
        default="citations",
        help="Output format (default: citations — one 'path:lines | kind | excerpt' per line)",
    )

    # grep — FTS5 MATCH over name, qualified_name, stored_text. Schema v2+.
    # Full FTS5 query syntax: quoted phrases, prefix foo*, boolean AND/OR,
    # column qualifier name:foo. No embedding.
    p_grep = sub.add_parser(
        "grep",
        help="FTS5 full-text match over chunk names + stored text (no embedding).",
    )
    p_grep.add_argument("pattern", help="FTS5 query (phrases, prefix foo*, AND/OR, name:X, ...)")
    p_grep.add_argument("--kind", help="Filter by chunk kind (function, resource, …)")
    p_grep.add_argument("--lang", help="Filter by language")
    p_grep.add_argument("--limit", type=int, default=10)
    p_grep.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_grep.add_argument("--all-projects", action="store_true")
    p_grep.add_argument(
        "--format",
        choices=("citations", "chunks", "json"),
        default="citations",
    )

    # ask — hybrid lexical + semantic retrieval with reranking (Phase 3).
    # Agent-first: citations output by default, answer cache warms
    # automatically, token budget truncates gracefully.
    p_ask = sub.add_parser(
        "ask",
        help="Hybrid search (FTS + vec, RRF merge, reranked). Best default for agents.",
    )
    p_ask.add_argument("question", help="Free-text query")
    p_ask.add_argument("--top-k", type=int, default=10)
    p_ask.add_argument("--kind", help="Filter by chunk kind")
    p_ask.add_argument("--lang", help="Filter by language")
    p_ask.add_argument(
        "--budget",
        type=int,
        default=0,
        help="Soft token budget — truncate citations list to roughly this many tokens (0=off)",
    )
    p_ask.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_ask.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the answer cache (force fresh FTS + vec on every call)",
    )
    p_ask.add_argument(
        "--format",
        choices=("citations", "chunks", "json"),
        default="citations",
    )

    # why — 6-line file brief (Phase 2 cartography).
    p_why = sub.add_parser(
        "why",
        help="One-file brief: description, top symbols, neighbors (no embedder).",
    )
    p_why.add_argument("path", help="File path (repo-relative, absolute, or cwd-relative)")
    p_why.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # map — directory tree with per-dir aggregates.
    p_map = sub.add_parser(
        "map",
        help="Directory-tree overview: file counts, langs, hub files per subtree.",
    )
    p_map.add_argument(
        "--dir",
        dest="dir_filter",
        help="Limit to files under this directory prefix (e.g. 'terraform')",
    )
    p_map.add_argument("--depth", type=int, default=2, help="Group by first N path components (default 2)")
    p_map.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # brief — repo-level snapshot.
    p_brief = sub.add_parser(
        "brief",
        help="Repo-wide summary: totals, top langs, hub files.",
    )
    p_brief.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # decide — record a non-obvious choice (Phase 4 session memory).
    p_decide = sub.add_parser(
        "decide",
        help="Record a non-obvious choice with rationale + files touched.",
    )
    p_decide.add_argument("topic", help="Short label (e.g. 'cache invalidation')")
    p_decide.add_argument("--decision", required=True, help="The choice itself")
    p_decide.add_argument("--rationale", help="One-line 'why' (optional)")
    p_decide.add_argument(
        "--files",
        nargs="+",
        metavar="PATH",
        help="Files touched by this decision (optional)",
    )
    p_decide.add_argument("--session-id", help="Tag with session identifier")
    p_decide.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # decisions — list / search past decisions.
    p_decs = sub.add_parser(
        "decisions",
        help="List or search recorded decisions.",
    )
    p_decs.add_argument("--topic", help="Case-insensitive substring filter on topic")
    p_decs.add_argument("--search", dest="search_q", help="Semantic search over topic+decision")
    p_decs.add_argument("--days", type=int, help="Only entries from the last N days")
    p_decs.add_argument("--limit", type=int, default=20)
    p_decs.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_decs.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )

    # resume — opinionated session-start brief.
    p_resume = sub.add_parser(
        "resume",
        help='"Where did I leave off?" — decisions + touched files + pending stage + hubs.',
    )
    p_resume.add_argument("--project", help="Scope to a specific project (name or abs path)")

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

    p_h_stage = p_h_sub.add_parser(
        "stage",
        help="Append one entry to the per-project, per-session JSONL stage "
             "(no DB write; flush later with `knowledge history ingest`).",
    )
    p_h_stage.add_argument("--short", required=True, help="One-line summary (~160 chars)")
    p_h_stage.add_argument("--long", required=True, help="Detailed summary with file refs + rationale")
    p_h_stage.add_argument("--tags", help="Optional comma-separated tags")
    p_h_stage.add_argument("--session-id", help="Optional session identifier (stored with the entry)")

    p_h_ingest = p_h_sub.add_parser("ingest", help="Flush staged JSONL entries into SQLite")
    p_h_ingest.add_argument(
        "--stage-file",
        help="Override: flush exactly this one JSONL file under the current project "
             "(truncates on success). Without it, ingest walks every per-project "
             "stage dir under ~/.knowledge/stage/ and absorbs the legacy "
             "~/.knowledge/stage/pending.jsonl once if present.",
    )
    p_h_ingest.add_argument(
        "--gc",
        action="store_true",
        help="After the ingest flow, delete any *.inflight-* debris older than "
             "1 hour (crash leftovers from interrupted ingests). Ignored when "
             "combined with --stage-file.",
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
        "--no-orphans",
        action="store_true",
        help="Drop indexed files that have no edges in or out "
             "(CI/CD files stay visible). Default shows every file as a "
             "dot so the graph reads as a full repo map.",
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


def cmd_find(args: argparse.Namespace) -> int:
    """Exact/prefix/regex symbol lookup — schema v2+."""
    from . import fts

    with db.connect() as conn:
        project_id = _resolve_scope(conn, args)
        if project_id is _SCOPE_ERROR:
            return 1
        try:
            results = fts.find(
                conn,
                name=args.name,
                project_id=project_id,
                exact=args.exact,
                regex=args.regex,
                kind=args.kind,
                lang=args.lang,
                limit=args.limit,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    _emit_results(results, args.format)
    return 0


def cmd_grep(args: argparse.Namespace) -> int:
    """FTS5 MATCH lookup — schema v2+."""
    import apsw  # FTS5 syntax errors surface as apsw.SQLError.
    from . import fts

    with db.connect() as conn:
        project_id = _resolve_scope(conn, args)
        if project_id is _SCOPE_ERROR:
            return 1
        try:
            results = fts.grep(
                conn,
                pattern=args.pattern,
                project_id=project_id,
                kind=args.kind,
                lang=args.lang,
                limit=args.limit,
            )
        except apsw.SQLError as exc:
            print(
                f"error: invalid FTS5 query: {exc}\n"
                "examples: 'vault auth', '\"exact phrase\"', 'foo*', "
                "'vault AND approle', 'name:handle_*'.",
                file=sys.stderr,
            )
            return 1

    _emit_results(results, args.format)
    return 0


# Sentinel for _resolve_scope — distinguishes "error emitted, bail out"
# from the legitimate ``None`` value meaning "--all-projects".
_SCOPE_ERROR: object = object()


def _resolve_scope(conn, args) -> int | None | object:
    """Return project_id (int), None (all-projects), or _SCOPE_ERROR.

    Shared by find/grep. Emits the error message to stderr on failure so
    the caller just has to check for the sentinel and return 1.
    """
    if getattr(args, "all_projects", False):
        return None
    try:
        proj = projects.resolve_project(conn, args.project)
    except projects.AmbiguousProjectName as exc:
        _print_ambiguous(exc)
        return _SCOPE_ERROR
    if proj is None:
        where = args.project or str(projects.current_project_root())
        print(
            f"error: project not registered: {where}\n"
            "run 'knowledge build' from the repo root first.",
            file=sys.stderr,
        )
        return _SCOPE_ERROR
    return proj.id


def _emit_results(results, fmt: str) -> None:
    """Dispatch SearchResult list to the chosen formatter."""
    if fmt == "json":
        print(json.dumps([r._asdict() for r in results], indent=2, default=str))
    elif fmt == "chunks":
        _print_results_pretty(results)
    else:  # citations
        _print_results_citations(results)


def _print_results_citations(results) -> None:
    """One result per line, parseable with ``split(' | ', 2)``.

    Format: ``rel/path:start-end | kind | one-line excerpt``.

    The excerpt is the first non-empty line of the stored text after
    whitespace decompression, trimmed at 160 chars so an LLM can diff
    results at a glance without scrolling.
    """
    if not results:
        return
    from .whitespace import decompress

    for r in results:
        excerpt = _first_line(decompress(r.preview))
        print(f"{r.rel_path}:{r.start_line}-{r.end_line} | {r.kind} | {excerpt}")


def _first_line(text: str, max_chars: int = 160) -> str:
    """Collapse to the first meaningful line for a citation excerpt."""
    for ln in text.splitlines():
        s = ln.strip()
        if s:
            return s[:max_chars]
    return ""


def cmd_decide(args: argparse.Namespace) -> int:
    """Record a decision — Phase 4 session memory."""
    from . import decisions as decisions_mod

    topic = args.topic.strip()
    decision = args.decision.strip()
    if not topic or not decision:
        print("error: topic and --decision must be non-empty", file=sys.stderr)
        return 2

    with db.connect() as conn:
        # `decide` should work even before a `build`; auto-create the project
        # row the same way `history add` does.
        root = projects.current_project_root()
        proj = projects.get_or_create_project(conn, root)
        new_id = decisions_mod.add(
            conn,
            project_id=proj.id,
            topic=topic,
            decision=decision,
            rationale=args.rationale,
            files_touched=args.files,
            session_id=args.session_id,
        )
    print(f"recorded decision id={new_id} in '{proj.name}' ({proj.root_path})")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """List / search decisions — Phase 4."""
    from . import decisions as decisions_mod

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        if args.search_q:
            raw = decisions_mod.search(
                conn,
                query=args.search_q,
                project_id=proj.id,
                top_k=args.limit,
            )
            entries = [(d, dist) for d, dist in raw]
        else:
            plain = decisions_mod.recent(
                conn,
                project_id=proj.id,
                days=args.days,
                topic=args.topic,
                limit=args.limit,
            )
            # Pair with None distance so the formatter handles both paths uniformly.
            entries = [(d, None) for d in plain]

    if args.format == "json":
        print(json.dumps(
            [
                {"decision": d._asdict(), "distance": dist}
                for d, dist in entries
            ],
            indent=2,
            default=str,
        ))
    else:
        _print_decisions(entries)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Session-start brief — Phase 4 session memory."""
    from . import resume as resume_mod

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        brief = resume_mod.build(
            conn,
            project_id=proj.id,
            project_name=proj.name,
            project_root=proj.root_path,
        )
    _print_resume(brief)
    return 0


def _print_decisions(entries) -> None:
    """Pretty-print (Decision, distance|None) tuples."""
    from datetime import datetime

    if not entries:
        print("(no decisions)")
        return
    for d, dist in entries:
        when = datetime.fromtimestamp(d.created_at).strftime("%Y-%m-%d %H:%M")
        dist_s = f"  dist={dist:.3f}" if dist is not None else ""
        print(f"{when}  id={d.id}{dist_s}")
        print(f"  topic:    {d.topic}")
        print(f"  decision: {d.decision}")
        if d.rationale:
            print(f"  why:      {d.rationale}")
        if d.files_touched:
            print(f"  files:    {', '.join(d.files_touched)}")


def _print_resume(rb) -> None:
    """Four-block session-start brief. Plain text, token-budget-aware."""
    from datetime import datetime

    print(f"{rb.project_name}  ({rb.project_root})")
    print(
        f"history: {rb.total_history_entries} entries  "
        f"decisions: {rb.total_decisions}"
    )

    # 1. Decisions
    print("\n# recent decisions")
    if not rb.last_decisions:
        print("  (none yet — log with `knowledge decide`)")
    else:
        for d in rb.last_decisions:
            when = datetime.fromtimestamp(d.created_at).strftime("%Y-%m-%d")
            print(f"  {when}  {d.topic}")
            print(f"            → {d.decision}")

    # 2. Touched files
    print("\n# most-touched files (last 7d)")
    if not rb.touched_files:
        print("  (no history tokens or recent commits matched indexed files)")
    else:
        for rel, score in rb.touched_files:
            print(f"  [{score:>2}] {rel}")

    # 3. Pending stage
    if rb.pending_stage:
        print("\n# pending in stage (not yet ingested)")
        for entry in rb.pending_stage:
            short = str(entry.get("short", "?")).strip()
            print(f"  - {short[:100]}")

    # 4. Hub files
    if rb.hub_files:
        print("\n# hub files (orientation)")
        for rel, deg in rb.hub_files:
            print(f"  {deg:>3}×  {rel}")

    # Footer guidance for the agent reading this output.
    print(
        "\n# how to proceed\n"
        "  - use `knowledge ask '<question>'` for search\n"
        "  - use `knowledge why <path>` to understand one file\n"
        "  - log non-obvious choices with `knowledge decide <topic> --decision <...>`"
    )


def cmd_ask(args: argparse.Namespace) -> int:
    """Hybrid search + rerank + cache — Phase 3 entry point for agents."""
    from . import hybrid_search

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        results = hybrid_search.ask(
            conn,
            query=args.question,
            project_id=proj.id,
            project_root=proj.root_path,
            kind=args.kind,
            lang=args.lang,
            top_k=args.top_k,
            use_cache=not args.no_cache,
        )

    kept, omitted = hybrid_search.truncate_to_budget(results, args.budget)
    _emit_results(kept, args.format)
    if omitted > 0 and args.format == "citations":
        print(f"...{omitted} more omitted (raise --budget to see)")
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    """One-file brief — Phase 2 cartography."""
    from . import cartography

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        rel_path = _resolve_relations_target(args.path, proj.root_path)
        brief = cartography.why(conn, rel_path, proj.id, proj.root_path)

    if brief is None:
        print(
            f"error: file not indexed: {rel_path}\n"
            "check the path, or run 'knowledge update' if the file is new.",
            file=sys.stderr,
        )
        return 1
    _print_why(brief)
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    """Directory-tree overview — Phase 2 cartography."""
    from . import cartography

    if args.depth < 1:
        print("error: --depth must be >= 1", file=sys.stderr)
        return 1

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        entries, truncated = cartography.map_tree(
            conn,
            project_id=proj.id,
            dir_filter=args.dir_filter,
            depth=args.depth,
        )

    if not entries:
        scope = f" under '{args.dir_filter}'" if args.dir_filter else ""
        print(f"(no files indexed{scope} for {proj.name})")
        return 0
    _print_map(entries, proj, truncated, args.dir_filter, args.depth)
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """Repo-wide snapshot — Phase 2 cartography."""
    from . import cartography

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        rb = cartography.brief(
            conn,
            project_id=proj.id,
            project_name=proj.name,
            project_root=str(proj.root_path),
            last_updated=proj.last_update,
        )
    _print_brief(rb)
    return 0


def _print_why(b) -> None:
    """Render a :class:`FileBrief` as a compact text block."""
    print(f"{b.rel_path}")
    meta = f"  lang={b.lang}  loc={b.loc}  size={_format_bytes(b.size)}"
    if b.last_commit_date:
        meta += f"  last_commit={b.last_commit_date}"
    print(meta)

    if b.description:
        print(f"  desc: {b.description}")

    if b.top_symbols:
        print("  top symbols:")
        for kind, name, sl, el, _cc in b.top_symbols:
            print(f"    [{kind}] {name}  ({sl}-{el})")

    if b.inbound:
        print("  inbound:")
        for src, kind in b.inbound:
            print(f"    ← {src}  ({kind})")
    if b.outbound:
        print("  outbound:")
        for tgt, kind in b.outbound:
            print(f"    → {tgt}  ({kind})")
    if not b.inbound and not b.outbound:
        print("  (no resolved cross-file edges)")


def _print_map(entries, proj, truncated: bool, dir_filter, depth: int) -> None:
    """Render a directory map as a fixed-width table."""
    header_dir = "DIR"
    header = f"{header_dir:<38} {'FILES':>6} {'LANG':<12}  TOP KINDS / ENTRYPOINT"
    print(f"{proj.name} ({proj.root_path})")
    if dir_filter:
        print(f"filter: {dir_filter}  depth: {depth}")
    else:
        print(f"depth: {depth}")
    print(header)
    print("-" * len(header))
    for e in entries:
        kinds = ", ".join(f"{k}×{n}" for k, n in e.top_kinds) or "-"
        entrypoint = f"  →{e.entrypoint}" if e.entrypoint else ""
        dir_display = e.dir_path or "(repo root)"
        # Truncate excessively long dir paths so the table stays aligned.
        if len(dir_display) > 37:
            dir_display = "…" + dir_display[-36:]
        print(
            f"{dir_display:<38} {e.file_count:>6} "
            f"{(e.dominant_lang or '-'):<12}  {kinds}{entrypoint}"
        )
    if truncated:
        print(
            "\nwarning: output truncated at 200 directory rows. "
            "Narrow with --dir <path> or reduce --depth.",
            file=sys.stderr,
        )


def _print_brief(rb) -> None:
    """Render a :class:`RepoBrief`."""
    from datetime import datetime

    print(f"{rb.project_name}  ({rb.project_root})")
    if rb.last_updated:
        print(f"last_update: {datetime.fromtimestamp(rb.last_updated).strftime('%Y-%m-%d %H:%M')}")
    print(f"files={rb.file_count}  chunks={rb.chunk_count}  edges={rb.edge_count}")

    if rb.top_langs:
        print("\ntop langs:")
        for lang, n in rb.top_langs:
            print(f"  {lang:<14} {n}")

    if rb.hub_files:
        print("\nhub files (by in-degree):")
        for rel, deg in rb.hub_files:
            print(f"  {deg:>3}×  {rel}")
    else:
        print("\n(no resolved cross-file edges)")


def _format_bytes(size: int) -> str:
    """Compact byte-size like '4.2KB' / '1.1MB'. Zero returns '0B'."""
    s: float = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if s < 1024:
            return f"{s:.1f}{unit}" if unit != "B" else f"{int(s)}B"
        s /= 1024
    return f"{s:.1f}TB"


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
        "  1. `knowledge history stage --short '...' --long '...'` "
        "(appends to ~/.knowledge/stage/<project>/sess-<id>.jsonl)"
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


def cmd_history_stage(args: argparse.Namespace) -> int:
    """Append one JSONL entry to the per-project, per-session stage file.

    No DB write for the entry itself — a later ``knowledge history ingest``
    flushes it under an APSW savepoint. We *do* register the project
    (``get_or_create_project``) so ingest can resolve the dir back to a
    project without re-hashing every known root. The project row is cheap
    and the user is about to write durable work-history into it anyway, so
    the "no empty rows for random cwds" concern from the ingest fast path
    doesn't apply here.
    """
    import fcntl  # POSIX-only; repo-knowledge doesn't support Windows.

    short = args.short.strip()
    long_ = args.long.strip()
    if not short or not long_:
        print("error: --short and --long must be non-empty", file=sys.stderr)
        return 2

    root = projects.current_project_root()
    with db.connect() as conn:
        proj = projects.get_or_create_project(conn, root)

    project_dir = paths.project_stage_dir(root)
    sidecar = paths.root_sidecar_path(project_dir)
    if not sidecar.exists():
        sidecar.write_text(f"{proj.root_path}\n", encoding="utf-8")

    stage = paths.session_stage_file(root)
    entry: dict = {"short": short, "long": long_}
    if args.tags:
        entry["tags"] = args.tags
    if args.session_id:
        entry["session_id"] = args.session_id
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    # flock serializes appends if two Bash invocations from the same
    # session race. O_APPEND alone is only atomic up to PIPE_BUF (~4 KB);
    # long_summary can exceed that. flock is a cheap guarantee.
    with open(stage, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    print(f"staged in '{proj.name}' ({stage})")
    return 0


def cmd_history_ingest(args: argparse.Namespace) -> int:
    from . import history

    # Explicit single-file override: current-project, truncate-on-success.
    # Used by tests, manual replay, and internally for legacy migration.
    if args.stage_file:
        if args.gc:
            print(
                "note: --gc ignored with --stage-file (GC sweeps the stage "
                "dir, not an arbitrary path)",
                file=sys.stderr,
            )
        stage = Path(args.stage_file).expanduser()
        if not stage.exists() or not stage.read_text(encoding="utf-8").strip():
            print(f"stage is empty: {stage}")
            return 0
        with db.connect() as conn:
            root = projects.current_project_root()
            proj = projects.get_or_create_project(conn, root)
            ingested, skipped = history.ingest_stage(conn, stage, proj.id)
        if ingested > 0:
            print(f"ingested: {ingested} into '{proj.name}' ({proj.root_path})")
            print(f"stage file truncated: {stage}")
        if skipped > 0:
            print(f"skipped (malformed): {skipped}", file=sys.stderr)
        return 0

    # Default flow: walk every per-project stage dir + absorb legacy file.
    legacy = paths.legacy_stage_path()
    project_dirs = paths.iter_stage_project_dirs()
    has_legacy = legacy.exists() and legacy.read_text(encoding="utf-8").strip() != ""
    has_sess = any(any(d.glob("sess-*.jsonl")) for d in project_dirs)

    # Empty-stage fast path — no DB connect. The hook fires on every Stop /
    # PreCompact / SessionEnd for every repo the user touches; skipping DB
    # work here keeps the cost near zero when there's nothing to flush.
    if not has_legacy and not has_sess:
        print("stage is empty")
        if args.gc:
            removed = history.sweep_inflight_debris(paths.stage_dir(), 3600)
            if removed > 0:
                print(f"gc: removed {removed} inflight debris file(s)")
        return 0

    total_ingested = 0
    total_skipped = 0
    per_project: list[tuple[str, Path, int]] = []  # (name, root, ingested)

    with db.connect() as conn:
        # Legacy file: absorb under current cwd's project (same heuristic
        # the tool has always used), then delete so we don't migrate twice.
        if has_legacy:
            root = projects.current_project_root()
            proj = projects.get_or_create_project(conn, root)
            i, s = history.ingest_stage(conn, legacy, proj.id)
            legacy.unlink(missing_ok=True)
            total_ingested += i
            total_skipped += s
            if i > 0:
                per_project.append((proj.name, proj.root_path, i))

        # Per-project dirs: resolve each via its .root sidecar.
        for pd in project_dirs:
            sidecar = paths.root_sidecar_path(pd)
            if not sidecar.exists():
                # Orphan dir (no sidecar → can't resolve). Skip silently;
                # a future `stage` call in the matching repo re-creates it.
                continue
            root_str = sidecar.read_text(encoding="utf-8").strip()
            if not root_str:
                continue
            proj = projects.get_or_create_project(conn, Path(root_str))
            i, s = history.ingest_stage_dir(conn, pd, proj.id)
            total_ingested += i
            total_skipped += s
            if i > 0:
                per_project.append((proj.name, proj.root_path, i))

    for name, rpath, count in per_project:
        print(f"ingested: {count} into '{name}' ({rpath})")
    if total_ingested == 0 and total_skipped == 0:
        print("stage is empty")
    if total_skipped > 0:
        print(f"skipped (malformed): {total_skipped}", file=sys.stderr)

    if args.gc:
        # 1 hour: ingest completes in seconds in practice, so anything
        # still inflight after an hour is almost certainly crash debris.
        removed = history.sweep_inflight_debris(paths.stage_dir(), 3600)
        if removed > 0:
            print(f"gc: removed {removed} inflight debris file(s)")

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
    "stage": cmd_history_stage,
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
            include_orphans=not args.no_orphans,
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
    "find": cmd_find,
    "grep": cmd_grep,
    "ask": cmd_ask,
    "why": cmd_why,
    "map": cmd_map,
    "brief": cmd_brief,
    "decide": cmd_decide,
    "decisions": cmd_decisions,
    "resume": cmd_resume,
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
