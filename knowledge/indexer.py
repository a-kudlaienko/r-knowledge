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

from . import config, db, relations
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

    with conn:  # APSW: outer savepoint = one atomic transaction
        # Clean slate for this project. chunks_vec has no FK cascade (virtual
        # table), so we clear it first before chunks drops the ids it refs.
        conn.execute(
            "DELETE FROM chunks_vec WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE project_id = ?)",
            (project.id,),
        )
        conn.execute("DELETE FROM chunks WHERE project_id = ?", (project.id,))
        conn.execute("DELETE FROM files WHERE project_id = ?", (project.id,))

        # Buffer for batch embedding: (chunk_id, embedded_text) pairs.
        embed_queue: list[tuple[int, str]] = []
        # Buffer for per-file edge extraction. Resolution needs the whole
        # files table, so we collect raw edges during the walk and resolve
        # them in a second pass after the walk is complete.
        pending_edges: list[tuple[int, str, str, list[Edge]]] = []
        files_indexed = 0
        now = time.time()

        for abs_path, lang in walk_project(root):
            chunker = dispatch_chunker(lang)
            if chunker is None:
                continue  # language recognized but chunker not implemented yet

            try:
                raw_bytes = abs_path.read_bytes()
                stat = abs_path.stat()
            except OSError:
                continue

            raw_chunks = chunker.chunk(raw_bytes, abs_path)
            chunks = _apply_big_split(raw_chunks) if raw_chunks else []

            content_hash = hashlib.sha256(raw_bytes).hexdigest()
            rel = abs_path.relative_to(root).as_posix()

            # Always insert the file row — even when the chunker produced
            # no chunks (empty YAML, ``{}`` JSON, empty stub scripts). If
            # we skip it, subsequent `update` runs keep classifying the
            # file as "new" and re-scan it forever.
            conn.execute(
                "INSERT INTO files(project_id, rel_path, content_hash, mtime, "
                "size, lang, last_scanned) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project.id, rel, content_hash, stat.st_mtime, stat.st_size, lang, now),
            )
            file_id = conn.last_insert_rowid()
            files_indexed += 1

            # Edge extraction runs regardless of chunker output — a file
            # can have imports even when it has no chunkable top-level
            # defs (e.g., a re-export barrel file).
            raw_edges = relations.extract_edges(raw_bytes, abs_path, lang)
            if raw_edges:
                pending_edges.append((file_id, rel, lang, raw_edges))

            if not chunks:
                continue  # tracked in files table; no chunks to embed

            # Track inserted chunk ids by their index in `chunks` so we can
            # resolve parent_idx → parent_id on the fly. Chunkers emit
            # parents before children, so this lookup is always valid.
            inserted_ids: list[int] = []
            for c in chunks:
                sanitized = scrub_text(c.text)
                stored = compress(sanitized)
                chunk_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()

                parent_id = (
                    inserted_ids[c.parent_idx] if c.parent_idx is not None else None
                )
                metadata_json = json.dumps(c.metadata) if c.metadata else None

                conn.execute(
                    "INSERT INTO chunks(project_id, file_id, parent_id, "
                    "sibling_order, kind, name, qualified_name, start_line, "
                    "end_line, start_byte, end_byte, char_count, content_hash, "
                    "stored_text, embedded_text, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project.id, file_id, parent_id, c.sibling_order,
                        c.kind, c.name, c.qualified_name,
                        c.start_line, c.end_line, c.start_byte, c.end_byte,
                        len(stored), chunk_hash, stored, stored, metadata_json,
                    ),
                )
                cid = conn.last_insert_rowid()
                inserted_ids.append(cid)
                embed_queue.append((cid, stored))

        # Batch-embed outside the per-file loop to maximize throughput.
        if embed_queue:
            if verbose:
                print(f"embedding {len(embed_queue)} chunks...", flush=True)
            embedder = get_embedder()
            texts = [t for (_, t) in embed_queue]
            vectors = embedder.encode(texts)
            for (cid, _), vec in zip(embed_queue, vectors):
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                    (cid, vec.tobytes()),
                )

        # Resolve + persist edges. Deferred to here so forward references
        # (A imports B, where A is walked before B) resolve against the
        # complete files table.
        if pending_edges:
            edge_count = _flush_edges(conn, project.id, root, pending_edges)
            if verbose:
                print(f"edges: {edge_count} across {len(pending_edges)} files")

        conn.execute(
            "UPDATE projects SET last_build = ?, last_update = ? WHERE id = ?",
            (now, now, project.id),
        )

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
        for row in conn.execute(
            "SELECT id, rel_path, content_hash FROM files WHERE project_id = ?",
            (project.id,),
        ).fetchall()
    }

    # One-time backfill: a project indexed before the relations feature
    # has files but zero edges. Without this, update() would leave such
    # projects with an empty graph until every file's content hash
    # changed — which rarely happens by itself. We detect the empty
    # state up-front and force edge extraction for every file, even
    # those whose bytes are unchanged.
    needs_edge_backfill = False
    if existing_files:
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM file_edges WHERE project_id = ?",
            (project.id,),
        ).fetchone()[0]
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
    seen_paths: set[str] = set()
    files_changed = 0
    files_new = 0
    now = time.time()

    with conn:
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
                    # (touch, `git checkout`, external tool rewrite). Sync
                    # it so `status` doesn't flag this file as stale
                    # forever. Without this, status stays red after a
                    # successful update.
                    conn.execute(
                        "UPDATE files SET mtime = ?, last_scanned = ? "
                        "WHERE id = ?",
                        (stat.st_mtime, now, file_id),
                    )
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
                _reindex_changed_file(
                    conn, project.id, file_id, abs_path, lang,
                    raw_bytes, new_hash, stat, chunker, embed_queue,
                    pending_edges, rel, now,
                )
            else:
                files_new += 1
                _insert_new_file(
                    conn, project.id, rel, abs_path, lang,
                    raw_bytes, new_hash, stat, chunker, embed_queue,
                    pending_edges, now,
                )

        # Drop files that are gone on disk.
        stale_paths = set(existing_files.keys()) - seen_paths
        files_deleted = len(stale_paths)
        for rel in stale_paths:
            file_id, _ = existing_files[rel]
            conn.execute(
                "DELETE FROM chunks_vec WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE file_id = ?)",
                (file_id,),
            )
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            # files cascade-deletes chunks AND file_edges (ON DELETE CASCADE)

        # Batch-embed just the chunks that were actually new/changed.
        if embed_queue:
            if verbose:
                print(f"embedding {len(embed_queue)} chunks...", flush=True)
            embedder = get_embedder()
            texts = [t for (_, t) in embed_queue]
            vectors = embedder.encode(texts)
            for (cid, _), vec in zip(embed_queue, vectors):
                conn.execute(
                    "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                    (cid, vec.tobytes()),
                )

        # Flush edges after the whole walk so forward references resolve
        # against the final files table. Unchanged files keep their
        # existing edges untouched — no work done for them.
        if pending_edges:
            edge_count = _flush_edges(conn, project.id, root, pending_edges)
            if verbose:
                print(
                    f"edges: {edge_count} across {len(pending_edges)} "
                    f"file(s) changed/added"
                )

        conn.execute(
            "UPDATE projects SET last_update = ? WHERE id = ?",
            (now, project.id),
        )

    update_counts(conn, project.id)

    if verbose:
        print(
            f"update: {files_new} new, {files_changed} changed, "
            f"{files_deleted} deleted; {len(embed_queue)} chunks re-embedded"
        )
    return project.id, len(seen_paths), len(embed_queue)


