"""Drift guard: the committed per-IDE skill siblings match a fresh render.

``skill-template/SKILL.md`` is the single source of truth. ``AGENTS.md`` (Codex /
OpenCode / Cursor-fallback) and ``knowledge.mdc`` (Cursor) are *generated* from
its body by ``knowledge.skill_render`` (``make sync-skill``). If someone edits
SKILL.md and forgets to regenerate, these tests fail with a reminder.

    python -m pytest tests/test_skill_sync.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge import skill_render

SKILL_DIR = Path(__file__).resolve().parent.parent / "skill-template"
HINT = "stale generated file — run `make sync-skill` (python -m knowledge.skill_render)"


@pytest.fixture(scope="module")
def skill_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_agents_md_in_sync(skill_text: str) -> None:
    expected = skill_render.render_agents(skill_text)
    actual = (SKILL_DIR / "AGENTS.md").read_text(encoding="utf-8")
    assert actual == expected, HINT


def test_cursor_mdc_in_sync(skill_text: str) -> None:
    expected = skill_render.render_cursor(skill_text, always_apply=False)
    actual = (SKILL_DIR / "knowledge.mdc").read_text(encoding="utf-8")
    assert actual == expected, HINT


def test_siblings_carry_no_yaml_frontmatter_leak(skill_text: str) -> None:
    # AGENTS.md must NOT start with a YAML block (Codex/OpenCode read plain prose).
    agents = skill_render.render_agents(skill_text)
    assert not agents.startswith("---\n"), "AGENTS.md leaked SKILL.md frontmatter"
    # The Claude description must survive into the Cursor .mdc frontmatter.
    assert skill_render.extract_description(skill_text) in skill_render.render_cursor(skill_text)


def test_strip_frontmatter_is_idempotent(skill_text: str) -> None:
    once = skill_render.strip_frontmatter(skill_text)
    assert skill_render.strip_frontmatter(once) == once
    assert not once.lstrip().startswith("---")


# ---------------------------------------------------------------------------
# Compact AGENTS.md render (todo/tasks/todo.md Item D)
# ---------------------------------------------------------------------------

# Bumped 8192 -> 8448 for Item H (decision id=195 had already flagged the
# render at 8072/8192, almost no headroom). The Item H spec requires two
# short additions to whitelisted sections (a `knowledge fact` trigger line in
# Session memory + a one-line mention in the conflict check) that push the
# render to 8410 bytes; trimming further would cut load-bearing prose rather
# than incidental padding, so the budget itself moves per the spec's explicit
# "if genuinely impossible, bump to 8448" fallback.
_MAX_COMPACT_BYTES = 8448  # ~2k tokens + Item H fact-verb mentions


def test_render_agents_is_compact_by_default(skill_text: str) -> None:
    # Default render must be the compact form, not the old full-body dump.
    compact = skill_render.render_agents(skill_text)
    full = skill_render.render_agents(skill_text, full=True)
    assert len(compact) < len(full)


def test_render_agents_compact_under_size_budget(skill_text: str) -> None:
    compact = skill_render.render_agents(skill_text)
    size = len(compact.encode("utf-8"))
    assert size <= _MAX_COMPACT_BYTES, (
        f"compact AGENTS.md render is {size} bytes, over the {_MAX_COMPACT_BYTES} "
        "byte (~2k token) budget"
    )


def test_render_agents_compact_is_deterministic(skill_text: str) -> None:
    first = skill_render.render_agents_compact(skill_text)
    second = skill_render.render_agents_compact(skill_text)
    assert first == second


def test_render_agents_compact_ends_with_full_guide_pointer(skill_text: str) -> None:
    compact = skill_render.render_agents_compact(skill_text)
    assert compact.rstrip("\n").endswith("Full guide: run `knowledge skill show`.")


def test_render_agents_compact_includes_whitelisted_sections(skill_text: str) -> None:
    compact = skill_render.render_agents_compact(skill_text)
    for heading in skill_render.COMPACT_SECTION_WHITELIST:
        assert f"## {heading}" in compact


def test_render_agents_compact_carries_fact_workflow(skill_text: str) -> None:
    """The always-on render must teach agents when and how to record facts."""
    compact = skill_render.render_agents_compact(skill_text)
    for required in (
        "knowledge fact",
        "--context",
        "--why",
        "--supersede",
        "--override-reason",
        "decisions --search",
        "resume",
        "conflict checks",
        "Four blocks in order",
    ):
        assert required in compact


def test_render_agents_full_is_escape_hatch(skill_text: str) -> None:
    # full=True must reproduce the old verbatim-body behavior exactly.
    full = skill_render.render_agents(skill_text, full=True)
    body = skill_render.strip_frontmatter(skill_text).lstrip("\n")
    assert full == f"{skill_render.GENERATED_BANNER}\n\n{body}"


def test_render_agents_compact_tolerates_missing_whitelisted_heading() -> None:
    # A whitelisted heading that no longer exists upstream must be skipped,
    # not crash — SKILL.md prose/heading edits shouldn't break the renderer.
    synthetic = (
        "---\n"
        "name: x\n"
        "description: y\n"
        "---\n"
        "# /knowledge — test\n\n"
        "## Priority directives — READ FIRST\n\n"
        "Some directive text.\n\n"
        "## Some Unrelated New Section\n\n"
        "Content the whitelist doesn't know about.\n"
    )
    compact = skill_render.render_agents_compact(synthetic)
    assert "## Priority directives — READ FIRST" in compact
    assert "Some directive text." in compact
    # Unknown heading not in the whitelist is simply absent from the compact form.
    assert "Some Unrelated New Section" not in compact
    # Other whitelisted headings that aren't present are skipped, not errors.
    assert compact.rstrip("\n").endswith("Full guide: run `knowledge skill show`.")


def test_render_agents_compact_tolerates_no_whitelisted_headings_at_all() -> None:
    # Degenerate case: none of the whitelist is present. Must not crash;
    # still produces a well-formed (if minimal) render ending in the pointer.
    synthetic = "# /knowledge — test\n\n## Totally Different Heading\n\nBody.\n"
    compact = skill_render.render_agents_compact(synthetic)
    assert compact.startswith(skill_render.GENERATED_BANNER)
    assert compact.rstrip("\n").endswith("Full guide: run `knowledge skill show`.")
