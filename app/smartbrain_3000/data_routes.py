"""Data portability: export, encrypted backup, and restore (requires unlock).

* POST /api/export  — the user's data as plaintext JSON (their own data, theirs
  to take). Decrypted in-memory; never leaves the machine unless the user saves it.
* POST /api/backup  — the raw encrypted DuckDB file (a complete, portable backup;
  it already contains the wrapped keys, so it restores with the same passphrase).
* POST /api/restore — stage an uploaded backup; it is validated then applied at
  the NEXT startup (swapping the live DB at runtime is unsafe).

Export and backup are sensitive egress (whole-vault file / decrypted plaintext), so
both are Desktop-local only (the WebRTC bridge can't reach them) AND require the
passphrase (or Recovery Key) to be re-entered. Restore is allowed only when unlocked,
or onto a fresh (uninitialized) install — never onto an initialized-but-locked vault.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import db, keyvault

router = APIRouter()


class ReauthRequest(BaseModel):
    """Credential re-entered to authorize a sensitive data egress (backup/export)."""

    passphrase: str | None = None
    recovery_key: str | None = None

_RESTORE_MAX = 1024 * 1024 * 1024  # 1 GiB cap on an uploaded restore (bounded)
_RESTORE_CHUNK = 1024 * 1024  # 1 MiB read chunk while streaming an upload to disk
# B8: marker the real Desktop UI sets that the WebRTC bridge cannot forward. The
# bridge (webrtc_bridge.parse_request) filters peer headers to a tiny allowlist
# (content-type/accept/accept-language); anything else is dropped, so a bridged-in
# request cannot carry this header. See ``account._require_desktop_local`` for
# the matching helper used by the passphrase-reset endpoint.
_LOCAL_HEADER = "x-sb-local"


def _require_desktop_local(request: Request) -> None:
    """Refuse requests that arrived via the WebRTC bridge (guards export/backup/restore)."""
    assert request is not None, "request required"
    marker = request.headers.get(_LOCAL_HEADER)
    assert isinstance(marker, str) or marker is None, "header must be a string or absent"
    if marker != "1":
        raise HTTPException(status_code=403, detail="this endpoint is Desktop-local only")


def _stores(request: Request):
    """Return app.state if unlocked, else raise 423."""
    state = request.app.state
    if getattr(state, "kb", None) is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return state


def _reauthorize(request: Request, body: ReauthRequest) -> None:
    """Re-verify the user's passphrase (or Recovery Key) before a sensitive egress.

    Backup hands out the whole vault file and export hands out DECRYPTED plaintext,
    so — beyond being Desktop-local and unlocked — both require re-entering the
    passphrase. This blocks a passer-by at an unattended-but-unlocked Desktop and
    a stale paired session from silently exfiltrating everything in one click. A
    Recovery Key is accepted too, so a user who unlocked via the Kit isn't stranded.
    """
    conn = request.app.state.dbx  # per-thread cursor facade (thread-safe under the pool)
    try:
        if body.passphrase:
            keyvault.unlock(conn, body.passphrase)
        elif body.recovery_key:
            keyvault.unlock_with_recovery(conn, body.recovery_key)
        else:
            raise HTTPException(status_code=400, detail="passphrase required")
    except HTTPException:
        raise
    except Exception:  # wrong passphrase / bad recovery key / malformed wrap
        raise HTTPException(status_code=401, detail="incorrect passphrase") from None


@router.post("/api/export")
def export_data(request: Request, body: ReauthRequest) -> dict:
    """Return all user data as plaintext JSON (knowledge, chats, tasks, memory).

    Desktop-local + unlocked + passphrase re-entry (see ``_reauthorize``): this is
    the single most sensitive read in the app — every authored value, decrypted.
    """
    _require_desktop_local(request)  # never expose decrypted data to a bridged remote device
    state = _stores(request)
    _reauthorize(request, body)
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


@router.post("/api/backup")
def backup_db(request: Request, body: ReauthRequest) -> FileResponse:
    """Download a complete, portable copy of the encrypted DuckDB as a backup.

    Desktop-local + unlocked + passphrase re-entry (see ``_reauthorize``): the
    backup file carries the wrapped keys + all ciphertext, so handing it out is a
    whole-vault egress and must be re-authorized just like export.

    Uses COPY FROM DATABASE into a fresh file rather than CHECKPOINT+copy: it
    reads the committed snapshot without needing the (idle) per-thread cursors'
    transactions to finish, and yields a self-contained, restorable file. The
    file is streamed from disk via FileResponse and removed after the response
    is sent (or on abort), so the whole DB is never held in memory.
    """
    _require_desktop_local(request)  # never hand the whole vault file to a bridged remote device
    _stores(request)  # require unlock
    _reauthorize(request, body)
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
        with db.write_lock:  # serialize concurrent backups; COPY reads a consistent MVCC snapshot
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
                    raise HTTPException(status_code=413, detail="restore file too large (max 1 GiB)")
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
        raise HTTPException(status_code=400, detail="empty restore file")
    if not db.is_smartbrain_db(tmp):  # reject anything that isn't a real backup
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="not a valid SmartBrain backup file")
    if db.is_future_schema_db(tmp):  # reject a backup from a NEWER app version (forward-compat guard)
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="this backup is from a newer version of SmartBrain — upgrade this app, then restore",
        )
    tmp.replace(staged)  # atomic promotion to the staged path (same volume)
    return {"ok": True, "message": "Backup staged — restart SmartBrain to apply it."}
