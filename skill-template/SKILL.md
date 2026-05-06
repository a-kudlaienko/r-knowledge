---
name: knowledge
description: Local semantic code search across the current repo. Auto-builds or updates the local index, then returns the exact chunks (function / class / ansible task / terraform resource / helm values / markdown section / …) that match the user's question. Use this instead of raw Grep when the query is about meaning ("how does X work", "where is Y configured") rather than exact strings.
argument-hint: [query] [--kind K] [--lang L] [--all-projects]
allowed-tools: Bash Read
---

# /knowledge — Local semantic code search

Complements `Grep` (exact text) and `graphify` (structural dependencies) by answering *meaning* questions: "how does vault auth work", "where is the load balancer defined", "what generates the workload cluster manifest". One SQLite DB at `~/.knowledge/index.sqlite` holds chunks + embeddings for every repo the user has indexed.

## Auto-maintenance — run BEFORE searching

On every invocation, check + refresh the index for the current repo. These three steps run in order:

### 1. Check state

```bash
knowledge status --json
```

Reads the `state` field from the JSON: one of `missing`, `stale`, `fresh`. Exit codes are also usable (2, 1, 0 respectively) — whichever is handier.

### 2. Build or update as needed

- `state: missing` → index doesn't exist for this repo yet. Run `knowledge build` and warn the user that first-time indexing takes 1-5 minutes (mostly embedding model load + initial encode).
- `state: stale` → files changed since last index. Run `knowledge update` silently (usually finishes in under 5s; only re-embeds chunks whose post-sanitize text actually changed).
- `state: fresh` → skip, go straight to search.

### 3. Run the search

```bash
knowledge search "$ENRICHED_QUERY" [--kind K] [--lang L] [--top-k 10]
```

## Query enrichment — rewrite the user's question before searching

The embedding model retrieves best when the query hints at *what kind of thing* is being sought. Prefix user queries based on their intent:

| User is looking for | Prefix the query with | Good `--kind` filter |
|---|---|---|
| Python function body | `python function:` | `function` or `big_parent` |
| Python class | `python class:` | `class` |
| Python method | `python method:` | `method` (M5 hierarchy, fallback: `function`) |
| JS / TS function | `javascript function:` | `function` |
| Terraform resource | `terraform resource:` | `resource` |
| Terraform variable / output | `terraform variable:` / `terraform output:` | `variable` / `output` |
| Terraform module | `terraform module:` | `module` |
| Terraform locals | `terraform locals:` | `locals_block` or `locals_entry` |
| Ansible task | `ansible task:` | `ansible_task` |
| Ansible handler | `ansible handler:` | `ansible_handler` |
| Helm template | `helm template:` | `helm_template` |
| Helm values key | `helm values:` | `helm_values_section` |
| K8s manifest | `kubernetes <Kind>:` (e.g. `kubernetes Deployment:`) | `yaml_doc` + `--lang yaml` |
| Shell function | `shell function:` | `shell_function` |
| Jinja macro or block | `jinja:` | `jinja_macro` / `jinja_block` |
| Dockerfile stage | `dockerfile stage:` | `dockerfile_stage` |
| Markdown doc / README | `docs:` | `markdown_section` |
| Config value / literal | `value:` | (no filter) |
| Docstring / doc comment | `docstring:` | (no filter) |

**Filters narrow the candidate pool AFTER semantic ranking.** If the top-K without a filter already matches, skip the filter. If irrelevant kinds crowd the result, add one.

## Cross-repo mode

By default, `knowledge search` scopes to the current repo (detected via `git rev-parse --show-toplevel`). Use `--all-projects` to search across every registered repo:

```bash
knowledge search "vault auto_load convention" --all-projects
```

Useful when the user's question is about *another* repo they've indexed, or when they want to see how a pattern is used across multiple projects.

## Reading a specific chunk

Each search result includes a `chunk_id`. To see the full contents:

```bash
knowledge get <chunk_id>                       # sanitized stored text
knowledge get <chunk_id> --with-siblings       # for big_parent: parent + all subchunks
knowledge get <chunk_id> --with-siblings --raw # exact original bytes from disk
knowledge path <chunk_id>                      # file_path:start_line-end_line
```

