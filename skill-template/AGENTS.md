<!-- Generated from skill-template/SKILL.md by `make sync-skill` (python -m knowledge.skill_render) — do not edit by hand. -->

# /knowledge — Code cartography + semantic search + session memory

## Priority directives — READ FIRST

These rules have scoped triggers: session resume applies at session start; the conflict gate applies only at the planning/material-execution transition defined below.

1. **On a new session, run `knowledge resume` BEFORE any other tool** in this skill.  It returns last decisions + touched files + any un-ingested stage entries + hub files. ~1200 tokens, <200ms. Skip only when the user's very first message makes it obvious (e.g. a typo fix on a specific line).

2. **Conflict preflight is a transition gate, not an invocation/query gate.** Run it before concrete implementation planning or material state-changing execution—not for ordinary read-only questions or exploration.

3. **Default to `knowledge ask` instead of `knowledge search`.** `ask` runs FTS + vector in parallel, merges via RRF, reranks by recency/session/hub centrality, caches by (query, HEAD sha), and now prefaces matching prior `decide`/`fact` entries inline. `search` is the vector-only raw-chunks path — use it only when you need `--top-k` with distance scores or downstream scripting.

4. **Record durable memory as you discover it, not at session end.** Use `knowledge decide` for non-obvious choices and `knowledge fact` for working fixes/research findings. Both are embedded, surfaced by `resume`, and found by `decisions --search`.

## Pre-change conflict gate (planning / execution only)

This is a transition gate, not a query gate. Do **not** run it merely because `/knowledge` was invoked or the user asked a question. Skip it for read-only Q&A, explanations, code navigation/search, status/history, summaries, reviews, diagnosis, and mechanical typo/format-only edits with no behavioral impact.

Run it once when work crosses into either:

- a concrete implementation, refactor, or migration plan/specification; or
- material execution: behavioral code/config/schema/dependency edits, migrations, deploys, commits, or other state changes.

If exploration later becomes a plan/change, run the gate at that transition. Re-run only when the topic, scope, or intended files materially change—not before every step.

**Required queries when triggered (parallel where possible):**

1. `knowledge decisions --search "<topic>"` — prior decisions and facts.
2. `knowledge history search "<topic>"` — incidents, rollbacks, and recent milestones.
3. `knowledge ask "what did we decide / fix about <topic>?"` — semantic catch-all.
4. Once candidate files are known, `knowledge relations <file>` for each one before editing.

**STOP conditions — halt and surface to the user before continuing if ANY apply:**

- Proposed work contradicts a prior decision/fact.
- It repeats an incident or undoes a recent history milestone.
- It touches a hub/high-blast-radius file with no prior context.

**Required warning format when stopping** (do not silently push through):

If the user explicitly chooses (1), record the override with `knowledge decide <topic> --decision "<new>" --supersede <id> --override-reason "<why>"`. The tool **blocks** (exit 3) until you supply `--override-reason`, and stamps the overriding author — so the reversal is attributable to whoever made it, which matters most in shared-DB mode where teammates rely on the old behavior. Never overwrite history silently.

## Finding code — intent → verb

| Intent | Use |
|--------|-----|
| Unfamiliar repo | **`knowledge brief`**, then `knowledge map` |
| Meaning / "how does X" / "where is Y" | **`knowledge ask "<question>"`** (default — not `search`) |
| Known symbol | **`knowledge find <name>`** |
| Exact phrase / keyword | **`knowledge grep '<pattern>'`** |
| One file before Read | **`knowledge why <path>`** |
| Imports / callers / blast radius | **`knowledge relations <file>`** before `ask` |
| Continue prior work | **`knowledge history recent`** or `knowledge decisions --search "<topic>"` |

Only after these return paths and line ranges: **Read** those slices. Built-in **Grep**/**Glob** only on paths the index has already narrowed — never as the first repo-wide step.

### Prohibited

- Repository-wide **Grep**, **Glob**, or **Task**/`explore` subagents as the **first** step for meaning-shaped questions.
- Speculative reads of whole trees or large files.
- **`knowledge search`** for normal Q&A — use **`knowledge ask`**.

### Escalation order

`knowledge` (resume → status → relations / ask / find / grep / why) → `docs/` → targeted **Read** → **Grep**/**Glob** only on a path the index already returned.

## Auto-maintenance — run BEFORE any query verb

```bash
knowledge status --json
```

Branch on `state`:
- `missing` → `knowledge build` (first-time: 1–5 min for embedding model + initial encode; warn the user).
- `stale`   → `knowledge update` (usually <5s; only re-embeds chunks whose sanitized text changed).
- `fresh`   → go straight to your query verb.

Embedding verbs auto-use a warm-model daemon (20-min idle exit; `knowledge daemon status|stop`; disable via `KNOWLEDGE_NO_DAEMON=1`). Daemon failures fall back in-process — never an error to handle.

## Session memory — `decide` + `resume`

See the priority directives above. Full detail:

### `decide` — record a non-obvious choice

```bash
knowledge decide "cache invalidation" \
  --decision "wipe per-project on any chunk change; preserve on no-op update" \
  --rationale "agent-driven updates on every turn shouldn't thrash cache" \
  --files knowledge/query_cache.py knowledge/indexer.py
```

Topic and decision are required; rationale/files preserve the useful why and blast radius. Every row is author-stamped (git identity, then UNIX-login fallback) for shared-DB attribution.

**Overriding a prior decision** (changing an established standard) requires an explicit acknowledgment:

- `--supersede <id>` links the new decision to the one it overrides.
- The tool **blocks (exit 3)** until `--override-reason "<why>"` is supplied — a teammate relying on the old behavior deserves to know why it changed.
- Reusing an exact topic without `--supersede` is legal but prints a non-blocking override nudge.

### `fact` — record a working fix, lesson, or project-related research finding

Record a non-obvious fix/research result with `knowledge fact "<topic>" --fact "<rule>" --context "<raw symptom>" --why "<evidence>" [--files PATH ...]`. Facts share decisions' attributed/embedded store and appear in search, resume, and conflict checks with a `[fact]` marker; pasted symptoms find them. Better fix: `--supersede <id> --override-reason "<why>"` (same gated chain). Keep this workflow in tracked skill templates; never rely on user-local/gitignored instruction or lessons files.

### `decisions` — list or semantically search

```bash
knowledge decisions --limit 5
knowledge decisions --topic cache              # substring filter on topic
knowledge decisions --search "how to handle stale caches"   # semantic over decisions+facts+context
```

### `resume` — the session-start brief

```bash
knowledge resume
```

Four blocks in order: last 5 decisions, 10 most-touched files (7d), un-ingested stage entries, top 3 hub files. ~1200 tokens, idempotent. Run first on every new session.

## Rules / gotchas

- **First build is slow** — cold-start downloads the 130MB embedding model to `~/.knowledge/models/`. Warn the user before running `build` on a fresh machine.
- **Don't commit the DB** — `~/.knowledge/index.sqlite` is per-machine. Each teammate rebuilds locally.
- **`.gitignore` is honored.** Secret-shaped files (`.env`, `*.pem`, etc.) that are gitignored are never scanned. Regex + structured-key sanitization scrub the rest. Any `CHANGE_ME` token in search results is either a user placeholder or a sanitizer replacement — never a real leaked secret.
- **Version drift → rebuild.** If the tool's chunker or embedding model was bumped, `update` auto-falls-back to `build` and warns you. Other projects in the shared DB need their own `build` too.
- **`.knowledgeignore`** in the repo root takes gitignore-style patterns for extra exclusions (e.g., generated docs) without polluting `.gitignore`.

Full guide: run `knowledge skill show`.