# ---------------------------------------------------------------------------
# Per-file helpers (update path)
# ---------------------------------------------------------------------------


def _insert_new_file(
    conn: Connection,
    project_id: int,
    rel: str,
    abs_path: Path,
    lang: str,
    raw_bytes: bytes,
    content_hash: str,
    stat,
    chunker,
    embed_queue: list[tuple[int, str]],
    pending_edges: list[tuple[int, str, str, list[Edge]]],
    now: float,
) -> None:
    """Full insertion path — same shape as build_project's inner loop."""
    raw_chunks = chunker.chunk(raw_bytes, abs_path)
    chunks = _apply_big_split(raw_chunks) if raw_chunks else []

    # File row always goes in, even for zero-chunk files. Prevents the
    # "perpetually new" loop where empty files get rescanned every update.
    conn.execute(
        "INSERT INTO files(project_id, rel_path, content_hash, mtime, "
        "size, lang, last_scanned) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, rel, content_hash, stat.st_mtime, stat.st_size, lang, now),
    )
    file_id = conn.last_insert_rowid()

    # Collect edges whether or not the file chunks. Edge resolution
    # happens in a later pass against the final files table.
    raw_edges = relations.extract_edges(raw_bytes, abs_path, lang)
    if raw_edges:
        pending_edges.append((file_id, rel, lang, raw_edges))

    if not chunks:
        return  # tracked, nothing to embed

    inserted_ids: list[int] = []
    for c in chunks:
        stored, chunk_hash, metadata_json = _prepare_chunk_row(c)
        parent_id = (
            inserted_ids[c.parent_idx] if c.parent_idx is not None else None
        )
        conn.execute(
            "INSERT INTO chunks(project_id, file_id, parent_id, sibling_order, "
            "kind, name, qualified_name, start_line, end_line, start_byte, end_byte, "
            "char_count, content_hash, stored_text, embedded_text, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id, file_id, parent_id, c.sibling_order,
                c.kind, c.name, c.qualified_name,
                c.start_line, c.end_line, c.start_byte, c.end_byte,
                len(stored), chunk_hash, stored, stored, metadata_json,
            ),
        )
        cid = conn.last_insert_rowid()
        inserted_ids.append(cid)
        embed_queue.append((cid, stored))


