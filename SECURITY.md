# Security policy & threat model

`knowledge` is a local developer tool that indexes source code into a database
(local SQLite by default, or an optional shared PostgreSQL for teams) and
answers semantic/structural queries over it. This document states what it does
and does not defend against, so you can deploy it safely.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainer rather than
opening a public issue. Include a description, affected version/commit, and a
minimal reproduction. We aim to acknowledge within a few days.

## Trust boundaries

1. **The local machine and the invoking user are trusted.** The tool reads the
   files you point it at and runs `git` against your repositories with your
   privileges. It is not a sandbox.

2. **Scanned repository content is UNTRUSTED.** A repository you index may be a
   clone of someone else's code. The tool therefore:
   - HTML-escapes repo-derived strings rendered into the graph visualization
     (no stored XSS from a maliciously named file).
   - Skips file symlinks whose target escapes the repository root (no indexing
     of `~/.ssh/id_rsa` via a planted symlink).
   - Treats a repo-local `.knowledge-config.json` as untrusted: it is honored
     (closer-config-wins is intentional) but the tool **warns** before sending
     your indexed source and PostgreSQL credentials to a non-localhost host it
     names. Review that warning before continuing in an untrusted repo.

3. **On shared PostgreSQL, rows written by other users are UNTRUSTED input to
   your CLI.** Another teammate (or anyone who compromises the shared database)
   can write arbitrary `project_root` / `rel_path` / chunk rows. The tool
   guards file access (`get --raw`, `path`) with a path-containment check so a
   poisoned row cannot read files outside its project root on your machine.
   Still: **only connect to a shared database you and your teammates control.**

## Secret handling

The indexer runs a best-effort **sanitizer** over code chunks and over the
memory layer (history entries, decisions, and the offline outbox) before
storage, redacting common secret shapes (PEM private keys — including keys that
span chunk boundaries — SSH keys, JWTs, cloud/API tokens, Slack/Stripe keys,
and connection strings with inline credentials) and values under sensitive
structured keys (`password`, `api_key`/`api-key`, `secret`, …).

**The sanitizer is a safety net, not a guarantee.** Do not rely on it to
scrub secrets you would not otherwise want in a database:

- Files whose secret shape is not covered by a pattern can slip through.
- `knowledge get --raw` intentionally re-reads the *original* bytes from disk
  (within the project root) — it returns unsanitized content by design.
- Keep real secrets out of source control (`.env`, `*.pem`, etc. are not
  indexed when gitignored), and treat the index/`~/.knowledge` as sensitive.

## Credentials & transport (shared PostgreSQL mode)

- Credentials live only in environment variables (`KNOWLEDGE_PG_USER` /
  `KNOWLEDGE_PG_PASSWORD`) or inline in `KNOWLEDGE_DATABASE_URL`. Config files
  carry env-var *names*, never values. `config show` masks the password.
- Default `sslmode` is `require` (encrypted but does not verify the server
  certificate). **For any remote host, set `sslmode=verify-full`** so the
  server's certificate is validated; the tool warns when you connect to a
  non-localhost host with a weaker mode.

## Local state permissions

`~/.knowledge/` (index, model cache, staged work-notes, outbox) is created with
`0o700` so other local users on a shared host cannot read your indexed source
or work history.

## Supply chain

- The embedding model is pinned to a specific Hugging Face revision and loaded
  via safetensors (no pickle/code-execution on download).
- The graph visualization pins its single CDN dependency (vis-network) by
  version and verifies it with a Subresource Integrity hash.
- CI third-party actions are pinned to commit SHAs.
