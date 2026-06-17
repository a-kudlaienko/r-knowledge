"""Secret sanitization.

Two layers, both producing the same sentinel ``CHANGE_ME``:

* **Layer 1 — regex scrub** (``scrub_text``): applied to every chunk's text
  before storage/embedding. Catches inlined tokens in source files
  regardless of surrounding structure (Python string literals, Markdown,
  HCL values, YAML scalars, etc.).

* **Layer 2 — sensitive-key replacement** (``is_sensitive_key``): used by
  structured-data chunkers (YAML/HCL/JSON, landing in M3) to replace
  values under sensitive-looking keys. Layer 2 lives in this module only
  as a helper — each chunker calls ``is_sensitive_key`` on its own tree
  during node traversal.

Patterns are intentionally permissive within each known format — false
positives (replacing a real non-secret) are much cheaper than false
negatives (leaking a secret into the DB).
"""

from __future__ import annotations

import re
from typing import Any

CHANGE_ME = "CHANGE_ME"

# Pattern → compiled regex. Order matters only for overlapping matches
# (first match wins; subsequent patterns run on the already-scrubbed text,
# so later patterns won't re-match a ``CHANGE_ME`` replacement).
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github_pat":            re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    "github_fine_grained":   re.compile(r"github_pat_[A-Za-z0-9_]{80,}"),
    "vault_token":           re.compile(r"hvs\.[A-Za-z0-9_-]{20,}"),
    "aws_access_key":        re.compile(r"AKIA[0-9A-Z]{16}"),
    "jwt":                   re.compile(r"eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_.=-]+"),
    "private_key_block":     re.compile(
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"
    ),
    "ssh_authorized_key":    re.compile(
        # Floor lowered to 40 chars so Ed25519 (~68-char base64 body) is caught;
        # RSA (372+ chars) and ECDSA nistp256 (~108 chars) still match as before.
        r"(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp\d+)\s+[A-Za-z0-9+/=]{40,}"
    ),
    "slack_token":           re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "stripe_secret_key":     re.compile(r"sk_live_[0-9a-zA-Z]{24,}"),
    # Connection strings that embed credentials inline: scheme://user:pass@host
    "dsn_with_credentials":  re.compile(
        r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^:@/\s]+:[^@/\s]+@[^\s/]+"
    ),
}

# Key names whose *values* should be replaced with ``CHANGE_ME`` in
# structured data. Substring match, case-insensitive — so ``vault_role_id``,
# ``VAULT_ROLE_ID``, and ``some_role_id`` all match.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "priv_key",
    "client_secret",
    "refresh_token",
    "access_token",
    "vault_role_id",
    "vault_secret_id",
    "role_id",
    "secret_id",
})


def scrub_text(text: str) -> str:
    """Apply layer-1 regex scrub. Idempotent."""
    out = text
    for pat in SECRET_PATTERNS.values():
        out = pat.sub(CHANGE_ME, out)
    return out


def is_sensitive_key(key: str) -> bool:
    """Layer-2 predicate for structured-data chunkers.

    Normalises hyphens to underscores before matching so that ``api-key``,
    ``client-secret``, and ``private-key`` are caught alongside their
    underscore equivalents already listed in ``SENSITIVE_KEYS``.
    """
    k = key.lower().replace("-", "_")
    return any(s in k for s in SENSITIVE_KEYS)


# ---------------------------------------------------------------------------
# Layer 2 — structured-data scrubbers
# ---------------------------------------------------------------------------
#
# These operate on chunk text (a slice of the original file) together with
# some chunker-specific context: PyYAML node tree for YAML, nothing for HCL
# (regex suffices). Output is the same text with sensitive values replaced.


def scrub_yaml_sensitive_values(chunk_text: str, chunk_node: Any, chunk_start_index: int) -> str:
    """Walk the YAML node tree rooted at ``chunk_node`` and replace the
    *values* of sensitive-named keys in ``chunk_text`` with ``CHANGE_ME``.

    ``chunk_start_index`` is the character offset of ``chunk_text`` within
    the original file — PyYAML's node marks are relative to the full file,
    so we translate to chunk-local coordinates before editing.
    """
    spans: list[tuple[int, int]] = []
    _collect_yaml_sensitive_spans(chunk_node, spans)
    if not spans:
        return chunk_text

    # Translate to chunk-local and apply from end to start so earlier
    # replacements don't shift later offsets.
    chunk_len = len(chunk_text)
    local_spans = []
    for s, e in spans:
        ls = s - chunk_start_index
        le = e - chunk_start_index
        if 0 <= ls < le <= chunk_len:
            local_spans.append((ls, le))

    local_spans.sort(key=lambda p: -p[0])
    out = chunk_text
    for s, e in local_spans:
        out = out[:s] + CHANGE_ME + out[e:]
    return out


def _collect_yaml_sensitive_spans(node: Any, spans: list[tuple[int, int]]) -> None:
    """Recurse a PyYAML node, collecting (start, end) character offsets of
    scalar values whose key is sensitive.
    """
    # PyYAML node types are checked by class name to avoid importing yaml
    # at module top-level (keeps sanitizer.py import-cheap for tests).
    cls = type(node).__name__

    if cls == "MappingNode":
        for key_node, val_node in node.value:
            key_cls = type(key_node).__name__
            val_cls = type(val_node).__name__
            if (
                key_cls == "ScalarNode"
                and is_sensitive_key(str(key_node.value))
                and val_cls == "ScalarNode"
            ):
                spans.append(
                    (val_node.start_mark.index, val_node.end_mark.index)
                )
                continue  # don't recurse into replaced value
            _collect_yaml_sensitive_spans(val_node, spans)
    elif cls == "SequenceNode":
        for item in node.value:
            _collect_yaml_sensitive_spans(item, spans)
    # ScalarNode: leaf, nothing to do


_HCL_KV_RE = re.compile(
    r'([a-zA-Z_][\w-]*)(\s*=\s*)"([^"\\]*(?:\\.[^"\\]*)*)"'
)


def scrub_hcl_sensitive_values(chunk_text: str) -> str:
    """Regex-based layer-2 scrub for HCL chunks.

    Matches ``key = "string_value"`` and replaces the value when the key
    is sensitive. Covers the overwhelmingly common case; more complex RHS
    forms (heredocs, function calls, references) are rarely secrets and
    are left alone.
    """
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        eq = m.group(2)
        if is_sensitive_key(key):
            return f'{key}{eq}"{CHANGE_ME}"'
        return m.group(0)

    return _HCL_KV_RE.sub(repl, chunk_text)