`--raw` re-slices the original file using the chunk's byte offsets — byte-identical to what's on disk. Use this when the user asks to see the actual code (not the sanitized DB copy).

## Rules / gotchas

- **First build is slow** — cold-start downloads the 130MB embedding model to `~/.knowledge/models/`. Warn the user before running `build` on a fresh machine.
- **Don't commit the DB** — `~/.knowledge/index.sqlite` is per-machine. Each teammate rebuilds locally.
- **`.gitignore` is honored.** Secret-shaped files (`.env`, `*.pem`, etc.) that are gitignored are never scanned. Regex + structured-key sanitization scrub the rest. Any `CHANGE_ME` token in search results is either a user placeholder or a sanitizer replacement — never a real leaked secret.
- **Version drift → rebuild.** If the tool's chunker or embedding model was bumped, `update` auto-falls-back to `build` and warns you. Other projects in the shared DB need their own `build` too.
- **`.knowledgeignore`** in the repo root takes gitignore-style patterns for extra exclusions (e.g., generated docs) without polluting `.gitignore`.

## Example end-to-end

User: "how does the karmada cert regeneration ansible task work"

1. `knowledge status --json` → `{"state": "fresh", ...}` → no maintenance needed
2. Rewrite to `ansible task: karmada cert regeneration`
3. `knowledge search "ansible task: karmada cert regeneration" --kind ansible_task --top-k 5`
4. Top result: `Regenerate Karmada TLS certificates (ansible/roles/karmada/tasks/main.yml:47-55)` with `chunk_id=682`
5. Optionally `knowledge get 682 --raw` to show the original YAML
6. Summarize for the user referencing `ansible/roles/karmada/tasks/main.yml:47`

## Continuity / memory — cross-session RAG over past work

Alongside code chunks, this tool stores per-project **work summaries** (short + long pairs) so a new session can pick up where the last one left off without re-reading the prior transcript. Two-tier retrieval: semantic search over short summaries, drill into the long summary of a specific hit when details are needed.

### Session start — check what we did before

When the user opens a new session on a project, or asks a question that sounds historical ("where did we stop", "what did we decide about X", "continue the Y refactor"), consult history **before** doing code search:

```bash
knowledge history recent --limit 10            # newest-first list, no vector work
knowledge history search "auth middleware"     # semantic over short summaries
knowledge history get <id>                     # full entry (short + long)
```

Typical RAG flow: `recent` or `search` → pick the relevant hit(s) → `get <id>` for only the entries you need. Never `get` every recent entry — that defeats the purpose of the two-tier design.

Skip history lookup entirely when the question is clearly about current code ("what does function X do", "find the config for Y") — go straight to `knowledge search`.

### During the session — write staged entries at natural boundaries

At each unit-of-work completion (task done, plan signed off, a focused change shipped), **append one JSON line** to `~/.knowledge/stage/pending.jsonl`. You do NOT re-read the file — a Python helper (`knowledge history ingest`) handles parsing and DB insert later. This avoids burning tokens on re-reading your own summaries.

Format (one object per line, unknown keys ignored):

```json
{"short": "Fixed ambiguous project-name resolution in forget/search.", "long": "Added AmbiguousProjectName exception in projects.py. resolve_project now uses fetchall() on the name branch and raises on >1 match. cmd_search and cmd_forget catch and dispatch to _print_ambiguous. Fixes the silent-pick-one behavior.\n\nFiles: knowledge/projects.py, knowledge/cli.py.\nDecision: keep error-out semantics rather than auto-pick — ambiguity should be user-resolved.", "tags": "fix,cli,projects"}
```

Guidelines:
- **Short** ≤ ~160 chars. The imperative-summary bar: someone skimming `recent` should know what happened. One line.
- **Long**: 1–5 paragraphs. Include file paths, rationale, decisions, and non-obvious tradeoffs. Skip obvious things a future session can derive from `git log` or the code.
- **Tags**: optional comma-separated. Useful for filtering (not indexed for semantic search — just metadata).
- Skip trivial Q&A — write entries only when there's something worth recalling later.
- **No secrets.** The sanitizer does not scrub this layer — you control what you write.

