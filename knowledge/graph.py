"""HTML graph renderer for the file-dependency graph.

Pulls nodes and edges from the SQLite store (``files`` + ``file_edges``)
for one project and writes a self-contained HTML file that loads
``vis-network`` from a CDN. The output is one static file you can open
in any browser; hover on a node for the full project-relative path +
language, drag nodes around, scroll to zoom.

Scope and defaults are set to what's most useful for an LLM-consumer
opening the file to understand the repo:

* **One project per run.** Defaults to the current project (from the
  cwd's git root). ``--project`` overrides.
* **Resolved project-to-project edges only, by default.** External
  (stdlib / third-party) and parametric (waiting for variables) and
  unresolved (syntactically irrecoverable) are skipped unless the
  caller opts in — they turn the graph into a hairball without adding
  useful structural signal. Opt-in via the ``include_*`` flags.
* **Color by top-level directory.** `knowledge/`, `ansible/`, …. The
  first path segment is a decent proxy for "which part of the repo is
  this" across every repo we've seen. Legend is rendered above the
  canvas.

No Python runtime deps beyond the stdlib for the renderer. vis-network
loads from ``unpkg.com`` on first open of the HTML — fine for dev
machines. A future ``--embed`` flag could inline the JS for offline
use, but it's ~300KB and not worth the default cost.
"""

from __future__ import annotations

import colorsys
import html
import json
from typing import NamedTuple

from . import db
from .db import Connection


# Version of vis-network pinned in the CDN URL. Bumping this changes the
# rendered HTML deterministically so cached-browser copies don't break.
_VIS_NETWORK_CDN = (
    "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
)
# M3: Subresource Integrity hash for the pinned vis-network bundle. The browser
# refuses to execute the script if the bytes fetched from the CDN don't match,
# closing the CDN-compromise / MITM supply-chain vector. Regenerate when the
# pinned version above changes:
#   curl -sSL <url> | openssl dgst -sha384 -binary | openssl base64 -A
_VIS_NETWORK_SRI = (
    "sha384-yxKDWWf0wwdUj/gPeuL11czrnKFQROnLgY8ll7En9NYoXibgg3C6NK/UDHNtUgWJ"
)


class GraphNode(NamedTuple):
    """One file rendered as a node in the graph."""
    id: int
    rel_path: str
    lang: str
    group: str       # color bucket — first path segment
    label: str       # display label (basename)
    title: str       # hover tooltip (HTML-safe)


class GraphEdge(NamedTuple):
    """One ``file_edges`` row rendered as an edge."""
    source: int      # file_id
    target: int      # file_id (resolved edges only — never None here)
    kind: str
    title: str       # hover tooltip (kind + raw, HTML-safe)


