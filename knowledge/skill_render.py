"""Render the canonical skill body into per-IDE instruction files.

The single source of truth is ``skill-template/SKILL.md`` (Claude Code form:
YAML frontmatter + markdown body). Other coding agents discover instructions in
different files with different (or no) frontmatter, so we derive sibling
artifacts from the *same body*:

- ``skill-template/AGENTS.md``    — COMPACT by default (see
  ``render_agents``/``render_agents_compact`` below): a whitelisted, trimmed
  subset of SKILL.md sections (~8KB target), because this file's content is
  injected into every Codex/OpenCode/Gemini session unconditionally. Ends with
  a pointer to ``knowledge skill show`` for the full guide (progressive
  disclosure). Also used verbatim (via managed-block merge) for Gemini's
  ``GEMINI.md``.
- ``skill-template/knowledge.mdc`` — Cursor ``.mdc`` frontmatter + FULL body.
  Cursor rules are agent-requested (pulled in on demand via the frontmatter
  ``description``), not always-on, so there is no token-budget pressure here.

``knowledge install-skill`` copies/symlinks/merges these siblings into a
target repo. Run ``python -m knowledge.skill_render`` (or ``make sync-skill``)
after editing SKILL.md to regenerate them; ``tests/test_skill_sync.py`` fails
if they drift. The full canonical body is always available progressively via
``knowledge skill show`` regardless of which compact render an IDE was given.
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


# Whitelist of SKILL.md "## " headings pulled into the compact AGENTS render,
# in the order they should appear. This is the ONLY hand-maintained list — the
# *content* of each section still comes straight from SKILL.md, so editing
# SKILL.md prose (without renaming its headings) never requires touching this
# file. A heading that no longer exists is silently skipped (no crash); a new
# SKILL.md heading not in this list is simply left out of the compact form
# (still available in full via `knowledge skill show`).
COMPACT_SECTION_WHITELIST = [
    "Priority directives — READ FIRST",
    "Pre-change conflict check (MANDATORY)",
    "Finding code — intent → verb",
    "Auto-maintenance — run BEFORE any query verb",
    "Session memory — `decide` + `resume`",
    "Rules / gotchas",
]

# Safety-valve caps so a future SKILL.md edit can't silently blow the compact
# render past its token budget. Comfortably above what today's whitelisted
# sections need (see tests/test_skill_sync.py for the measured byte count) —
# they bind only if a section grows substantially.
_COMPACT_SECTION_MAX_CHARS = 2000
_COMPACT_MAX_CODE_BLOCKS_PER_SUBSECTION = 1

_AGENTS_FOOTER = "Full guide: run `knowledge skill show`."


def _split_h1_title(body: str) -> tuple[str, str]:
    """Split a frontmatter-free body into its leading ``# `` H1 line and the rest."""
    lines = body.split("\n")
    if lines and lines[0].startswith("# "):
        return lines[0], "\n".join(lines[1:]).lstrip("\n")
    return "", body


def _split_sections(body: str) -> dict[str, str]:
    """Split a markdown body into ``{heading: content}`` on ``## `` (H2) boundaries.

    Content between one ``## `` line and the next (or EOF) — nested ``### ``
    subsections stay inside their parent's content. Text before the first
    ``## `` (e.g. an intro paragraph) is not represented; callers that need it
    should pull it from the body directly.
    """
    sections: dict[str, str] = {}
    heading: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if heading is not None:
                sections[heading] = "\n".join(buf).strip("\n")
            heading = line[3:].strip()
            buf = []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections[heading] = "\n".join(buf).strip("\n")
    return sections


def _blocks(content: str) -> list[str]:
    """Split section content into blank-line-delimited blocks.

    Fenced ```code blocks``` stay atomic (blank lines inside them do not
    split the block); heading lines end up as their own single-line block
    because they're always blank-line-bounded in well-formed markdown.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for line in content.split("\n"):
        if line.strip().startswith("```"):
            current.append(line)
            in_code = not in_code
            if not in_code:
                blocks.append("\n".join(current))
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if line.strip() == "":
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _trim_section(
    content: str,
    *,
    max_chars: int = _COMPACT_SECTION_MAX_CHARS,
    max_code_blocks: int = _COMPACT_MAX_CODE_BLOCKS_PER_SUBSECTION,
) -> str:
    """Deterministically shrink one section for the compact render.

    Trim rules (simple, block-level, robust to future prose edits):
      1. Drop blockquote blocks (``> ...``) — these are the "example call-out"
         style asides (e.g. a required warning-message template) that are
         useful in the full guide but not load-bearing in a compact summary.
      2. Cap fenced code examples to ``max_code_blocks`` per ``### ``
         subsection (the counter resets on every heading block) — keeps one
         representative example per verb instead of every variant shown in
         the full guide.
      3. Cap the total trimmed length at ``max_chars``, stopping at a block
         boundary (never mid-block) so the output stays well-formed markdown.
         Always keeps at least one block so a section never renders empty.
    """
    kept: list[str] = []
    code_blocks_seen = 0
    for block in _blocks(content):
        stripped = block.strip()
        if stripped.startswith("#"):
            code_blocks_seen = 0  # new subsection: reset the per-subsection cap
        if stripped.startswith(">"):
            continue
        if stripped.startswith("```"):
            code_blocks_seen += 1
            if code_blocks_seen > max_code_blocks:
                continue
        kept.append(block)

    out: list[str] = []
    total = 0
    for block in kept:
        total += len(block) + 2  # +2 for the blank-line join
        if total > max_chars and out:
            break
        out.append(block)
    return "\n\n".join(out)


def render_agents_compact(skill_text: str) -> str:
    """Compact AGENTS-style render: whitelisted + trimmed sections of SKILL.md.

    Single source of truth: every word comes from SKILL.md itself (see
    ``COMPACT_SECTION_WHITELIST`` + ``_trim_section``) — there is no second
    hand-written file to keep in sync. Deterministic for a given SKILL.md.
    Always ends with a pointer to the full guide (``knowledge skill show``).
    """
    body = strip_frontmatter(skill_text).lstrip("\n")
    title, rest = _split_h1_title(body)
    sections = _split_sections(rest)

    parts = [GENERATED_BANNER]
    if title:
        parts.append(title)
    for heading in COMPACT_SECTION_WHITELIST:
        content = sections.get(heading)
        if content is None:
            continue  # heading renamed/removed upstream — skip, don't crash
        parts.append(f"## {heading}\n\n{_trim_section(content)}")
    parts.append(_AGENTS_FOOTER)
    return "\n\n".join(parts) + "\n"


def render_agents(skill_text: str, *, full: bool = False) -> str:
    """Render the AGENTS.md form: generated banner + body.

    Compact by default (see ``render_agents_compact``) — this is what's
    written into Codex/OpenCode/Gemini's always-on context. Pass ``full=True``
    for the verbatim frontmatter-free body (escape hatch, e.g. for tooling
    that wants the complete guide inline rather than via `knowledge skill show`).
    """
    if full:
        body = strip_frontmatter(skill_text).lstrip("\n")
        return f"{GENERATED_BANNER}\n\n{body}"
    return render_agents_compact(skill_text)


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
