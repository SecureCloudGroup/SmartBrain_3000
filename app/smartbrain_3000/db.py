"""Embedded DuckDB data layer for SmartBrain_3000.

DuckDB is in-process (there is no database server). The file lives on a local
volume so state persists across container restarts. Migrations are an ordered,
append-only list with a fixed upper bound.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = "/app/data/smartbrain.duckdb"

# Ordered, append-only migrations: (id, SQL). Never edit a shipped migration —
# add a new one. The fixed tuple gives a verifiable upper bound.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"),
    (
        2,
        "CREATE TABLE IF NOT EXISTS documents ("
        "id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Per-document embedding vector for semantic search. The vector is
        # AES-256-GCM encrypted at rest (it can leak content); dim + model are
        # plaintext recall metadata (not security-sensitive). No FK — integrity
        # is maintained in code (KnowledgeBase.delete cascades).
        3,
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "doc_id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "dim INTEGER NOT NULL, model TEXT NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Chat history. A conversation's {title} and a message's {role, content}
        # are AES-256-GCM encrypted at rest (AAD domain-separated from documents/
        # embeddings). conversation_id is plaintext for querying; no FK — the
        # ChatHistory.delete cascade maintains integrity in code.
        4,
        "CREATE TABLE IF NOT EXISTS conversations ("
        "id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        5,
        "CREATE TABLE IF NOT EXISTS messages ("
        "id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Memory facts the assistant should remember; {text} encrypted at rest.
        6,
        "CREATE TABLE IF NOT EXISTS memories ("
        "id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Singleton identity profile (id is always 1); {assistant_name,
        # user_name, instructions} encrypted at rest.
        7,
        "CREATE TABLE IF NOT EXISTS profile ("
        "id INTEGER PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Planner tasks. {title, notes} encrypted at rest; status + due_date are
        # plaintext metadata so the UI can group Today/Week without decrypting.
        8,
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', due_date DATE, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Append-only audit log of tool attempts/decisions. Metadata plaintext
        # for the Activity view; {args_summary, result_summary, error} encrypted.
        9,
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "id TEXT PRIMARY KEY, ts TIMESTAMP DEFAULT current_timestamp, "
        "actor TEXT NOT NULL, tool_name TEXT NOT NULL, tier TEXT NOT NULL, "
        "decision TEXT NOT NULL, ok BOOLEAN NOT NULL, conversation_id TEXT, "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL);",
    ),
    (
        # Pending approvals for REVIEWED/IRREVERSIBLE tool calls. The full args +
        # session id are in the encrypted body (AAD pending:<id>); tool/tier/status
        # plaintext for the tiles. status: pending->approved->executed | ->denied.
        10,
        "CREATE TABLE IF NOT EXISTS pending_actions ("
        "id TEXT PRIMARY KEY, turn_id TEXT, conversation_id TEXT, tool_call_id TEXT, "
        "tool_name TEXT NOT NULL, tier TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, resolved_at TIMESTAMP);",
    ),
    (
        # Scheduled agent prompts. {title, prompt, model} encrypted; cadence
        # metadata (enabled/interval/next_run) plaintext so the runner can query
        # due rows without decrypting. Fires only while the vault is unlocked.
        11,
        "CREATE TABLE IF NOT EXISTS schedules ("
        "id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "enabled BOOLEAN NOT NULL DEFAULT true, interval_minutes INTEGER NOT NULL DEFAULT 0, "
        "next_run TIMESTAMP NOT NULL, last_run TIMESTAMP, "
        "created_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    (
        # Per-call token usage for the cost view. No content — just the model and
        # token counts (plaintext metadata, like audit_log) so spend can be summed
        # in SQL. Cloud cost is computed from the live catalog pricing; local = $0.
        12,
        "CREATE TABLE IF NOT EXISTS usage_log ("
        "id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT current_timestamp, "
        "model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0, "
        "completion_tokens INTEGER NOT NULL DEFAULT 0);",
    ),
    # Chunked embeddings: one vector PER CHUNK (not per doc) so a long document is
    # fully searchable, not just its head. Embeddings are derived data (regenerated by
    # /api/kb/reindex), so we drop the old per-doc table and recreate it keyed by
    # (doc_id, chunk_idx). Existing installs re-run reindex to backfill.
    (13, "DROP TABLE IF EXISTS embeddings;"),
    (
        14,
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "doc_id TEXT NOT NULL, chunk_idx INTEGER NOT NULL, "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "dim INTEGER NOT NULL, model TEXT NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "PRIMARY KEY (doc_id, chunk_idx));",
    ),
    # Richer planner tasks: priority + due time + recurrence are low-sensitivity plaintext
    # metadata (like status/due_date) so the UI can sort/group without decrypting. Tags live
    # in the encrypted body. Added as nullable/defaulted columns so existing tasks are valid.
    (15, "ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'medium';"),
    (16, "ALTER TABLE tasks ADD COLUMN due_time TEXT;"),
    (17, "ALTER TABLE tasks ADD COLUMN recur TEXT DEFAULT 'none';"),
    # Scheduled-run history: a timer-fired briefing's output was previously discarded, so the
    # user could never read it. Each run records plaintext metadata (schedule_id, ran_at,
    # status) plus the encrypted body (the assistant's message/error) so output is readable.
    (
        18,
        "CREATE TABLE IF NOT EXISTS schedule_runs ("
        "id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, "
        "ran_at TIMESTAMP DEFAULT current_timestamp, status TEXT NOT NULL, "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL);",
    ),
    # "Seen" flag driving the Scheduled-updates chat feed + nav badge. Plaintext metadata
    # (like status/ran_at) so the unseen COUNT is queryable without decryption. DEFAULT true
    # so pre-existing run history doesn't spam the badge on upgrade; record_run inserts new
    # runs as seen=false explicitly, so only genuinely-new output is counted as unseen.
    (19, "ALTER TABLE schedule_runs ADD COLUMN seen BOOLEAN DEFAULT true;"),
    # Vaults: a named, selectable SUBSET of the knowledge base — the unit you scope a search to,
    # and (next) the unit you export and share. A vault's name and description are encrypted,
    # because what you called a collection ("Divorce", "Cancer treatment") reveals as much as the
    # documents in it. `kind` and `version` stay plaintext: they are low-sensitivity and the UI
    # filters/sorts on them without decrypting, exactly like tasks.status and schedule_runs.seen.
    #   kind='local'    — you authored it (yours to edit and export)
    #   kind='imported' — it came from someone else's vault (replaceable by an update from source)
    (
        20,
        "CREATE TABLE IF NOT EXISTS vaults ("
        "id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'local', "
        "version INTEGER NOT NULL DEFAULT 1, "
        "nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, "
        "created_at TIMESTAMP DEFAULT current_timestamp, "
        "updated_at TIMESTAMP DEFAULT current_timestamp);",
    ),
    # Membership is MANY-TO-MANY: one document can belong to several vaults (a lease belongs in
    # both "Property" and "2026 taxes"), so membership cannot be a column on documents.
    (
        21,
        "CREATE TABLE IF NOT EXISTS vault_documents ("
        "vault_id TEXT NOT NULL, doc_id TEXT NOT NULL, "
        "added_at TIMESTAMP DEFAULT current_timestamp, "
        "PRIMARY KEY (vault_id, doc_id));",
    ),
    # Who OWNS a vault member. 'import' = the document came from someone else's vault, so a later
    # update from that vault may replace it. 'owner' = the user's own document (it merely also sits
    # in this vault), so a vault update must NEVER clobber it. Plaintext because it is a permission
    # bit the API must check on every rename/delete, and it says nothing about content.
    (22, "ALTER TABLE vault_documents ADD COLUMN origin TEXT DEFAULT 'owner';"),
)

# The newest migration this build knows how to apply. A database recording a
# migration id beyond this was written by a NEWER app version; opening it with
# this (older) code could silently drop/corrupt columns it doesn't know about,
# so we refuse rather than risk it (see run_migrations / is_future_schema_db).
NEWEST_MIGRATION = max(mid for mid, _ in _MIGRATIONS)


def resolve_db_path() -> Path:
    """Return the DuckDB file path from the environment (with a default)."""
    raw = os.environ.get("SMARTBRAIN_DB_PATH", DEFAULT_DB_PATH)
    assert raw, "database path must be non-empty"
    path = Path(raw)
    assert path.suffix == ".duckdb", "database path must end in .duckdb"
    return path


def open_db(path: Path) -> duckdb.DuckDBPyConnection:
    """Open the embedded DuckDB at `path`, creating parent dirs if needed."""
    assert isinstance(path, Path), "path must be a Path"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    assert conn is not None, "duckdb.connect must return a connection"
    return conn


class ThreadLocalConn:
    """Thread-safe ``.execute`` facade over one DuckDB connection.

    A DuckDB connection is NOT safe to use from multiple threads at once: it has
    a single result set, so one thread's ``execute`` clobbers another thread's
    pending fetch (verified: heavy result corruption + None crashes under load).
    FastAPI dispatches sync routes on a threadpool, so stores sharing one
    connection would corrupt each other. This facade hands each thread its own
    cursor (DuckDB's documented multi-thread pattern), created lazily from the
    root connection; stores call ``.execute`` exactly as before. The root is used
    only to spawn cursors (and for single-threaded startup/shutdown).

    Per-thread cursors are bounded by Starlette's threadpool size (a small, fixed
    upper bound) and are NOT explicitly closed — they're released when the thread
    exits at process shutdown. This is intentional: cursors are cheap, the pool
    is bounded, and an explicit close-on-thread-exit hook would add complexity
    without a measurable benefit.
    """

    def __init__(self, root: duckdb.DuckDBPyConnection) -> None:
        assert root is not None, "root connection required"
        self._root = root
        self._local = threading.local()

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """Return this thread's private cursor, creating it once on first use."""
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._root.cursor()  # an independent connection sharing the DB
            assert cursor is not None, "cursor creation must succeed"
            self._local.cursor = cursor
        return cursor

    def execute(self, sql: str, parameters: list | None = None):
        """Run a statement on this thread's cursor — never the shared root."""
        assert sql, "sql required"
        cursor = self._cursor()
        assert cursor is not None, "thread cursor must exist"
        return cursor.execute(sql, parameters) if parameters is not None else cursor.execute(sql)

    def fetchone(self):
        """Fetch one row from this thread's cursor (delegates to the cursor)."""
        cursor = self._cursor()
        assert cursor is not None, "thread cursor must exist"
        return cursor.fetchone()

    def fetchall(self):
        """Fetch all rows from this thread's cursor (delegates to the cursor)."""
        cursor = self._cursor()
        assert cursor is not None, "thread cursor must exist"
        return cursor.fetchall()

    def commit(self) -> None:
        """Commit on this thread's cursor (DuckDB autocommits; explicit is safe)."""
        cursor = self._cursor()
        assert cursor is not None, "thread cursor must exist"
        cursor.commit()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """Return this thread's cursor (each thread sees its own)."""
        cur = self._cursor()
        assert cur is not None, "thread cursor must exist"
        return cur

    def close(self) -> None:
        """Close the root connection (shutdown path; not per-thread)."""
        assert self._root is not None, "root connection required"
        self._root.close()


