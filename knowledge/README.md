# knowledge package

Local semantic code search, dependency cartography, and session memory. Default storage is SQLite at `~/.knowledge/index.sqlite`; optional shared PostgreSQL via `.knowledge.yaml` (see root README).

**Entry point**: `knowledge.cli:main` (registered as `knowledge` console_script). All logic lives in this package.

## Module Map

> **First rule**: look here before `grep`-ing the source.

| Module | Purpose | Key symbols |
|--------|---------|-------------|
| `paths.py` | Filesystem locations (`~/.knowledge/`, overridable via `KNOWLEDGE_HOME`) | `user_dir`, `db_path`, `models_dir`, `config_path`, `stage_path` |
| `config.py` | Hardcoded constants | `MODEL`, `EMBEDDING_DIM`, `MAX_CHARS`, `SCHEMA_VERSION`, `CHUNKER_VERSION`, `INCLUDE_GLOBS` |
| `settings.py` | Load `.knowledge.yaml` (walk-up + `$HOME` fallback); resolve PG DSN from env | `load_settings`, `resolve_pg_dsn`, `Settings` |
| `db.py` | Connection factory + schema/meta helpers; delegates to active backend | `connect`, `init_schema`, `get_meta`, `set_meta` |
| `backends/base.py` | Backend ABC (connect, transaction, advisory lock) | `Backend` |
| `backends/sqlite.py` | Default laptop backend — APSW + sqlite-vec | `SqliteBackend` |
| `backends/postgres.py` | Team-shared backend — psycopg3 + pgvector + tsvector | `PostgresBackend` |
| `backends/__init__.py` | Backend dispatch | `load_backend` |
| `migrate/sqlite_to_pg.py` | Copy a project from SQLite to PostgreSQL | `migrate_project` |
| `schema/postgres/` | PostgreSQL DDL (`001_init.sql`) | applied by `db init-postgres` |
| `projects.py` | Project detection + registry CRUD | `current_project_root`, `resolve_project`, `list_projects`, `forget_project` |
| `whitespace.py` | Reversible whitespace compression for embedded text | `compress`, `decompress` |
| `gitignore.py` | `.gitignore` + `.knowledgeignore` handling | `load_specs`, `is_ignored` |
| `sanitizer.py` | Regex scrub (L1) + sensitive-key replacement (L2) | `scrub_text`, `SECRET_PATTERNS`, `SENSITIVE_KEYS` |
| `scanner.py` | Walk project, apply ignore rules, dispatch chunker | `walk_project`, `classify_file` |
| `chunkers/` | Language → chunker registry + per-lang parsers | `dispatch_chunker`, `PythonChunker`, `HclChunker`, … |
| `big_split.py` | Oversized chunks → `big_parent` + `big_subchunk` | `split_if_oversized` |
| `embedder.py` | sentence-transformers loader + batch encode | `Embedder`, `get_embedder` |
| `indexer.py` | Scan → chunk → sanitize → embed → upsert; build + incremental update | `build_project`, `update_project` |
| `search.py` | Vector query + project scoping; chunk retrieval | `search`, `get_chunk`, `get_family`, `SearchResult` |
| `fts.py` | FTS5 queries over chunk text (no embedder) | `grep`, symbol/name lookup helpers |
| `hybrid_search.py` | Parallel FTS + vector, RRF merge, rerank | `ask` |
| `query_cache.py` | Answer cache keyed by `(query, HEAD sha)` | `get_cached`, `put_cached`, `invalidate_project` |
| `cartography.py` | Static repo/file/dir briefs (no embedder) | `why`, `map_tree`, `repo_brief` |
| `history.py` | Work-summary store; embeds `short_summary`, stores `long_summary` | `add`, `search`, `recent`, `get`, `ingest_stage` |
| `decisions.py` | Structured decision log (topic + decision + rationale) | `add_decision`, `search_decisions`, `recent_decisions` |
| `resume.py` | Session-start brief aggregator | `build_resume_brief` |
| `resolvers/` | Per-language edge extractors | `dispatch_resolver`, `PythonResolver`, `HelmResolver`, `ArgoCDResolver`, … |
| `relations.py` | File-edge store + resolution (imports, includes, chart refs) | `FileIndex`, `extract_edges`, `get_forward`, `get_reverse`, `stats` |
| `variables.py` | Per-project vars for Jinja / Terraform substitution | `set_many`, `apply_variables`, `substitute` |
| `graph.py` | Dependency graph → self-contained HTML (vis-network) | `build_graph_html` |
| `cache.py` | Byte-bounded LRU (internal shim) | `ByteBoundedLRU` |
| `cli.py` | argparse + dispatch for all verbs | `main`, `cmd_*` |

## Resolvers

| Resolver | Edge kinds | Notes |
|----------|------------|-------|
| `python_resolver` | `import`, `from_import`, `dynamic_import` | tree-sitter walk |
| `javascript_resolver` | `import`, `require`, dynamic `import()` | JS + TS subclass |
| `terraform_resolver` | `tf_module`, `tf_templatefile`, `tf_file` | regex + brace tracking |
| `helm_resolver` | `helm_chart_dep`, `helm_include`, `helm_define` | Chart.yaml + template refs |
| `argocd_resolver` | `argocd_app_source` | Application/ApplicationSet → chart paths |
| `ansible_resolver` | `ansible_*` | tasks, roles, modules via `ansible.cfg` |
| `github_actions_resolver` | `gha_*` | local workflows/actions vs external `uses:` |
| `kustomize_resolver` | `kustomize_*` | resources, bases, patches, generators |

YAML files are classified by path (`yaml_classifier.py`) before resolver dispatch.

## CLI verbs

| Group | Commands |
|-------|----------|
| Index | `build`, `update`, `status` |
| Search | `ask`, `search`, `find`, `grep`, `get`, `path` |
| Cartography | `why`, `map`, `brief` |
| Graph | `relations`, `graph`, `vars` |
| Memory | `history` (add/stage/ingest/recent/search/get), `decide`, `decisions`, `resume` |
| Admin | `projects`, `stats`, `forget` |
| Config / DB | `config` (init/show/check-env), `db` (ping/init-postgres/migrate) |
| Integration | `install-skill`, `install-hooks` |

Prefer **`ask`** over **`search`** for agent use (hybrid + rerank + cache).

## Key conventions

- `paths.user_dir()` → `~/.knowledge/` (override with `KNOWLEDGE_HOME` for tests).
- `projects.current_project_root()` walks up from cwd until `.git/`; falls back to cwd.
- All `cmd_*` functions are command handlers dispatched from `cli.py`.
- `CHANGE_ME` is the only sanitizer replacement token — consistent across both layers.
- `config.SCHEMA_VERSION` + `config.CHUNKER_VERSION` in `meta`; mismatch → forced rebuild.
- Storage mode resolution: `KNOWLEDGE_DATABASE_URL` → walk `.knowledge.yaml` → `$HOME/.knowledge.yaml` → SQLite default.

## CLI entry point

```bash
knowledge --help
knowledge build
knowledge ask "your question"
knowledge resume
```
