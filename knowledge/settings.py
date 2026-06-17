"""Runtime configuration for the ``knowledge`` CLI.

This module is the single source of truth for *runtime* settings (which
backend to talk to, how to assemble a DSN, etc.). Hardcoded build-time
constants like the embedding model name still live in :mod:`knowledge.config`.

Resolution rule (JSON, project-closer-wins, then laptop default):

1. ``KNOWLEDGE_DATABASE_URL`` env (CI override) — full DSN, wins everything.
2. Walk up from cwd to filesystem root looking for ``.knowledge-config.json``.
   First match wins — the file *closer to the cwd* takes precedence.
3. If the walk found nothing, fall back to ``~/.knowledge/config.json``
   (the laptop default, inside the state dir).
4. If still nothing, defaults (``mode = sqlite``).

Same JSON schema at every scope. Pick a scope by where you put the file:

* in your repo root → applies only inside that repo
* ``~/.knowledge/config.json`` → applies everywhere else on this laptop

Delete the project file and you fall straight back to the laptop default.

Credentials never go on disk. The JSON carries env-var **names** only;
actual values come from ``os.environ`` at connect time. ``config show``
reports which file was selected so the active scope is never ambiguous.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from . import paths

StorageMode = Literal["sqlite", "shared_postgresql"]

_DEFAULT_USER_ENV = "KNOWLEDGE_PG_USER"
_DEFAULT_PASSWORD_ENV = "KNOWLEDGE_PG_PASSWORD"

# Forbidden top-level keys in any config file. Defense-in-depth: catches the
# "I'll just put my password in here for testing" footgun before any network
# call.
_FORBIDDEN_TOP_LEVEL = ("password", "user")

# Exhaustive set of values accepted by libpq's sslmode parameter.
_VALID_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

# sslmode values that do NOT authenticate the server certificate (weaker than
# verify-ca).  Used to emit the one-time H4 warning for remote hosts.
_WEAK_SSLMODES = frozenset({"disable", "allow", "prefer", "require"})

# Emitted at most once per process so bulk DSN-construction loops are quiet.
_tls_warned: bool = False

# Template written by ``knowledge config init`` (see cli.cmd_config_init).
# Lives here as a Python constant so ``knowledge/`` ships only ``*.py`` — no
# example data file. sqlite by default; the postgresql block is an inert
# example until ``storage.mode`` is flipped to ``shared_postgresql``.
CONFIG_TEMPLATE_JSON = """\
{
  "storage": {
    "mode": "sqlite",
    "postgresql": {
      "host": "db.example.com",
      "port": 5432,
      "database": "knowledge",
      "sslmode": "require",
      "user_env": "KNOWLEDGE_PG_USER",
      "password_env": "KNOWLEDGE_PG_PASSWORD",
      "connect_timeout_seconds": 10
    }
  },
  "cache_bytes": 2147483648,
  "embedding_model": null
}
"""


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int = 5432
    database: str = "knowledge"
    sslmode: str = "require"
    user_env: str = _DEFAULT_USER_ENV
    password_env: str = _DEFAULT_PASSWORD_ENV
    connect_timeout_seconds: int = 10


@dataclass(frozen=True)
class Settings:
    """Loaded runtime settings.

    ``config_source`` is ``"default"`` when no file was found anywhere, or
    the absolute path of the config file that won the resolution.
    """

    mode: StorageMode = "sqlite"
    postgresql: PostgresSettings | None = None
    cache_bytes: int = 2 * 1024 * 1024 * 1024
    embedding_model: str | None = None  # consumed by embedder._ensure_loaded()
    config_source: str = "default"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class SettingsError(Exception):
    """Raised when a discovered config file is unparseable or invalid."""


def load_settings(start_dir: Path | None = None) -> Settings:
    """Find the active config file and load it.

    Walks up from ``start_dir`` (default cwd) to the filesystem root,
    returning the first ``.knowledge-config.json`` found. If the walk exits
    without a hit, falls back to ``~/.knowledge/config.json`` (the laptop
    default). Returns built-in defaults when nothing is found anywhere.

    ``KNOWLEDGE_DATABASE_URL`` overrides backend selection: when set it forces
    ``mode = shared_postgresql`` (the DSN, with credentials inline, is itself
    the whole target), so a container / CI run needs only that one env var and
    no config file at all.

    Malformed JSON or a forbidden field → :class:`SettingsError` (the CLI
    maps to exit code 2).
    """

    url_override = os.environ.get("KNOWLEDGE_DATABASE_URL")

    config_path = _find_config(start_dir or Path.cwd())
    if config_path is None:
        # A full DSN in the env is a self-sufficient PostgreSQL target (creds
        # inline): it selects shared_postgresql by itself, so a container / CI
        # job needs only this one variable and no config file. resolve_pg_dsn()
        # returns the URL verbatim, so a null structured block is fine.
        if url_override:
            return Settings(
                mode="shared_postgresql",
                config_source="KNOWLEDGE_DATABASE_URL",
            )
        return Settings()

    raw = _parse_json(config_path) or {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{config_path}: top level must be an object")

    for forbidden in _FORBIDDEN_TOP_LEVEL:
        if forbidden in raw:
            raise SettingsError(
                f"{config_path}: top-level '{forbidden}' field is not allowed. "
                f"Credentials must come from env vars. "
                f"Use 'storage.postgresql.{forbidden}_env' to name the env var "
                f"that holds the value."
            )

    storage = raw.get("storage", {}) or {}
    pg_settings, mode = _parse_storage_block(storage, source=str(config_path))

    cache_bytes = int(raw.get("cache_bytes", 2 * 1024 * 1024 * 1024))
    embedding_model = raw.get("embedding_model")
    if embedding_model is not None and not isinstance(embedding_model, str):
        raise SettingsError(
            f"{config_path}: embedding_model must be a string or null"
        )

    # KNOWLEDGE_DATABASE_URL wins over the file's storage.mode — the same
    # precedence the resolution order has always documented. The file is still
    # parsed so cache_bytes / embedding_model carry over; only the backend
    # selection is overridden (resolve_pg_dsn returns the URL verbatim).
    if url_override:
        mode = "shared_postgresql"

    return Settings(
        mode=mode,
        postgresql=pg_settings,
        cache_bytes=cache_bytes,
        embedding_model=embedding_model,
        config_source=str(config_path),
    )


def _find_config(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.knowledge-config.json``; home fallback.

    Returns the closest match (cwd or any ancestor). If the walk-up runs
    off the filesystem root with no hit, also checks
    ``~/.knowledge/config.json`` so users running from a tmpdir or a
    non-home tree still pick up their laptop default.
    """

    p = start.resolve()
    for d in [p, *p.parents]:
        candidate = d / paths.PROJECT_CONFIG_NAME
        if candidate.exists():
            return candidate
        if d == d.parent:
            break

    home_default = paths.home_config_path()
    if home_default.exists():
        return home_default
    return None


