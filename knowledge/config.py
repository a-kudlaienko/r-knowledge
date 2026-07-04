"""All hardcoded constants live here.

Anything that might be tuned by a user belongs in ``.knowledge-config.json``
(per-project closer-wins or ``~/.knowledge/config.json`` as the laptop
default — see :mod:`knowledge.settings`). This module only holds
build-time constants that the chunker/embedder/scanner depend on; they
shouldn't change at runtime.
"""

from __future__ import annotations

# Schema + chunker versioning. Bump CHUNKER_VERSION when chunking rules change
# in a way that invalidates existing chunks (forces rebuild via meta mismatch).
# Bumped 2 -> 3: tree-sitter-languages -> tree-sitter-language-pack migration
# (Python 3.13 CI fix). Same get_parser(name) surface and node-walking logic,
# but the grammars themselves are newer upstream builds; chunk boundaries can
# shift subtly even though extraction code is unchanged. Forcing a rebuild
# here avoids a silently-inconsistent index with some chunks parsed by the
# old grammars and some by the new ones.
SCHEMA_VERSION = "2"
CHUNKER_VERSION = "3"

# Embedding model. BAAI/bge-small-en-v1.5: 384-dim, ~130MB, strong MTEB
# score for mixed code+text retrieval, 512-token window.
MODEL = "BAAI/bge-small-en-v1.5"
# Pinned to the exact HuggingFace commit that is already on disk.
# Supply-chain safety: floating "main" would re-download whatever the hub
# currently serves — a compromised or MITM'd repo could swap in a pickle
# (.bin) variant and achieve RCE on the first ``encode`` call.
# Confirmed from local cache:
#   ~/.knowledge/models/models--BAAI--bge-small-en-v1.5/snapshots/<sha>/
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
EMBEDDING_DIM = 384

# Chunk size threshold. Above this, split into big_parent + big_subchunks.
MAX_CHARS = 1500

# RAM cache budget (bytes). Overridable via .knowledge-config.json (cache_bytes key).
CACHE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Scan defaults. User-repo overrides live in .knowledge-config.json.
# Applied as a last-resort floor on top of .gitignore + .knowledgeignore
# (so .git/ internals stay out even if the user unignores them by accident).
# Gitignore syntax — pathspec GitWildMatchPattern interprets these.
EXCLUDE_GLOBS = [
    ".git/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".terraform/",
    "_local/",
    ".local/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "dist/",
    "build/",
    "*.egg-info/",
]

# Extension → language tag. Unknown extensions are skipped silently.
EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".tfvars": "hcl",
    ".yaml": "yaml",
    ".yml": "yaml",
    # Helm's ``_helpers.tpl`` and Terraform's ``*.tftpl`` templates are
    # YAML-shaped in practice. Classified as ``yaml`` so the path-based
    # resolver dispatch (yaml_classifier) picks the right flavor — or,
    # for terraform templates, just indexes them as leaf files so
    # ``tf_templatefile`` edges have a target to resolve to.
    ".tpl":   "yaml",
    ".tftpl": "yaml",
    ".json": "json",
    ".sh": "shell",
    ".bash": "shell",
    ".j2": "jinja",
    ".jinja": "jinja",
    ".jinja2": "jinja",
    "Dockerfile": "dockerfile",  # matched by name, not extension
    ".md": "markdown",
    ".markdown": "markdown",
}

# Default top-k for search (overridable per-query).
DEFAULT_TOP_K = 10

# Staleness threshold for `status`: if any scanned file has mtime newer than
# this many seconds past last_update, the project is "stale". Zero means
# strict equality — any modification makes it stale.
STALE_GRACE_SECONDS = 1.0
