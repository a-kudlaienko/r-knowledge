# Skill templates — copy into your repo

`SKILL.md` is the **single source of truth** for the knowledge instruction file. The other
two files here are *generated* from it (via `make sync-skill`) so each coding agent gets the
content in the format it discovers:

| File | Read by | Form |
|------|---------|------|
| `SKILL.md` | Claude Code | YAML frontmatter (`name`/`description`/…) + body |
| `AGENTS.md` | Codex, OpenCode, Cursor (root fallback) | plain prose, no frontmatter |
| `knowledge.mdc` | Cursor (native scoped rule) | `.mdc` frontmatter + body |

This directory also ships an optional second skill:
- **`knowledge-bench`** (dir: `knowledge-bench/SKILL.md`) — A/B comparison. `/knowledge-bench <question>` answers the same question twice (once with grep only, once with `knowledge`) and prints a side-by-side verdict.

## Install

The easiest path is the bundled installer, which writes the right file for each tool:

```bash
knowledge install-skill                       # Claude Code → .claude/skills/knowledge/SKILL.md
knowledge install-skill --ide cursor          # → .cursor/rules/knowledge.mdc
knowledge install-skill --ide codex,opencode  # → ./AGENTS.md (shared, merge-safe)
knowledge install-skill --ide all             # all four
knowledge install-skill --ide all --symlink   # link instead of copy (follows upstream updates)
```

### Manual copy (equivalent)

```bash
# Claude Code
mkdir -p .claude/skills/knowledge
cp ~/git/repo-knowledge/skill-template/SKILL.md .claude/skills/knowledge/SKILL.md

# Cursor
mkdir -p .cursor/rules
cp ~/git/repo-knowledge/skill-template/knowledge.mdc .cursor/rules/knowledge.mdc

# Codex / OpenCode (root AGENTS.md)
cp ~/git/repo-knowledge/skill-template/AGENTS.md ./AGENTS.md
```

### Benchmark skill (optional)

```bash
mkdir -p .claude/skills/knowledge-bench
cp ~/git/repo-knowledge/skill-template/knowledge-bench/SKILL.md \
   .claude/skills/knowledge-bench/SKILL.md
```

Adjust the source path if you cloned `repo-knowledge` elsewhere. Symlinks work too (`ln -s $(realpath …)`) if you want upstream updates to follow automatically. **Do not hand-edit `AGENTS.md` / `knowledge.mdc`** — edit `SKILL.md` and run `make sync-skill`.

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
