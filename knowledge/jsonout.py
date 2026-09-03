"""Machine-actionable error contract for the CLI's top-level exception handler.

Phase 1a foundation (see `knowledge decide` for this item): today, any
exception that escapes a ``cmd_*`` handler without a bespoke ``except`` clause
reaches the invoking coding agent as a raw Python traceback on stderr — the
worst possible payload for an LLM caller, which has no way to distinguish
"you passed a bad flag" from "the index is corrupt" from "unrelated bug".

This module defines the shape of a *good* error and nothing else:

* :class:`KnowledgeError` — the exception ``cmd_*`` handlers should raise
  instead of ``print(..., file=sys.stderr); return N``. It carries a stable
  machine ``code``, a human ``message``, an optional ``remedy`` (an exact
  command the agent can re-run), and the ``exit_code`` the process should
  return.
* :func:`error_payload` — builds the JSON-serializable dict for that error.
* :func:`emit` — the single place that writes a JSON payload to stdout.

**The contract**: a JSON error payload is only ever written to **stdout**,
and only when the invoked verb asked for machine-readable output — via
either of the two conventions this CLI has accreted (see
:func:`wants_json`): a bare ``--json`` flag, or ``--format json``. Every
other case — neither flag given, or a verb that supports neither — gets
prose on **stderr**, exactly as today. Nothing in this module changes that
split; it only gives `knowledge/cli.py`'s top-level handler a structured
payload to reach for when the caller *did* ask for machine-readable output.

Stdlib only — deliberately dependency-free (no pydantic/jsonschema): the
payload is a plain ``dict`` with a fixed, small set of keys, which is all the
"schema" this needs.
"""

from __future__ import annotations

import json


def wants_json(args) -> bool:
    """True when the parsed args asked for machine-readable output.

    Two conventions have accreted for this across the CLI's lifetime:
    the original ``--json`` (``action="store_true"``) flag on the older
    verbs, and ``--format {text,json}`` on the newer/richer verbs — chosen
    there so a future ``--format yaml`` (or similar) doesn't require a new
    boolean flag per format. Most verbs have only one of the two attributes
    (or neither), hence ``getattr`` with defaults rather than ``args.json``
    / ``args.format`` directly.

    This is the single source of truth for "does this invocation want
    JSON?" — callers (in particular `knowledge/cli.py`'s top-level
    exception handler) should call this instead of re-deriving the check,
    so a future third convention only needs to be taught here.
    """
    if getattr(args, "json", False):
        return True
    return getattr(args, "format", None) == "json"


def emit(payload: dict) -> None:
    """Print ``payload`` as a single line of JSON to stdout.

    ``ensure_ascii=False`` matches the rest of the CLI's ``--json`` output
    (paths/names with non-ASCII characters print verbatim, not as ``\\uXXXX``
    escapes).
    """
    print(json.dumps(payload, ensure_ascii=False))


class KnowledgeError(Exception):
    """Raise this from a `cmd_*` handler instead of printing + returning.

    The top-level handler in ``cli.main()`` catches it once and renders it
    either as prose-on-stderr or JSON-on-stdout depending on
    :func:`wants_json` — callers don't need to know which convention the
    invoked verb uses.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        remedy: str | None = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remedy = remedy
        self.exit_code = exit_code


def error_payload(
    code: str,
    message: str,
    *,
    remedy: str | None = None,
    exit_code: int = 1,
) -> dict:
    """Build the JSON error envelope for stdout.

    Always ``{"ok": false, "code", "message", "exit"}``; ``"remedy"`` is
    included only when non-None — an absent key is a clearer "no remedy"
    signal to a parsing agent than ``"remedy": null``.
    """
    payload: dict = {
        "ok": False,
        "code": code,
        "message": message,
    }
    if remedy is not None:
        payload["remedy"] = remedy
    payload["exit"] = exit_code
    return payload