def _reindex_changed_file(
    conn: Connection,
    project_id: int,
    file_id: int,
    abs_path: Path,
    lang: str,
    raw_bytes: bytes,
    content_hash: str,
    stat,
    chunker,
    embed_queue: list[tuple[int, str]],
    pending_edges: list[tuple[int, str, str, list[Edge]]],
    rel: str,
    now: float,
) -> None:
    """Per-chunk diff: reuse embeddings where content_hash matches."""
    raw_chunks = chunker.chunk(raw_bytes, abs_path)
    new_chunks = _apply_big_split(raw_chunks)

    # Edges are recomputed wholesale for changed files — the file's bytes
    # changed, so every import statement is potentially different. The
    # later resolve-and-flush pass wipes prior edges before inserting.
    raw_edges = relations.extract_edges(raw_bytes, abs_path, lang)
    if raw_edges:
        pending_edges.append((file_id, rel, lang, raw_edges))
    else:
        # File changed and now has no imports — still need to wipe the
        # old edges. Do it now rather than in _flush_edges, which only
        # visits files with new raw_edges to insert.
        relations.wipe_file(conn, file_id)

    # Map existing chunks by content_hash. Dup hashes (two identical
    # functions in one file) stack into a list; pop off as we reuse.
    existing_by_hash: dict[str, list[int]] = {}
    for cid, ch in conn.execute(
        "SELECT id, content_hash FROM chunks WHERE file_id = ?",
        (file_id,),
    ).fetchall():
        existing_by_hash.setdefault(ch, []).append(cid)

    processed_ids: list[int] = []
    for c in new_chunks:
        stored, chunk_hash, metadata_json = _prepare_chunk_row(c)
        parent_id = (
            processed_ids[c.parent_idx] if c.parent_idx is not None else None
        )

        reused_id = None
        bucket = existing_by_hash.get(chunk_hash)
        if bucket:
            reused_id = bucket.pop()

        if reused_id is not None:
            # Keep embedding; only refresh positional + parent fields.
            conn.execute(
                "UPDATE chunks SET parent_id=?, sibling_order=?, "
                "start_line=?, end_line=?, start_byte=?, end_byte=?, "
                "name=?, qualified_name=?, metadata=? WHERE id=?",
                (
                    parent_id, c.sibling_order,
                    c.start_line, c.end_line, c.start_byte, c.end_byte,
                    c.name, c.qualified_name, metadata_json, reused_id,
                ),
            )
            processed_ids.append(reused_id)
        else:
            conn.execute(
                "INSERT INTO chunks(project_id, file_id, parent_id, sibling_order, "
                "kind, name, qualified_name, start_line, end_line, start_byte, end_byte, "
                "char_count, content_hash, stored_text, embedded_text, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, file_id, parent_id, c.sibling_order,
                    c.kind, c.name, c.qualified_name,
                    c.start_line, c.end_line, c.start_byte, c.end_byte,
                    len(stored), chunk_hash, stored, stored, metadata_json,
                ),
            )
            cid = conn.last_insert_rowid()
            processed_ids.append(cid)
            embed_queue.append((cid, stored))

    # Delete leftover existing chunks that weren't reused.
    orphan_ids = [cid for ids in existing_by_hash.values() for cid in ids]
    if orphan_ids:
        placeholders = ",".join("?" * len(orphan_ids))
        conn.execute(
            f"DELETE FROM chunks_vec WHERE chunk_id IN ({placeholders})",
            orphan_ids,
        )
        conn.execute(
            f"DELETE FROM chunks WHERE id IN ({placeholders})",
            orphan_ids,
        )

    # Refresh the files row with the new bytes-hash + mtime.
    conn.execute(
        "UPDATE files SET content_hash=?, mtime=?, size=?, last_scanned=? "
        "WHERE id=?",
        (content_hash, stat.st_mtime, stat.st_size, now, file_id),
    )


def _prepare_chunk_row(c: Chunk) -> tuple[str, str, str | None]:
    """Sanitize + compress + hash a chunk. Shared by all insert paths."""
    sanitized = scrub_text(c.text)
    stored = compress(sanitized)
    chunk_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    metadata_json = json.dumps(c.metadata) if c.metadata else None
    return stored, chunk_hash, metadata_json


def _version_mismatches(conn: Connection) -> list[str]:
    """Return meta keys whose stored value differs from current config.

    Returning an empty list means the index's chunker/model/schema are
    compatible with the code in this process and incremental update is
    safe. Any mismatch means chunks stored under an older chunker have
    stale semantics (different embeddings, different chunk boundaries).
    """
    wanted = {
        "schema_version":  config.SCHEMA_VERSION,
        "chunker_version": config.CHUNKER_VERSION,
        "embedding_model": config.MODEL,
    }
    return [k for k, v in wanted.items() if db.get_meta(conn, k) != v]


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

    Returns total edge rows inserted. :func:`relations.insert_edges` wipes
    each file's prior edges before inserting, so re-running this is
    idempotent.
    """
    index = relations.FileIndex.load(conn, project_id, root)
    # Populate Phase 2 side-maps (ansible roles_path, custom-module
    # scan, helm template-name map) AND Phase 3 project variables.
    # ``conn`` lets prepare() load the per-project variables table so
    # ``{{ var }}`` / ``${var.x}`` paths can resolve during the build.
    index.prepare(pending, conn=conn)
    total = 0
    for file_id, rel, lang, edges in pending:
        total += relations.insert_edges(conn, index, file_id, rel, lang, edges)
    return total


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
