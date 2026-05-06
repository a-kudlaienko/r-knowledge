"""GitHub Actions resolver — workflow + composite-action dependencies.

Covers the two files GHA calls ``uses:`` from:

* Workflow files (``.github/workflows/*.yml``) — each step's ``uses:`` and
  job-level ``uses:`` (for reusable-workflow calls). A single job can
  have 5-20 steps and 2-3 job-level uses.
* Composite action manifests (``.github/actions/<name>/action.yml``) —
  nested steps inside ``runs.steps[].uses``.

Edge kinds:

* ``gha_uses_action``   — ``uses: ./.github/actions/<name>`` → resolves
                          to ``<name>/action.yml``.
* ``gha_uses_workflow`` — ``uses: ./.github/workflows/<file>.yml`` →
                          reusable workflow; resolves to that file.
* ``gha_uses_external`` — ``uses: owner/repo@ref`` /
                          ``uses: owner/repo/.github/actions/x@ref`` →
                          never a project file; raw preserved, target
                          NULL (renders as external in the CLI).

We don't try to resolve ``run: ./scripts/foo.sh`` — workflows inline
scripts too freely for that to be a stable signal, and the user didn't
ask for it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .base import BaseResolver, Edge


class GitHubActionsResolver(BaseResolver):
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        try:
            doc = yaml.safe_load(source_bytes)
        except yaml.YAMLError:
            return []
        if not isinstance(doc, dict):
            return []

        edges: list[Edge] = []

        # Composite action manifest: runs.steps[].uses.
        runs = doc.get("runs")
        if isinstance(runs, dict):
            for step in _list(runs.get("steps")):
                _collect_step_uses(step, edges)

        # Workflow file: jobs.<name>.steps[].uses, plus job-level
        # ``uses:`` for reusable-workflow calls.
        jobs = doc.get("jobs")
        if isinstance(jobs, dict):
            for _job_name, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                job_uses = job.get("uses")
                if isinstance(job_uses, str):
                    edges.append(_classify_uses(job_uses))
                for step in _list(job.get("steps")):
                    _collect_step_uses(step, edges)

        return edges


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify_uses(raw: str) -> Edge:
    """Build an Edge from a ``uses:`` string. Resolution of local forms
    to a specific file happens in ``relations._resolve_gha`` — here we
    just tag the shape.
    """
    # Normalize leading "./" and spaces but preserve the original in raw.
    stripped = raw.strip()
    if stripped.startswith("./.github/workflows/"):
        return Edge(kind="gha_uses_workflow", raw=stripped, symbol=None, line=0)
    if stripped.startswith("./.github/actions/"):
        return Edge(kind="gha_uses_action", raw=stripped, symbol=None, line=0)
    # Third-party / upstream / monorepo-external. Registry format is
    # owner/repo[/path]@ref. Always external — we record it so the LLM
    # can see what the workflow depends on even if it's not in the tree.
    return Edge(kind="gha_uses_external", raw=stripped, symbol=None, line=0)


def _collect_step_uses(step, out: list[Edge]) -> None:
    if not isinstance(step, dict):
        return
    uses = step.get("uses")
    if isinstance(uses, str) and uses.strip():
        out.append(_classify_uses(uses))


def _list(value) -> list:
    return value if isinstance(value, list) else []