### Flushing staged entries to SQLite

Run `knowledge history ingest` when you want to durably persist staged entries:

```bash
knowledge history ingest                                # default path
knowledge history ingest --stage-file /path/to/x.jsonl  # override
```

Behavior:
- Reads all JSONL lines, embeds the short summaries in one batch, inserts rows transactionally.
- On SQL success → truncates the stage file to zero bytes.
- On SQL failure → leaves the file intact; new entries can still be appended and a later ingest will pick up everything.
- Malformed lines (bad JSON, missing short/long, empty short) are skipped and counted in the output; they do **not** block the valid entries.

**When to ingest:** after a batch of entries (e.g. end of a focused work stretch), or before a context-window compact event. In Phase 2 a `PreCompact` hook will do this automatically; for now, it's explicit.

### Rules for history

- **Scope is per-project** by default (uses the current git root). Pass `--all-projects` to `recent`/`search` to cross-project.
- **Don't pollute** with trivial or conversational summaries — the vector index stays useful only if what's in it is worth recalling.
- **Don't search history for code questions** — `knowledge search` is faster and more precise. History is for *decisions, context, and continuity*, not code.

## Dependency graph — first step before code search

`knowledge relations <file>` returns a compact JSON view of file-to-file imports for one file: what it imports (forward edges) and what imports it (reverse edges). **Use this BEFORE `knowledge search`** when the task involves understanding or changing existing code — it tells you which files to pull into context, with no embedding work required.

### When to use

- "How does X work" / "where do I start with file F" — `relations` narrows the search surface before you read anything.
- "What will break if I change Y" — reverse edges list the callers.
- "What does this module depend on" — forward edges with `--kinds import,from_import,require`.
- Skip when the question is clearly semantic ("find the code that authenticates vault tokens") — go straight to `knowledge search`.

### Typical flow

```bash
knowledge relations knowledge/cli.py                                   # both directions, depth 1
knowledge relations knowledge/cli.py --direction forward --depth 2     # follow imports two hops
knowledge relations knowledge/db.py  --direction reverse               # who imports db.py
knowledge relations knowledge/cli.py --kinds import,from_import,require # drop external/unresolved noise
knowledge relations stats                                              # sanity check: edge counts
```

### Output format (LLM-optimized)

Compact JSON by default. One object with `file`, `project`, and the requested direction arrays. Each edge is `{kind, raw, [file], [symbol], [line]}`:

- `kind`: `import` | `from_import` | `require` | `dynamic_import` | `external` | `unresolved`
- `raw`: the literal specifier as written in source (`.db`, `./utils`, `os.path`)
- `file`: project-relative path of the resolved target. **Absent** for external and unresolved edges.
- `symbol`: the imported name for `from_import`, else absent.
- `line`: 1-based source line.

Add `--pretty` for human-readable output when you want to show the user directly.

### Coverage

