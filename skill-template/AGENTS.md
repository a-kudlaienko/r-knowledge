<!-- Generated from skill-template/SKILL.md by `make sync-skill` (python -m knowledge.skill_render) — do not edit by hand. -->

# /knowledge — Code cartography + semantic search + session memory

## Priority directives — READ FIRST

These four rules apply on every invocation. They exist because they reduce tool-call count, prevent re-opening solved problems, and keep cross-session continuity intact.

1. **On a new session, run `knowledge resume` BEFORE any other tool** in this skill.  It returns last decisions + touched files + any un-ingested stage entries + hub files. ~1200 tokens, <200ms. Skip only when the user's very first message makes it obvious (e.g. a typo fix on a specific line).

2. **Pre-change conflict check (MANDATORY before any plan or non-trivial change).** See the dedicated section below. The user has lost time to changes that re-opened already-solved problems because a new session had no memory of the prior fight. This check is non-negotiable — even when the request looks small.

3. **Default to `knowledge ask` instead of `knowledge search`.** `ask` runs FTS + vector in parallel, merges via RRF, reranks by recency/session/hub centrality, caches by (query, HEAD sha). `search` is the vector-only raw-chunks path — use it only when you need `--top-k` with distance scores or downstream scripting.

4. **Record durable memory as you discover it, not at session end.** Use `knowledge decide` for non-obvious choices and `knowledge fact` for working fixes/research findings. Both are embedded, surfaced by `resume`, and found by `decisions --search`.

## Pre-change conflict check (MANDATORY)

Before drafting a plan, before writing/editing any file, and before each major step within a multi-step plan, query the index for prior decisions and incidents on the same topic. Sessions are weeks apart; the user will not remember every prior fight, and neither will you. The index does — including `knowledge fact` entries (working fixes), not just `decide` choices.

**Required queries (run in parallel when possible):**

1. `knowledge decisions --search "<topic>"` — prior `knowledge decide` entries on the same area.
2. `knowledge history search "<topic>"` — past work-log entries: incidents, rollbacks, painful fixes.
3. `knowledge relations <file>` for each file you intend to touch — hidden coupling that was likely the *reason* for an earlier decision.
4. `knowledge ask "what did we decide / fix about <topic>?"` — semantic catch-all when the topic word is fuzzy.

**STOP conditions — halt and surface to the user before continuing if ANY apply:**

- The proposed change contradicts a prior `knowledge decide` entry.
- The proposed change matches a pattern that previously caused an incident in `knowledge history` (e.g. "we tried this last month and it broke X").
- The user's request appears to undo work captured in a recent `knowledge history` milestone.
- The change touches a hub file or high-blast-radius file with no prior decision context (ask before proceeding).

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
