"""Orchestrate: scan → chunk → sanitize → compress → embed → upsert.

Two top-level entry points:

* ``build_project`` — full rebuild (``knowledge build``). Wipes and
  re-creates all rows for the project.
* ``update_project`` — incremental (``knowledge update``). Skips unchanged
  files via per-file content_hash; for changed files, re-embeds only
  chunks whose sanitized+compressed text actually changed (matched by
  per-chunk content_hash). "One function edited in a 2k-line file" →
  one chunk re-embedded, not fifty.

Pipeline invariants:

* Every chunk's ``stored_text`` and ``embedded_text`` go through **sanitize
  → compress** before hashing or insert. ``content_hash`` is the sha256 of
  the compressed, sanitized text — so the "has this chunk changed?" check
  compares what's actually in the DB, not what was on disk.
* One outer transaction wraps each top-level call. APSW commits on
  ``with`` block exit; on exception everything rolls back atomically.
* Embeddings are batched: all new chunks across all files are collected,
  then a single ``encode`` call vectorizes them. Much faster than per-file
  encode.
* Reused chunks (unchanged content_hash) keep their existing
  ``chunks_vec`` row — no re-embed, no delete. Their positional fields
  (line/byte offsets, parent_id, sibling_order, metadata) are UPDATEd in
  place to reflect the new structure of the file.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import NamedTuple

from . import ansible_vars, config, db, query_cache, relations, variables
from .big_split import split_if_oversized
from .chunkers import dispatch_chunker
from .chunkers.base import Chunk
from .db import Connection
from .embedder import get_embedder
from .projects import get_or_create_project, update_counts
from .resolvers import Edge
from .sanitizer import scrub_text
from .scanner import walk_project
from .whitespace import compress


def build_project(
    conn: Connection,
    root: Path,
    name_override: str | None = None,
    verbose: bool = True,
) -> tuple[int, int, int]:
    """Full rebuild. Returns ``(project_id, file_count, chunk_count)``."""
    project = get_or_create_project(conn, root, name_override)
    backend = db.get_backend()

    with db.transaction(conn):
        # On PG: non-blocking advisory lock so two concurrent build/update
        # runs against the same project on the same server fail-fast
        # (exit code 3 from the CLI) instead of stacking up. SQLite is a
        # no-op — single-writer journal already serializes.
        if not backend.try_advisory_lock_project(conn, project.id):
            raise db.ProjectBusyError(project.name)

        # Any prior `ask` answer for this project is stale after a full
        # rebuild — chunk IDs / file paths may change. Wipe within the
        # same txn so cache state stays consistent with chunk state.
        query_cache.wipe_project(conn, project.id)

        # Clean slate for this project. SQLite vec0 has no FK cascade so
        # the helper wipes chunks_vec rows explicitly before chunks goes;
        # the PG side table cascades from ``chunks`` so the helper is a
        # no-op there.
        db.delete_chunk_embeddings_for_project(conn, project.id)
        db.execute(
            conn, "DELETE FROM chunks WHERE project_id = ?", (project.id,)
        )
        db.execute(
            conn, "DELETE FROM files WHERE project_id = ?", (project.id,)
        )

        # Buffer for batch embedding: (chunk_id, embedded_text) pairs.
        embed_queue: list[tuple[int, str]] = []
        # Buffer for per-file edge extraction. Resolution needs the whole
        # files table, so we collect raw edges during the walk and resolve
        # them in a second pass after the walk is complete.
        pending_edges: list[tuple[int, str, str, list[Edge]]] = []
        now = time.time()

        # Scan the repo and bulk-write via the db.* helpers, which pick
        # COPY (PostgreSQL) vs executemany (SQLite) internally — one path,
        # both backends.
        files_indexed = _build_project_bulk(
            conn, project.id, root, embed_queue, pending_edges, now
        )

        # Batch-embed outside the per-file loop to maximize throughput.
        # On shared PG the bulk helper collapses N single-row INSERTs into
        # one COPY — the difference between minutes and seconds on any
        # non-local database (LB, WAN, cloud PG).
        if embed_queue:
            if verbose:
                print(f"embedding {len(embed_queue)} chunks...", flush=True)
            embedder = get_embedder()
            texts = [t for (_, t) in embed_queue]
            vectors = embedder.encode(texts)
            db.insert_chunk_embeddings_bulk(
                conn,
                ((cid, vec) for (cid, _), vec in zip(embed_queue, vectors)),
            )

        # Auto-discover ansible inventory variables before edges resolve,
        # so templated paths (``{{ deploy_env }}/main.yml``) substitute
        # against fresh values on the same pass.
        _autoload_ansible_vars(conn, project.id, root, verbose)

        # Resolve + persist edges. Deferred to here so forward references
        # (A imports B, where A is walked before B) resolve against the
        # complete files table.
        if pending_edges:
            edge_count = _flush_edges(conn, project.id, root, pending_edges)
            if verbose:
                print(f"edges: {edge_count} across {len(pending_edges)} files")

        db.execute(
            conn,
            "UPDATE projects SET last_build = ?, last_update = ? WHERE id = ?",
            (now, now, project.id),
        )

        # Bump meta to the currently-compiled versions. `init_schema` seeds
        # these only on fresh init, so without this line a v1→v2 rebuild
        # leaves `meta.schema_version = "1"` — and the next `update` run
        # would loop into another forced rebuild forever.
        for k, v in {
            "schema_version":  config.SCHEMA_VERSION,
            "chunker_version": config.CHUNKER_VERSION,
            "embedding_model": config.MODEL,
        }.items():
            db.set_meta(conn, k, v)

    # update_counts queries outside the savepoint above — harmless, auto-commits.
    update_counts(conn, project.id)
    return project.id, files_indexed, len(embed_queue)


def update_project(
    conn: Connection,
    root: Path,
    name_override: str | None = None,
    verbose: bool = True,
) -> tuple[int, int, int]:
    """Incremental reindex. Returns ``(project_id, files_visited, chunks_embedded)``.

    * Unchanged files (by sha256 of bytes) are skipped entirely — no
      chunking, no embedding, ``last_scanned`` is NOT updated either
      (it's only touched when we actually scan).
    * Changed files are rechunked and per-chunk diffed: chunks whose
      post-sanitize+compress hash matches an existing row reuse that row
      (UPDATE positional fields, keep the embedding). Everything else is
      inserted + queued for embedding, and orphaned existing rows are
      deleted.
    * Files that no longer exist on disk are dropped (cascade removes
      their chunks and chunks_vec entries).
    * If any version in ``meta`` (schema, chunker, embedding model) no
      longer matches the code, we force a full rebuild — incremental
      semantics aren't valid across those changes.
    """
    mismatches = _version_mismatches(conn)
    if mismatches:
        if verbose:
            print(
                f"warning: meta mismatch {mismatches} — forcing full rebuild; "
                "other projects in this DB also need `knowledge build`.",
                flush=True,
            )
        return build_project(conn, root, name_override, verbose)

    project = get_or_create_project(conn, root, name_override)

    # Map existing files by rel_path for O(1) per-path lookup.
    existing_files: dict[str, tuple[int, str]] = {
        row[1]: (row[0], row[2])
        for row in db.fetch_all(
            conn,
            "SELECT id, rel_path, content_hash FROM files WHERE project_id = ?",
            (project.id,),
        )
    }

    # One-time backfill: a project indexed before the relations feature
    # has files but zero edges. Without this, update() would leave such
    # projects with an empty graph until every file's content hash
    # changed — which rarely happens by itself. We detect the empty
    # state up-front and force edge extraction for every file, even
    # those whose bytes are unchanged.
    needs_edge_backfill = False
    if existing_files:
        edge_count = db.fetch_one(
            conn,
            "SELECT COUNT(*) FROM file_edges WHERE project_id = ?",
            (project.id,),
        )[0]
        if edge_count == 0:
            needs_edge_backfill = True
            if verbose:
                print(
                    "first run with relations — extracting edges for all "
                    f"{len(existing_files)} file(s) (one-time pass).",
                    flush=True,
                )

    embed_queue: list[tuple[int, str]] = []
    # New/changed files' edges accumulate here; flushed after the walk so
    # resolution sees the final state of the files table (incl. newly
    # inserted rows and with stale rows already dropped).
    pending_edges: list[tuple[int, str, str, list[Edge]]] = []
    # (file_id, mtime, last_scanned) for byte-identical files — flushed in one
    # bulk UPDATE after the walk so a no-op update over a large repo is a
    # single round-trip on PG, not one per unchanged file.
    touch_rows: list[tuple[int, float, float]] = []
    seen_paths: set[str] = set()
    files_changed = 0
    files_new = 0
    now = time.time()

    # Accumulated during the walk; persisted in one bulk flush after.
    changed_files: list[tuple[int, _ScannedFile]] = []   # (file_id, sf)
    new_files_raw: list[_ScannedFile] = []                # sf only (id reserved later)

    backend = db.get_backend()
    with db.transaction(conn):
        if not backend.try_advisory_lock_project(conn, project.id):
            raise db.ProjectBusyError(project.name)

        for abs_path, lang in walk_project(root):
            rel = abs_path.relative_to(root).as_posix()
            seen_paths.add(rel)

            chunker = dispatch_chunker(lang)
            if chunker is None:
                continue

            try:
                raw_bytes = abs_path.read_bytes()
                stat = abs_path.stat()
            except OSError:
                continue

            new_hash = hashlib.sha256(raw_bytes).hexdigest()

            if rel in existing_files:
                file_id, old_hash = existing_files[rel]
                if old_hash == new_hash:
                    # Content unchanged, but disk mtime may have bumped
                    # (touch, `git checkout`, external tool rewrite). Defer
                    # the mtime/last_scanned sync to one bulk UPDATE after the
                    # walk so `status` doesn't flag this file stale — without
                    # paying a round-trip per unchanged file.
                    touch_rows.append((file_id, stat.st_mtime, now))
                    # One-time edge backfill for projects indexed before
                    # this feature — normal update wouldn't touch them.
                    # Skips any language without a resolver (dispatch
                    # returns [] → no pending entry added).
                    if needs_edge_backfill:
                        raw_edges = relations.extract_edges(
                            raw_bytes, abs_path, lang
                        )
                        if raw_edges:
                            pending_edges.append(
                                (file_id, rel, lang, raw_edges)
                            )
                    continue  # fast path: bytes unchanged
                files_changed += 1
                sf = _scan_bytes(abs_path, root, lang, raw_bytes, stat)
                if sf is None:
                    continue
                changed_files.append((file_id, sf))
            else:
                files_new += 1
                sf = _scan_bytes(abs_path, root, lang, raw_bytes, stat)
                if sf is None:
                    continue
                new_files_raw.append(sf)

        # Sync mtime for unchanged files in one shot (see touch_rows above).
        db.bulk_touch_files(conn, touch_rows)

        # Drop files that are gone on disk.
        stale_paths = set(existing_files.keys()) - seen_paths
        files_deleted = len(stale_paths)
        stale_ids = [existing_files[rel][0] for rel in stale_paths]
        if stale_ids:
            if db.current_mode() == "postgresql":
                # One round-trip; chunk_embeddings/chunks/file_edges all
                # cascade from the files DELETE.
                db.execute(
                    conn, "DELETE FROM files WHERE id = ANY(?)", (stale_ids,)
                )
            else:
                # SQLite: vec0 has no FK cascade, so sweep chunks_vec for each
                # doomed file's chunks first, then drop the files row (which
                # cascades chunks + file_edges).
                for fid in stale_ids:
                    conn.execute(
                        "DELETE FROM chunks_vec WHERE chunk_id IN "
                        "(SELECT id FROM chunks WHERE file_id = ?)",
                        (fid,),
                    )
                    db.execute(conn, "DELETE FROM files WHERE id = ?", (fid,))

        # --- bulk flush: new files → reserve ids + file rows ---
        new_files: list[tuple[int, _ScannedFile]] = []
        if new_files_raw:
            new_file_ids = db.reserve_ids(conn, "files", len(new_files_raw))
            file_rows: list[tuple] = []
            for sf, fid in zip(new_files_raw, new_file_ids):
                file_rows.append(
                    (fid, project.id, sf.rel, sf.content_hash,
                     sf.mtime, sf.size, sf.lang, now)
                )
                new_files.append((fid, sf))
            db.copy_file_rows(conn, file_rows)

        # --- fetch existing chunks for ALL changed files in one query ---
        changed_ids = [fid for fid, _ in changed_files]
        by_file: dict[int, dict[str, list[int]]] = {}
        for file_id, cid, chash in db.fetch_chunks_for_files(conn, changed_ids):
            by_file.setdefault(file_id, {}).setdefault(chash, []).append(cid)
        changed_with_maps = [
            (fid, sf, by_file.get(fid, {}))
            for fid, sf in changed_files
        ]

        # --- classify, reserve exact new-chunk ids, build rows ---
        file_classes = _classify_update_chunks(changed_with_maps, new_files)
        total_new = sum(
            1 for fc in file_classes for r in fc.reuse_ids if r is None
        )
        new_chunk_ids = db.reserve_ids(conn, "chunks", total_new)
        copy_rows, update_rows, embed_pairs = _build_update_rows(
            project.id, file_classes, new_chunk_ids
        )

        # ORDER MATTERS (FK): COPY new first, then UPDATE reused (may repoint
        # to a new parent id), then DELETE orphans (nothing references them
        # after the update).
        db.copy_chunk_rows(conn, copy_rows)
        db.bulk_update_chunk_positions(conn, update_rows)
        orphan_ids = [cid for fc in file_classes for cid in fc.orphan_ids]
        db.delete_chunks_by_ids(conn, orphan_ids)

        # --- update changed files' rows (content_hash/mtime/size/last_scanned) ---
        db.bulk_update_file_rows(
            conn,
            [(fid, sf.content_hash, sf.mtime, sf.size, now)
             for fid, sf in changed_files],
        )

        # Accumulate embed pairs from the bulk classify step.
        embed_queue.extend(embed_pairs)

        # --- edges: collect pending entries for changed+new files ---
        for fid, sf in changed_files:
            if sf.raw_edges:
                pending_edges.append((fid, sf.rel, sf.lang, sf.raw_edges))
        for fid, sf in new_files:
            if sf.raw_edges:
                pending_edges.append((fid, sf.rel, sf.lang, sf.raw_edges))

        # Batch-embed just the chunks that were actually new/changed.
        # Bulk helper: one COPY on PG, single-row loop on SQLite (see
        # db.insert_chunk_embeddings_bulk for why the SQLite path stays
        # per-row).
        if embed_queue:
            if verbose:
                print(f"embedding {len(embed_queue)} chunks...", flush=True)
            embedder = get_embedder()
            texts = [t for (_, t) in embed_queue]
            vectors = embedder.encode(texts)
            db.insert_chunk_embeddings_bulk(
                conn,
                ((cid, vec) for (cid, _), vec in zip(embed_queue, vectors)),
            )

        # Auto-discover ansible inventory variables before edges resolve.
        # Even when no source files changed, an edited group_vars/all.yml
        # should propagate — if there are no pending edges, we still
        # re-resolve in place via apply_variables (cheap, idempotent).
        autoload_changed = _autoload_ansible_vars(
            conn, project.id, root, verbose
        )

        # A changed file that now has NO edges still needs its stale edges
        # wiped (mirrors the old per-file wipe_file in _reindex_changed_file).
        # Files WITH edges are wiped inside _flush_edges; new files had none.
        db.wipe_file_edges(
            conn, [fid for fid, sf in changed_files if not sf.raw_edges]
        )

        # Flush edges after the whole walk so forward references resolve
        # against the final files table. Unchanged files keep their existing
        # edges untouched — no work done for them.
        if pending_edges:
            edge_count = _flush_edges(conn, project.id, root, pending_edges)
            if verbose:
                print(
                    f"edges: {edge_count} across {len(pending_edges)} "
                    f"file(s) changed/added"
                )
        elif autoload_changed:
            # No code changes, but YAML may have moved variable values —
            # re-resolve any parametric edges against the new map.
            variables.apply_variables(conn, project.id, root)

        db.execute(
            conn,
            "UPDATE projects SET last_update = ? WHERE id = ?",
            (now, project.id),
        )

        # Invalidate cache only when something actually changed. A no-op
        # update (all files byte-identical) shouldn't wipe cached answers
        # that are still correct, which matters when the agent runs
        # `knowledge update` as a hook on every turn.
        if files_new or files_changed or files_deleted:
            query_cache.wipe_project(conn, project.id)

    update_counts(conn, project.id)

    if verbose:
        print(
            f"update: {files_new} new, {files_changed} changed, "
            f"{files_deleted} deleted; {len(embed_queue)} chunks re-embedded"
        )
    return project.id, len(seen_paths), len(embed_queue)


def _prepare_chunk_row(c: Chunk) -> tuple[str, str, str | None]:
    """Sanitize + compress + hash a chunk. Shared by all insert paths."""
    sanitized = scrub_text(c.text)
    stored = compress(sanitized)
    chunk_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    metadata_json = json.dumps(c.metadata) if c.metadata else None
    return stored, chunk_hash, metadata_json


# ---------------------------------------------------------------------------
# Build strategies: one scan, two ways to persist.
# ---------------------------------------------------------------------------


class _ScannedFile(NamedTuple):
    """Everything read from one file, with NO database interaction yet."""
    rel: str
    lang: str
    content_hash: str
    mtime: float
    size: int
    chunks: list[Chunk]
    raw_edges: list[Edge]


class _FileClass(NamedTuple):
    """Intermediate classification result for one file in the bulk update path.

    ``file_id``   — DB id of the file row (reserved or existing).
    ``chunks``    — ordered chunk list from the scanner (parents before children).
    ``prepared``  — per-chunk ``(stored, content_hash, metadata)`` from
                    ``_prepare_chunk_row``; computed ONCE, never re-done.
    ``reuse_ids`` — per-chunk: an existing chunk id to UPDATE (popped by hash
                    from ``existing_by_hash``), or None (must INSERT).
    ``orphan_ids``— existing chunk ids that were NOT matched by any new chunk
                    in this file; these rows must be deleted.
    """
    file_id: int
    chunks: list[Chunk]
    prepared: list[tuple]          # list[(stored, hash, meta)]
    reuse_ids: list[int | None]
    orphan_ids: list[int]


def _classify_update_chunks(
    changed_files: list[tuple],   # (file_id, _ScannedFile, existing_by_hash)
    new_files: list[tuple],       # (file_id, _ScannedFile)
) -> list[_FileClass]:
    """Classify every chunk in every changed/new file as REUSE or NEW.

    ``existing_by_hash`` maps ``content_hash -> [existing_id, ...]`` and IS
    mutated (popped) so whatever ids remain after this call are orphans.

    Pure (no DB). The result is a list of ``_FileClass`` entries — one per
    file — carrying the prepared chunk rows, reuse id decisions, and orphan
    id lists needed by ``_build_update_rows``.
    """
    out: list[_FileClass] = []
    for file_id, sf, existing_by_hash in changed_files:
        prepared = [_prepare_chunk_row(c) for c in sf.chunks]
        reuse_ids: list[int | None] = []
        for (_, chash, _) in prepared:
            bucket = existing_by_hash.get(chash)
            reuse_ids.append(bucket.pop() if bucket else None)
        orphans = [cid for ids in existing_by_hash.values() for cid in ids]
        out.append(_FileClass(file_id, sf.chunks, prepared, reuse_ids, orphans))
    for file_id, sf in new_files:
        prepared = [_prepare_chunk_row(c) for c in sf.chunks]
        out.append(
            _FileClass(file_id, sf.chunks, prepared, [None] * len(sf.chunks), [])
        )
    return out


def _build_update_rows(
    project_id: int,
    file_classes: list[_FileClass],
    new_chunk_ids: list[int],
) -> tuple[list[tuple], list[tuple], list[tuple[int, str]]]:
    """Turn classified file data into the three row-lists needed for the bulk flush.

    ``new_chunk_ids`` must have length == total count of ``None`` entries
    across all ``fc.reuse_ids`` (i.e. the IDs returned by
    ``db.reserve_ids('chunks', total_new)``).

    Returns:
        ``copy_rows``   — fully-formed chunk rows for ``db.copy_chunk_rows``
                          (column order: id, project_id, file_id, parent_id,
                          sibling_order, kind, name, qualified_name,
                          start_line, end_line, start_byte, end_byte,
                          char_count, content_hash, stored_text,
                          embedded_text, metadata)
        ``update_rows`` — positional-update rows for
                          ``db.bulk_update_chunk_positions``
                          (column order: id, parent_id, sibling_order,
                          start_line, end_line, start_byte, end_byte,
                          name, qualified_name, metadata)
        ``embed_pairs`` — ``(chunk_id, stored_text)`` for NEW chunks only;
                          accumulated into the shared embed_queue.

    Ordering invariant: within each file chunks are in chunker order (parents
    before children), so ``ids[c.parent_idx]`` is already resolved by the
    time any child is processed. ``copy_rows`` are appended parent-before-child
    per file, keeping the NOT-DEFERRABLE self-referential ``parent_id`` FK safe
    as each row lands in the COPY stream.
    """
    copy_rows: list[tuple] = []
    update_rows: list[tuple] = []
    embed_pairs: list[tuple[int, str]] = []
    it = iter(new_chunk_ids)

    for fc in file_classes:
        ids: list[int] = [0] * len(fc.chunks)
        for i, rid in enumerate(fc.reuse_ids):
            ids[i] = rid if rid is not None else next(it)

        for i, c in enumerate(fc.chunks):
            stored, chash, meta = fc.prepared[i]
            parent_id = ids[c.parent_idx] if c.parent_idx is not None else None
            if fc.reuse_ids[i] is None:
                # NEW chunk — must be COPYed and embedded.
                copy_rows.append((
                    ids[i], project_id, fc.file_id, parent_id, c.sibling_order,
                    c.kind, c.name, c.qualified_name,
                    c.start_line, c.end_line, c.start_byte, c.end_byte,
                    len(stored), chash, stored, stored, meta,
                ))
                embed_pairs.append((ids[i], stored))
            else:
                # REUSED chunk — UPDATE positional fields only; embedding kept.
                update_rows.append((
                    fc.reuse_ids[i], parent_id, c.sibling_order,
                    c.start_line, c.end_line, c.start_byte, c.end_byte,
                    c.name, c.qualified_name, meta,
                ))

    return copy_rows, update_rows, embed_pairs


def _scan_bytes(
    abs_path: Path,
    root: Path,
    lang: str,
    raw_bytes: bytes,
    stat,
) -> _ScannedFile | None:
    """Core scan logic operating on already-read bytes.

    No DB calls, no I/O — caller provides bytes and stat. Returns None if
    the language has no chunker (shouldn't happen in the update walk because
    we guard with dispatch_chunker before reading bytes, but kept for safety).
    """
    chunker = dispatch_chunker(lang)
    if chunker is None:
        return None
    raw_chunks = chunker.chunk(raw_bytes, abs_path)
    chunks = _apply_big_split(raw_chunks) if raw_chunks else []
    return _ScannedFile(
        rel=abs_path.relative_to(root).as_posix(),
        lang=lang,
        content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        mtime=stat.st_mtime,
        size=stat.st_size,
        chunks=chunks,
        raw_edges=relations.extract_edges(raw_bytes, abs_path, lang),
    )


def _scan_file(abs_path: Path, root: Path, lang: str) -> _ScannedFile | None:
    """Pure per-file scan: read bytes, chunk, big-split, hash, extract edges.

    No DB calls — this is the shared core both the SQLite walk and the PG
    bulk builder run, so "how a file becomes rows" lives in exactly one
    place. Returns None when the file has no chunker or can't be read.
    """
    chunker = dispatch_chunker(lang)
    if chunker is None:
        return None  # language recognized but chunker not implemented yet
    try:
        raw_bytes = abs_path.read_bytes()
        stat = abs_path.stat()
    except OSError:
        return None

    return _scan_bytes(abs_path, root, lang, raw_bytes, stat)



def _flatten_chunks_with_parents(
    scanned: list[_ScannedFile], file_ids: list[int]
) -> list[tuple[int, Chunk, str, str, str | None, int | None]]:
    """Concatenate every file's chunks into one global list, translating each
    chunk's file-local ``parent_idx`` to a GLOBAL index into that list.

    Each entry: ``(file_id, chunk, stored, content_hash, metadata, gparent)``
    where ``gparent`` is the flattened-list index of the parent, or None.
    Order preserves per-file order with files concatenated, so every parent
    precedes its children — required so the COPY satisfies the
    NOT-DEFERRABLE self-referential ``parent_id`` FK row-by-row. Pure (no DB)
    to keep the parent-offset arithmetic unit-testable.
    """
    flat: list[tuple[int, Chunk, str, str, str | None, int | None]] = []
    for sf, fid in zip(scanned, file_ids):
        base = len(flat)
        for c in sf.chunks:
            stored, chash, meta = _prepare_chunk_row(c)
            gparent = base + c.parent_idx if c.parent_idx is not None else None
            flat.append((fid, c, stored, chash, meta, gparent))
    return flat


def _build_project_bulk(
    conn: Connection,
    project_id: int,
    root: Path,
    embed_queue: list[tuple[int, str]],
    pending_edges: list[tuple[int, str, str, list[Edge]]],
    now: float,
) -> int:
    """Single build path for both PostgreSQL and SQLite backends.

    Scans the repo, then bulk-writes via the ``db.*`` helpers, which pick
    COPY (PostgreSQL) vs ``executemany`` (SQLite) internally. On PostgreSQL
    this keeps wire cost O(1) in files+chunks rather than O(N) — the whole
    point on a remote/LB-fronted database. SQLite runs the same code via
    ``executemany`` inserts with explicit ids; the self-referential
    ``parent_id`` FK is satisfied because ``_flatten_chunks_with_parents``
    emits parents before children and the inserts proceed in order.
    Returns files indexed.
    """
    scanned = [
        sf
        for abs_path, lang in walk_project(root)
        if (sf := _scan_file(abs_path, root, lang)) is not None
    ]
    if not scanned:
        return 0

    # Files: reserve ids, then one COPY. Edges are tagged with the reserved
    # id and flushed later (after the files table is fully populated).
    file_ids = db.reserve_ids(conn, "files", len(scanned))
    file_rows = []
    for sf, fid in zip(scanned, file_ids):
        file_rows.append(
            (fid, project_id, sf.rel, sf.content_hash, sf.mtime, sf.size, sf.lang, now)
        )
        if sf.raw_edges:
            pending_edges.append((fid, sf.rel, sf.lang, sf.raw_edges))
    db.copy_file_rows(conn, file_rows)

    # Chunks: flatten repo-wide, reserve ids, resolve parent_id, one COPY.
    flat = _flatten_chunks_with_parents(scanned, file_ids)
    if flat:
        chunk_ids = db.reserve_ids(conn, "chunks", len(flat))
        copy_rows = []
        for idx, (fid, c, stored, chash, meta, gparent) in enumerate(flat):
            cid = chunk_ids[idx]
            parent_id = chunk_ids[gparent] if gparent is not None else None
            copy_rows.append((
                cid, project_id, fid, parent_id, c.sibling_order,
                c.kind, c.name, c.qualified_name,
                c.start_line, c.end_line, c.start_byte, c.end_byte,
                len(stored), chash, stored, stored, meta,
            ))
            embed_queue.append((cid, stored))
        db.copy_chunk_rows(conn, copy_rows)

    return len(scanned)


def _version_mismatches(conn: Connection) -> list[str]:
    """Return meta keys whose stored value DIFFERS from current config.

    Returning an empty list means the index's chunker/model/schema are
    compatible with the code in this process and incremental update is
    safe. Any mismatch means chunks stored under an older chunker have
    stale semantics (different embeddings, different chunk boundaries).

    Missing keys (``get_meta`` returns ``None``) are *not* a mismatch —
    on shared_postgresql, the ``meta`` table is freshly seeded by the
    first ``build`` to run; freshly-migrated projects have no meta until
    the first build/update writes it. Treating missing as "different"
    would force a destructive rebuild on every PG migration.
    """
    wanted = {
        "schema_version":  config.SCHEMA_VERSION,
        "chunker_version": config.CHUNKER_VERSION,
        "embedding_model": config.MODEL,
    }
    out: list[str] = []
    for k, v in wanted.items():
        stored = db.get_meta(conn, k)
        if stored is not None and stored != v:
            out.append(k)
    return out


def _autoload_ansible_vars(
    conn: Connection,
    project_id: int,
    root: Path,
    verbose: bool,
) -> bool:
    """Discover ``group_vars/all*`` + ``host_vars/*`` and upsert into
    ``project_variables`` under ``scope='ansible'`` with auto sources.

    Manual rows (``source='manual'``) are never overwritten — that
    contract lives in :func:`variables.set_auto`. Stale auto rows whose
    name is no longer present in the YAML are deleted.

    Returns True if any auto rows were written or removed (used by the
    update path to decide whether to re-resolve parametric edges when no
    source files changed).
    """

    cfgs = relations._find_ansible_cfgs(root)
    loaded = ansible_vars.load_inventory_vars(root, cfgs)
    total_pairs = 0
    for src, pairs in loaded.items():
        source_label = f"auto:{src}"
        # Both helpers run their own statements; the surrounding build/
        # update transaction already wraps everything for atomicity.
        variables.set_auto(
            conn, project_id, "ansible", pairs, source=source_label
        )
        variables.delete_stale_auto(
            conn, project_id, source_label, set(pairs.keys())
        )
        total_pairs += len(pairs)
    if verbose and total_pairs:
        files_seen = sum(1 for _, m in loaded.items() if m)
        print(
            f"ansible vars: loaded {total_pairs} entries from "
            f"{files_seen} source(s) (group_vars/host_vars)"
        )
    return total_pairs > 0


def _flush_edges(
    conn: Connection,
    project_id: int,
    root: Path,
    pending: list[tuple[int, str, str, list[Edge]]],
) -> int:
    """Resolve + persist buffered edges against a freshly-loaded FileIndex.

    Two reasons this is a post-walk pass instead of per-file:

    1. Forward references. File A imports file B; A may be walked and
       inserted before B. Loading the FileIndex after the walk means B is
       always available for resolution.
    2. Batched I/O. One FileIndex build amortizes over all files that had
       edges, vs. one dict-rebuild per file.

    Returns total edge rows inserted. The wipe-before-write pattern makes
    re-running idempotent. Callers that need a changed-but-now-edgeless file's
    stale edges cleared must wipe those ids separately (see update_project) —
    such files are not in ``pending``.
    """
    index = relations.FileIndex.load(conn, project_id, root)
    # Populate Phase 2 side-maps (ansible roles_path, custom-module
    # scan, helm template-name map) AND Phase 3 project variables.
    # ``conn`` lets prepare() load the per-project variables table so
    # ``{{ var }}`` / ``${var.x}`` paths can resolve during the build.
    index.prepare(pending, conn=conn)

    # Resolve all edges in memory (pure — no DB), then wipe + bulk-write
    # in two statements.  PG: one DELETE ANY + one COPY → 2 round-trips
    # regardless of edge count.  SQLite: executemany (same statement
    # count as before; each helper forks internally).
    rows: list[tuple] = []
    for file_id, rel, lang, edges in pending:
        rows.extend(relations.resolve_edges(index, file_id, rel, lang, edges))

    db.wipe_file_edges(conn, [file_id for (file_id, _, _, _) in pending])
    db.copy_file_edge_rows(conn, rows)
    return len(rows)


def _apply_big_split(chunks: list[Chunk]) -> list[Chunk]:
    """Flatten big_split output into the per-file chunk list, translating
    local ``parent_idx`` offsets to their global positions.

    big_split returns ``[parent, sub_0, sub_1, ...]`` for oversized inputs
    with each sub's ``parent_idx = 0`` (pointing to the parent inside that
    local list). Here we concatenate the per-chunk results and shift those
    local indices by the current length of the global list so the parent
    link still points at the right row after the indexer inserts them in
    order and resolves ``inserted_ids[parent_idx]``.
    """
    out: list[Chunk] = []
    for original in chunks:
        local = split_if_oversized(original)
        base_offset = len(out)
        for c in local:
            if c.parent_idx is not None:
                c.parent_idx = base_offset + c.parent_idx
            out.append(c)
    return out
