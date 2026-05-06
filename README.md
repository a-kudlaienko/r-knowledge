# repo-knowledge

Local semantic code search. One SQLite DB at `~/.knowledge/index.sqlite` holds chunks + embeddings for many repos. Respects `.gitignore`, scrubs a short list of secret patterns, no external services.

Answers *meaning* questions: "how does vault auth work", "where is the ingress load balancer defined", "find the function that handles cert regeneration".

## Install

```bash
git clone <repo-url> ~/git/repo-knowledge
cd ~/git/repo-knowledge
pip install -e .
```

This registers the `knowledge` command globally (or in your active venv). First run downloads `BAAI/bge-small-en-v1.5` (~130MB) to `~/.knowledge/models/`. Torch wheel on macOS ARM is ~300MB — expected.

## Usage (per repo)

```bash
cd ~/git/my-repo
knowledge build          # first time: scan + chunk + embed (cold: 1-5 min)
knowledge update         # incremental; auto-detects changed files
knowledge search "how does the vault callback inject secrets"
knowledge search "terraform resource: load balancer" --kind resource --lang hcl
```

Add more repos the same way — each `knowledge build` registers a new project row in the shared DB. Searches default to the current repo (detected via `git rev-parse --show-toplevel`); `--all-projects` widens.

## Skill integration

Wire the `/knowledge` skill into a project (or into your whole user profile) with a single command:

```bash
cd ~/your-project
knowledge install-skill              # project-scoped → .claude/skills/knowledge/SKILL.md
knowledge install-skill --user       # user-scoped   → ~/.claude/skills/knowledge/SKILL.md
knowledge install-skill --symlink    # symlink to the source (auto-updates on `git pull` in repo-knowledge)
knowledge install-skill --force      # overwrite an existing install
```

The skill auto-builds the index on first use, auto-updates when files have changed, and stores/retrieves per-project work summaries so a new session can pick up where the last one left off (see `knowledge history --help`).

### Auto-flush staged summaries (optional)

To have Claude Code automatically run `knowledge history ingest` at compaction and session end — so staged work summaries always make it into SQLite before the context is summarized away — register the hooks:

```bash
cd ~/your-project
knowledge install-hooks              # → <cwd>/.claude/settings.json  (project-scoped)
knowledge install-hooks --user       # → ~/.claude/settings.json      (every session, any project)
```

The command idempotently merges into an existing `settings.json`; other hooks and config keys are preserved. It registers three events:

- `Stop` — fires after every assistant turn. Incrementally drains the stage so SQLite stays nearly live with the session. If the terminal gets killed abruptly, the previous turn's entries are already persisted.
- `PreCompact` — fires before manual `/compact` or auto-compaction. Catches anything written after the last `Stop`.
- `SessionEnd` — fires when the session closes gracefully. Final sweep.

All three run `knowledge history ingest`. An empty stage is a no-op — user-scoped hooks are safe to install globally; they won't create project rows for repos that don't use `knowledge`.

#### PATH caveat — hooks run in a subshell that may not see your venv

Claude Code runs hook commands in a subprocess. That subprocess inherits `PATH` from whatever launched Claude Code:

- **Terminal launch**: inherits your shell's `PATH`, so a `knowledge` on `PATH` works.
- **GUI/dock launch** (macOS), **launchd service**, **IDE plugin**: often gets a minimal system `PATH` that does NOT include per-user venv directories (e.g. `~/venvs/*/bin`).

If `knowledge` lives in a venv (`which knowledge` points at something like `/Users/you/venvs/claude/bin/knowledge`), the hook silently fails on GUI launches — your stage file stays unflushed.

**Two fixes, pick one:**

1. **Install with `--absolute`** (recommended when the tool lives in a venv):
   ```bash
   knowledge install-hooks --absolute              # project-scoped
   knowledge install-hooks --user --absolute       # user-scoped
   ```
   The hook command is written as an absolute path (e.g. `/Users/you/venvs/claude/bin/knowledge history ingest`), so `PATH` doesn't matter. Re-running `install-hooks` upgrades the existing entry in place — no duplicates.

   Trade-off: the settings.json becomes machine-specific. For a `--user` install that's fine (it's already under `~/.claude/`). For a project-scoped install you want to commit, prefer option 2.

2. **Put `knowledge` on a system `PATH` directory** (portable across teammates):
   ```bash
   sudo ln -s "$(which knowledge)" /usr/local/bin/knowledge
   ```
   Then leave `install-hooks` in its default (bare) mode, so `.claude/settings.json` stays portable.

You can switch modes any time — re-run `install-hooks` with or without `--absolute`; the in-place upgrade rewrites existing entries cleanly.

**Verify the flow:**
1. Append a JSON entry to `~/.knowledge/stage/pending.jsonl` during a running Claude Code session.
2. Run `/compact` (or let auto-compact fire).
3. `knowledge history recent --limit 1` — the entry should be in the DB and the stage file truncated to zero bytes.

## What's indexed

Everything matching `.gitignore` rules is skipped. Supported languages: Python, JavaScript/TypeScript, Terraform/HCL, YAML (Ansible + Helm + K8s manifests), JSON, Shell, Jinja2, Dockerfile, Markdown.

The following domains also get a **file-to-file dependency graph** (`knowledge relations <file>`): Python, JavaScript/TypeScript, Terraform/HCL (module sources + templatefile/file), Helm (Chart.yaml deps + intra-chart `{{ include }}`), Ansible (include_tasks/import_tasks, include_role/import_role honoring `ansible.cfg` `roles_path`, custom modules in `library/`/`action_plugins/`), GitHub Actions (local reusable workflows + composite actions, external `uses:` passed through), and Kustomize (`resources`, `bases`, `components`, patches, generators). Before opening code to answer a question, you can ask the graph which files are worth reading first — compact JSON designed for LLM consumption.

**Dynamic paths** like `include_tasks: "_tasks/{{ deploy_env }}/..."` or Terraform `source = "./${var.env}"` can be resolved by setting **per-project variables**: `knowledge vars set ansible deploy_env=prod` (or `knowledge vars import ansible vars.json` for bulk). Scoped by domain (`ansible`/`terraform`/`helm`/`all`); auto-applies against existing edges. Edges waiting for variables show as `kind="parametric"` (distinct from `external` stdlib or `unresolved` non-literal expressions).

**Visualize as HTML**: `knowledge graph [--output file.html] [--open]` writes a self-contained HTML (vis-network via CDN) with nodes colored by top-level directory and hover tooltips showing full paths. Resolved project-to-project edges by default; flags add `--include-external` / `--include-parametric` / `--include-unresolved`.

## Secret sanitization

Two layers applied before any chunk is embedded:

1. **Regex scrub** — `ghp_*`, `github_pat_*`, `hvs.*`, `AKIA*`, JWTs, `-----BEGIN ... PRIVATE KEY-----`, long SSH keys → `CHANGE_ME`.
2. **Sensitive-key replacement** — in YAML/HCL/JSON, values under keys like `password`, `*_token`, `*_secret`, `api_key`, `vault_*_id` → `CHANGE_ME`.

Plus `.gitignore` + `.knowledgeignore` are honored, so gitignored files (where secrets usually live) aren't scanned at all.

## Layout

See `knowledge/README.md` for the module mapping table.
