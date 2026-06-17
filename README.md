# 🧠 repo-knowledge

> **Local semantic search + memory for your git repos.** Ask how code works, map dependencies, and pick up where you left off — fully offline, on your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-≥3.10-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.1.0-orange.svg)](pyproject.toml)
[![SQLite](https://img.shields.io/badge/SQLite-default-003B57.svg?logo=sqlite&logoColor=white)](#-details)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-optional-4169E1.svg?logo=postgresql&logoColor=white)](#-details)

Answers *meaning* questions — *"how does vault auth work?"*, *"where is the ingress LB defined?"*, *"what did we decide about cache invalidation last week?"* — with hybrid full-text + vector search and cross-session memory.

---

## ✨ What it does

- **🗜️ Turns a huge repo into a knowledge base — and cuts token usage.** Indexes the whole tree, builds the import / dependency relations between files, and serves precise file + line slices. Instead of grepping a massive codebase into the context window, an LLM reads only the handful of chunks that actually matter — a large token saving on big repositories.
- **🧠 Remembers every change without overwhelming context.** Decisions and a work-log persist across sessions; `knowledge resume` rehydrates the last decisions, touched files, and hub files in ~1200 tokens. Both humans and LLMs pick up exactly where they left off without re-reading the repo.
- **🤝 Keeps a team in sync via one shared PostgreSQL.** Everyone shares a single index, history, and decision log. You see *why* a choice was made, and get a gated warning **before** feature A re-opens a problem feature B already solved — so dev B's work doesn't silently break the main logic or overrule a teammate's standard. And secrets stay safe: tokens, passwords, and keys are **sanitized to `CHANGE_ME` before anything is embedded or written to the shared DB**, so nothing sensitive ever lands in the team index.

> 💻 **Platform & requirements:** developed and tested on **macOS (Apple Silicon)**. Should run on any Unix-like OS (Linux / WSL2) — it's pure Python + CPU PyTorch — but those are not yet verified, and Windows-native is untested. See [Requirements & platform ↓](#-details) for hardware guidance before you build.

---

## 🚀 Quick start

```bash
# 1. Install (registers the `knowledge` command)
git clone https://github.com/AKudlaienko/repo-knowledge.git ~/git/repo-knowledge
cd ~/git/repo-knowledge
```

- **For single user(local) mode:**
```bash
pip install -e .
```

- **For team(shared) mode:**
- ⚠️Make sure you have installed and configured the PostgreSQL server with correct plugins and settings!

```bash
pip install -e '.[postgres]'
```

> 🤖 **Using an AI coding agent?** Run `knowledge install-skill --ide all` to wire the skill into Claude Code, Cursor, Codex, and OpenCode — auto-build, auto-update, agent-first verbs.


# Index any repo (first run downloads a ~130 MB embedding model)
```bash
cd ~/your-project
knowledge build

# 3. Ask
knowledge ask "how does X work?"
```

## 🧭 Everyday commands

| | Command | What it does |
|---|---|---|
| 🔍 **Ask** | `knowledge ask "<question>"` | Hybrid search — the default for *how / where / why* |
| 🎯 **Find** | `knowledge find <symbol>` · `grep '<pattern>'` | Exact symbol / full-text lookup (no embedder) |
| 🧭 **Orient** | `knowledge why <file>` · `map` · `brief` | Understand a file or tree before reading it |
| 🕸️ **Graph** | `knowledge relations <file>` · `graph` | Imports, callers, blast radius |
| 🧠 **Remember** | `knowledge resume` · `decide` · `history` | Decisions + work log across sessions |
| 🔄 **Maintain** | `knowledge update` · `status --json` | Keep the index fresh (`missing`/`stale`/`fresh`) |

> 💡 **Sharpen retrieval** by prefixing queries with a *kind hint*: `python function:`, `terraform resource:`, `ansible task:`, `helm template:`, `docs:`. → [full table in Details ↓](#-details)

<details>
<summary>🗂️ <strong>Supported languages & formats</strong></summary>

Every file below is **chunked and semantically searchable**. A subset also gets a
**dependency-relations graph** (`knowledge relations <file>` — imports, callers, blast
radius). Files in languages without a dedicated chunker are still indexed at a coarser
granularity; `.gitignore` + `.knowledgeignore` are always honored.

| Language / format | Searchable (chunked) | Dependency graph (relations) |
|---|:---:|---|
| **Python** | ✅ | ✅ `import` / `from` / `importlib` / relative |
| **JavaScript / TypeScript** | ✅ | ✅ `import` / `require()` / dynamic `import()` |
| **Terraform / HCL** | ✅ | ✅ `module.source` / `templatefile()` / `file()` |
| **Ansible** (YAML) | ✅ | ✅ playbooks / `include_*` / roles / modules (honors `ansible.cfg`) |
| **Helm** (YAML) | ✅ | ✅ `Chart.yaml` deps / `{{ include }}` / `{{ template }}` |
| **Kustomize** (YAML) | ✅ | ✅ `resources` / `bases` / `components` / patches / generators |
| **GitHub Actions** (YAML) | ✅ | ✅ reusable workflows / composite actions / `owner/repo@ref` |
| **ArgoCD** (YAML) | ✅ | ✅ App-of-Apps source references |
| **Kubernetes / plain YAML** | ✅ | ➖ siblings hint only (no resolver) |
| **JSON** | ✅ | ➖ |
| **Shell** | ✅ | ➖ |
| **Jinja2** | ✅ | ➖ (Jinja edges surface via Ansible/Helm) |
| **Dockerfile** | ✅ | ➖ |
| **Markdown** | ✅ | ➖ |

Dynamic relation paths (e.g. `include_tasks: "_tasks/{{ deploy_env }}/…"`) resolve once you
set the variables — see [Dependency graph ↓](#-details).

</details>

---

## 📚 Details

<sub>Everything below is collapsed — open what you need.</sub>

<details>
<summary>📦 <strong>Install & first run</strong></summary>

```bash
git clone https://github.com/AKudlaienko/repo-knowledge.git ~/git/repo-knowledge
cd ~/git/repo-knowledge
pip install -e .            # core (SQLite)
pip install -e '.[postgres]'   # + shared PostgreSQL support
```

Registers `knowledge` globally (or in the active venv). First run downloads `BAAI/bge-small-en-v1.5` (~130 MB) to `~/.knowledge/models/`; the Torch wheel on macOS ARM is ~300 MB — expected.

</details>

<details>
<summary>💻 <strong>Requirements & platform</strong></summary>

**Tested on:** macOS (Apple Silicon). It's pure Python + CPU PyTorch, so it *should* run on any Unix-like OS — Linux and WSL2 — but those have not been verified yet. Windows-native is untested.

**Software:**

- Python ≥ 3.10 and `pip`
- `git` (used for project identity / git-remote normalization)
- Team mode only: a reachable **PostgreSQL ≥ 14** with the **pgvector** extension — or Docker, to run the bundled image (see *Set up the PostgreSQL server* below).

**Hardware** — guidance, not hard limits; scales with repo size:

| | Minimum | Recommended |
|---|---|---|
| RAM | ~4 GB free | 8 GB+ for large monorepos |
| Free disk | ~1 GB (≈130 MB model + Torch ≈300 MB + index) | 2 GB+ |
| CPU | any x86-64 / ARM64 — **no GPU required** | more cores → faster cold build |

The embedding model runs CPU-only. The first `knowledge build` is the heavy step (cold: 1–5 min on a typical repo); `update` afterwards is incremental. If you index very large trees, expect RAM and build time to grow roughly with the number of indexed files.

</details>

<details>
<summary>🔄 <strong>Index maintenance</strong></summary>

```bash
knowledge build          # first time: scan + chunk + embed (cold: 1–5 min)
knowledge update         # incremental; auto-detects changed files
knowledge status         # human: missing | stale | fresh
knowledge status --json  # machine: branch on state before queries
```

Each `build` registers a new project in the shared DB. A chunker/model version bump forces a rebuild on the next `update`. Add a `.knowledgeignore` (gitignore-style) for extra exclusions.

</details>

<details>
<summary>🔍 <strong>Search & cartography</strong></summary>

```bash
# Meaning questions (default): hybrid FTS + vector, RRF merge, reranked, cached
knowledge ask "how does the vault callback inject secrets"
knowledge ask "octavia LB floating IP" --top-k 5 --kind resource --lang hcl
knowledge ask "cert regen" --budget 2000 --no-cache

# Vector-only (scripting / distance scores)
knowledge search "terraform resource: load balancer" --kind resource --lang hcl
knowledge search "vault auto_load convention" --all-projects

# Fast lookup (no embedder)
knowledge find VaultClient --exact
knowledge grep 'vault AND approle'

# Orient before reading
knowledge why ansible/roles/karmada/tasks/main.yml
knowledge map --dir terraform --depth 3
knowledge brief

# Follow a hit
knowledge get <chunk_id> --with-siblings --raw
knowledge path <chunk_id>
```

Queries scope to the current repo by default; `--all-projects` searches every registered repo.

</details>

<details>
<summary>🕸️ <strong>Dependency graph</strong></summary>

```bash
knowledge relations knowledge/cli.py                     # both directions
knowledge relations knowledge/cli.py --direction forward --depth 2
knowledge relations knowledge/db.py --direction reverse  # who imports it
knowledge relations stats
```

**Coverage:** Python, JS/TS, Terraform/HCL, Helm (`Chart.yaml` + `{{ include }}`), Ansible (tasks/roles/modules via `ansible.cfg`), GitHub Actions, Kustomize, ArgoCD App-of-Apps. Output is compact JSON for LLMs; add `--pretty` for humans.

**Dynamic paths** (`include_tasks: "_tasks/{{ deploy_env }}/…"`, Terraform `source = "./${var.env}"`):

```bash
knowledge vars set ansible deploy_env=prod region=us-east
knowledge vars set terraform env=prod
knowledge vars list [--scope ansible] [--json]
```

Scoped by domain (`ansible`/`terraform`/`helm`/`all`); mutations auto-apply. Unresolved edges show as `parametric`.

**Visualize:** `knowledge graph [--output file.html] [--open]` — self-contained HTML, nodes colored by directory.

</details>

<details>
<summary>🧠 <strong>Session memory</strong></summary>

Two stores: **history** (`knowledge history …`) for narrative — *"what did we do last Tuesday"* — and **decisions** (`knowledge decide` / `decisions` / `resume`) for commitments — *"why did we pick X over Y"*.

```bash
knowledge resume          # session start: last decisions, touched files, hub files (~1200 tokens)

knowledge decide "cache invalidation" \
  --decision "wipe per-project on any chunk change" \
  --rationale "agent-driven updates shouldn't thrash cache" \
  --files knowledge/query_cache.py knowledge/indexer.py

# Override a prior standard — author is auto-stamped; the override is gated:
knowledge decide "cache invalidation" \
  --decision "wipe on every update" \
  --supersede 42 --override-reason "no-op detection unreliable on PG"

knowledge history stage --short "Fixed project-name resolution." --long "…" --tags "fix,cli"
knowledge history ingest
knowledge history recent --limit 10
knowledge history search "auth middleware"
```

Every decision is stamped with its author (git identity, UNIX-login fallback). Overriding an existing decision needs `--supersede <id> --override-reason "<why>"` — the tool blocks until you justify it, so in shared mode teammates relying on the old behavior aren't silently overruled.

Use `ask` for code questions, not history search.

</details>

<details>
<summary>🤖 <strong>Agent / IDE integration (Claude Code, Cursor, Codex, OpenCode)</strong></summary>

The `knowledge` CLI is tool-agnostic — any agent that can run a shell uses it the same way. `install-skill` wires the *instruction file* into the location each tool discovers, all generated from one source (`skill-template/SKILL.md`):

```bash
knowledge install-skill                          # default → Claude Code (.claude/skills/knowledge/SKILL.md)
knowledge install-skill --ide cursor             # → .cursor/rules/knowledge.mdc
knowledge install-skill --ide codex,opencode     # → ./AGENTS.md  (shared; written once)
knowledge install-skill --ide all                # all four at once
knowledge install-skill --ide all --user         # user/global location per tool
knowledge install-skill --ide claude --symlink   # auto-updates on git pull here
knowledge install-skill --ide cursor --force     # overwrite an existing dedicated file
```

| `--ide` | Project destination | `--user` (global) |
|---------|--------------------|-------------------|
| `claude` (default) | `.claude/skills/knowledge/SKILL.md` | `~/.claude/skills/knowledge/SKILL.md` |
| `cursor` | `.cursor/rules/knowledge.mdc` | *(project only)* |
| `codex` | `AGENTS.md` | `~/.codex/AGENTS.md` |
| `opencode` | `AGENTS.md` | `~/.config/opencode/AGENTS.md` |

`codex` and `opencode` (and Cursor as a fallback) all read the same root **`AGENTS.md`** — it's written once and merged into a `<!-- BEGIN/END knowledge skill -->` block, so any content you already keep there is preserved. Dedicated files (`SKILL.md`, `.mdc`) need `--force` to overwrite. Cursor's `.mdc` is an *agent-requested* rule by default (`alwaysApply: false`); pass `--always-apply` to attach it to every request.

> 🔧 Maintainers: `AGENTS.md` and `knowledge.mdc` are generated from `SKILL.md` — run `make sync-skill` after editing it (CI guards drift via `tests/test_skill_sync.py`).

Auto-builds on first use, auto-updates on file changes, prefers `ask`/`find`/`grep`/`why`/`map`/`brief`/`resume`/`decide`.

**Hooks (optional)** — auto-flush staged summaries at compaction / session end:

```bash
knowledge install-hooks              # → <cwd>/.claude/settings.json
knowledge install-hooks --user       # → ~/.claude/settings.json
```

Idempotently merges `Stop` / `PreCompact` / `SessionEnd` hooks, each running `knowledge history ingest`. An empty stage is a no-op.

> ⚠️ **PATH caveat:** Claude Code runs hooks in a subshell. GUI/dock/IDE launches often get a minimal `PATH` that excludes venv dirs, so a venv-installed `knowledge` silently won't fire. Fix with `knowledge install-hooks --absolute` (writes an absolute path), or symlink onto a system path: `sudo ln -s "$(which knowledge)" /usr/local/bin/knowledge`.

</details>

<details>
<summary>🔐 <strong>What's indexed & secret sanitization</strong></summary>

Indexes Python, JS/TS, Terraform/HCL, YAML (Ansible + Helm + K8s), JSON, Shell, Jinja2, Dockerfile, Markdown. `.gitignore` + `.knowledgeignore` are honored, so gitignored files (where secrets live) are never scanned.

Two scrub layers run **before** any chunk is embedded:

1. **Regex** — `ghp_*`, `github_pat_*`, `hvs.*`, `AKIA*`, JWTs, private keys, long SSH keys → `CHANGE_ME`.
2. **Sensitive keys** — values under `password`, `*_token`, `*_secret`, `api_key`, `vault_*_id` (YAML/HCL/JSON) → `CHANGE_ME`.

Any `CHANGE_ME` in results is a placeholder or a sanitizer replacement — never a real leaked secret.

</details>

<details>
<summary>🗂️ <strong>Multi-repo admin</strong></summary>

```bash
knowledge projects
knowledge stats
knowledge forget <name>                # drop project + all chunks/edges/history
knowledge forget <name> --sqlite-only  # after PG migration — local copy only
```

One DB (`~/.knowledge/index.sqlite`) holds many projects. Each teammate rebuilds locally unless on shared PostgreSQL.

</details>

<details>
<summary>🐘 <strong>Set up the PostgreSQL server (Linux VM / Docker)</strong></summary>

Team mode needs a PostgreSQL instance reachable by every teammate, with the **pgvector** extension available. This repo's schema is idempotent — `knowledge db init-postgres` (from the laptop) creates every table/index/extension `IF NOT EXISTS`, so you only have to stand up the server and point a project at it.

### 🐳 Docker (fastest — bundled image)

The repo ships a `Dockerfile` that wraps `pgvector/pgvector:pg17` and bakes the schema (`knowledge/schema/postgres/*.sql`) into the Postgres init dir, so the database is fully initialized on first boot. The `Makefile` wraps it:

```bash
export KNOWLEDGE_PG_USER=postgres
export KNOWLEDGE_PG_PASSWORD=$(openssl rand -hex 16)

make pg-run        # build image + start container on localhost:5432 (data on a persistent volume)
make pg-psql       # interactive psql shell
make pg-logs       # tail logs
make pg-stop       # stop, keep data
make pg-clean      # remove container + wipe data volume (destructive)
```

Or run it by hand on any Docker host (e.g. a remote VM):

```bash
docker build -t repo-knowledge-pg .
docker run -d --name knowledge-pg \
  -e POSTGRES_USER="$KNOWLEDGE_PG_USER" \
  -e POSTGRES_PASSWORD="$KNOWLEDGE_PG_PASSWORD" \
  -e POSTGRES_DB=knowledge \
  -p 5432:5432 \
  -v knowledge-pg-data:/var/lib/postgresql/data \
  repo-knowledge-pg
```

### 🐧 Native PostgreSQL on a Linux VM

```bash
# 1. Install PostgreSQL + the pgvector extension package.
#    Debian/Ubuntu (replace <N> with your server major, e.g. 16):
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib postgresql-<N>-pgvector
#    RHEL/Fedora: sudo dnf install postgresql-server pgvector_<N>

# 2. Create the role, database, and the vector extension (extension needs a superuser):
sudo -u postgres psql <<'SQL'
CREATE ROLE knowledge LOGIN PASSWORD 'change-me';
CREATE DATABASE knowledge OWNER knowledge;
\connect knowledge
CREATE EXTENSION IF NOT EXISTS vector;
SQL

# 3. Allow remote clients (skip if the VM is localhost-only).
#    postgresql.conf:  listen_addresses = '*'
#    pg_hba.conf:      hostssl  knowledge  knowledge  <client-cidr>  scram-sha-256
sudo systemctl restart postgresql
```

### Point a project at the server

From the laptop, configure a repo and let `knowledge` apply the rest of the schema:

```bash
export KNOWLEDGE_PG_USER=knowledge
export KNOWLEDGE_PG_PASSWORD='change-me'

cd /path/to/your-repo
knowledge config init --project
$EDITOR .knowledge-config.json     # mode=shared_postgresql, host=<vm-ip>, port=5432, sslmode=require
knowledge config show && knowledge db ping
knowledge db init-postgres         # idempotent — adds any missing tables/indexes/extension
knowledge build && knowledge ask "..."
```

> 🔒 Use `sslmode=require` for any non-localhost server (`disable` is only safe for a local Docker container). Credentials live in env vars only — the JSON stores variable *names*, never the secrets themselves.

</details>

<details>
<summary>🐘 <strong>Shared PostgreSQL (team mode)</strong></summary>

Switch a project — or the whole laptop — to a team-shared **pgvector** database so teammates share one index, history, and decisions. **Choice is per project:** project A on PG and project B on local SQLite is fine.

```bash
pip install -e '.[postgres]'

export KNOWLEDGE_PG_USER=postgres
export KNOWLEDGE_PG_PASSWORD=$(openssl rand -hex 16)
make pg-run                                # local Docker dev container

cd /path/to/your-repo
knowledge config init --project
$EDITOR .knowledge-config.json             # mode=shared_postgresql, host=127.0.0.1, sslmode=disable
knowledge config show && knowledge db ping
knowledge db init-postgres
knowledge build && knowledge ask "..."
```

Helpers: `make pg-stop`, `make pg-logs`, `make pg-psql`, `make pg-clean` (destructive).

**Migrate an existing SQLite project** (local copy untouched):

```bash
knowledge db migrate --project <name|abs-path> --dry-run
knowledge db migrate --project <name|abs-path> --yes
knowledge forget <name> --sqlite-only
```

`migrate` keys on the normalized `git remote` URL, so the same repo at different paths collapses to one row (falls back to `root_path` when there's no `.git`).

**Credentials never touch the config file** — export `KNOWLEDGE_PG_USER` / `KNOWLEDGE_PG_PASSWORD`; the JSON carries env-var *names* only. For containers / CI, a single `KNOWLEDGE_DATABASE_URL` (full DSN, creds inline) selects PostgreSQL by itself — no config file needed → [details ↓](#-details).

**Concurrency & offline resilience.** Multiple teammates on one DB never block each other: reads are lock-free, and `build`/`update` take a *non-blocking* per-project advisory lock — a concurrent index run on the same project fails fast (exit 3, retry) rather than waiting, so deadlock can't happen. If the shared DB is unreachable, `decide` and `history add` **buffer locally** (`~/.knowledge/stage/<slug>/outbox.jsonl`) and **auto-sync** on the next reachable command — you never lose a decision or hit a traceback. Reads exit cleanly (code 4) with a "shared index unreachable" message; index writes just re-run when the DB is back (chunks are re-derivable).

</details>

<details>
<summary>⚙️ <strong>Configuration</strong></summary>

Config is **one JSON file**. SQLite is the default, so you only need a file to opt into PostgreSQL or tune knobs.

```bash
knowledge config init              # ~/.knowledge/config.json   (laptop default)
knowledge config init --project    # <git-root>/.knowledge-config.json
knowledge config show              # which file is active + resolved mode
```

```json
{
  "storage": {
    "mode": "sqlite",
    "postgresql": {
      "host": "db.example.com",
      "port": 5432,
      "database": "knowledge",
      "sslmode": "require",
      "user_env": "KNOWLEDGE_PG_USER",
      "password_env": "KNOWLEDGE_PG_PASSWORD",
      "connect_timeout_seconds": 10
    }
  },
  "cache_bytes": 2147483648,
  "embedding_model": null
}
```

**Resolution:** `KNOWLEDGE_DATABASE_URL` → `<repo>/.knowledge-config.json` (walk up from cwd) → `~/.knowledge/config.json` → SQLite default. Closer file wins; delete the project file to fall back. Credentials come from env vars only. `KNOWLEDGE_HOME` overrides the data dir (tests).

> 🐳 **`KNOWLEDGE_DATABASE_URL` is self-sufficient** — a full DSN with credentials inline (`postgresql://user:pass@host:5432/knowledge`). Setting it **selects PostgreSQL by itself**, with no config file and without the separate `KNOWLEDGE_PG_USER` / `KNOWLEDGE_PG_PASSWORD` vars (those belong to the structured-config path). That makes it the one-variable way to run in a container / CI. It wins over a config file's `storage.mode`, but `storage.postgresql.{sslmode,connect_timeout,…}` are then ignored — put any such options in the URL query string.

```bash
# Container / CI: one variable, no config file — PostgreSQL is selected by the DSN itself.
export KNOWLEDGE_DATABASE_URL="postgresql://knowledge:<PASSWORD>@db.example.com:5432/knowledge?sslmode=verify-full"

knowledge db ping                 # confirms it reached PostgreSQL (reports server + pgvector)
knowledge build && knowledge ask "how does X work?"
```

```dockerfile
# …or bake it into an image / pass at run time:
#   docker run -e KNOWLEDGE_DATABASE_URL="postgresql://user:pass@host:5432/knowledge" your-image
ENV KNOWLEDGE_DATABASE_URL="postgresql://user:pass@host:5432/knowledge?sslmode=require"
```

</details>

<details>
<summary>🎁 <strong>Query enrichment</strong></summary>

The embedder retrieves best when the query hints at *what kind of thing* you want. Prefix by intent; add `--kind` when irrelevant kinds crowd results.

| Looking for | Prefix | `--kind` |
|---|---|---|
| Python function / class / method | `python function:` / `class:` / `method:` | `function` / `class` / `method` |
| JS / TS function | `javascript function:` | `function` |
| Terraform resource / variable / output / module / locals | `terraform resource:` (etc.) | `resource` / `variable` / `output` / `module` / `locals_block` |
| Ansible task / handler | `ansible task:` / `handler:` | `ansible_task` / `ansible_handler` |
| Helm template / values | `helm template:` / `values:` | `helm_template` / `helm_values_section` |
| K8s manifest | `kubernetes Deployment:` (etc.) | `yaml_doc` (+ `--lang yaml`) |
| Shell function | `shell function:` | `shell_function` |
| Jinja macro / block | `jinja:` | `jinja_macro` / `jinja_block` |
| Dockerfile stage | `dockerfile stage:` | `dockerfile_stage` |
| Markdown / README | `docs:` | `markdown_section` |
| Config value / docstring | `value:` / `docstring:` | (none) |

</details>

<details>
<summary>🛠️ <strong>Development & internals</strong></summary>

```bash
pip install -e '.[dev]'
make guide          # quick install reminder
make pg-run         # local PostgreSQL dev container
```

Module map: [`docs/module-map.md`](docs/module-map.md). License: [MIT](LICENSE).

</details>

---

<sub>SQLite by default · optional team-shared PostgreSQL · secrets sanitized before embedding · MIT licensed</sub>
