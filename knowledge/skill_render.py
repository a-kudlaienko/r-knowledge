"""Render the canonical skill body into per-IDE instruction files.

The single source of truth is ``skill-template/SKILL.md`` (Claude Code form:
YAML frontmatter + markdown body). Other coding agents discover instructions in
different files with different (or no) frontmatter, so we derive sibling
artifacts from the *same body*:

- ``skill-template/AGENTS.md``    — body only, no frontmatter. Read by Codex,
  OpenCode, and Cursor (as a root fallback).
- ``skill-template/knowledge.mdc`` — Cursor ``.mdc`` frontmatter + body.

``knowledge install-skill`` copies/symlinks these siblings into a target repo.
Run ``python -m knowledge.skill_render`` (or ``make sync-skill``) after editing
SKILL.md to regenerate them; ``tests/test_skill_sync.py`` fails if they drift.
"""

from __future__ import annotations

from pathlib import Path

# Marker that flags a file as generated. Mirrors the banner used elsewhere so a
# human opening AGENTS.md / knowledge.mdc knows not to hand-edit it.
GENERATED_BANNER = (
    "<!-- Generated from skill-template/SKILL.md by `make sync-skill` "
    "(python -m knowledge.skill_render) — do not edit by hand. -->"
)


def _skill_template_dir() -> Path:
    """Locate the repo's ``skill-template/`` directory (editable-install layout)."""
    return Path(__file__).resolve().parent.parent / "skill-template"


def strip_frontmatter(text: str) -> str:
    """Return ``text`` with a leading ``---\\n…\\n---\\n`` YAML block removed.

    Idempotent: text without frontmatter is returned unchanged.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n") :]


def extract_description(text: str) -> str:
    """Pull the ``description:`` value out of a SKILL.md YAML frontmatter block.

    Returns an empty string when there is no frontmatter or no description line.
    The description is a single physical line in SKILL.md (no folding).
    """
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    front = text[4:end]
    for line in front.splitlines():
        if line.startswith("description:"):
            return line[len("description:") :].strip()
    return ""


def render_agents(skill_text: str) -> str:
    """Render the AGENTS.md form: generated banner + frontmatter-free body."""
    body = strip_frontmatter(skill_text).lstrip("\n")
    return f"{GENERATED_BANNER}\n\n{body}"


def render_cursor(skill_text: str, *, always_apply: bool = False) -> str:
    """Render the Cursor ``.mdc`` form: ``.mdc`` frontmatter + banner + body.

    ``alwaysApply: false`` (default) makes this an *Agent-Requested* rule —
    Cursor pulls it in when the task is relevant, via the ``description``,
    instead of prepending the whole body to every prompt. Pass
    ``always_apply=True`` to attach it unconditionally.
    """
    description = extract_description(skill_text)
    body = strip_frontmatter(skill_text).lstrip("\n")
    frontmatter = (
        "---\n"
        f"description: {description}\n"
        f"alwaysApply: {'true' if always_apply else 'false'}\n"
        "---\n"
    )
    return f"{frontmatter}\n{GENERATED_BANNER}\n\n{body}"


def regenerate(skill_dir: Path | None = None, *, always_apply: bool = False) -> list[Path]:
    """Regenerate AGENTS.md + knowledge.mdc from SKILL.md. Returns written paths."""
    skill_dir = skill_dir or _skill_template_dir()
    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    targets = {
        skill_dir / "AGENTS.md": render_agents(skill_text),
        skill_dir / "knowledge.mdc": render_cursor(skill_text, always_apply=always_apply),
    }
    written = []
    for path, content in targets.items():
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    for path in regenerate():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
