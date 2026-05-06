# knowledge package

Local semantic code search. Single SQLite DB at `~/.knowledge/index.sqlite` holds many projects keyed by `projects.root_path`.

**Entry point**: `knowledge.cli:main` (registered as `knowledge` console_script). All logic lives in this package.

## Module Map

> **First rule**: look here before `grep`-ing the source.

| Module | Purpose | Key functions |
|--------|---------|---------------|
| `paths.py` | Filesystem locations (user home ~/.knowledge/) | `user_dir`, `db_path`, `models_dir`, `config_path`, `stage_path` |
| `config.py` | All hardcoded constants | `MODEL`, `EMBEDDING_DIM`, `MAX_CHARS`, `CACHE_BYTES`, `INCLUDE_GLOBS`, `SCHEMA_VERSION`, `CHUNKER_VERSION` |
| `db.py` | SQLite connect + schema + sqlite-vec load | `connect`, `init_schema`, `get_meta`, `set_meta` |
| `projects.py` | Project detection + registry CRUD | `current_project_root`, `get_or_create_project`, `resolve_project`, `list_projects`, `forget_project` |
| `whitespace.py` | Reversible whitespace compression for embedded text | `compress`, `decompress` |
| `gitignore.py` | .gitignore + .knowledgeignore handling | `load_specs`, `is_ignored` |
| `sanitizer.py` | Regex scrub (layer 1) + sensitive-key replacement (layer 2) | `scrub_text`, `is_sensitive_key`, `SECRET_PATTERNS`, `SENSITIVE_KEYS` |
| `scanner.py` | Walk project, apply ignore rules, dispatch chunker | `walk_project`, `classify_file` |
| `chunkers/base.py` | Chunk dataclass + BaseChunker ABC (takes `file_path` for path-based dispatch) | `Chunk`, `BaseChunker` |
| `chunkers/__init__.py` | Language tag → chunker registry | `dispatch_chunker` |
| `chunkers/python_chunker.py` | Python AST via tree-sitter | `PythonChunker` |
| `chunkers/javascript_chunker.py` | JS + TS (shared logic, `TypeScriptChunker` subclass overrides `PARSER_NAME`) | `JavaScriptChunker`, `TypeScriptChunker` |
| `chunkers/yaml_chunker.py` | PyYAML-based; Ansible tasks/handlers + Helm templates + K8s manifests + generic (path-driven) | `YamlChunker` |
| `chunkers/hcl_chunker.py` | Terraform/HCL blocks via regex + brace tracker (no tree-sitter-hcl) | `HclChunker` |
| `chunkers/json_chunker.py` | tree-sitter-json, with sanitizer L2 for sensitive string values | `JsonChunker` |
| `chunkers/shell_chunker.py` | tree-sitter-bash for function definitions | `ShellChunker` |
| `chunkers/jinja_chunker.py` | Regex for `{% block/macro/call/filter %}` | `JinjaChunker` |
| `chunkers/dockerfile_chunker.py` | Regex for `FROM … AS stage` | `DockerfileChunker` |
| `chunkers/markdown_chunker.py` | Regex for H1/H2 section split | `MarkdownChunker` |
| `big_split.py` | Split oversized chunks into `big_parent` + ordered `big_subchunk` (line-window); parent keeps full byte range for exact `--raw` reassembly | `split_if_oversized` |
| `embedder.py` | sentence-transformers loader + batch encode; offline mode after first cache | `Embedder`, `get_embedder` |
| `indexer.py` | Orchestrate scan → chunk → big_split → sanitize+compress → embed → upsert. Build + incremental-update with selective re-embed | `build_project`, `update_project`, `_prepare_chunk_row`, `_version_mismatches` |
| `search.py` | Vector query + project scoping; `get_family` for big_parent+subchunks | `search`, `get_chunk`, `get_family`, `SearchResult` |
| `history.py` | Per-project work-summary store (RAG memory); embeds only `short_summary`, retrieves `long_summary` by id | `add`, `search`, `recent`, `get`, `ingest_stage`, `HistoryEntry` |
| `resolvers/base.py` | `Edge` dataclass + `BaseResolver` ABC | `Edge`, `BaseResolver` |
| `resolvers/__init__.py` | `(lang, file_path)` → resolver registry (YAML variants via path classifier) | `dispatch_resolver` |
| `resolvers/yaml_classifier.py` | YAML flavor picker (ansible / helm / gha / kustomize / plain-k8s) by path convention | `classify_yaml_path` |
| `resolvers/python_resolver.py` | Tree-sitter walk for `import`, `from_import`, `importlib.import_module` | `PythonResolver` |
| `resolvers/javascript_resolver.py` | Tree-sitter walk for `import`, `require()`, dynamic `import()`; TS subclass swaps parser | `JavaScriptResolver`, `TypeScriptResolver` |
| `resolvers/terraform_resolver.py` | HCL regex + brace-tracking for `module{source=}`, `templatefile()`, `file()` | `TerraformResolver` |
| `resolvers/helm_resolver.py` | `Chart.yaml` deps + regex for `{{ include/template "name" }}` and `{{ define "name" }}` | `HelmResolver` |
| `resolvers/ansible_resolver.py` | PyYAML walk for `import_playbook`, `include_tasks/import_tasks`, `include_role/import_role/roles:`, `vars_files`, `include_vars`, custom-module detection on task keys | `AnsibleResolver` |
| `resolvers/github_actions_resolver.py` | PyYAML walk for `uses:` — classifies local reusable workflows, local composite actions, external `owner/repo@ref` | `GitHubActionsResolver` |
| `resolvers/kustomize_resolver.py` | PyYAML walk for `resources`/`bases`/`components`/patches/generators (files entries) | `KustomizeResolver` |
| `relations.py` | File-edge store: resolver-to-file resolution for Python/JS/TF/Helm/Ansible/GHA/Kustomize. `FileIndex.prepare()` scans ansible.cfg(s), builds custom-module map, per-chart helm-template map, and loads per-project variables for Jinja / Terraform substitution. | `FileIndex`, `extract_edges`, `insert_edges`, `wipe_file`, `get_forward`, `get_reverse`, `stats`, `find_file_id`, `EdgeRow` |
| `variables.py` | Per-project variable table (`project_variables`): CRUD, `{{ name }}` / `${var.x}` substitution, `apply_variables` re-resolves existing edges after vars change. Scoped by domain (`ansible`/`terraform`/`helm`/`all`). | `set_many`, `unset`, `unset_scope`, `list_vars`, `import_json`, `substitute`, `apply_variables`, `has_template_markers`, `Variable`, `VALID_SCOPES` |
| `graph.py` | Renders the dependency graph to a self-contained HTML with vis-network. Color by top-level directory; resolved project-to-project edges by default; opt-in external/parametric/unresolved buckets as synthetic nodes. | `build_graph_html`, `GraphNode`, `GraphEdge` |
| `cache.py` | Byte-bounded LRU (daemon-mode shim; not wired in CLI hot path) | `ByteBoundedLRU` |
| `cli.py` | argparse + dispatch | `main`, `cmd_build`, `cmd_update`, `cmd_search`, `cmd_status`, `cmd_projects`, `cmd_stats`, `cmd_get`, `cmd_path`, `cmd_forget`, `cmd_history` (+ nested `add`/`ingest`/`recent`/`search`/`get`), `cmd_relations` |

## Key conventions

- `paths.user_dir()` returns `~/.knowledge/` (overridable via `KNOWLEDGE_HOME` env var — useful for tests).
- `projects.current_project_root()` walks up from cwd until it finds `.git/`; falls back to cwd.
- All `cmd_*` functions are public command handlers, imported and dispatched in `cli.py`.
- The `CHANGE_ME` marker is the *only* replacement value for sanitized content — consistent across both sanitization layers.
- `config.SCHEMA_VERSION` + `config.CHUNKER_VERSION` are written to the `meta` table on first init; a mismatch triggers a forced rebuild.

## CLI entry point

```bash
knowledge --help                 # after `pip install -e .`
knowledge build                  # from any git repo root
knowledge search "your query"
```
