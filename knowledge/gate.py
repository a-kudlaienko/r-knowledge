"""Pre-change conflict sensor for ``knowledge gate``.

``AGENTS.local.md`` mandates a "pre-change conflict check" — query prior
decisions/incidents before touching code — but as prose it is a guide, not a
sensor: nothing returns a verdict an agent (or a hook) can branch on. This
module is that sensor. It answers one question: "before I change this, is
there prior knowledge I should heed?"

Four signals, all local, zero LLM calls (reuses the same embedder every other
retrieval verb uses):

1. Decisions/facts semantically near ``topic`` (:func:`knowledge.decisions.search`).
2. History entries semantically near ``topic`` (:func:`knowledge.history.search`)
   — "we tried this and it broke X" incidents.
3. Decisions whose ``files_touched`` intersects ``files`` — an exact-match
   signal, independent of embeddings, often the strongest one available.
4. Coupling for ``files`` — direct (depth=1) import/imported-by neighbours via
   :mod:`knowledge.relations`' existing graph walk (blast radius). Structural
   data, not memory, so it is shown for context but never itself drives the
   verdict.

Design mirrors ``knowledge/doctor.py``: every function here is pure (plain
arguments in, a result dataclass out, never prints, never writes). All
rendering — prose and ``--format json`` — belongs to ``knowledge/cli.py``.

**Never writes.** Recording a decision is the user's deliberate act (decision
id=185 rejected auto-writing memory); gate only reads.

**Advisory, not a veto.** A "conflict" verdict is information — a hit exists
— not a refusal to proceed. Callers (including the opt-in PreToolUse hook)
must never treat it as a block.

**Supersede handling is the subtlest part.** A dead (superseded) decision
row must never, by itself, produce a "conflict" verdict — it is stale
guidance, not live. It is still SHOWN, clearly marked, with a pointer to its
replacement. If the replacement itself is a genuine hit, the replacement is
what counts toward the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import config
from . import decisions as decisions_mod
from . import history as history_mod
from . import relations as relations_mod
from .db import Connection
from .decisions import Decision
from .history import HistoryEntry


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionHit:
    """One decision/fact row bearing on the change.

    ``distance`` is ``None`` for the exact ``files_touched`` signal (no
    embedding involved there — see :func:`_exact_file_matches`).
    ``superseded_by`` is the id of the newest row that supersedes this one,
    or ``None`` when the row is live. A hit with ``superseded_by`` set must
    never, by itself, flip :attr:`GateReport.verdict` to ``"conflict"``.
    """

    decision: Decision
    distance: float | None
    superseded_by: int | None = None


@dataclass(frozen=True)
class HistoryHit:
    """One history entry (prior incident) near the topic."""

    entry: HistoryEntry
    distance: float


@dataclass(frozen=True)
class CoupledFile:
    """One file coupled (direct import / imported-by) to a ``--files`` target."""

    rel_path: str
    direction: str  # "forward" (this target imports rel_path) | "reverse" (rel_path imports this target)
    kind: str  # edge kind, e.g. "python_import"
    via: str  # which requested --files entry this coupling came from


@dataclass(frozen=True)
class GateReport:
    """Everything gathered for one ``knowledge gate`` invocation."""

    topic: str | None
    files: tuple[str, ...]
    decision_hits: tuple[DecisionHit, ...]
    history_hits: tuple[HistoryHit, ...]
    files_touched_hits: tuple[DecisionHit, ...]
    coupled_files: tuple[CoupledFile, ...]

    @property
    def verdict(self) -> str:
        """``"conflict"`` iff at least one LIVE hit exists in a memory
        signal (semantic decisions, semantic history, or exact
        files_touched match). Coupling is structural context, not memory,
        and never drives this. A dead row alone never counts — only a
        superseded_by=None hit does.
        """
        live_decision = any(h.superseded_by is None for h in self.decision_hits)
        live_files_touched = any(h.superseded_by is None for h in self.files_touched_hits)
        if live_decision or live_files_touched or self.history_hits:
            return "conflict"
        return "clear"

    @property
    def exit_code(self) -> int:
        # 5 is reserved specifically for this verb (docs/exit-codes.md).
        return 5 if self.verdict == "conflict" else 0

    @property
    def superseded_shown(self) -> int:
        """Count of distinct dead decision ids shown (semantic + exact-match
        groups combined) — informational, not part of the verdict."""
        dead_ids = {
            h.decision.id
            for h in (*self.decision_hits, *self.files_touched_hits)
            if h.superseded_by is not None
        }
        return len(dead_ids)


# ---------------------------------------------------------------------------
# Signal builders — each pure, each independently testable
# ---------------------------------------------------------------------------


def _search_decisions(conn: Connection, project_id: int, topic: str) -> list[DecisionHit]:
    """Signal 1: decisions/facts semantically near ``topic``.

    Gated on ``config.GATE_MAX_DISTANCE`` — the same threshold ``ask`` uses
    for its decisions preface, since both answer "is this actually
    relevant?" with the same calibration.
    """
    hits = decisions_mod.search(conn, topic, project_id=project_id, top_k=config.GATE_TOP_K)
    hits = [(d, dist) for d, dist in hits if dist <= config.GATE_MAX_DISTANCE]
    dead_map = decisions_mod.superseded_by(conn, project_id=project_id)
    return [DecisionHit(d, dist, dead_map.get(d.id)) for d, dist in hits]


def _search_history(conn: Connection, project_id: int, topic: str) -> list[HistoryHit]:
    """Signal 2: prior work-log entries ("we tried this and it broke X")
    semantically near ``topic``. History has no supersede mechanism."""
    hits = history_mod.search(conn, topic, project_id=project_id, top_k=config.GATE_TOP_K)
    hits = [(e, dist) for e, dist in hits if dist <= config.GATE_MAX_DISTANCE]
    return [HistoryHit(e, dist) for e, dist in hits]


def _exact_file_matches(
    conn: Connection, project_id: int, files: Sequence[str]
) -> list[DecisionHit]:
    """Signal 3: decisions whose ``files_touched`` intersects ``files``.

    Independent of embeddings entirely — no ``get_embedder()`` call on this
    path, so a ``--files``-only invocation never needs the model loaded.
    Fetches all of the project's decisions and intersects in Python (mirrors
    ``consolidate.py``'s ``recent(..., limit=10_000)`` pattern) rather than a
    SQL LIKE over the JSON column, which is fragile and backend-inconsistent.
    """
    if not files:
        return []
    wanted = set(files)
    dead_map = decisions_mod.superseded_by(conn, project_id=project_id)
    all_decisions = decisions_mod.recent(conn, project_id=project_id, limit=10_000)
    out: list[DecisionHit] = []
    for d in all_decisions:
        if wanted & set(d.files_touched):
            out.append(DecisionHit(d, None, dead_map.get(d.id)))
    return out


def _coupled_files(conn: Connection, project_id: int, files: Sequence[str]) -> list[CoupledFile]:
    """Signal 4: direct (depth=1) import graph neighbours of ``files``.

    Reuses :func:`knowledge.relations.get_forward`/``get_reverse`` rather
    than re-walking ``file_edges`` — one graph implementation, one source of
    truth. Unresolved/external forward edges (``target_rel is None`` —
    stdlib, node_modules, an unsatisfied template) are skipped: this signal
    is about coupling to OTHER FILES IN THIS PROJECT, not every import.
    """
    out: list[CoupledFile] = []
    seen: set[tuple[str, str]] = set()
    for f in files:
        file_id = relations_mod.find_file_id(conn, project_id, f)
        if file_id is None:
            continue
        for edge in relations_mod.get_forward(conn, file_id, depth=1):
            if edge.target_rel and (edge.target_rel, "forward") not in seen:
                seen.add((edge.target_rel, "forward"))
                out.append(CoupledFile(edge.target_rel, "forward", edge.kind, via=f))
        for edge in relations_mod.get_reverse(conn, file_id, depth=1):
            if edge.source_rel and (edge.source_rel, "reverse") not in seen:
                seen.add((edge.source_rel, "reverse"))
                out.append(CoupledFile(edge.source_rel, "reverse", edge.kind, via=f))
    return out


def build(
    conn: Connection,
    project_id: int,
    topic: str | None,
    files: Sequence[str],
) -> GateReport:
    """Assemble a full :class:`GateReport`. Pure read — never writes.

    Semantic signals (1, 2) only run when ``topic`` is given — they need
    something to embed. Files-scoped signals (3, 4) only run when ``files``
    is given. Callers must ensure at least one of the two is non-empty
    (``knowledge/cli.py`` enforces this as a usage error before calling in).
    """
    files = tuple(files)
    decision_hits: list[DecisionHit] = []
    history_hits: list[HistoryHit] = []
    if topic:
        decision_hits = _search_decisions(conn, project_id, topic)
        history_hits = _search_history(conn, project_id, topic)

    files_touched_hits = _exact_file_matches(conn, project_id, files)
    coupled = _coupled_files(conn, project_id, files) if files else []

    return GateReport(
        topic=topic,
        files=files,
        decision_hits=tuple(decision_hits),
        history_hits=tuple(history_hits),
        files_touched_hits=tuple(files_touched_hits),
        coupled_files=tuple(coupled),
    )