- **Python**: `import a.b`, `from a import b`, `importlib.import_module('x')`, relative imports.
- **JavaScript/TypeScript**: `import`, `require()`, `import()` dynamic with string-literal arg.
- **Terraform / HCL**: `module "x" { source = "…" }`, `templatefile("…", …)`, `file("…")`. Local relative sources resolve to the module's `main.tf` (fallback to any `.tf` in the dir).
- **Helm**: `Chart.yaml` `dependencies:` (file:// → subchart `Chart.yaml`, remote → external), `{{ include "name" . }}` / `{{ template "name" . }}` → the file in the same chart containing `{{ define "name" }}`. Scope is the containing chart (walk up to the nearest `Chart.yaml`).
- **Ansible**: `import_playbook`, `include_tasks` / `import_tasks`, `include_role` / `import_role` / `roles:`, `vars_files`, `include_vars`. Role resolution honors `ansible.cfg` `roles_path` — multi-cfg / non-root layouts work (e.g. `ansible/ansible.cfg` with `roles_path = roles` resolves to `ansible/roles/`). `tasks_from:` on an include_role narrows the target to the specific task file. Custom modules in `library/` + `action_plugins/` (or wherever `ansible.cfg` points) produce `ansible_module` edges — builtin modules (`debug`, `copy`, …) don't clutter the graph.
- **GitHub Actions**: `uses: ./.github/workflows/*.yml` (reusable workflows), `uses: ./.github/actions/*` (local composite actions → their `action.yml`), `uses: owner/repo@ref` → external.
- **Kustomize**: `kustomization.yaml` `resources`, `bases`, `components`, `patchesStrategicMerge`, `patches[].path`, `configMapGenerator`/`secretGenerator.files`. A `resources:` entry that's a directory is resolved to its nested `kustomization.yaml`. Plain (non-kustomize) k8s manifests have no edges.
- **Plain YAML / Markdown / other files without a resolver**: `knowledge relations <file>` returns the file's same-directory siblings (with lang) under a `siblings` key — a "where does this file live" hint rather than a real dep.

### Freshness

The graph is rebuilt alongside chunks during `knowledge build` / `knowledge update`. Because this skill always runs `knowledge update` before any query (see "Auto-maintenance" at the top), `relations` reflects current-on-disk state.

If `relations` returns `error: file not indexed` for a file you know exists, run `knowledge update` — it's probably new since the last index.

### Variables — resolving `{{ var }}` and `${var.x}` paths

Some edges (mostly Ansible `include_tasks`/`include_role` and Terraform `templatefile`/`source`) carry template expressions like `_tasks/{{ deploy_env }}/...` or `source = "./${var.env}"`. Without the variables, these edges show as `kind="parametric"` with no `file` — the LLM can see *something* is there but not where it points.

Set per-project variables (scoped by domain) to resolve them:

```bash
knowledge vars set ansible deploy_env=prod region=us-east            # multi-kv
knowledge vars set terraform env=prod                                 # scoped separately
knowledge vars set all region=us-east-1                               # catch-all merged into any scope
knowledge vars import ansible /path/to/vars.json                      # bulk from JSON
knowledge vars list [--scope ansible] [--json]
knowledge vars unset ansible deploy_env                               # remove one
knowledge vars unset ansible --all                                    # clear a scope
```

Every mutation auto-applies against the existing graph — no rebuild needed. Scope routing:

| Edge kind | Syntax | Scope lookup order |
|---|---|---|
| `ansible_*` | Jinja `{{ name }}` | `ansible`, then `all` |
| `helm_*` | Jinja `{{ name }}` | `helm`, then `all` |
| `tf_*` | Terraform `${var.name}` | `terraform`, then `all` |

**Display kinds for NULL-target edges:**
- `parametric` — waiting for variables. Set them with `vars set`.
- `external` — resolved to not-a-project-file (stdlib / third-party / remote module source).
- `unresolved` — syntactically irrecoverable (e.g., `import_module(some_expr)` with a non-literal arg).

**Not substituted:** Jinja filters (`{{ x | lower }}` → takes `x`, ignores filter), loop vars (`{{ item }}`, `{{ role_item }}`), nested attrs (`{{ foo.bar }}`), arithmetic/expressions. Those stay parametric by design — set a concrete value if you want them resolved.

### Visualize the graph (HTML)

When the user wants to *see* the dependency shape (e.g. "what's the overall structure here", "show me the graph", "are there cycles"), render it to a static HTML:

```bash
knowledge graph                                    # writes ./relations_graph.html
knowledge graph --output /tmp/graph.html --open    # write to specific path + launch browser
knowledge graph --include-external                 # include stdlib / third-party as gray nodes
knowledge graph --include-parametric               # include vars-waiting as yellow nodes
```

One project per run (`--project` overrides the cwd default). The rendered file is a single self-contained HTML with vis-network loaded from CDN — open in any browser, hover a node for the full project-relative path + language, drag nodes, scroll to zoom. Nodes are colored by top-level directory. The default scope is resolved project-to-project edges only (cleanest for large repos); opt in to `external` / `parametric` / `unresolved` via the flags above.

This is a **display** command, not a query command — it writes a file to disk and prints its path. Don't use it when the user asks a narrow "where does X point" question; `knowledge relations <file>` is faster and more focused for that.