# Process-wide lock that SERIALIZES backup snapshots against each other — only
# data_routes.backup_db acquires it. It does NOT quiesce store writes (those
# paths never take it); DuckDB's MVCC gives ``COPY FROM DATABASE`` a consistent
# committed snapshot on its own. The lock exists so two concurrent /api/backup
# calls can't race on the ATTACH alias / temp file, not for write consistency.
write_lock = threading.Lock()


_RESTORE_SUFFIX = ".restore"      # an uploaded backup staged to apply on next boot
_PRERESTORE_SUFFIX = ".pre-restore"  # the displaced DB, kept so a restore is reversible


def staged_restore_path(db_path: Path) -> Path:
    """Path where an uploaded restore is staged (applied at next startup)."""
    assert isinstance(db_path, Path), "path must be a Path"
    return db_path.parent / (db_path.name + _RESTORE_SUFFIX)


def is_smartbrain_db(path: Path) -> bool:
    """True if `path` is a DuckDB file holding our key_wraps table (a real backup)."""
    assert isinstance(path, Path), "path must be a Path"
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        probe = duckdb.connect(str(path), read_only=True)
    except Exception:  # not a DuckDB file / unreadable
        return False
    try:
        row = probe.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'key_wraps';"
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        probe.close()


def is_future_schema_db(path: Path) -> bool:
    """True if the DuckDB at `path` records a migration newer than this build knows.

    Used to reject a backup taken on a NEWER app version at restore time (and as
    defense-in-depth alongside run_migrations' boot guard), so a forward-version
    vault is never opened by older code that could corrupt it. Unreadable / no
    schema_migrations -> False (is_smartbrain_db handles the not-a-backup case).
    """
    assert isinstance(path, Path), "path must be a Path"
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        probe = duckdb.connect(str(path), read_only=True)
    except Exception:  # not a DuckDB file / unreadable
        return False
    try:
        has = probe.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations';"
        ).fetchone()
        if has is None:
            return False
        row = probe.execute("SELECT MAX(id) FROM schema_migrations;").fetchone()
        seen_max = int(row[0]) if row is not None and row[0] is not None else 0
        return seen_max > NEWEST_MIGRATION
    except Exception:
        return False
    finally:
        probe.close()