def _parse_json(path: Path):
    """Load JSON from ``path``, raising :class:`SettingsError` on bad JSON."""

    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path}: invalid JSON — {exc}") from None


def _parse_storage_block(
    storage, source: str
) -> tuple[PostgresSettings | None, StorageMode]:
    """Validate and unpack a ``storage`` block.

    Empty or missing block → defaults (``mode = sqlite``, no postgres).
    """

    if not storage:
        return None, "sqlite"
    if not isinstance(storage, dict):
        raise SettingsError(f"{source}: 'storage' must be a mapping")

    mode = storage.get("mode", "sqlite")
    if mode not in ("sqlite", "shared_postgresql"):
        raise SettingsError(
            f"{source}: storage.mode must be 'sqlite' or 'shared_postgresql' "
            f"(got {mode!r})"
        )

    pg_block = storage.get("postgresql")
    if pg_block is None:
        return None, mode
    if not isinstance(pg_block, dict):
        raise SettingsError(f"{source}: storage.postgresql must be a mapping")

    for forbidden in ("password", "user"):
        if forbidden in pg_block:
            raise SettingsError(
                f"{source}: storage.postgresql.{forbidden} is not allowed; "
                f"use '{forbidden}_env' to name an env var instead."
            )

    host = pg_block.get("host")
    if mode == "shared_postgresql" and not host:
        raise SettingsError(
            f"{source}: storage.postgresql.host is required when "
            f"mode == 'shared_postgresql'"
        )

    sslmode = pg_block.get("sslmode", "require")
    if sslmode not in _VALID_SSLMODES:
        raise SettingsError(
            f"{source}: storage.postgresql.sslmode {sslmode!r} is not valid; "
            f"must be one of: {', '.join(sorted(_VALID_SSLMODES))}"
        )

    pg_settings = PostgresSettings(
        host=host or "",
        port=int(pg_block.get("port", 5432)),
        database=pg_block.get("database", "knowledge"),
        sslmode=sslmode,
        user_env=pg_block.get("user_env", _DEFAULT_USER_ENV),
        password_env=pg_block.get("password_env", _DEFAULT_PASSWORD_ENV),
        connect_timeout_seconds=int(
            pg_block.get("connect_timeout_seconds", 10)
        ),
    )
    return pg_settings, mode


# ---------------------------------------------------------------------------
# DSN assembly
# ---------------------------------------------------------------------------


class DsnError(Exception):
    """Raised when a PG DSN cannot be assembled (missing env, bad mode, …)."""


def dsn_source(settings: Settings) -> str:
    """Where the effective DSN comes from.

    Returns one of:
      * ``"KNOWLEDGE_DATABASE_URL"`` — env override active
      * ``"<config-path> + env"`` — file-driven config + env-var lookup
      * ``"default"`` — sqlite mode, no DSN applies
    """

    if os.environ.get("KNOWLEDGE_DATABASE_URL"):
        return "KNOWLEDGE_DATABASE_URL"
    if settings.mode == "sqlite":
        return "default"
    return f"{settings.config_source} + env"


