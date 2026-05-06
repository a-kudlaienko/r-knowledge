"""Resolver ABC + the ``Edge`` dataclass.

A resolver takes raw file bytes and yields a flat list of ``Edge`` objects —
one per import / require / include statement found in the file. Resolvers
are **pure**: they don't know about file ids, project roots, or DB state.
Converting ``raw`` strings to ``target_file_id`` happens in ``relations.py``
with a project-scoped files table in hand. That separation keeps resolvers
unit-testable without a DB and lets multiple languages share the same
resolution policy surface.

Mirrors ``chunkers/base.py`` on purpose: if you know the chunker interface,
you know the resolver interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Edge:
    """One outbound import/require/include from a source file.

    ``kind`` is a small closed vocabulary the store understands:

    * ``import``          — ``import foo`` (Python), ``import foo from ...`` (JS)
    * ``from_import``     — ``from X import Y`` (Python)
    * ``require``         — ``require('foo')`` (JS/CommonJS)
    * ``dynamic_import``  — ``import('foo')`` (JS) with string-literal arg,
                            or ``importlib.import_module('foo')`` (Python)
    * ``unresolved``      — the import target is an expression we can't
                            resolve statically (``{{ var }}``, template
                            literals in dynamic imports, etc.). ``raw``
                            preserves the expression verbatim so the LLM
                            can still see something is there.
    * ``external``        — resolver couldn't match ``raw`` to a project
                            file (stdlib, node_modules, third-party). Set
                            at resolution time, not by resolvers directly.

    ``raw`` is the literal string as written in source (``.utils``,
    ``./foo``, ``os.path``). Preserved for resolved edges too — consumers
    display it alongside the resolved path.

    ``symbol`` is only populated for ``from_import`` (Python ``from X
    import Y`` → ``raw='X'``, ``symbol='Y'``). NULL everywhere else.
    Future symbol-level queries ("who uses ``Foo``?") become cheap.

    ``line`` is 1-based for display alignment with editors.
    """

    kind: str
    raw: str
    symbol: str | None
    line: int


class BaseResolver(ABC):
    """All resolvers implement ``extract(source_bytes, file_path) -> list[Edge]``.

    Resolvers do not resolve ``raw`` to files — that's the store's job.
    Their sole responsibility is to walk the parse tree and collect every
    syntactic import site, annotating it with the correct ``kind`` + ``raw``
    + optional ``symbol``. Dedup and normalization happen downstream.
    """

    @abstractmethod
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        ...