def _unique_sibling(path: Path, suffix: str) -> Path:
    """A unique sibling ``<name><suffix>-<UTC stamp>`` — never clobbers a prior copy.

    Restore safety copies (the displaced live DB, a quarantined bad upload) used to
    reuse a fixed name, so a second restore overwrote the first one's copy. Stamping
    each with a microsecond UTC timestamp keeps every copy, so no recoverable data is
    silently discarded by a rapid or repeated restore (they accumulate; an operator
    can delete them once a restore is confirmed good).
    """
    assert isinstance(path, Path), "path must be a Path"
    assert suffix, "suffix required"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return path.parent / (path.name + suffix + "-" + stamp)


def apply_pending_restore(db_path: Path) -> bool:
    """If a valid restore is staged, swap it in before the DB is opened.

    The currently-open DB must NOT be in use when this runs (call it at startup
    before ``open_db``). The displaced DB is kept at ``*.pre-restore-<stamp>`` so the
    operation is reversible (and a second restore never overwrites an earlier copy);
    an invalid staged file is quarantined to ``*.invalid-<stamp>``, a future-schema
    one to ``*.future-<stamp>`` — neither is ever applied.
    """
    assert isinstance(db_path, Path), "path must be a Path"
    staged = staged_restore_path(db_path)
    if not staged.exists():
        return False
    if not is_smartbrain_db(staged):
        staged.replace(_unique_sibling(staged, ".invalid"))  # quarantine, don't brick
        return False
    if is_future_schema_db(staged):
        # A backup from a NEWER app version: swapping it in would displace the original
        # and then brick boot at run_migrations (the forward-compat guard). Quarantine it
        # and leave the live DB untouched — the /api/restore endpoint refuses these too;
        # this is the defense for one staged on a newer build then opened by an older image.
        staged.replace(_unique_sibling(staged, ".future"))
        return False
    if db_path.exists():
        pre = _unique_sibling(db_path, _PRERESTORE_SUFFIX)
        db_path.replace(pre)
        # Keep the displaced original's WAL WITH its rollback copy: it may hold
        # committed-but-not-checkpointed transactions (likely if the prior process
        # exited uncleanly — the exact case a user reaches for restore), so the
        # pre-restore snapshot must travel with it to stay complete and reversible.
        orig_wal = db_path.parent / (db_path.name + ".wal")
        if orig_wal.exists():
            orig_wal.replace(pre.parent / (pre.name + ".wal"))
    # Any WAL still at db_path.wal belongs to nothing being swapped in (a
    # COPY-FROM-DATABASE backup is a single file, no WAL); drop it so it can't be
    # mis-applied to the freshly restored DB.
    stray_wal = db_path.parent / (db_path.name + ".wal")
    if stray_wal.exists():
        stray_wal.unlink()
    staged.replace(db_path)
    return True


