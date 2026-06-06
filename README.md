# repo-knowledge

Local semantic search and session memory for git repos — ask how code works, map dependencies, and pick up where you left off. SQLite by default; optional team-shared PostgreSQL.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-≥3.10-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.1.0-green.svg)](pyproject.toml)
[![CI](https://github.com/AKudlaienko/repo-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/AKudlaienko/repo-knowledge/actions/workflows/ci.yml)

Answers *meaning* questions: "how does vault auth work", "where is the ingress load balancer defined", "what did we decide about cache invalidation last week".

---

## How To

**1. [Install](#install--first-run)** — clone [this repo](https://github.com/AKudlaienko/repo-knowledge), run `pip install -e .`, then [`knowledge build`](#index-maintenance) inside any git project. First run downloads the embedding model (~130 MB) and may take 1–5 minutes.

**2. [Ask questions](#search--cartography)** — from your repo root, run `knowledge ask "how does X work"`. Use [`find`](#search--cartography) / [`grep`](#search--cartography) for exact symbols; [`why`](#search--cartography) / [`map`](#search--cartography) / [`brief`](#search--cartography) to orient before reading files.

**3. [Keep the index fresh](#index-maintenance)** — `knowledge update` after changes; `knowledge status --json` for scripts and agents (`missing` → build, `stale` → update, `fresh` → query).

**4. [Explore dependencies](#dependency-graph)** — `knowledge relations <file>` before diving into code; [`knowledge graph`](#dependency-graph) for an HTML view; [`knowledge vars set`](#dependency-graph) for Ansible/Terraform template paths.

**5. [Remember work across sessions](#session-memory)** — `knowledge resume` at session start; `knowledge decide` for non-obvious choices; [`knowledge history stage`](#session-memory) + [`install-hooks`](#claude-code-integration) to auto-persist summaries.

**6. [Use with Claude Code](#claude-code-integration)** — `knowledge install-skill` wires the `/knowledge` skill (auto-build, auto-update, agent-first verbs).

**7. [Share with your team (optional)](#shared-postgresql-mode)** — switch a project to shared PostgreSQL via [`.knowledge.yaml`](#configuration-reference); `make pg-run` for local Docker dev.

### Query hints (short)

Prefix natural-language queries with a *kind hint* when you know what you're looking for — it improves retrieval. Examples: `python function:`, `terraform resource:`, `ansible task:`, `helm template:`, `docs:`.

| Looking for | Prefix | Good `--kind` |
|---|---|---|
| Python function | `python function:` | `function` |
| Terraform resource | `terraform resource:` | `resource` |
| Ansible task | `ansible task:` | `ansible_task` |
| Helm template | `helm template:` | `helm_template` |
| README / doc section | `docs:` | `markdown_section` |


## Query enrichment (full table)

<details id="query-enrichment-full">
<summary><strong>Full table</strong></summary>


The embedding model retrieves best when the query hints at *what kind of thing* is being sought. Prefix user queries based on intent; add `--kind` when irrelevant kinds crowd results.

| User is looking for | Prefix the query with | Good `--kind` filter |
|---|---|---|
| Python function body | `python function:` | `function` or `big_parent` |
| Python class | `python class:` | `class` |
| Python method | `python method:` | `method` |
| JS / TS function | `javascript function:` | `function` |
| Terraform resource | `terraform resource:` | `resource` |
| Terraform variable / output | `terraform variable:` / `terraform output:` | `variable` / `output` |
| Terraform module | `terraform module:` | `module` |
| Terraform locals | `terraform locals:` | `locals_block` or `locals_entry` |
| Ansible task | `ansible task:` | `ansible_task` |
| Ansible handler | `ansible handler:` | `ansible_handler` |
| Helm template | `helm template:` | `helm_template` |
| Helm values key | `helm values:` | `helm_values_section` |
| K8s manifest | `kubernetes Deployment:` (etc.) | `yaml_doc` + `--lang yaml` |
| Shell function | `shell function:` | `shell_function` |
| Jinja macro or block | `jinja:` | `jinja_macro` / `jinja_block` |
| Dockerfile stage | `dockerfile stage:` | `dockerfile_stage` |
| Markdown doc / README | `docs:` | `markdown_section` |
| Config value / literal | `value:` | (no filter) |
| Docstring / doc comment | `docstring:` | (no filter) |

</details>

---

## Details

Jump to: [Install](#install--first-run) · [Index](#index-maintenance) · [Search](#search--cartography) · [Graph](#dependency-graph) · [Memory](#session-memory) · [Claude Code](#claude-code-integration) · [Indexed](#whats-indexed--sanitization) · [Multi-repo](#multi-repo-admin) · [PostgreSQL](#shared-postgresql-mode) · [Config](#configuration-reference) · [Development](#development--internals)

<details>
<summary><strong>Install & first run</strong></summary>

### Install & first run

```bash
git clone https://github.com/AKudlaienko/repo-knowledge.git ~/git/repo-knowledge
cd ~/git/repo-knowledge
pip install -e .
```

This registers the `knowledge` command globally (or in your active venv). First run downloads `BAAI/bge-small-en-v1.5` (~130 MB) to `~/.knowledge/models/`. The Torch wheel on macOS ARM is ~300 MB — expected.

For shared PostgreSQL support:

```bash
pip install -e '.[postgres]'
```

Then continue with [Shared PostgreSQL mode](#shared-postgresql-mode).

</details>

<details>
<summary><strong>Index maintenance</strong></summary>

### Index maintenance

```bash
cd ~/git/my-repo
knowledge build          # first time: scan + chunk + embed (cold: 1–5 min)
knowledge update         # incremental; auto-detects changed files
knowledge status         # human-readable: missing | stale | fresh
knowledge status --json  # machine-readable — branch on state before queries
```

Add more repos the same way — each `knowledge build` registers a new project row in the DB. Version drift (chunker or embedding model bump) triggers a forced rebuild on the next `update`.

Extra exclusions without touching `.gitignore`: add a `.knowledgeignore` file at the repo root (gitignore-style patterns).

</details>

<details>
<summary><strong>Search & cartography</strong></summary>

### Search & cartography

**Default for meaning questions:** `knowledge ask` — hybrid FTS + vector search, RRF merge, reranked by recency/session/hub centrality, cached per `(query, HEAD sha)`.

```bash
knowledge ask "how does the vault callback inject secrets"
knowledge ask "octavia LB floating IP" --top-k 5 --kind resource --lang hcl
knowledge ask "cert regen" --budget 2000 --no-cache
```

**Vector-only (scripting / distance scores):** `knowledge search` — same filters as `ask`, no RRF/rerank/cache.

```bash
knowledge search "terraform resource: load balancer" --kind resource --lang hcl
knowledge search "vault auto_load convention" --all-projects
```

**Fast lookup (no embedder):**

```bash
knowledge find VaultClient --exact
knowledge find regen --kind ansible_task
knowledge grep 'vault AND approle'
knowledge grep '"exact phrase"'
```

**Orient before reading:**

```bash
knowledge why ansible/roles/karmada/tasks/main.yml
knowledge map --dir terraform --depth 3
knowledge brief
```

**Follow a hit:**

```bash
knowledge get <chunk_id>
knowledge get <chunk_id> --with-siblings --raw
knowledge path <chunk_id>
```

By default, queries scope to the current repo (`git rev-parse --show-toplevel`). Use `--all-projects` to search across every registered repo.

<details>
<summary><strong>Dependency graph</strong></summary>

### Dependency graph

Before opening code, ask which files matter:

```bash
knowledge relations knowledge/cli.py
knowledge relations knowledge/cli.py --direction forward --depth 2
knowledge relations knowledge/db.py --direction reverse
knowledge relations stats
```

**Coverage:** Python, JavaScript/TypeScript, Terraform/HCL, Helm (`Chart.yaml` + `{{ include }}`), Ansible (tasks/roles/modules via `ansible.cfg`), GitHub Actions (local workflows/actions), Kustomize, **ArgoCD** (`Application` / `ApplicationSet` → chart paths in App-of-Apps layouts).

Output is compact JSON for LLM consumption. Add `--pretty` for human-readable output.

**Dynamic paths** like `include_tasks: "_tasks/{{ deploy_env }}/..."` or Terraform `source = "./${var.env}"`:

```bash
knowledge vars set ansible deploy_env=prod region=us-east
knowledge vars set terraform env=prod
knowledge vars import ansible /path/to/vars.json
knowledge vars list [--scope ansible] [--json]
knowledge vars unset ansible deploy_env
```

Scoped by domain (`ansible` / `terraform` / `helm` / `all`); mutations auto-apply against existing edges. Edges waiting for variables show as `kind="parametric"` (distinct from `external` or `unresolved`).

**Visualize as HTML:**

```bash
knowledge graph [--output file.html] [--open]
knowledge graph --include-external --include-parametric --include-unresolved
```

Self-contained HTML (vis-network via CDN); nodes colored by top-level directory.

</details>

<details>
<summary><strong>Session memory</strong></summary>

### Session memory

Two complementary stores:

- **History** (`knowledge history …`) — free-form work summaries. Good for "what did we do last Tuesday."
- **Decisions** (`knowledge decide` / `decisions` / `resume`) — structured choices with topic, decision, rationale, files. Good for "why did we pick X over Y."

**Session start:**

```bash
knowledge resume
```

Returns last decisions, recently touched files, un-ingested stage entries, and hub files (~1200 tokens).

**Record a choice as you make it:**

```bash
knowledge decide "cache invalidation" \
  --decision "wipe per-project on any chunk change" \
  --rationale "agent-driven updates shouldn't thrash cache" \
  --files knowledge/query_cache.py knowledge/indexer.py
```

**History — stage during work, ingest to persist:**

```bash
knowledge history stage \
  --short "Fixed ambiguous project-name resolution." \
  --long "Added AmbiguousProjectName in projects.py …" \
  --tags "fix,cli"

knowledge history ingest
knowledge history recent --limit 10
knowledge history search "auth middleware"
knowledge history get <id>
```

Use history for narrative continuity; use decisions for commitments. Don't search history for code questions — use `ask` instead.

</details>

<details>
<summary><strong>Claude Code integration</strong></summary>

### Claude Code integration

**Skill** — wire `/knowledge` into a project or user profile:

```bash
cd ~/your-project
knowledge install-skill              # project → .claude/skills/knowledge/SKILL.md
knowledge install-skill --user       # user    → ~/.claude/skills/knowledge/SKILL.md
knowledge install-skill --symlink    # symlink (auto-updates on git pull here)
knowledge install-skill --force      # overwrite existing install
```

The skill auto-builds on first use, auto-updates when files change, and prefers `ask` / `find` / `grep` / `why` / `map` / `brief` / `resume` / `decide`.

**Hooks (optional)** — auto-flush staged summaries at compaction and session end:

```bash
knowledge install-hooks              # → <cwd>/.claude/settings.json
knowledge install-hooks --user       # → ~/.claude/settings.json
```

Idempotently merges into existing `settings.json`. Registers three events, all running `knowledge history ingest`:

- `Stop` — after every assistant turn (incremental drain)
- `PreCompact` — before manual `/compact` or auto-compaction
- `SessionEnd` — graceful session close

An empty stage is a no-op — user-scoped hooks are safe globally.

#### PATH caveat — hooks run in a subshell that may not see your venv

Claude Code runs hook commands in a subprocess. That subprocess inherits `PATH` from whatever launched Claude Code:

- **Terminal launch**: inherits your shell's `PATH`, so a `knowledge` on `PATH` works.
- **GUI/dock launch** (macOS), **launchd service**, **IDE plugin**: often gets a minimal system `PATH` that does NOT include per-user venv directories (e.g. `~/venvs/*/bin`).

If `knowledge` lives in a venv, the hook silently fails on GUI launches — your stage file stays unflushed.

**Two fixes, pick one:**

1. **Install with `--absolute`** (recommended when the tool lives in a venv):
   ```bash
   knowledge install-hooks --absolute
   knowledge install-hooks --user --absolute
   ```
   Writes an absolute path to the hook command. Re-running upgrades in place — no duplicates. Trade-off: machine-specific `settings.json`.

2. **Put `knowledge` on a system `PATH` directory** (portable across teammates):
   ```bash
   sudo ln -s "$(which knowledge)" /usr/local/bin/knowledge
   ```
   Then use default (bare) `install-hooks` so `.claude/settings.json` stays portable.

**Verify the flow:**

1. `knowledge history stage --short "..." --long "..."` during a session.
2. Run `/compact` (or let auto-compact fire).
3. `knowledge history recent --limit 1` — entry in DB; stage file gone after ingest.

</details>

<details>
<summary><strong>What's indexed & sanitization</strong></summary>

### What's indexed & sanitization

Everything matching `.gitignore` rules is skipped. Supported languages: Python, JavaScript/TypeScript, Terraform/HCL, YAML (Ansible + Helm + K8s manifests), JSON, Shell, Jinja2, Dockerfile, Markdown.

**Secret sanitization** — two layers applied before any chunk is embedded:

1. **Regex scrub** — `ghp_*`, `github_pat_*`, `hvs.*`, `AKIA*`, JWTs, `-----BEGIN … PRIVATE KEY-----`, long SSH keys → `CHANGE_ME`.
2. **Sensitive-key replacement** — in YAML/HCL/JSON, values under keys like `password`, `*_token`, `*_secret`, `api_key`, `vault_*_id` → `CHANGE_ME`.

Plus `.gitignore` + `.knowledgeignore` are honored, so gitignored files (where secrets usually live) aren't scanned at all. Any `CHANGE_ME` in search results is either a user placeholder or a sanitizer replacement — never a real leaked secret.

</details>

<details>
<summary><strong>Multi-repo admin</strong></summary>

### Multi-repo admin

```bash
knowledge projects
knowledge stats
knowledge forget <name>                # drop project + all chunks/edges/history
knowledge forget <name> --sqlite-only  # after PG migration — local copy only
```

Default storage is a single DB (`~/.knowledge/index.sqlite` for SQLite mode) holding many projects. Each teammate rebuilds locally unless using [shared PostgreSQL](#shared-postgresql-mode).

</details>

<details>
<summary><strong>Shared PostgreSQL mode</strong></summary>

### Shared PostgreSQL mode

Default storage is local SQLite — fine for solo work. Switch a project (or every project on the laptop) to a team-shared **pgvector** database when you want teammates to share the same index, history, and decisions.

**Storage choice is per project.** The same machine can keep project A on shared PG and project B on local SQLite. Resolution at runtime:

1. `KNOWLEDGE_DATABASE_URL` env (CI override) — full DSN, wins everything
2. Walk cwd → parents looking for `.knowledge.yaml` — first match wins
3. `$HOME/.knowledge.yaml` — laptop-wide default
4. Built-in default: SQLite

Same file name and schema at every scope ([template](knowledge/config.example.yaml)). The closer file wins.

#### Quick start (Docker dev container)

```bash
pip install -e '.[postgres]'

export KNOWLEDGE_PG_USER=postgres
export KNOWLEDGE_PG_PASSWORD=$(openssl rand -hex 16)
make pg-run

cd /path/to/your-repo
knowledge config init --project
$EDITOR .knowledge.yaml    # mode=shared_postgresql, host=127.0.0.1, sslmode=disable

knowledge config show
knowledge db ping
knowledge db init-postgres
knowledge build
knowledge ask "..."
```

Alternative: drop `.knowledge.yaml` at `$HOME` to make PG the laptop default for projects that don't override.

Makefile helpers: `make pg-stop`, `make pg-logs`, `make pg-psql`, `make pg-clean` (destructive — wipes data volume).

#### Migrating an existing SQLite project

The local SQLite copy stays untouched — `migrate` only writes to the target.

```bash
knowledge db migrate --project <name|abs-path> --dry-run
knowledge db migrate --project <name|abs-path>
knowledge db migrate --project <name|abs-path> --yes

knowledge forget <name> --sqlite-only
```

`migrate` keys on the project's `git remote` URL (normalized) so the same repo at different paths collapses to one row on PG. Falls back to `root_path` when there's no `.git`.

#### Credentials

Never in `.knowledge.yaml`. Each laptop exports its own `KNOWLEDGE_PG_USER` / `KNOWLEDGE_PG_PASSWORD` ([template](knowledge/config.example.env)). The YAML carries env-var **names** only.

`KNOWLEDGE_DATABASE_URL` is the CI escape hatch (full libpq URL with credentials inline). Don't use it on laptops.

Design notes: [`todo/01-postgresql-shared-mode.md`](todo/01-postgresql-shared-mode.md).

</details>

<details>
<summary><strong>Configuration reference</strong></summary>

### Configuration reference

```bash
knowledge config init              # ~/.knowledge.yaml (laptop default)
knowledge config init --project    # <git-root>/.knowledge.yaml
knowledge config show
knowledge config check-env

knowledge db ping
knowledge db init-postgres
knowledge db migrate --project <name> [--dry-run] [--yes]
```

Templates: [`knowledge/config.example.yaml`](knowledge/config.example.yaml), [`knowledge/config.example.env`](knowledge/config.example.env).

Override data directory for tests: `KNOWLEDGE_HOME`.

</details>

<details>
<summary><strong>Development & internals</strong></summary>

### Development & internals

See [`knowledge/README.md`](knowledge/README.md) for the module map.

```bash
make guide          # quick install reminder
make pg-run         # local PostgreSQL dev container
pip install -e '.[dev]'
```

License: [AGPL-3.0](LICENSE).

</details>
