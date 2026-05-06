---
name: knowledge-bench
description: Answer a question TWICE — once WITHOUT the `knowledge` skill (grep/glob/read only) and once WITH it — then compare the two passes head-to-head on tool-call count, files touched, answer quality, and latency. Use when the user wants to validate whether `knowledge` actually beats plain grep for their real queries, or when the user explicitly asks to "compare" / "benchmark" / "A/B" the two approaches.
argument-hint: <question>
allowed-tools: Bash Read Grep Glob
---

# /knowledge-bench — A/B the `knowledge` skill against plain grep

Runs the user's question through **two independent passes** in a single response, then presents both answers and a structured comparison. The goal is honest measurement — if Pass A (grep) answers just as well as Pass B (knowledge), say so.

## Workflow

### Pass A — baseline (Grep / Glob / Read only)

**Hard constraints — do not violate:**

- Do NOT invoke the `/knowledge` skill.
- Do NOT call the `knowledge` CLI (`knowledge status`, `knowledge search`, etc.) via Bash.
- Do NOT read any file under `~/.knowledge/`.
- Do NOT peek at the chunk previews that `knowledge` would return.
- Pretend the `knowledge` skill simply isn't installed.

**Work the question** using only `Grep`, `Glob`, and `Read`:

1. Identify candidate keywords / file globs from the question.
2. `Grep` for keywords; `Glob` for file patterns.
3. `Read` the most promising files (prefer offsets + limits over full reads — you're on a budget).
4. Stop when you have a confident answer **or** after ~15 tool calls, whichever comes first.

Capture for the comparison (see below):
- Number of tool calls you made in this pass.
- Unique files you `Read` (full relative paths).
- Final answer (concise, with `file:line` refs).

### Pass B — knowledge skill

Now answer the **same** question using the `knowledge` skill workflow:

1. `knowledge status --json` → branch on `state` (missing → build, stale → update, fresh → go).
2. Rewrite the question with the intent prefix that fits (`ansible task:`, `python function:`, `terraform resource:`, etc.).
3. `knowledge search "<enriched>" [--kind K] [--lang L] [--top-k N]`.
4. `knowledge get <id> --raw` or `Read file:line_range` for any chunks you want to inspect.

Capture:
- Number of tool calls.
- Files / chunk ids you referenced.
- Final answer (concise, with `file:line` refs).

### Comparison

Print **both answers in full** first. Then a metrics table:

```
| Metric                | Pass A (grep) | Pass B (knowledge) |
|-----------------------|---------------|--------------------|
| Tool calls            |     N         |       N            |
| Unique files opened   |     N         |       N            |
| Approx bytes read     |     KB        |       KB           |
| Pinpointed file:line? |    yes/no     |      yes/no        |
| Answer char count     |     N         |       N            |
```

Then a **verdict** in 2-4 sentences:
- Did both passes reach the same answer? If not, where did they diverge?
- Which pass was faster / used fewer tool calls?
- Which pass produced better file:line refs?
- Bottom line: was `knowledge` worth invoking for this question?

## Rules

- **Run Pass A FIRST, cleanly.** Don't slip into Pass B's workflow while still in Pass A. No "let me just check with knowledge to verify" — commit.
- **Verbatim question.** Don't rephrase the question between passes.
- **No contamination the other way.** In Pass B, don't lean on what Pass A's grep hits already told you — trust the `knowledge` workflow on its own merits.
- **Be honest in the verdict.** If Pass A found the right answer in 3 greps and Pass B took the same time, say so. If `knowledge` pointed at a file Pass A missed, say so. The point is measurement, not advocacy.

## When the verdict is usually interesting

- **Semantic questions** ("how does X work", "where is Y configured", "what generates Z") — `knowledge` should win on precision.
- **Exact-string questions** ("who calls `get_or_create_project`", "grep for `TODO`") — grep will tie or win; `knowledge` isn't designed for this.
- **Questions spanning multiple languages** ("where is vault mentioned anywhere") — `knowledge --all-projects` or no filter should surface cross-language hits that grep has to chase file type by file type.

## Example

User: `/knowledge-bench how does the karmada cert regeneration work`

- **Pass A**: `Grep karmada`, `Glob ansible/roles/karmada/**`, `Read ansible/roles/karmada/tasks/main.yml`, `Read ansible/roles/karmada/tasks/regenerate_certs.yml`, answer referencing those two files. ~5 tool calls.
- **Pass B**: `knowledge status`, `knowledge search "ansible task: karmada cert regeneration" --kind ansible_task --top-k 5`, Read regenerate_certs.yml once, answer referencing the same two files. ~3 tool calls.
- **Verdict**: Both found the right file. Pass B got there in 3 calls vs 5 and gave exact chunk IDs (so follow-up `knowledge get <id> --raw` is available). For well-known ansible tasks, the difference is marginal. For Python functions in a big codebase, the gap would be larger.
