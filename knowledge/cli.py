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
    p_ask.add_argument(
        "--no-decisions",
        action="store_true",
        help="Skip the prior decisions/facts preface",
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
    p_decide.add_argument(
        "--supersede",
        type=int,
        metavar="ID",
        help="Override an existing decision (its id). Requires --override-reason.",
    )
    p_decide.add_argument(
        "--override-reason",
        help="Why you are overriding the --supersede'd decision (required with it).",
    )
    p_decide.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # fact — record a working fix / research finding. Thin wrapper over the
    # decide plumbing: same store (decisions.kind='fact'), same author
    # stamping / outbox buffering / supersede gating.
    p_fact = sub.add_parser(
        "fact",
        help="Record a working fix / research finding (kind='fact' in the decisions store).",
    )
    p_fact.add_argument("topic", help="Short searchable label (e.g. 'pg-types-cache-stale-oid')")
    p_fact.add_argument(
        "--fact",
        dest="fact_text",
        required=True,
        help="The finding/fix, stated as a reusable rule.",
    )
    p_fact.add_argument(
        "--context",
        help="The raw symptom (error text / failing behavior) — embedded "
             "alongside topic+fact so a future semantic search by error text "
             "hits this row.",
    )
    p_fact.add_argument("--why", dest="rationale", help="Evidence the fix/finding works.")
    p_fact.add_argument(
        "--files",
        nargs="+",
        metavar="PATH",
        help="Files touched by this fact (optional)",
    )
    p_fact.add_argument("--session-id", help="Tag with session identifier")
    p_fact.add_argument(
        "--supersede",
        type=int,
        metavar="ID",
        help="Override an existing decision/fact (its id). Requires --override-reason.",
    )
    p_fact.add_argument(
        "--override-reason",
        help="Why you are overriding the --supersede'd entry (required with it).",
    )
    p_fact.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # decisions — list / search past decisions (and facts).
    p_decs = sub.add_parser(
        "decisions",
        help="List or search recorded decisions and facts.",
    )
    p_decs.add_argument("--topic", help="Case-insensitive substring filter on topic")
    p_decs.add_argument("--search", dest="search_q", help="Semantic search over topic+decision[+context]")
    p_decs.add_argument("--days", type=int, help="Only entries from the last N days")
    p_decs.add_argument("--limit", type=int, default=20)
    p_decs.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_decs.add_argument(
        "--kind",
        choices=("decision", "fact"),
        help="Filter to just this kind (default: both).",
    )
    p_decs.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )
    p_decs.add_argument(
        "--full",
        action="store_true",
        help="Print untruncated decision/why text (default: compact, ~200/120 char truncation).",
    )

    # resume — opinionated session-start brief.
    p_resume = sub.add_parser(
        "resume",
        help='"Where did I leave off?" — decisions + touched files + pending stage + hubs.',
    )
    p_resume.add_argument("--project", help="Scope to a specific project (name or abs path)")

    # consolidate — recurring-theme gap report (read-only).
    p_consol = sub.add_parser(
        "consolidate",
        help="Surface recurring history themes not yet recorded as decisions (read-only).",
    )
    p_consol.add_argument("--project", help="Scope to a specific project (name or abs path)")
    p_consol.add_argument("--days", type=int, default=90,
                          help="History window in days (default: 90)")
    p_consol.add_argument("--limit", type=int, default=100,
                          help="Max history entries to scan (default: 100)")
    p_consol.add_argument("--similarity", type=float, default=0.55,
                          help="Cluster similarity threshold (default: 0.55)")
    p_consol.add_argument("--covered", type=float, default=0.68,
                          help="Coverage threshold against decisions (default: 0.68)")
    p_consol.add_argument("--min-cluster", type=int, default=2, dest="min_cluster",
                          help="Minimum cluster size (default: 2)")
    p_consol.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )

    # get
    p_get = sub.add_parser("get", help="Fetch a chunk by id")
    p_get.add_argument("chunk_id", type=int)
    p_get.add_argument("--with-siblings", action="store_true")
    p_get.add_argument("--raw", action="store_true", help="Re-slice original bytes from disk")

    # path
    p_path = sub.add_parser("path", help="Print file_path:start_line-end_line for a chunk")
    p_path.add_argument("chunk_id", type=int)

    # projects
    p_projects = sub.add_parser("projects", help="List registered projects")
    p_projects.add_argument(
        "--local-sqlite",
        action="store_true",
        help="Force the listing against local sqlite even if the current "
             "cwd resolves to shared PostgreSQL. Useful for spotting "
             "projects you haven't migrated yet, or for verifying a "
             "post-migrate forget --sqlite-only worked.",
    )

    # stats
    p_stats = sub.add_parser("stats", help="DB + project statistics")
    p_stats.add_argument("--project", help="Scope stats to one project")

    # forget
    p_forget = sub.add_parser("forget", help="Delete a project and all its chunks")
    p_forget.add_argument("project", help="Project name or absolute path")
    p_forget.add_argument(
        "--sqlite-only",
        action="store_true",
        help="Force the deletion against local sqlite even if the current "
             "cwd resolves to shared PostgreSQL. Use after `db migrate` to "
             "drop the now-redundant local copy without cwd gymnastics.",
    )

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
        help="Remove a variable (or --all to clear a scope, --auto to clear "
             "auto-loaded rows).",
    )
    p_v_unset.add_argument(
        "scope",
        nargs="?",
        choices=sorted(("ansible", "terraform", "helm", "all")),
        help="Domain to operate on (required unless --auto is used).",
    )
    p_v_unset.add_argument("name", nargs="?", help="Variable name (omit with --all)")
    p_v_unset.add_argument(
        "--all",
        dest="unset_all",
        action="store_true",
        help="Clear every variable in the given scope",
    )
    p_v_unset.add_argument(
        "--auto",
        dest="unset_auto",
        action="store_true",
        help="Clear every auto-loaded variable (group_vars/host_vars). "
             "Manual rows are kept. Scope argument is ignored.",
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
        help="Wire the knowledge skill into one or more IDEs (Claude/Cursor/Codex/OpenCode/Gemini)",
    )
    p_install.add_argument(
        "--ide",
        default="claude",
        help=(
            "Comma-separated target IDEs: claude,cursor,codex,opencode,gemini "
            "(or 'all' for all five). Default: claude — "
            ".claude/skills/knowledge/SKILL.md. codex/opencode/gemini get the "
            "COMPACT AGENTS-style render (see `knowledge skill show` for the "
            "full guide); cursor's .mdc stays full (agent-requested, not "
            "always-on)."
        ),
    )
    p_install.add_argument(
        "--user",
        action="store_true",
        help="Install to the user/global location for each IDE instead of the cwd repo",
    )
    p_install.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink the source instead of copying (auto-updates on git pull)",
    )
    p_install.add_argument(
        "--always-apply",
        action="store_true",
        help="Cursor only: set alwaysApply: true in the .mdc (default: false, agent-requested)",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing dedicated skill file (SKILL.md / .mdc) at the target",
    )

    # skill — progressive-disclosure escape hatch: the compact AGENTS.md /
    # GEMINI.md renders end with "Full guide: run `knowledge skill show`" so
    # any IDE stuck with the compact form can pull the complete guide on demand.
    p_skill = sub.add_parser(
        "skill",
        help="Inspect the canonical knowledge skill content",
    )
    p_skill_sub = p_skill.add_subparsers(dest="skill_cmd", required=True)
    p_skill_sub.add_parser(
        "show",
        help="Print the full canonical skill body (frontmatter stripped)",
    )

    # config — runtime settings (storage mode, PG DSN status). Phase 0 of
    # the shared-PostgreSQL plan; works against sqlite-only installs too
    # (just reports "mode: sqlite").
    p_config = sub.add_parser(
        "config",
        help="Inspect / initialize .knowledge-config.json (storage mode, PG env)",
    )
    p_config_sub = p_config.add_subparsers(dest="config_cmd", required=True)

    p_cfg_init = p_config_sub.add_parser(
        "init",
        help="Write a config file: ~/.knowledge/config.json by default, "
             "--project for the git root",
    )
    p_cfg_init.add_argument(
        "--project",
        action="store_true",
        help="Write <git-root>/.knowledge-config.json instead of the laptop "
             "default. Same schema either way; the closer file wins at runtime.",
    )
    p_cfg_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file at the chosen target",
    )

    p_cfg_show = p_config_sub.add_parser(
        "show",
        help="Print effective storage mode, masked DSN, env-var status, source",
    )
    p_cfg_show.add_argument("--json", action="store_true", help="Machine-readable output")

    p_config_sub.add_parser(
        "check-env",
        help="Verify KNOWLEDGE_PG_* env vars are set when mode=shared_postgresql",
    )

    # db — backend administration (init-postgres). Phase 1a of the shared-PG
    # plan; only meaningful when storage.mode = shared_postgresql.
    p_db = sub.add_parser(
        "db",
        help="Backend administration (ping, init-postgres, …)",
    )
    p_db_sub = p_db.add_subparsers(dest="db_cmd", required=True)
    p_db_sub.add_parser(
        "ping",
        help="Open a connection to the configured backend, report version + "
             "extension status, then close. No state changes. Use this to "
             "verify your config / env / network before knowledge build.",
    )
    p_db_sub.add_parser(
        "init-postgres",
        help="Apply knowledge/schema/postgres/*.sql to the configured PG database "
             "(idempotent — re-applies are no-ops)",
    )
    p_db_migrate = p_db_sub.add_parser(
        "migrate",
        help="Copy ONE project from local SQLite to the configured shared PG. "
             "Embeddings, edges, history, decisions, project variables. "
             "Idempotent only at the conflict-check level — re-running on a "
             "project already present on PG fails fast.",
    )
    p_db_migrate.add_argument(
        "--project",
        required=True,
        help="Project name or absolute path (resolved against local sqlite)",
    )
    p_db_migrate.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (use in scripts/CI)",
    )
    p_db_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pre-flight checks (resolve, model match, conflict, counts) "
             "and print the plan; do not write to PG",
    )

    # daemon — warm-embedder background process (Item F). Spawned on demand
    # by embedder.get_embedder(); these verbs exist for manual control/triage.
    p_daemon = sub.add_parser(
        "daemon",
        help="Warm-embedder daemon: keeps the model loaded between commands "
             "(on by default; disable via config daemon.enabled=false or "
             "KNOWLEDGE_NO_DAEMON=1)",
    )
    p_daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd", required=True)
    p_daemon_sub.add_parser(
        "run",
        help="Run the daemon in the foreground (this is what the automatic "
             "spawn launches; log at ~/.knowledge/daemon/daemon.log)",
    )
    p_daemon_sub.add_parser(
        "status",
        help="Report running/not + pid, model, idle seconds "
             "(exit 0 running, 1 not)",
    )
    p_daemon_sub.add_parser(
        "stop",
        help="Ask a running daemon to exit (exit 0 even if none is running)",
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write the absolute path to `knowledge` (default; robust against "
        "PATH hijacking and GUI-launch PATH quirks). Use --no-absolute to write "
        "the bare 'knowledge' command instead.",
    )

    args = parser.parse_args(argv)
    try:
        return _DISPATCH[args.cmd](args)
    except db.offline_errors():
        # Safety net for the read commands (ask/find/grep/search/resume/
        # decisions/…): they need the shared DB and can't be served offline.
        # Write commands handle this themselves by buffering to the outbox,
        # so they won't reach here. Clean message instead of a raw traceback.
        print(
            "error: shared index unreachable (PostgreSQL is down or "
            "unconfigured). Reads need the DB; any writes are buffered locally "
            "and sync on the next reachable run.",
            file=sys.stderr,
        )
        return 4


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _warn_project_pg_redirect() -> None:
    """Warn when a repo-local config silently redirects storage to a remote PG (H2a).

    The cwd-walk-up config resolution is intentional (closer file wins), but a
    cloned/untrusted repo can ship a ``.knowledge-config.json`` pointing storage
    at an attacker-controlled PostgreSQL host — exfiltrating the indexed source
    and the env-var PG credentials' auth exchange. We do not block (that would
    break the legitimate team-mode workflow); we make the redirect visible.
    KNOWLEDGE_DATABASE_URL and the home config are explicit user choices and are
    not flagged.
    """
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError:
        return
    if s.mode != "shared_postgresql" or s.postgresql is None:
        return
    if not s.config_source.endswith(paths.PROJECT_CONFIG_NAME):
        return
    host = s.postgresql.host or ""
    if host in {"", "localhost", "127.0.0.1", "::1"}:
        return
    print(
        f"warning: project-local {paths.PROJECT_CONFIG_NAME} is redirecting "
        f"storage to postgresql://{host} — indexed source and your PostgreSQL "
        "credentials will be sent there. Ctrl-C now if this repo is untrusted.",
        file=sys.stderr,
    )


def cmd_build(args: argparse.Namespace) -> int:
    from . import indexer, outbox

    root = projects.current_project_root()
    _warn_project_pg_redirect()
    proposed_name = args.name or root.name
    print(f"building index for: {root}", flush=True)

    with db.connect() as conn:
        outbox.drain(conn, root)  # opportunistic flush of any buffered backlog
        # Rebuild in place? If a row already exists at this exact root, it's
        # not a collision — just a re-build of the same project. Skip the
        # collision check so the user isn't prompted on every rebuild.
        existing_here = db.fetch_one(
            conn,
            "SELECT 1 FROM projects WHERE root_path = ? LIMIT 1",
            (str(root.resolve()),),
        )

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
        try:
            project_id, files, chunks = indexer.build_project(
                conn, root, name_override=resolved_name
            )
        except db.ProjectBusyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    elapsed = time.time() - t0
    print(f"done: {files} files, {chunks} chunks in {elapsed:.1f}s (project_id={project_id})")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from . import indexer, outbox

    root = projects.current_project_root()
    _warn_project_pg_redirect()
    print(f"updating index for: {root}", flush=True)
    t0 = time.time()
    with db.connect() as conn:
        # The post-edit hook runs `update` often → a reliable, frequent moment
        # to flush any locally buffered decisions/history now the DB is up.
        synced = outbox.drain(conn, root)
        if synced:
            print(f"synced {synced} locally-buffered entr"
                  f"{'y' if synced == 1 else 'ies'} to the shared DB", flush=True)
        try:
            project_id, files_visited, chunks_embedded = indexer.update_project(
                conn, root, name_override=None
            )
        except db.ProjectBusyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
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

    rows = db.fetch_all(
        conn,
        "SELECT rel_path, mtime FROM files WHERE project_id = ?",
        (proj.id,),
    )

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


def _filter_decision_hits(hits, max_dist: float):
    """Return only hits whose distance is <= *max_dist*."""
    return [(d, dist) for (d, dist) in hits if dist <= max_dist]


def _print_ask_decisions(hits) -> None:
    """Print a compact preface of prior decisions/facts relevant to an ``ask`` query.

    Prints nothing when *hits* is empty.  Otherwise emits a header line and one
    compact line per hit so agents see recorded choices before reading code
    citations.

    Format::

        ⚑ prior decisions/facts (heed before answering):
          [kind] topic — first-line-of-decision (trimmed to 120 chars)  (id=N, YYYY-MM-DD, dist=D.DD)

    Followed by one blank line to visually separate the block from citations.
    """
    if not hits:
        return
    from datetime import datetime

    print("⚑ prior decisions/facts (heed before answering):")
    for d, dist in hits:
        when = datetime.fromtimestamp(d.created_at).strftime("%Y-%m-%d")
        excerpt = _first_line(d.decision, max_chars=120)
        print(
            f"  [{d.kind}] {d.topic} — {excerpt}"
            f"  (id={d.id}, {when}, dist={dist:.2f})"
        )
    print()


def cmd_decide(args: argparse.Namespace) -> int:
    """Record a decision (or, via ``cmd_fact``, a fact) — Phase 4 session
    memory / Item H. ``kind``/``context`` default to a plain decision when
    absent from ``args`` (the ``decide`` subparser doesn't define them —
    only ``fact`` does), so this one function backs both CLI verbs."""
    from . import outbox

    kind = getattr(args, "kind", "decision") or "decision"
    context = getattr(args, "context", None)

    topic = args.topic.strip()
    decision = args.decision.strip()
    if not topic or not decision:
        label = "--fact" if kind == "fact" else "--decision"
        print(f"error: topic and {label} must be non-empty", file=sys.stderr)
        return 2

    override_reason = (args.override_reason or "").strip() or None
    if override_reason and args.supersede is None:
        print(
            "error: --override-reason is only valid together with --supersede <id>",
            file=sys.stderr,
        )
        return 2

    # `decide`/`fact` should work even before a `build`; auto-create the
    # project row the same way `history add` does. Author is stamped on
    # every row so shared-DB teammates can see who set each standard.
    root = projects.current_project_root()
    author = projects.current_author(root)

    try:
        return _decide_online(
            args, root, author, topic, decision, override_reason, kind, context
        )
    except db.offline_errors() as exc:
        # Shared DB unreachable — buffer locally instead of crashing. Enforce
        # the override gate without the DB (the comment requirement is policy,
        # not a lookup); the prior-decision detail just isn't available offline.
        if args.supersede is not None and not override_reason:
            print(
                f"error: overriding id={args.supersede} requires "
                f'--override-reason "<why>" (shared DB offline — prior '
                f"details unavailable).",
                file=sys.stderr,
            )
            return 3
        outbox.append(
            "decision",
            root,
            {
                "topic": topic,
                "decision": decision,
                "rationale": args.rationale,
                "files_touched": args.files,
                "session_id": args.session_id,
                "author": author,
                "supersedes": args.supersede,
                "override_reason": override_reason if args.supersede else None,
                "kind": kind,
                "context": context,
            },
        )
        noun = "fact" if kind == "fact" else "decision"
        print(
            f"note: shared DB unreachable — {noun} buffered locally; "
            "will sync on the next reachable run."
        )
        return 0


def _decide_online(
    args, root, author, topic, decision, override_reason, kind="decision", context=None
) -> int:
    """The DB-backed decide/fact path. Raises ``db.offline_errors()`` if PG is
    unreachable; ``cmd_decide`` catches that and buffers to the outbox."""
    from datetime import datetime

    from . import decisions as decisions_mod
    from . import outbox

    noun = "fact" if kind == "fact" else "decision"

    with db.connect() as conn:
        outbox.drain(conn, root)  # push any backlog now that the DB is reachable
        proj = projects.get_or_create_project(conn, root)

        supersedes: int | None = None
        if args.supersede is not None:
            # --- Override gate ---------------------------------------------
            # Overriding a teammate's standard is exactly the case where
            # others rely on the old behavior, so we hard-block until a
            # justification comment is supplied (non-interactive: exit 3,
            # like the indexer's busy path).
            target = decisions_mod.get(conn, args.supersede)
            if target is None or target.project_id != proj.id:
                print(
                    f"error: no decision id={args.supersede} in "
                    f"'{proj.name}' ({proj.root_path})",
                    file=sys.stderr,
                )
                return 2
            if not override_reason:
                prior_when = datetime.fromtimestamp(
                    target.created_at
                ).strftime("%Y-%m-%d")
                prior_by = target.author or "unknown"
                print(
                    f"⚠️  overriding {target.kind} id={target.id} "
                    f"'{target.topic}' (set {prior_when} by {prior_by}).\n"
                    f"    decision: {target.decision}\n"
                    f"error: overriding a prior entry requires "
                    f'--override-reason "<why>" — teammates rely on it.',
                    file=sys.stderr,
                )
                return 3
            supersedes = target.id

        # Non-blocking nudge: same-topic re-use without an explicit override is
        # usually an unintended fork of an existing standard. Look up the prior
        # match BEFORE inserting so we don't match the new row itself.
        prior = (
            decisions_mod.exact_topic_match(conn, proj.id, topic)
            if supersedes is None
            else None
        )

        new_id = decisions_mod.add(
            conn,
            project_id=proj.id,
            topic=topic,
            decision=decision,
            rationale=args.rationale,
            files_touched=args.files,
            session_id=args.session_id,
            author=author,
            supersedes=supersedes,
            override_reason=override_reason if supersedes else None,
            kind=kind,
            context=context,
        )

        if prior is not None:
            prior_by = f" by {prior.author}" if prior.author else ""
            print(
                f"note: {prior.kind} id={prior.id} already covers topic "
                f"'{topic}'{prior_by}; pass --supersede {prior.id} "
                f"if you mean to override it.",
                file=sys.stderr,
            )

    suffix = f" (supersedes id={supersedes})" if supersedes else ""
    print(
        f"recorded {noun} id={new_id} by {author} in "
        f"'{proj.name}' ({proj.root_path}){suffix}"
    )
    return 0


def cmd_fact(args: argparse.Namespace) -> int:
    """Record a working fix / research finding — Item H. Thin wrapper over
    ``cmd_decide``: same store (``decisions.kind='fact'``), same author
    stamping / outbox buffering / supersede gating.
    """
    args.decision = args.fact_text
    args.kind = "fact"
    return cmd_decide(args)


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
                kind=args.kind,
            )
            entries = [(d, dist) for d, dist in raw]
        else:
            plain = decisions_mod.recent(
                conn,
                project_id=proj.id,
                days=args.days,
                topic=args.topic,
                limit=args.limit,
                kind=args.kind,
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
        _print_decisions(entries, full=args.full)
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


def _truncate_words(text, limit: int) -> str:
    """Cut ``text`` at the last word boundary at-or-before ``limit`` chars.

    Appends '…' only when truncation actually happened. Text at or under the
    limit is returned unchanged. ``None``/empty input passes through as-is.
    """
    if not text:
        return text
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def _short_author(author):
    """Drop the '<email>' portion of a 'Name <email>' author string.

    Leaves bare emails (no name part) or plain names/logins untouched —
    there is nothing shorter to fall back to in those cases.
    """
    if not author:
        return author
    import re

    m = re.match(r"^(.+?)\s*<[^>]*>$", author)
    if m and m.group(1):
        return m.group(1)
    return author


def _print_decisions(entries, full: bool = False) -> None:
    """Pretty-print (Decision, distance|None) tuples.

    Compact by default (~200-char decision / ~120-char why, short author) to
    keep the mandated pre-change conflict check cheap for LLM agents; pass
    ``full=True`` (CLI: --full) for the verbatim text and full author string.
    """
    from datetime import datetime

    if not entries:
        print("(no decisions)")
        return
    for i, (d, dist) in enumerate(entries):
        if full:
            author_s = d.author
            decision_s = d.decision
            rationale_s = d.rationale
        else:
            if i > 0:
                print()
            author_s = _short_author(d.author)
            decision_s = _truncate_words(d.decision, 200)
            rationale_s = _truncate_words(d.rationale, 120)
        when = datetime.fromtimestamp(d.created_at).strftime("%Y-%m-%d %H:%M")
        dist_s = f"  dist={dist:.3f}" if dist is not None else ""
        by = f"  by {author_s}" if author_s else ""
        marker = "[fact] " if d.kind == "fact" else ""
        print(f"{when}  id={d.id}{by}{dist_s}")
        print(f"  topic:    {marker}{d.topic}")
        print(f"  decision: {decision_s}")
        if rationale_s:
            print(f"  why:      {rationale_s}")
        if d.supersedes:
            ovr = f" — {d.override_reason}" if d.override_reason else ""
            print(f"  overrides: id={d.supersedes}{ovr}")
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
            by = f"  ({d.author})" if d.author else ""
            ovr = f"  [overrides id={d.supersedes}]" if d.supersedes else ""
            marker = "[fact] " if d.kind == "fact" else ""
            print(f"  {when}  {marker}{d.topic}{by}{ovr}")
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


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Recurring-theme gap report — read-only consolidation of history vs decisions."""
    import json as json_mod
    from . import consolidate as consolidate_mod

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        report = consolidate_mod.build(
            conn,
            project_id=proj.id,
            project_name=proj.name,
            project_root=str(proj.root_path),
            days=args.days,
            limit=args.limit,
            sim_threshold=args.similarity,
            covered_threshold=args.covered,
            min_size=args.min_cluster,
        )

    if args.format == "json":
        def _serialise(report) -> dict:
            return {
                "project_name": report.project_name,
                "project_root": report.project_root,
                "scanned_history_n": report.scanned_history_n,
                "total_decisions": report.total_decisions,
                "covered_skipped_n": report.covered_skipped_n,
                "singletons_n": report.singletons_n,
                "candidates": [
                    {
                        "suggested_topic": c.suggested_topic,
                        "entry_ids": [e.id for e in c.entries],
                        "entry_shorts": [e.short_summary for e in c.entries],
                        "files": c.files,
                        "nearest_decision_id": (
                            c.nearest_decision.id if c.nearest_decision else None
                        ),
                        "nearest_sim": round(c.nearest_sim, 4),
                        "cohesion": round(c.cohesion, 4),
                    }
                    for c in report.candidates
                ],
            }
        print(json_mod.dumps(_serialise(report), indent=2))
    else:
        _print_consolidate(report, covered_threshold=args.covered)
    return 0


def _print_consolidate(report, covered_threshold: float = 0.68) -> None:
    """Plain-text consolidation report."""
    from datetime import datetime

    print(f"{report.project_name}  ({report.project_root})")
    print(
        f"scanned {report.scanned_history_n} history · "
        f"{report.total_decisions} decisions · "
        f"{len(report.candidates)} candidate theme(s)  "
        f"({report.covered_skipped_n} covered, {report.singletons_n} one-off)"
    )
    print(
        "# candidates below are NOT recorded"
        " — review and run `knowledge decide` yourself"
    )

    # ---- empty states ----
    if report.scanned_history_n < 2:
        print("(not enough history yet)")
        return

    total_clusters = len(report.candidates) + report.covered_skipped_n
    if total_clusters == 0:
        print(
            f"(no recurring themes — all {report.scanned_history_n} entries are"
            " unique by similarity; try --similarity 0.45 to widen)"
        )
        return

    if not report.candidates and report.covered_skipped_n > 0:
        print(
            f"(all {report.covered_skipped_n} recurring theme(s) already covered"
            " by existing decisions)"
        )
        return

    # ---- per-candidate output ----
    for k, c in enumerate(report.candidates, start=1):
        notes = []
        if c.has_near_dupes:
            notes.append("near-duplicate entries")
        if c.truncated:
            notes.append("truncated")
        note_str = f"  ({', '.join(notes)})" if notes else ""

        print(f"\n# theme {k} — \"{c.suggested_topic}\"  ({len(c.entries)} entries){note_str}")

        # Nearest decision line.
        if c.nearest_decision:
            d = c.nearest_decision
            print(
                f"  closest decision: #{d.id} \"{d.topic}\""
                f" sim={c.nearest_sim:.2f}"
                f" (< {covered_threshold} → not covered)"
            )
        else:
            print("  no existing decisions")

        # Entries.
        print("  entries:")
        for e in c.entries:
            date_s = datetime.fromtimestamp(e.created_at).strftime("%Y-%m-%d")
            print(f"    id={e.id} {date_s}  {e.short_summary}")

        # Files.
        if c.files:
            print(f"  files: {', '.join(c.files)}")

        # Scaffold.
        files_arg = (
            " --files " + " ".join(c.files) if c.files else ""
        )
        print(
            f"  → consider: knowledge decide \"{c.suggested_topic}\""
            f" --decision \"<FILL IN>\"{files_arg}"
        )


def cmd_ask(args: argparse.Namespace) -> int:
    """Hybrid search + rerank + cache — Phase 3 entry point for agents."""
    from . import config as _config
    from . import decisions as decisions_mod
    from . import hybrid_search

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        # Cross-client cache-invalidation signal — no extra DB query, proj
        # is already fetched. See knowledge/query_cache.py module docstring.
        index_stamp = max(proj.last_build or 0, proj.last_update or 0)
        results = hybrid_search.ask(
            conn,
            query=args.question,
            project_id=proj.id,
            project_root=proj.root_path,
            kind=args.kind,
            lang=args.lang,
            top_k=args.top_k,
            use_cache=not args.no_cache,
            index_stamp=index_stamp,
        )
        # Decisions preface — only for the default citations format and unless
        # the caller opted out.  Runs inside the same conn block so
        # decisions.search can use the open connection.
        if args.format == "citations" and not args.no_decisions:
            dec_hits = decisions_mod.search(
                conn,
                args.question,
                project_id=proj.id,
                top_k=_config.ASK_DECISION_TOP_K,
            )
            dec_hits = _filter_decision_hits(
                dec_hits, _config.ASK_DECISION_MAX_DISTANCE
            )
            _print_ask_decisions(dec_hits)

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


def _contained_abs_path(project_root: str, rel_path: str) -> Path | None:
    """Resolve ``project_root / rel_path`` and confirm it stays inside the root.

    Security guard (H1): chunk rows carry ``rel_path`` / ``project_root`` from the
    database. On a SHARED PostgreSQL backend those rows are written by teammates
    and are therefore untrusted input to every other user's CLI. An absolute
    ``rel_path`` (``/etc/shadow``), a ``../`` traversal, or a ``project_root`` of
    ``/`` would otherwise make ``get --raw`` / ``path`` read or disclose arbitrary
    local files. Returns the resolved path only when it is contained within the
    resolved project root; otherwise ``None``.
    """
    root_resolved = Path(project_root).resolve()
    candidate = (root_resolved / rel_path).resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    return candidate


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

    if args.raw:
        abs_path = _contained_abs_path(project_root, rel_path)
        if abs_path is None:
            print(
                f"error: chunk path escapes project root: {rel_path!r}",
                file=sys.stderr,
            )
            return 1
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

    if raw:
        # Parent's byte range covers the whole original — one disk read
        # reassembles exactly what was on disk at build time.
        abs_path = _contained_abs_path(project_root, rel_path)
        if abs_path is None:
            print(
                f"error: chunk path escapes project root: {rel_path!r}",
                file=sys.stderr,
            )
            return 1
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
    abs_path = _contained_abs_path(project_root, rel_path)
    if abs_path is None:
        print(
            f"error: chunk path escapes project root: {rel_path!r}",
            file=sys.stderr,
        )
        return 1
    print(f"{abs_path}:{sl}-{el}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    if getattr(args, "local_sqlite", False):
        return _cmd_projects_sqlite_only()

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


def _cmd_projects_sqlite_only() -> int:
    """List projects from local sqlite regardless of cwd resolution.

    Same dispatch-bypass pattern as ``forget --sqlite-only`` — opens
    raw APSW via :func:`db.connect_sqlite` so the listing reflects the
    laptop's local index even when the current cwd routes everything
    else to shared PostgreSQL.
    """

    conn = db.connect_sqlite()
    try:
        rows = conn.execute(
            "SELECT name, root_path, file_count, chunk_count "
            "FROM projects ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No projects in local sqlite.")
        return 0
    header = f"{'NAME':<24} {'FILES':>6} {'CHUNKS':>7}  ROOT"
    print(header)
    print("-" * len(header))
    for name, root, fc, cc in rows:
        print(f"{name:<24} {fc:>6} {cc:>7}  {root}")
    print()
    print(f"({len(rows)} project(s) — sqlite source: {paths.db_path()})")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = projects.list_projects(conn)
        total_chunks = db.fetch_one(conn, "SELECT COUNT(*) FROM chunks")[0]
        total_files = db.fetch_one(conn, "SELECT COUNT(*) FROM files")[0]
    if db.current_mode() == "postgresql":
        from . import settings as settings_mod

        s = settings_mod.load_settings()
        pg = s.postgresql
        backend_descr = (
            f"postgresql ({pg.host}:{pg.port}/{pg.database})"
            if pg is not None else "postgresql"
        )
        print(f"DB:          {backend_descr}")
        print(f"config:      {s.config_source}")
    else:
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
    if getattr(args, "sqlite_only", False):
        return _cmd_forget_sqlite_only(args)

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


def _cmd_forget_sqlite_only(args: argparse.Namespace) -> int:
    """Force-delete a project from local sqlite regardless of current cwd's mode.

    Uses raw APSW (``db.connect_sqlite()`` + ``conn.execute()``) so we
    don't go through the ``db.fetch_one`` / ``db.execute`` helpers that
    dispatch on ``current_mode()``. When current_mode is "postgresql"
    those helpers would translate ``?`` placeholders to ``%s`` and break
    on APSW.
    """

    selector = args.project
    p = Path(selector).expanduser()
    conn = db.connect_sqlite()
    try:
        if p.is_absolute():
            row = conn.execute(
                "SELECT id, name, root_path FROM projects WHERE root_path = ?",
                (str(p.resolve()),),
            ).fetchone()
            matches = [row] if row else []
        else:
            matches = conn.execute(
                "SELECT id, name, root_path FROM projects WHERE name = ?",
                (selector,),
            ).fetchall()

        if not matches:
            print(
                f"error: project not found in local sqlite: {selector}",
                file=sys.stderr,
            )
            return 1

        if len(matches) > 1:
            if not sys.stdin.isatty():
                print(
                    f"error: '{selector}' matches {len(matches)} local sqlite "
                    f"rows. Re-run with the absolute path:",
                    file=sys.stderr,
                )
                for _id, _name, root in matches:
                    print(f"  {root}", file=sys.stderr)
                return 1
            print(
                f"'{selector}' matches {len(matches)} local sqlite rows.",
                "Pick which to forget (comma-separated indexes, or 'all'):",
            )
            for i, (_id, name, root) in enumerate(matches, start=1):
                print(f"  [{i}] {name}  {root}")
            try:
                answer = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\naborted.", file=sys.stderr)
                return 1
            if answer == "all":
                chosen = matches
            else:
                idxs = [int(t) for t in answer.split(",") if t.strip().isdigit()]
                chosen = [matches[i - 1] for i in idxs if 1 <= i <= len(matches)]
            if not chosen:
                print("aborted.", file=sys.stderr)
                return 1
        else:
            chosen = matches

        # vec0 has no FK cascade; wipe per-table before dropping the
        # project row. ``meta`` / `query_cache` cascade naturally via the
        # projects.id FK.
        for proj_id, name, root in chosen:
            conn.execute(
                "DELETE FROM chunks_vec WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE project_id = ?)",
                (proj_id,),
            )
            conn.execute(
                "DELETE FROM history_vec WHERE history_id IN "
                "(SELECT id FROM history WHERE project_id = ?)",
                (proj_id,),
            )
            conn.execute(
                "DELETE FROM decisions_vec WHERE decision_id IN "
                "(SELECT id FROM decisions WHERE project_id = ?)",
                (proj_id,),
            )
            conn.execute("DELETE FROM projects WHERE id = ?", (proj_id,))
            print(f"forgot from sqlite: {name} ({root})")
    finally:
        # APSW connections close on garbage collection but be explicit.
        conn.close()
    return 0


# Markers that delimit the knowledge block inside a (possibly user-owned)
# AGENTS.md, so re-installs replace only our content and never clobber prose the
# user added around it.
_AGENTS_BLOCK_BEGIN = "<!-- BEGIN knowledge skill (managed by `knowledge install-skill`) -->"
_AGENTS_BLOCK_END = "<!-- END knowledge skill -->"

# Per-IDE install matrix. `dest` is relative to the cwd repo (project scope) or
# to $HOME (user scope). `dedicated` files are ours alone (copy/symlink, --force
# to overwrite); non-dedicated files (AGENTS.md / GEMINI.md) are merged via a
# managed block because users commonly keep their own content there — no
# --force needed, re-installs just replace the block in place.
_IDE_TARGETS = {
    "claude": {
        "sibling": "SKILL.md",
        "project_dest": Path(".claude/skills/knowledge/SKILL.md"),
        "user_dest": Path(".claude/skills/knowledge/SKILL.md"),
        "dedicated": True,
    },
    "cursor": {
        "sibling": "knowledge.mdc",
        "project_dest": Path(".cursor/rules/knowledge.mdc"),
        "user_dest": None,  # Cursor has no stable user-global rules file
        "dedicated": True,
    },
    "codex": {
        "sibling": "AGENTS.md",
        "project_dest": Path("AGENTS.md"),
        "user_dest": Path(".codex/AGENTS.md"),
        "dedicated": False,
    },
    "opencode": {
        "sibling": "AGENTS.md",
        "project_dest": Path("AGENTS.md"),
        "user_dest": Path(".config/opencode/AGENTS.md"),
        "dedicated": False,
    },
    "gemini": {
        # Same generated sibling as codex/opencode (compact render) — gemini-cli
        # just reads it from a differently-named file. Its own `contextFileName`
        # setting can alternatively be pointed at AGENTS.md directly.
        "sibling": "AGENTS.md",
        "project_dest": Path("GEMINI.md"),
        "user_dest": Path(".gemini/GEMINI.md"),
        "dedicated": False,
    },
}


def _parse_ides(raw: str) -> list[str] | None:
    """Expand the --ide value into an ordered, de-duplicated list of IDE keys."""
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if "all" in tokens:
        return list(_IDE_TARGETS)
    out: list[str] = []
    for t in tokens:
        if t not in _IDE_TARGETS:
            print(
                f"error: unknown IDE '{t}'. choose from: "
                f"{', '.join(_IDE_TARGETS)}, all",
                file=sys.stderr,
            )
            return None
        if t not in out:
            out.append(t)
    return out


def _merge_agents_block(target: Path, body: str) -> str:
    """Insert/replace the managed knowledge block in an AGENTS.md, returning status."""
    block = f"{_AGENTS_BLOCK_BEGIN}\n{body.rstrip()}\n{_AGENTS_BLOCK_END}\n"
    if not (target.exists() and not target.is_symlink()):
        target.write_text(block, encoding="utf-8")
        return "created"
    existing = target.read_text(encoding="utf-8")
    start = existing.find(_AGENTS_BLOCK_BEGIN)
    if start != -1:
        end = existing.find(_AGENTS_BLOCK_END, start)
        if end != -1:
            end += len(_AGENTS_BLOCK_END)
            # consume a single trailing newline so re-runs don't accumulate them
            if end < len(existing) and existing[end] == "\n":
                end += 1
            merged = existing[:start] + block + existing[end:]
            target.write_text(merged, encoding="utf-8")
            return "block replaced"
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    target.write_text(existing + sep + block, encoding="utf-8")
    return "block appended (existing content preserved)"


def cmd_install_skill(args: argparse.Namespace) -> int:
    """Install the knowledge skill into one or more IDEs (copy, symlink, or merge)."""
    import shutil

    from . import skill_render

    src_dir = Path(__file__).resolve().parent.parent / "skill-template"
    skill_path = src_dir / "SKILL.md"
    if not skill_path.is_file():
        print(
            f"error: skill template not found at {skill_path}\n"
            "expected the repo-knowledge repo layout (editable install).",
            file=sys.stderr,
        )
        return 1

    ides = _parse_ides(args.ide)
    if ides is None:
        return 2

    skill_text = skill_path.read_text(encoding="utf-8")
    base = Path.home() if args.user else Path.cwd()
    rc = 0
    seen_dests: set[Path] = set()

    for ide in ides:
        spec = _IDE_TARGETS[ide]
        dest_rel = spec["user_dest"] if args.user else spec["project_dest"]
        if dest_rel is None:
            print(f"note: {ide} has no user-global location — skipping (try without --user).")
            continue
        target = (base / dest_rel).resolve()

        # codex + opencode share ./AGENTS.md — write it once.
        if target in seen_dests:
            print(f"note: {ide} shares {dest_rel} with an already-written target — skipping.")
            continue
        seen_dests.add(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        sibling = src_dir / spec["sibling"]

        if spec["dedicated"]:
            if target.exists() or target.is_symlink():
                if not args.force:
                    print(
                        f"error: {target} already exists. Re-run with --force to overwrite.",
                        file=sys.stderr,
                    )
                    rc = 1
                    continue
                target.unlink()
            if args.symlink:
                target.symlink_to(sibling)
                mode = f"symlinked → {sibling}"
            elif ide == "cursor":
                target.write_text(
                    skill_render.render_cursor(skill_text, always_apply=args.always_apply),
                    encoding="utf-8",
                )
                mode = "rendered"
            else:  # claude — SKILL.md verbatim
                shutil.copyfile(skill_path, target)
                mode = "copied"
            print(f"[{ide}] installed: {target}  ({mode})")
        else:
            # AGENTS.md — shared, user-owned-friendly merge.
            if args.symlink and not (target.exists() or target.is_symlink()):
                target.symlink_to(sibling)
                print(f"[{ide}] installed: {target}  (symlinked → {sibling})")
            else:
                if args.symlink:
                    print(
                        f"[{ide}] {target} already exists — merging a managed block "
                        "instead of symlinking (can't symlink alongside your content)."
                    )
                status = _merge_agents_block(target, skill_render.render_agents(skill_text))
                print(f"[{ide}] installed: {target}  ({status})")

    print()
    print("next steps:")
    if args.user:
        print("  the knowledge skill is now available in every project for the chosen IDEs.")
    else:
        print("  the knowledge skill is now available in this project for the chosen IDEs.")
        print("  commit the installed file(s) if you want teammates to share them.")
    print("  from a repo root, run `knowledge build` once to index the code.")

    # Hint when this scope resolves to shared_postgresql — agents picking up
    # the skill need the env vars set, otherwise every command fails at the
    # first DSN resolution.
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError:
        return rc
    if s.mode == "shared_postgresql":
        print()
        print(
            "shared_postgresql is active for this scope "
            f"({s.config_source}) — verify the laptop has env vars exported:"
        )
        print("  knowledge config check-env")
    return rc


def cmd_skill_show(args: argparse.Namespace) -> int:
    """Print the full canonical skill body (frontmatter stripped).

    The progressive-disclosure escape hatch: compact IDE renders (AGENTS.md /
    GEMINI.md) end with "Full guide: run `knowledge skill show`" because their
    always-on context budget can't hold the complete guide. Reuses the same
    ``skill-template/SKILL.md`` resolution as ``cmd_install_skill``.
    """
    from . import skill_render

    src_dir = Path(__file__).resolve().parent.parent / "skill-template"
    skill_path = src_dir / "SKILL.md"
    if not skill_path.is_file():
        print(
            f"error: skill template not found at {skill_path}\n"
            "expected the repo-knowledge repo layout (editable install).",
            file=sys.stderr,
        )
        return 1

    skill_text = skill_path.read_text(encoding="utf-8")
    print(skill_render.strip_frontmatter(skill_text).lstrip("\n"))
    return 0


_SKILL_DISPATCH = {
    "show": cmd_skill_show,
}


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
    """Return the shell command string written into settings.json.

    Defaults to the ABSOLUTE path of the ``knowledge`` binary (M1). Writing a
    bare ``knowledge`` into a hook that auto-fires every turn is a PATH-hijack
    vector: anything earlier on ``$PATH`` (a ``.``-in-PATH entry, a compromised
    venv ``bin/``, a project-local ``./knowledge``) would run on every Claude
    Code interaction. ``--no-absolute`` opts back into the bare command.
    """
    if not absolute:
        return _HOOK_CMD_SUFFIX
    import shutil
    resolved = shutil.which("knowledge")
    if resolved is None:
        print(
            "warning: 'knowledge' not found on PATH; writing the bare command. "
            "Re-run from an environment where `knowledge` resolves, or pass "
            "--no-absolute deliberately.",
            file=sys.stderr,
        )
        return _HOOK_CMD_SUFFIX
    # Surface (don't block) when the resolved binary lives somewhere that could
    # itself be attacker-influenced — a virtualenv or a path under cwd.
    markers = ("/.venv/", "/venv/", "/site-packages/", "/node_modules/")
    if any(m in resolved for m in markers) or resolved.startswith(str(Path.cwd())):
        print(
            f"note: hook will call {resolved} — verify this path is trusted "
            "(it sits inside a virtualenv or the current project tree).",
            file=sys.stderr,
        )
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
    from . import history, outbox

    root = projects.current_project_root()
    try:
        with db.connect() as conn:
            outbox.drain(conn, root)
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
    except db.offline_errors():
        outbox.append(
            "history",
            root,
            {
                "short_summary": args.short,
                "long_summary": args.long,
                "session_id": args.session_id,
                "tags": args.tags,
            },
        )
        print(
            "note: shared DB unreachable — history entry buffered locally; "
            "will sync on the next reachable run."
        )
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
        # L5: --stage-file accepts an arbitrary path whose entries are inserted
        # under the current project. Surface (don't block) an out-of-tree file so
        # a scripted/hook caller can't quietly ingest untrusted JSONL.
        try:
            stage_dir_root = paths.stage_dir().resolve()
            if not stage.resolve().is_relative_to(stage_dir_root):
                print(
                    f"note: ingesting a stage file outside {stage_dir_root}: "
                    f"{stage.resolve()}",
                    file=sys.stderr,
                )
        except (OSError, AttributeError):
            pass
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
    rows = db.fetch_all(
        conn,
        "SELECT rel_path, lang FROM files "
        "WHERE project_id = ? AND rel_path LIKE ? AND rel_path != ? "
        "ORDER BY rel_path LIMIT 50",
        (project_id, pattern, rel_path),
    )
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

    if args.unset_auto:
        # --auto is mutually exclusive with name/scope/all — it clears
        # every auto-loaded row regardless of scope.
        if args.unset_all or args.name or args.scope:
            print(
                "error: --auto clears all auto-loaded rows; do not pass "
                "scope/name/--all with it.",
                file=sys.stderr,
            )
            return 1
    else:
        if not args.scope:
            print(
                "error: scope is required (ansible|terraform|helm|all) "
                "unless --auto is used.",
                file=sys.stderr,
            )
            return 1
        if not args.unset_all and not args.name:
            print(
                "error: give a variable name, or pass --all to clear the "
                "whole scope.",
                file=sys.stderr,
            )
            return 1

    with db.connect() as conn:
        proj = _resolve_project_or_error(conn, args.project)
        if proj is None:
            return 1
        try:
            if args.unset_auto:
                removed = variables.unset_auto_all(conn, proj.id)
                print(
                    f"cleared {removed} auto-loaded variable(s) for {proj.name}"
                )
            elif args.unset_all:
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
                        "source": v.source,
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
        suffix = "" if v.source == "manual" else f"  ({v.source})"
        print(f"  [{v.scope:<9}] {v.name:<24} = {v.value}{suffix}")
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
    # L4: --output writes (and mkdir-parents) to any user-supplied path. Warn
    # when the target escapes the current working tree so a stray/relative
    # ``../../`` can't silently clobber a file elsewhere.
    abs_target = out_path.resolve()
    if not abs_target.is_relative_to(Path.cwd().resolve()):
        print(f"note: writing graph outside the current directory: {abs_target}",
              file=sys.stderr)
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
    "fact": cmd_fact,
    "decisions": cmd_decisions,
    "resume": cmd_resume,
    "consolidate": cmd_consolidate,
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
    "skill": lambda args: _SKILL_DISPATCH[args.skill_cmd](args),
    "config": lambda args: _CONFIG_DISPATCH[args.config_cmd](args),
    "db": lambda args: _DB_DISPATCH[args.db_cmd](args),
    "daemon": lambda args: _DAEMON_DISPATCH[args.daemon_cmd](args),
}


# ---------------------------------------------------------------------------
# config subcommands (Phase 0)
# ---------------------------------------------------------------------------


def cmd_config_init(args: argparse.Namespace) -> int:
    """Write a config file at the chosen scope.

    Default target: ``~/.knowledge/config.json`` (laptop default).
    With ``--project``: ``<git-root>/.knowledge-config.json`` (per-repo override).

    Same JSON schema at every scope; resolution at runtime walks up from cwd
    and uses the closest match (with the laptop default as last-resort
    fallback). So ``--project`` is just "put it closer", nothing more.

    Refuses to overwrite an existing file unless ``--force`` is passed.
    """

    from . import settings as settings_mod

    if getattr(args, "project", False):
        dst = projects.current_project_root() / paths.PROJECT_CONFIG_NAME
        scope = "project"
    else:
        dst = paths.home_config_path()
        scope = "laptop default"

    if dst.exists() and not args.force:
        print(f"{dst} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    dst.write_text(settings_mod.CONFIG_TEMPLATE_JSON, encoding="utf-8")
    print(f"wrote {dst}  ({scope})")
    print("next: set storage.mode to 'shared_postgresql' if you want PG, then")
    print("      export KNOWLEDGE_PG_USER / KNOWLEDGE_PG_PASSWORD in your shell.")
    print("      knowledge config show  # confirms which file is in effect")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """Print mode + masked DSN + env-var status + source.

    Never prints secrets — passwords are masked, env-var values are reported
    as ``set`` / ``unset`` only.
    """

    from . import settings as settings_mod

    report = settings_mod.build_report()
    s = report.settings
    if args.json:
        out = {
            "mode": s.mode,
            "config_source": s.config_source,
            "dsn_source": report.dsn_source,
            "dsn_masked": report.dsn_masked,
            "env_status": report.env_status,
            "error": report.error,
        }
        print(json.dumps(out, indent=2))
        return 0 if not report.error else 1

    print(f"mode:           {s.mode}")
    print(f"config_source:  {s.config_source}{_describe_source(s.config_source)}")
    print(f"dsn_source:     {report.dsn_source}")
    if s.mode == "shared_postgresql":
        if report.dsn_masked:
            print(f"dsn:            {report.dsn_masked}")
        for name, present in report.env_status.items():
            print(f"{name:14s}  {'set' if present else 'unset'}")
    if report.error:
        print(f"error:          {report.error}", file=sys.stderr)
        return 1
    return 0


def _describe_source(source: str) -> str:
    """Append a human-readable scope tag to a config_source path.

    The tag is derived from *where* the file was found: the laptop default
    lives at ``~/.knowledge/config.json``; anything else is a per-project
    ``.knowledge-config.json`` (or per-subdir).
    """

    if source == "default":
        return ""
    try:
        # Resolve both sides to match symlinks (macOS ``/tmp`` ↔
        # ``/private/tmp`` etc. — settings.load_settings calls .resolve()
        # on the discovered path, paths.home_config_path() does not).
        if Path(source).resolve() == paths.home_config_path().resolve():
            return "  (laptop default)"
    except OSError:
        pass
    return "  (project)"


def cmd_config_check_env(args: argparse.Namespace) -> int:
    """Exit 0 if PG env vars are set (or mode is sqlite); exit 2 otherwise.

    Designed for CI / shell scripts: ``knowledge config check-env || exit``.
    """

    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if s.mode == "sqlite":
        print("mode: sqlite — env vars not required")
        return 0

    try:
        settings_mod.resolve_pg_dsn(s)
    except settings_mod.DsnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("ok: shared_postgresql credentials resolved from environment")
    return 0


_CONFIG_DISPATCH = {
    "init": cmd_config_init,
    "show": cmd_config_show,
    "check-env": cmd_config_check_env,
}


# ---------------------------------------------------------------------------
# db subcommands (Phase 1a)
# ---------------------------------------------------------------------------


def cmd_db_init_postgres(args: argparse.Namespace) -> int:
    """Apply ``knowledge/schema/postgres/NNN_*.sql`` to the configured DB.

    Refuses to run unless ``storage.mode == 'shared_postgresql'`` — there
    is no scenario where a sqlite-only user benefits from creating a remote
    PG schema. Migrations are idempotent (every CREATE uses IF NOT EXISTS),
    so re-running is safe — handy when bumping the schema version later.
    """

    del args  # currently unused
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if s.mode != "shared_postgresql":
        print(
            "error: storage.mode is 'sqlite'. Set "
            "storage.mode='shared_postgresql' in a discoverable "
            ".knowledge-config.json (repo root or ~/.knowledge/config.json) "
            "before running this.",
            file=sys.stderr,
        )
        return 2

    from .backends.postgres import PostgresBackend, _DependencyMissing

    backend = PostgresBackend(s)
    try:
        # init-postgres is where extension state can change (fresh install /
        # recreation ⇒ new type OIDs) — bypass and rewrite the local type
        # cache so it can never go stale across a re-init.
        conn = backend.connect(refresh_types=True)
    except _DependencyMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except settings_mod.DsnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — psycopg has many failure modes
        print(f"error connecting to PostgreSQL: {exc}", file=sys.stderr)
        return 2

    try:
        applied = backend.apply_schema(conn)
    finally:
        conn.close()

    if applied:
        print("applied:")
        for name in applied:
            print(f"  {name}")
    else:
        print("no migrations found (knowledge/schema/postgres/ is empty?)")
    return 0


def cmd_db_ping(args: argparse.Namespace) -> int:
    """Connect to the configured backend, verify it works, close.

    For sqlite: opens the local DB, runs a trivial SELECT, reports the file
    path and a row-count from `meta`.

    For shared_postgresql: opens psycopg, runs ``SELECT version()``, checks
    that ``CREATE EXTENSION vector`` has been applied (else hints at
    ``knowledge db init-postgres``), prints database name + role.

    Exits 2 on any connection or auth failure. Read-only — safe to run
    against production.
    """

    del args  # currently unused
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if s.mode == "sqlite":
        return _ping_sqlite()
    if s.mode == "shared_postgresql":
        return _ping_postgres(s)
    print(f"error: unknown storage mode {s.mode!r}", file=sys.stderr)
    return 2


def _ping_sqlite() -> int:
    try:
        with db.connect() as conn:
            row = db.fetch_one(conn, "SELECT value FROM meta WHERE key = ?",
                               ("schema_version",))
            schema_version = row[0] if row else "unknown"
            chunks = db.fetch_one(conn, "SELECT COUNT(*) FROM chunks")[0]
    except Exception as exc:  # noqa: BLE001 — APSW has many failure modes
        print(f"error opening sqlite DB: {exc}", file=sys.stderr)
        return 2
    print("ok: connected to sqlite")
    print(f"  path:           {paths.db_path()}")
    print(f"  schema_version: {schema_version}")
    print(f"  chunks:         {chunks}")
    return 0


def _ping_postgres(s) -> int:
    from . import settings as settings_mod
    from .backends.postgres import PostgresBackend, _DependencyMissing

    backend = PostgresBackend(s)
    try:
        # ping is the triage verb — always re-fetch the pgvector type OIDs
        # and rewrite the local type cache, so a server-side extension
        # recreation (stale cached OIDs) is healed by the same command a
        # user would naturally reach for.
        conn = backend.connect(refresh_types=True)
    except _DependencyMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except settings_mod.DsnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — psycopg has many failure modes
        print(f"error connecting to PostgreSQL: {exc}", file=sys.stderr)
        return 2

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg_full = cur.fetchone()[0]
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            row = cur.fetchone()
            pgvector_version = row[0] if row else None
            cur.execute("SELECT current_database(), current_user")
            db_name, db_user = cur.fetchone()
            cur.execute(
                "SELECT to_regclass('public.projects') IS NOT NULL, "
                "       to_regclass('public.chunk_embeddings') IS NOT NULL"
            )
            schema_ok, embeddings_ok = cur.fetchone()
            project_count = 0
            if schema_ok:
                cur.execute("SELECT COUNT(*) FROM projects")
                project_count = cur.fetchone()[0]
    finally:
        conn.close()

    # PostgreSQL 17.0 (Debian 17.0-1.pgdg120+1) on aarch64-... ->
    # extract just the version number for compactness.
    pg_short = pg_full.split(" ", 2)[1] if pg_full.startswith("PostgreSQL ") else pg_full

    print(f"ok: connected to {db_name!r} as {db_user!r}")
    print(f"  postgres: {pg_short}")
    if pgvector_version:
        print(f"  pgvector: {pgvector_version}")
    else:
        print("  pgvector: NOT INSTALLED")
        print("            run: knowledge db init-postgres")
    if schema_ok and embeddings_ok:
        print(f"  schema:   ready ({project_count} project(s) registered)")
    else:
        print("  schema:   NOT APPLIED")
        print("            run: knowledge db init-postgres")
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    """Copy one project from local SQLite to the configured shared PG.

    Two phases (mirrors :mod:`knowledge.migrate.sqlite_to_pg`):

    1. Open both DBs, resolve the source project, validate
       embedding-model match, check for a target conflict, count rows.
    2. With ``--dry-run`` we stop here and print the plan. Otherwise we
       prompt (suppressed by ``--yes``) and execute the copy in one PG
       transaction.

    Source SQLite is never modified — the local project row stays so you
    can re-run if something goes wrong on the target side, or compare
    side-by-side after a successful migrate.
    """

    from . import migrate, settings as settings_mod
    from .backends.postgres import PostgresBackend, _DependencyMissing

    try:
        s = settings_mod.load_settings()
    except settings_mod.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if s.mode != "shared_postgresql":
        print(
            "error: storage.mode is 'sqlite' — set 'shared_postgresql' in a "
            "discoverable .knowledge-config.json before migrating.",
            file=sys.stderr,
        )
        return 2

    # Open both sides explicitly. db.connect() would dispatch on mode and
    # give us PG; we need raw sqlite for the source.
    sqlite_conn = db.connect_sqlite()

    backend = PostgresBackend(s)
    try:
        pg_conn = backend.connect()
    except _DependencyMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except settings_mod.DsnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — psycopg has many failure modes
        print(f"error connecting to PostgreSQL: {exc}", file=sys.stderr)
        return 2

    try:
        try:
            plan = migrate.prepare(sqlite_conn, pg_conn, args.project)
        except migrate.MigrationConflict as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except migrate.EmbeddingModelMismatch as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except migrate.MigrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        _print_plan(plan, settings=s)

        if args.dry_run:
            print("\ndry-run: nothing written. drop --dry-run to migrate.")
            return 0

        if not args.yes:
            try:
                answer = input("\nContinue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\naborted.", file=sys.stderr)
                return 1
            if answer not in ("y", "yes"):
                print("aborted.", file=sys.stderr)
                return 1

        print("\nmigrating...", flush=True)
        try:
            counts = migrate.execute(sqlite_conn, pg_conn, plan)
        except migrate.MigrationConflict as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 — surfaces psycopg errors
            print(f"\nerror during migrate: {exc}", file=sys.stderr)
            return 1
    finally:
        sqlite_conn.close()
        pg_conn.close()

    print("done. inserted on PG:")
    for label, value in counts.items():
        print(f"  {label:20s} {value}")
    print(
        f"\nsource SQLite still contains project {plan.project_name!r} "
        f"(id={plan.sqlite_project_id}). Once you've verified the migrated "
        "copy works, remove the local row with: "
        f"knowledge forget {plan.project_name}"
    )
    return 0


def _print_plan(plan, settings) -> None:
    """Pretty-print a MigrationPlan for the user to confirm."""

    print(f"source: local SQLite ({paths.db_path()})")
    print(f"  project_id     {plan.sqlite_project_id}")
    print(f"  name           {plan.project_name}")
    print(f"  root           {plan.project_root}")
    print(f"  git_remote     {plan.git_remote or '(none)'}")
    print(f"  project_key    {plan.project_key_kind} = "
          f"{plan.git_remote_normalized or plan.project_root.resolve()}")
    print(f"  emb. model     {plan.source_embedding_model}")
    print()

    pg = settings.postgresql
    if pg is not None:
        print(f"target: shared PostgreSQL")
        print(f"  host           {pg.host}")
        print(f"  database       {pg.database}")
        print(f"  user (env)     {pg.user_env}")
        print(f"  emb. model     {plan.target_embedding_model}")
    else:
        print("target: shared PostgreSQL (configured)")
    print()

    print("rows to copy:")
    print(f"  files               {plan.file_count}")
    print(f"  chunks              {plan.chunk_count}")
    print(f"  chunk embeddings    {plan.chunk_embedding_count}")
    print(f"  file edges          {plan.edge_count}")
    print(f"  project variables   {plan.variable_count}")
    print(f"  history             {plan.history_count}")
    print(f"  history embeddings  {plan.history_embedding_count}")
    print(f"  decisions           {plan.decision_count}")
    print(f"  decision embeddings {plan.decision_embedding_count}")


_DB_DISPATCH = {
    "ping": cmd_db_ping,
    "init-postgres": cmd_db_init_postgres,
    "migrate": cmd_db_migrate,
}


# ---------------------------------------------------------------------------
# daemon subcommands (Item F — warm-embedder daemon)
# ---------------------------------------------------------------------------


def cmd_daemon_run(args: argparse.Namespace) -> int:
    """Foreground server loop — the process the automatic spawn launches.

    Always hosts the LOCAL embedder (``daemon.run_server`` builds its own
    ``Embedder``; it never calls ``embedder.get_embedder()``), so the
    daemon can't recurse into spawning itself. Exits on its own after
    ``daemon.idle_timeout_seconds`` without requests, or on ``daemon stop``.
    """
    del args  # no flags — idle timeout comes from the config block
    import logging

    from . import daemon as daemon_mod

    # The spawn path redirects our stdout/stderr to daemon.log; make the
    # server's logging visible there (and on the terminal for manual runs).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon_mod.run_server()
    return 0


def cmd_daemon_status(args: argparse.Namespace) -> int:
    """Report whether a daemon is serving. Exit 0 = running, 1 = not."""
    del args
    from . import daemon as daemon_mod

    client = daemon_mod.DaemonEmbedder()
    try:
        info = client.ping()
    except daemon_mod.DaemonUnavailable:
        print("daemon: not running")
        print(f"  socket: {paths.daemon_socket_path()}")
        return 1

    idle_s = max(0.0, time.time() - float(info.get("last_used", time.time())))
    print("daemon: running")
    print(f"  pid:     {info.get('pid')}")
    print(f"  model:   {info.get('model')}")
    print(f"  version: {info.get('version')}")
    print(f"  idle:    {idle_s:.0f}s")
    print(f"  socket:  {paths.daemon_socket_path()}")
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Send the ``shutdown`` op. Exit 0 even when no daemon is running —
    "make sure it's not running" is an idempotent request."""
    del args
    from . import daemon as daemon_mod

    client = daemon_mod.DaemonEmbedder()
    try:
        info = client.ping()
    except daemon_mod.DaemonUnavailable:
        print("daemon: not running")
        return 0
    client.shutdown()
    print(f"daemon: stopped (pid {info.get('pid')})")
    return 0


_DAEMON_DISPATCH = {
    "run": cmd_daemon_run,
    "status": cmd_daemon_status,
    "stop": cmd_daemon_stop,
}


if __name__ == "__main__":
    sys.exit(main())
