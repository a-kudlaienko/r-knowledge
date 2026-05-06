# Skill templates — copy into your repo

This directory ships two skills for Claude Code:

- **`knowledge`** (file: `SKILL.md`) — the main semantic search skill. `/knowledge <question>` auto-builds/updates the index, enriches the query, returns the matching chunks.
- **`knowledge-bench`** (dir: `knowledge-bench/SKILL.md`) — A/B comparison. `/knowledge-bench <question>` answers the same question twice (once with grep only, once with `knowledge`) and prints a side-by-side verdict.

## Install

### Main skill (required)

```bash
mkdir -p .claude/skills/knowledge
cp ~/git/repo-knowledge/skill-template/SKILL.md \
   .claude/skills/knowledge/SKILL.md
```

### Benchmark skill (optional)

```bash
mkdir -p .claude/skills/knowledge-bench
cp ~/git/repo-knowledge/skill-template/knowledge-bench/SKILL.md \
   .claude/skills/knowledge-bench/SKILL.md
```

Adjust the source path if you cloned `repo-knowledge` elsewhere. Symlinks work too (`ln -s $(realpath …)`) if you want upstream skill updates to follow automatically.

## What each skill does

### `/knowledge <question>`

1. Checks `knowledge status --json` — auto-runs `build` (first time) or `update` (if stale).
2. Rewrites your question with a kind-specific prefix (`python function:`, `ansible task:`, etc.) that retrieves better.
3. Runs `knowledge search` and returns matching chunks with `file:line` refs.

### `/knowledge-bench <question>`

1. **Pass A** — answers using only `Grep`, `Glob`, `Read`. Measures tool calls + files opened.
2. **Pass B** — answers using `/knowledge`'s normal workflow. Measures the same.
3. Prints both answers in full + a metrics table + an honest verdict on whether `knowledge` actually helped for this question.

Use `knowledge-bench` when you want to validate the tool against your real queries, or when you're debating whether a semantic index is worth maintaining for a given codebase.