def build_graph_html(
    conn: Connection,
    project_id: int,
    project_name: str,
    *,
    include_external: bool = False,
    include_parametric: bool = False,
    include_unresolved: bool = False,
    include_orphans: bool = True,
) -> str:
    """Return a self-contained HTML document visualizing the project's
    dependency graph. Caller writes it to disk.

    ``include_external`` — include edges whose ``target_file_id`` is NULL
    and whose raw doesn't carry template markers (stdlib / third-party /
    remote). These have no file node to point at; when enabled, one
    synthetic "external" node per unique raw is added, colored neutrally
    so it stays visually distinct from project files.

    ``include_parametric`` — same synthetic-node treatment for edges
    whose raw carries `{{ var }}` / `${var.x}` that's not yet satisfied.

    ``include_unresolved`` — edges with stored ``kind='unresolved'``
    (non-literal dynamic imports). Rare; included as opt-in.

    ``include_orphans`` — when True (default), every indexed project
    file becomes a node even if it has no edges in or out. The graph
    becomes a full repo map: isolated dots show "this file exists" and
    clusters show structural relations. When False, only files that
    participate in an edge are rendered (plus CI/CD files, which stay
    visible under both modes).
    """
    nodes, edges = _collect(
        conn,
        project_id,
        include_external=include_external,
        include_parametric=include_parametric,
        include_unresolved=include_unresolved,
        include_orphans=include_orphans,
    )
    groups = _group_color_map(nodes)
    return _render_html(project_name, nodes, edges, groups)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _collect(
    conn: Connection,
    project_id: int,
    *,
    include_external: bool,
    include_parametric: bool,
    include_unresolved: bool,
    include_orphans: bool,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Pull edges + the file rows they connect into typed tuples.

    With ``include_orphans=True`` (default), every indexed project file
    becomes a node — the graph reads as a full repo map. With False,
    only files that appear as source/target of an included edge are
    kept (plus CI/CD files via ``_ensure_cicd_nodes``), which is the
    old compact "structure only" view.
    """
    # One query to pull every edge we might include; we filter in
    # Python because the predicates mix NULL-checks, kind exclusions,
    # and raw-substring checks that aren't worth a 5-branch SQL CASE.
    rows = db.fetch_all(
        conn,
        """
        SELECT e.source_file_id, sf.rel_path, sf.lang,
               e.target_file_id, tf.rel_path, tf.lang,
               e.kind, e.raw
        FROM file_edges e
        JOIN files sf      ON sf.id = e.source_file_id
        LEFT JOIN files tf ON tf.id = e.target_file_id
        WHERE e.project_id = ?
        """,
        (project_id,),
    )

    nodes_by_id: dict[int, GraphNode] = {}
    edges: list[GraphEdge] = []
    # Synthetic nodes for external / parametric / unresolved if enabled.
    # Keyed by a stable string so the same raw collapses to one node
    # instead of a fan-out (e.g. every `argparse` import converges on
    # one "external: argparse" dot).
    synthetic_by_key: dict[str, int] = {}
    next_synth_id = -1  # negative ids keep them distinct from file rows

    for (src_id, src_rel, src_lang,
         tgt_id, tgt_rel, tgt_lang,
         kind, raw) in rows:

        has_template = raw and ("{{" in raw or "${var." in raw)

        # Decide what kind of edge this is and whether to include it.
        if tgt_id is not None:
            # Resolved project-to-project — always included.
            _remember_file(nodes_by_id, src_id, src_rel, src_lang)
            _remember_file(nodes_by_id, tgt_id, tgt_rel, tgt_lang)
            edges.append(GraphEdge(
                source=src_id, target=tgt_id, kind=kind,
                title=_edge_title(kind, raw),
            ))
        elif kind == "unresolved":
            if not include_unresolved:
                continue
            _remember_file(nodes_by_id, src_id, src_rel, src_lang)
            synth_id, next_synth_id = _synthetic_node(
                synthetic_by_key, f"unresolved:{raw}",
                label=_short(raw), group="_unresolved",
                title=_synth_title("unresolved", raw),
                next_id=next_synth_id, nodes_by_id=nodes_by_id,
            )
            edges.append(GraphEdge(
                source=src_id, target=synth_id, kind=kind,
                title=_edge_title(kind, raw),
            ))
        elif has_template:
            if not include_parametric:
                continue
            _remember_file(nodes_by_id, src_id, src_rel, src_lang)
            synth_id, next_synth_id = _synthetic_node(
                synthetic_by_key, f"parametric:{raw}",
                label=_short(raw), group="_parametric",
                title=_synth_title("parametric", raw),
                next_id=next_synth_id, nodes_by_id=nodes_by_id,
            )
            edges.append(GraphEdge(
                source=src_id, target=synth_id, kind=kind,
                title=_edge_title(kind, raw),
            ))
        else:
            # External (resolver tried + missed — stdlib / third-party /
            # marketplace GHA action). CI/CD pipeline files are always
            # shown: a workflow whose only ``uses:`` are marketplace
            # actions is still a meaningful repo artifact that the user
            # expects on the graph. For other languages the default
            # still drops externals to keep the graph readable.
            if not include_external and not _is_cicd_file(src_rel):
                continue
            _remember_file(nodes_by_id, src_id, src_rel, src_lang)
            synth_id, next_synth_id = _synthetic_node(
                synthetic_by_key, f"external:{raw}",
                label=_short(raw), group="_external",
                title=_synth_title("external", raw),
                next_id=next_synth_id, nodes_by_id=nodes_by_id,
            )
            edges.append(GraphEdge(
                source=src_id, target=synth_id, kind=kind,
                title=_edge_title(kind, raw),
            ))

    # Add isolated-file nodes.
    # - include_orphans=True: every indexed project file becomes a
    #   dot. Connected files cluster; isolated files sit as dots in
    #   their group's color. This is the "repo map" view.
    # - include_orphans=False: only CI/CD files are pulled in (via
    #   _ensure_cicd_nodes), preserving the "always show CI/CD"
    #   guarantee under the compact structure-only view.
    if include_orphans:
        _ensure_all_project_nodes(conn, project_id, nodes_by_id)
    else:
        _ensure_cicd_nodes(conn, project_id, nodes_by_id)

    return list(nodes_by_id.values()), edges


def _remember_file(
    nodes: dict[int, GraphNode], file_id: int, rel_path: str, lang: str
) -> None:
    if file_id in nodes:
        return
    group = _top_level_dir(rel_path) or "(root)"
    basename = rel_path.rsplit("/", 1)[-1]
    nodes[file_id] = GraphNode(
        id=file_id,
        rel_path=rel_path,
        lang=lang,
        group=group,
        label=basename,
        title=_node_title(rel_path, lang),
    )


def _synthetic_node(
    synthetic_by_key: dict[str, int],
    key: str,
    *,
    label: str,
    group: str,
    title: str,
    next_id: int,
    nodes_by_id: dict[int, GraphNode],
) -> tuple[int, int]:
    """Reuse-or-create a synthetic node for a NULL-target edge.

    Returns ``(node_id, next_id)`` — the caller threads ``next_id`` as
    a running counter so the generated ids stay stable within one run.
    """
    if key in synthetic_by_key:
        return synthetic_by_key[key], next_id
    synth_id = next_id
    next_id -= 1
    synthetic_by_key[key] = synth_id
    nodes_by_id[synth_id] = GraphNode(
        id=synth_id,
        rel_path=f"<{group[1:]}>",
        lang=group,
        group=group,
        label=label,
        title=title,
    )
    return synth_id, next_id


def _top_level_dir(rel_path: str) -> str:
    """First path segment of ``rel_path``, or ``''`` for root-level files."""
    if "/" not in rel_path:
        return ""
    return rel_path.split("/", 1)[0]


# CI/CD pipeline files are always shown on the graph — even when every
# ``uses:`` is a marketplace action (external, target_file_id=NULL).
# These files are structurally meaningful to the user ("what automation
# does this repo run?") and dropping them as orphans is surprising.
# Covers the resolvers we ship today; add new path shapes here as we
# add CI/CD resolvers (GitLab CI, CircleCI, Jenkins, …).
def _is_cicd_file(rel_path: str | None) -> bool:
    if not rel_path:
        return False
    parts = rel_path.split("/")
    # GitHub Actions workflow: .github/workflows/*.yml|*.yaml
    if (
        len(parts) >= 3
        and parts[0] == ".github"
        and parts[1] == "workflows"
        and parts[-1].endswith((".yml", ".yaml"))
    ):
        return True
    # GitHub Actions composite action manifest: .github/actions/<name>/action.yml
    if (
        len(parts) >= 4
        and parts[0] == ".github"
        and parts[1] == "actions"
        and parts[-1] in ("action.yml", "action.yaml")
    ):
        return True
    return False


def _ensure_all_project_nodes(
    conn: Connection, project_id: int, nodes_by_id: dict[int, "GraphNode"]
) -> None:
    """Promote every indexed project file to a node.

    Idempotent: files already added by the edge loop are skipped via
    ``_remember_file``'s dedup. This is the "repo map" pass — isolated
    dots mean the file is indexed but has no resolved relations.
    """
    rows = db.fetch_all(
        conn,
        "SELECT id, rel_path, lang FROM files WHERE project_id = ?",
        (project_id,),
    )
    for file_id, rel_path, lang in rows:
        _remember_file(nodes_by_id, file_id, rel_path, lang)


def _ensure_cicd_nodes(
    conn: Connection, project_id: int, nodes_by_id: dict[int, "GraphNode"]
) -> None:
    """Pull CI/CD files into the node set even when they have no edges at all.

    A workflow with no ``uses:`` statements is legal (scripted-only job).
    Without this pass, it would vanish from the graph with no signal
    that it was indexed.
    """
    rows = db.fetch_all(
        conn,
        """
        SELECT id, rel_path, lang FROM files
        WHERE project_id = ?
          AND rel_path LIKE '.github/workflows/%'
        UNION ALL
        SELECT id, rel_path, lang FROM files
        WHERE project_id = ?
          AND rel_path LIKE '.github/actions/%/action.yml'
        UNION ALL
        SELECT id, rel_path, lang FROM files
        WHERE project_id = ?
          AND rel_path LIKE '.github/actions/%/action.yaml'
        """,
        (project_id, project_id, project_id),
    )
    for file_id, rel_path, lang in rows:
        if not _is_cicd_file(rel_path):
            # LIKE is coarse (matches ``.github/workflows/README.md``);
            # double-check with the predicate.
            continue
        _remember_file(nodes_by_id, file_id, rel_path, lang)


# ---------------------------------------------------------------------------
# Hover tooltip builders
# ---------------------------------------------------------------------------


def _node_title(rel_path: str, lang: str) -> str:
    """HTML-safe tooltip for a file node: full path + language badge."""
    return (
        f"<b>{html.escape(rel_path)}</b><br/>"
        f"<span style='color:#888'>{html.escape(lang)}</span>"
    )


def _synth_title(bucket: str, raw: str) -> str:
    return (
        f"<b>{html.escape(bucket)}</b><br/>"
        f"<code>{html.escape(raw)}</code>"
    )


def _edge_title(kind: str, raw: str) -> str:
    """HTML-safe edge tooltip: kind + raw specifier."""
    return (
        f"<b>{html.escape(kind)}</b><br/>"
        f"<code>{html.escape(raw or '')}</code>"
    )


def _short(raw: str, limit: int = 40) -> str:
    """Truncate a raw path for on-graph label display. Full text lives
    in the hover tooltip."""
    if raw is None:
        return ""
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------


def _group_color_map(nodes: list[GraphNode]) -> dict[str, str]:
    """Visually distinct color per group.

    Special buckets (``_external`` / ``_parametric`` / ``_unresolved``)
    stay at curated neutrals so they don't compete with real groups.

    For real groups we sort names alphabetically and walk the hue wheel
    by the golden-ratio conjugate — each new hue lands as far as
    possible from every previous one (classic low-discrepancy trick),
    so even with 3-4 groups no two collide. Hash-by-name had no spacing
    guarantee and produced near-duplicate blues in practice (e.g.
    ``argocd`` 240° vs ``terraform`` 239°).
    """
    special = {
        "_external":    "#cccccc",
        "_parametric":  "#e5c07b",
        "_unresolved":  "#e06c75",
    }
    # Collect real groups first; deterministic order = alphabetical.
    real_groups = sorted({
        n.group for n in nodes
        if n.group not in special and not n.group.startswith("_")
    })

    out: dict[str, str] = {}
    # Golden-ratio conjugate: every new i lands in the largest remaining
    # gap on the hue wheel. Starting hue 0.08 (≈29°, warm coral) picks
    # a pleasant first color instead of pure red.
    golden = 0.6180339887498949
    start = 0.08
    for i, g in enumerate(real_groups):
        h = (start + i * golden) % 1.0
        r, g_, b = colorsys.hls_to_rgb(h, 0.55, 0.65)
        out[g] = f"#{int(r*255):02x}{int(g_*255):02x}{int(b*255):02x}"

    # Specials fill in last so real groups dominate the legend ordering
    # logic upstream (which sorts the full dict for display).
    for n in nodes:
        g = n.group
        if g in out:
            continue
        if g in special:
            out[g] = special[g]
    return out


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------


def _render_html(
    project_name: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    groups: dict[str, str],
) -> str:
    """Assemble the self-contained HTML. Data is emitted as a JSON
    literal inside a ``<script>`` — vis-network's DataSet parses it
    directly, so the runtime cost is just the DOM setup.
    """
    node_dicts = [
        {
            "id": n.id,
            "label": n.label,
            "title": n.title,
            "group": n.group,
            "color": groups.get(n.group, "#7fb3d5"),
            # Larger font on real files; synthetic nodes stay quiet.
            "font": {"size": 12 if n.id >= 0 else 10},
            "shape": "dot" if n.id >= 0 else "diamond",
            "size": 10 if n.id >= 0 else 6,
        }
        for n in nodes
    ]
    edge_dicts = [
        {
            "from": e.source,
            "to": e.target,
            "title": e.title,
            "arrows": "to",
            "smooth": {"type": "dynamic"},
            "color": {"color": "#aaa", "opacity": 0.6},
            "width": 1,
        }
        for e in edges
    ]
    legend_items = sorted(groups.items(), key=lambda kv: kv[0])
    # ``data-group`` is what the click handler reads to toggle visibility.
    # ``title`` adds a native browser tooltip so users discover that the
    # legend entry is interactive without needing separate docs.
    legend_html = "".join(
        f'<span class="legend-item" data-group="{html.escape(group)}" '
        f'title="click to hide/show {html.escape(group)}">'
        f'<span class="legend-dot" style="background:{html.escape(color)}"></span>'
        f'{html.escape(group)}'
        f'</span>'
        for group, color in legend_items
    )

    payload = {
        "nodes": node_dicts,
        "edges": edge_dicts,
    }
    # H3 fix: json.dumps does NOT escape '<', '>', or '/' by default.
    # A repo file literally named '</script>' would break out of the
    # enclosing <script> element and execute arbitrary JS.  Unicode-escape
    # all three so the JSON is still valid but can never terminate a script
    # block.  Applies to every field in the payload (label, group, …).
    payload_json = (
        json.dumps(payload, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("/", "\\u002f")
    )

    # One string, no extra Python templating engine — ``.format`` is
    # rejected by the JS braces we'd have to double-escape. Use
    # %-substitution for the handful of interpolated values instead.
    return _TEMPLATE % {
        "project": html.escape(project_name),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "legend": legend_html,
        "vis_url": _VIS_NETWORK_CDN,
        "vis_sri": _VIS_NETWORK_SRI,
        "payload": payload_json,
    }


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>%(project)s — dependency graph</title>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      height: 100%%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1e1e1e;
      color: #e0e0e0;
    }
    #header {
      padding: 10px 16px;
      background: #262626;
      border-bottom: 1px solid #333;
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }
    #title {
      font-weight: 600;
      font-size: 14px;
      white-space: nowrap;
    }
    #stats {
      color: #888;
      font-size: 12px;
      white-space: nowrap;
    }
    #legend {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 11px;
      color: #bbb;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      user-select: none;
      padding: 2px 4px;
      border-radius: 3px;
      transition: opacity 0.15s, background 0.15s;
    }
    .legend-item:hover {
      background: #333;
    }
    /* Disabled = group currently hidden. Dim the whole pill and
       strike through the label so the state is unmistakable. */
    .legend-item.disabled {
      opacity: 0.4;
      text-decoration: line-through;
    }
    .legend-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%%;
      display: inline-block;
    }
    #network {
      width: 100%%;
      height: calc(100vh - 56px);
    }
    .vis-tooltip {
      background: #2a2a2a !important;
      color: #e0e0e0 !important;
      border: 1px solid #444 !important;
      font-family: inherit !important;
      padding: 6px 10px !important;
      border-radius: 4px !important;
    }
  </style>
</head>
<body>
  <div id="header">
    <span id="title">%(project)s</span>
    <span id="stats">%(node_count)d nodes &middot; %(edge_count)d edges</span>
    <div id="legend">%(legend)s</div>
  </div>
  <div id="network"></div>
  <script src="%(vis_url)s" integrity="%(vis_sri)s" crossorigin="anonymous"></script>
  <script>
    const data = %(payload)s;
    // vis-network 5+ treats ``title`` as plain text by default (XSS
    // safety) — passing a raw HTML string would show the tags
    // verbatim. Convert each title to a DOM element BEFORE putting
    // the rows into a DataSet; vis-network detects Elements and
    // renders them as-is. The source HTML is Python-escaped upstream
    // (see ``html.escape`` in graph.py) so this isn't an XSS vector
    // even though we're using ``innerHTML``.
    function toHtmlTooltip(s) {
      if (typeof s !== "string") return s;
      const el = document.createElement("div");
      el.innerHTML = s;
      return el;
    }
    for (const n of data.nodes) n.title = toHtmlTooltip(n.title);
    for (const e of data.edges) e.title = toHtmlTooltip(e.title);

    const nodes = new vis.DataSet(data.nodes);
    const edges = new vis.DataSet(data.edges);
    const container = document.getElementById("network");
    const options = {
      interaction: {
        hover: true,
        tooltipDelay: 120,
        navigationButtons: false,
      },
      physics: {
        solver: "barnesHut",
        barnesHut: {
          gravitationalConstant: -8000,
          springLength: 95,
          springConstant: 0.04,
          damping: 0.3,
          avoidOverlap: 0.1,
        },
        stabilization: { iterations: 200 },
      },
      nodes: {
        borderWidth: 0,
        font: { color: "#e0e0e0" },
      },
      edges: {
        hoverWidth: 1.5,
        selectionWidth: 2,
      },
    };
    const network = new vis.Network(container, { nodes, edges }, options);
    // Freeze physics after stabilization so the graph doesn't wander
    // while the user inspects it — they can still drag nodes.
    network.once("stabilizationIterationsDone", () => {
      network.setOptions({ physics: false });
    });

    // Click a legend entry to toggle that group's visibility.
    // vis-network's ``hidden`` flag on a node auto-hides its edges,
    // so we don't need to touch the edges DataSet at all. Index
    // node ids by group once so toggling is O(|group|), not O(|N|).
    const idsByGroup = {};
    for (const n of data.nodes) {
      (idsByGroup[n.group] ||= []).push(n.id);
    }
    const hiddenGroups = new Set();
    document.querySelectorAll(".legend-item").forEach(el => {
      el.addEventListener("click", () => {
        const group = el.dataset.group;
        const ids = idsByGroup[group] || [];
        const nowHidden = !hiddenGroups.has(group);
        if (nowHidden) hiddenGroups.add(group); else hiddenGroups.delete(group);
        el.classList.toggle("disabled", nowHidden);
        // Batched update — one DataSet call for the whole group.
        nodes.update(ids.map(id => ({ id, hidden: nowHidden })));
      });
    });
  </script>
</body>
</html>
"""