def run_migrations(conn: duckdb.DuckDBPyConnection) -> int:
    """Apply any unapplied migrations in order; return the count applied."""
    assert conn is not None, "connection must be open"
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp);"
    )
    # Forward-compat guard: refuse to open a database written by a NEWER app
    # version (a migration id beyond what this build knows). Applying this older
    # code to a future schema could drop/corrupt columns it has never heard of,
    # so fail loudly — prompting an app upgrade — instead of silently risking data.
    row = conn.execute("SELECT MAX(id) FROM schema_migrations;").fetchone()
    seen_max = int(row[0]) if row is not None and row[0] is not None else 0
    if seen_max > NEWEST_MIGRATION:
        raise RuntimeError(
            f"database schema v{seen_max} is newer than this app (v{NEWEST_MIGRATION}); "
            "upgrade SmartBrain_3000 — refusing to open to avoid data loss"
        )
    applied = 0
    for migration_id, sql in _MIGRATIONS:  # fixed, bounded list
        seen = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?;", [migration_id]
        ).fetchone()
        if seen is not None:
            continue
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?);", [migration_id])
        applied += 1
    assert applied <= len(_MIGRATIONS), "applied count cannot exceed migrations"
    return applied


def meta_get(conn: duckdb.DuckDBPyConnection, key: str) -> str | None:
    """Return the stored value for `key`, or None if absent."""
    assert key, "meta key must be non-empty"
    row = conn.execute("SELECT value FROM meta WHERE key = ?;", [key]).fetchone()
    assert row is None or len(row) == 1, "unexpected meta row shape"
    return None if row is None else str(row[0])


