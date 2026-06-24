"""Data portability: export, encrypted backup, and restore (requires unlock).

* GET  /api/export  — the user's data as plaintext JSON (their own data, theirs
  to take). Decrypted in-memory; never leaves the machine unless the user saves it.
* GET  /api/backup  — the raw encrypted DuckDB file (a complete, portable backup;
  it already contains the wrapped keys, so it restores with the same passphrase).
* POST /api/restore — stage an uploaded backup; it is validated then applied at
  the NEXT startup (swapping the live DB at runtime is unsafe).

Restore is allowed only when unlocked, or onto a fresh (uninitialized) install —
never onto an initialized-but-locked vault (that would let anyone overwrite it).
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import db, keyvault

router = APIRouter()

_RESTORE_MAX = 1024 * 1024 * 1024  # 1 GiB cap on an uploaded restore (bounded)
_RESTORE_CHUNK = 1024 * 1024  # 1 MiB read chunk while streaming an upload to disk
# B8: marker the real Desktop UI sets that the WebRTC bridge cannot forward. The
# bridge (webrtc_bridge.parse_request) filters peer headers to a tiny allowlist
# (content-type/accept/accept-language); anything else is dropped, so a bridged-in
# request cannot carry this header. See ``account._require_desktop_local`` for
# the matching helper used by the passphrase-reset endpoint.
_LOCAL_HEADER = "x-sb-local"


def _require_desktop_local(request: Request) -> None:
    """Refuse restore requests that arrived via the WebRTC bridge (remote device)."""
    assert request is not None, "request required"
    marker = request.headers.get(_LOCAL_HEADER)
    assert isinstance(marker, str) or marker is None, "header must be a string or absent"
    if marker != "1":
        raise HTTPException(status_code=403, detail="restore is Desktop-local only")


def _stores(request: Request):
    """Return app.state if unlocked, else raise 423."""
    state = request.app.state
    if getattr(state, "kb", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return state


@router.get("/api/export")
def export_data(request: Request) -> dict:
    """Return all user data as plaintext JSON (knowledge, chats, tasks, memory)."""
    state = _stores(request)
    knowledge = []
    for doc in state.kb.list_docs():  # bounded by the store's list limit
        full = state.kb.get(doc["id"])
        if full is not None:
            knowledge.append({"title": full["title"], "content": full["content"]})
    conversations = []
    for conv in state.history.list_conversations():  # bounded
        messages = state.history.get_messages(conv["id"])
        conversations.append({
            "title": conv.get("title", ""),
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        })
    return {
        "schema": "smartbrain-export-v1",
        "profile": state.memory.get_profile(),
        "memories": [m["text"] for m in state.memory.list_memories()],
        "tasks": [
            {"title": t["title"], "notes": t.get("notes", ""), "status": t["status"], "due_date": t.get("due_date")}
            for t in state.planner.list_tasks()
        ],
        "knowledge": knowledge,
        "conversations": conversations,
    }


def _cleanup_backup_temp(tmp: Path) -> None:
    """Unlink the backup temp file and its WAL sidecar (called after send/abort)."""
    assert isinstance(tmp, Path), "tmp must be a Path"
    assert tmp.name, "tmp path must have a filename"
    for leftover in (tmp, tmp.parent / (tmp.name + ".wal")):
        if leftover.exists():
            try:
                leftover.unlink()
            except OSError:
                pass  # best-effort cleanup; a leftover does not corrupt state


@router.get("/api/backup")
def backup_db(request: Request) -> FileResponse:
    """Download a complete, portable copy of the encrypted DuckDB as a backup.

    Uses COPY FROM DATABASE into a fresh file rather than CHECKPOINT+copy: it
    reads the committed snapshot without needing the (idle) per-thread cursors'
    transactions to finish, and yields a self-contained, restorable file. The
    file is streamed from disk via FileResponse and removed after the response
    is sent (or on abort), so the whole DB is never held in memory.
    """
    _stores(request)  # require unlock
    dbx = request.app.state.dbx
    source = dbx.execute("SELECT current_database();").fetchone()[0]
    assert source, "could not resolve the source database name"
    # current_database() is the DB filename stem, so it may contain hyphens/spaces;
    # quote it as an identifier (and the temp path as a string literal) so the COPY
    # statement is well-formed regardless of SMARTBRAIN_DB_PATH.
    source_id = '"' + str(source).replace('"', '""') + '"'
    alias = f"sb_backup_{uuid.uuid4().hex[:8]}"  # unique so a leaked attach can't collide
    parent = db.resolve_db_path().parent
    # NamedTemporaryFile gives us a unique path in the DB dir; close it
    # immediately so DuckDB can open the file itself (we own cleanup via the
    # background task on the response).
    handle = tempfile.NamedTemporaryFile(
        prefix=f"{alias}_", suffix=".duckdb", dir=str(parent), delete=False
    )
    tmp = Path(handle.name)
    handle.close()
    tmp.unlink()  # DuckDB ATTACH refuses to overwrite an existing file
    tmp_lit = str(tmp).replace("'", "''")
    try:
        with db.write_lock:  # quiesce other threads during the snapshot
            dbx.execute(f"ATTACH '{tmp_lit}' AS {alias};")
            try:
                dbx.execute(f"COPY FROM DATABASE {source_id} TO {alias};")
            finally:
                dbx.execute(f"DETACH {alias};")
    except Exception:
        _cleanup_backup_temp(tmp)
        raise
    assert tmp.exists() and tmp.stat().st_size > 0, "backup must produce a non-empty file"
    return FileResponse(
        path=str(tmp),
        media_type="application/octet-stream",
        filename="smartbrain-backup.duckdb",
        background=BackgroundTask(_cleanup_backup_temp, tmp),
    )


async def _stream_restore_to_disk(request: Request, dest: Path) -> int:
    """Stream an uploaded restore body to ``dest`` in bounded chunks; return size.

    Buffering the whole upload in RAM (``await request.body()``) puts up to a
    full GiB on the heap; instead we drain ``request.stream()`` straight into a
    temp file on the same volume as the live DB. The chunk loop is bounded:
    each non-empty iteration writes at least one byte, so a peer sending more
    than _RESTORE_MAX bytes trips the size guard before unbounded reads.
    """
    assert isinstance(dest, Path), "dest must be a Path"
    assert _RESTORE_CHUNK > 0, "chunk size must be positive"
    total = 0
    max_iters = _RESTORE_MAX // _RESTORE_CHUNK + 2  # NASA-rule fixed upper bound
    try:
        with dest.open("wb") as out:
            stream = request.stream()
            for _ in range(max_iters):
                try:
                    chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
                if not chunk:
                    continue  # empty keep-alive chunk; the loop bound still terminates us
                total += len(chunk)
                if total > _RESTORE_MAX:
                    raise HTTPException(status_code=400, detail="empty or oversized restore file")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    assert total >= 0, "streamed byte count cannot be negative"
    return total


@router.post("/api/restore")
async def restore_db(request: Request) -> dict:
    """Stage an uploaded backup (validated); it is applied at the next startup.

    Desktop-local only (B8): a request bridged in from a paired remote device
    is refused with 403. The upload is streamed to a temp file on the DB volume
    in bounded chunks (B17) rather than buffered in RAM, then validated, then
    promoted to the staged path.
    """
    _require_desktop_local(request)  # B8: refuse bridged-in requests
    conn = request.app.state.dbx
    initialized = keyvault.is_initialized(conn)
    unlocked = getattr(request.app.state, "kb", None) is not None
    if initialized and not unlocked:  # don't let a locked vault be overwritten
        raise HTTPException(status_code=423, detail="unlock first, or restore onto a fresh install")
    live_db = db.resolve_db_path()
    staged = db.staged_restore_path(live_db)
    # NamedTemporaryFile gives us a unique sibling path on the same volume so
    # the final rename to ``staged`` is atomic. We close the handle and unlink
    # the path immediately so our own ``open("wb")`` in _stream_restore_to_disk
    # owns it cleanly.
    handle = tempfile.NamedTemporaryFile(
        prefix="sb_restore_", suffix=".part", dir=str(staged.parent), delete=False
    )
    tmp = Path(handle.name)
    handle.close()
    tmp.unlink()
    written = await _stream_restore_to_disk(request, tmp)
    if written == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="empty or oversized restore file")
    if not db.is_smartbrain_db(tmp):  # reject anything that isn't a real backup
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="not a valid SmartBrain backup file")
    tmp.replace(staged)  # atomic promotion to the staged path (same volume)
    return {"ok": True, "message": "Backup staged — restart SmartBrain to apply it."}