def resolve_pg_dsn(settings: Settings) -> str:
    """Build the libpq DSN, reading credentials from env at the last moment.

    Precedence:
      1. ``KNOWLEDGE_DATABASE_URL`` if set — wins over everything (CI hatch).
      2. Structured ``storage.postgresql`` block + env-var lookup.

    Raises :class:`DsnError` (caller maps to exit code 2) when:
      * mode is sqlite (caller should not have asked)
      * postgresql block missing
      * referenced env var unset
    """

    override = os.environ.get("KNOWLEDGE_DATABASE_URL")
    if override:
        return override

    if settings.mode != "shared_postgresql":
        raise DsnError(
            "storage.mode is 'sqlite'; no PostgreSQL DSN to resolve"
        )
    pg = settings.postgresql
    if pg is None:
        raise DsnError(
            "storage.postgresql block missing in config — "
            "run 'knowledge config init' to write a template"
        )

    user = os.environ.get(pg.user_env)
    password = os.environ.get(pg.password_env)
    if not user or not password:
        missing = [
            name
            for name, value in (
                (pg.user_env, user),
                (pg.password_env, password),
            )
            if not value
        ]
        raise DsnError(
            "missing PostgreSQL credentials in environment: "
            f"{', '.join(missing)}. "
            "Export them in your shell profile, or set "
            "KNOWLEDGE_DATABASE_URL for a one-shot CI override."
        )

    # URL-encode user, password, host, and database — all four can contain
    # ``@``, ``/``, ``:`` which are libpq URL delimiters and would silently
    # corrupt parsing if left verbatim.  port and connect_timeout are already
    # int()-coerced in the PostgresSettings constructor so no encoding needed.
    _emit_tls_warning(pg.host, pg.sslmode)
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{quote(pg.host, safe='')}:{pg.port}"
        f"/{quote(pg.database, safe='')}"
        f"?sslmode={pg.sslmode}"
        f"&connect_timeout={pg.connect_timeout_seconds}"
    )


def _emit_tls_warning(host: str, sslmode: str) -> None:
    """Emit a one-time stderr warning when the server certificate is not verified.

    Only fires for non-localhost targets (empty host string is treated as
    localhost by libpq).  Never prints the DSN or any credential.
    The module-level ``_tls_warned`` flag ensures the message appears at most
    once per process even if many DSNs are assembled in a batch.
    """
    global _tls_warned
    if _tls_warned:
        return
    _localhost = {"localhost", "127.0.0.1", "::1", ""}
    if host in _localhost:
        return
    if sslmode in _WEAK_SSLMODES:
        print(
            f"warning: sslmode={sslmode} does not verify the server certificate; "
            "for remote hosts prefer sslmode=verify-full",
            file=sys.stderr,
        )
        _tls_warned = True


def mask_dsn(dsn: str) -> str:
    """Replace the password in a libpq URL with ``***`` for display.

    The ``user`` portion keeps its first three chars + ``***``; the
    password is masked entirely. Non-URL DSNs (e.g. ``host=… password=…``
    keyword form) are scrubbed by ``KEY=value`` regex on the password key
    only — host/db remain visible.
    """

    if dsn.startswith(("postgres://", "postgresql://")):
        scheme_end = dsn.find("://") + 3
        rest = dsn[scheme_end:]
        at = rest.rfind("@")
        if at < 0:
            return dsn
        creds, host_part = rest[:at], rest[at:]
        if ":" in creds:
            user, _ = creds.split(":", 1)
        else:
            user = creds
        masked_user = (user[:3] + "***") if user else "***"
        return f"{dsn[:scheme_end]}{masked_user}{host_part}"
    # Keyword form: mask the password value regardless of quoting style or
    # embedded spaces.  Single-quoted, double-quoted, and bare (space-delimited)
    # values are all handled by the alternation below.
    return re.sub(
        r"password\s*=\s*('[^']*'|\"[^\"]*\"|\S+)",
        "password=***",
        dsn,
        flags=re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Reporting helpers (used by `knowledge config show` / `check-env`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigReport:
    """Snapshot of the current configuration state, for display / smoke tests."""

    settings: Settings
    dsn_source: str
    dsn_masked: str | None
    env_status: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


def build_report() -> ConfigReport:
    """Best-effort report of the current configuration.

    Never raises: errors that would normally raise (missing env vars,
    malformed config) are captured into ``ConfigReport.error`` so the CLI
    can print them as part of ``config show`` instead of crashing.
    """

    try:
        settings = load_settings()
    except SettingsError as exc:
        return ConfigReport(
            settings=Settings(),
            dsn_source="default",
            dsn_masked=None,
            error=str(exc),
        )

    source = dsn_source(settings)
    env_status: dict[str, bool] = {}
    dsn_masked: str | None = None
    error: str | None = None

    if settings.mode == "shared_postgresql":
        pg = settings.postgresql
        if pg is not None:
            env_status = {
                pg.user_env: bool(os.environ.get(pg.user_env)),
                pg.password_env: bool(os.environ.get(pg.password_env)),
            }
        try:
            dsn_masked = mask_dsn(resolve_pg_dsn(settings))
        except DsnError as exc:
            error = str(exc)

    return ConfigReport(
        settings=settings,
        dsn_source=source,
        dsn_masked=dsn_masked,
        env_status=env_status,
        error=error,
    )