def meta_set(conn: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    """Insert or update a meta key/value pair."""
    assert key, "meta key must be non-empty"
    assert value is not None, "meta value must not be None"
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value;",
        [key, value],
    )


def record_boot(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Ensure install identity exists, increment boot_count, return status.

    ``desktop_routing_id`` is a dedicated random id used as the WebRTC broker routing
    key — separate from ``install_id`` so the routing key is NOT the install identity
    that ``/api/status`` exposes (the status route omits this field). (Arch H6)
    """
    assert conn is not None, "connection must be open"
    install_id = meta_get(conn, "install_id")
    if install_id is None:
        install_id = str(uuid.uuid4())
        meta_set(conn, "install_id", install_id)
        meta_set(conn, "first_seen", datetime.now(timezone.utc).isoformat())
    routing_id = meta_get(conn, "desktop_routing_id")
    if routing_id is None:
        routing_id = str(uuid.uuid4())
        meta_set(conn, "desktop_routing_id", routing_id)
    count = int(meta_get(conn, "boot_count") or "0") + 1
    meta_set(conn, "boot_count", str(count))
    first_seen = meta_get(conn, "first_seen")
    assert first_seen is not None, "first_seen must be set after first boot"
    return {
        "install_id": install_id,
        "desktop_routing_id": routing_id,
        "first_seen": first_seen,
        "boot_count": str(count),
    }
