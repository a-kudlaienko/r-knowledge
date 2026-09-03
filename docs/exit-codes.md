# Exit codes

Reference for every `knowledge` verb's process exit status. A coding agent
invoking this CLI can branch on these without parsing stderr prose.

| Code | Meaning | Where it comes from |
|------|---------|----------------------|
| `0`  | Success | Normal completion. |
| `1`  | General failure, or `status` reporting the index as stale | Default `KnowledgeError.exit_code`; also `cmd_status`'s "stale" result. |
| `2`  | Usage error — bad arguments, a `resolve` that can't find its target, or a requested thing that doesn't exist | Individual `cmd_*` handlers' own argument/lookup validation. |
| `3`  | `ProjectBusyError` — a concurrent `build`/`update` already holds the per-project advisory lock | `knowledge/db.py`'s advisory-lock check; fails fast rather than blocking, so retry the command. |
| `4`  | Shared index unreachable — PostgreSQL is down or misconfigured and the command needed a read | `main()`'s `except db.offline_errors()` clause in `knowledge/cli.py`. Writes don't hit this path: they buffer to the local outbox and sync on the next reachable run. |
| `5`  | `knowledge gate` found a live conflict — prior knowledge (a decision/fact, a history incident, or an exact `files_touched` match) bears on the requested topic/files | `knowledge/gate.py`'s `GateReport.exit_code`. **Advisory, not a veto** — it means "read the output before you proceed", never "refuse to proceed". A dead (superseded) row alone never triggers this; only a live hit does. **`gate --hook` (PreToolUse hook mode) always exits 0 by design**, conflict or not — it reports a live conflict via `hookSpecificOutput.additionalContext` in its JSON stdout instead, precisely so a hook invocation can never block the tool call. |
| `70` | Internal error — an exception no `cmd_*` handler anticipated | `main()`'s last-resort `except Exception` clause. Set `KNOWLEDGE_TRACEBACK=1` to get the real Python traceback instead of the structured message. |

## The error envelope

Verbs that raise `knowledge.jsonout.KnowledgeError` (see that module's
docstring for the full contract) produce a structured, non-zero-exit failure
instead of a raw traceback:

* Verb invoked **without** `--json` or `--format json` (or the verb supports
  neither convention): prose on stderr — `error: <message>`, plus
  `  try: <remedy>` when a remedy command is available.
* Verb invoked **with** `--json` (older verbs) or `--format json` (newer
  verbs — see `knowledge.jsonout.wants_json` for which verb uses which):
  a single JSON line on **stdout** —
  `{"ok": false, "code": "<slug>", "message": "<sentence>", "remedy": "<cmd>", "exit": <n>}`
  (`"remedy"` is omitted when there is none).

The unexpected-exception fallback (exit `70`) follows the same split, with
`"code": "internal_error"` and the remedy pointing at `KNOWLEDGE_TRACEBACK=1`.
