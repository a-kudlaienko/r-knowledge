# Visualizing the dependency graph (HTML)

Reference for `knowledge graph`. The skill carries only the short version; this is the
full behaviour. For the per-file query verb, use `knowledge relations <file>` (documented
in `skill-template/SKILL.md`). See also [`docs/variables.md`](variables.md) for resolving
`parametric` edges before rendering.

When you want to *see* the dependency shape (e.g. "what's the overall structure here",
"show me the graph", "are there cycles"), render it to a static HTML:

```bash
knowledge graph                                    # writes ./relations_graph.html
knowledge graph --output /tmp/graph.html --open    # write to specific path + launch browser
knowledge graph --include-external                 # include stdlib / third-party as gray nodes
knowledge graph --include-parametric               # include vars-waiting as yellow nodes
```

One project per run (`--project` overrides the cwd default). The rendered file is a single
self-contained HTML with vis-network loaded from CDN — open in any browser, hover a node
for the full project-relative path + language, drag nodes, scroll to zoom. Nodes are
colored by top-level directory. The default scope is resolved project-to-project edges
only (cleanest for large repos); opt in to `external` / `parametric` / `unresolved` via
the flags above.

## When NOT to use it

This is a **display** command, not a query command — it writes a file to disk and prints
its path. Don't use it when the user asks a narrow "where does X point" question;
`knowledge relations <file>` is faster and more focused for that.
