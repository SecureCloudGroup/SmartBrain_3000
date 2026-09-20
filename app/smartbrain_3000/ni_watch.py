"""NI oversight plane v0 — W-CREATE, the flow-creation watcher (round 8, G1).

Watchers observe execution-plane artifacts and emit typed, egress-inert
Findings; they never mutate flows directly. W-CREATE consumes flow records
across all items once per engine tick (budgeted, feeds-contract isolation)
and detects what no single flow can see about itself:

- **Stalled builds**: absorbs the M3 stale sweep — a worker that died leaves
  a non-terminal record; the sweep fails it honestly and W-CREATE files the
  finding so the pattern is visible, not just the instance.
- **Dead-end-law violations**: every terminal record must surface a reason
  plus a question or reopen affordance (ni_master.terminal_surface). A
  violation is a HIGH finding — the masking class, machine-detected.
- **Failure clustering**: the same error class across several cards in a day
  is a class break candidate (a provider changed, a stage regressed) — one
  WARN finding names it once, edge-triggered by the open-finding dedupe.

Findings are code-authored, host-free strings in a plaintext operational
table (``ni_findings``, migration 41) — same posture as ``last_status``.
Severity ``high`` additionally rides the NI carrier row (badge + /info).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from . import ni, ni_flow, ni_master

log = logging.getLogger("smartbrain.ni_watch")

SEVERITIES: frozenset[str] = frozenset({"info", "warn", "high"})
_MAX_FINDINGS = 500
_CLUSTER_MIN = 3
_CLUSTER_WINDOW_HOURS = 24


def file_finding(conn, watcher: str, severity: str, title: str,
                 item_id: str | None = None) -> str | None:
    """Insert one finding; dedupe on an identical OPEN finding (edge trigger).

    Returns the new finding id, or None when an open twin already exists.
    Titles are code-authored and host-free — never fetched or model text.
    """
    assert conn is not None and watcher and title, "args required"
    assert severity in SEVERITIES, f"severity must be one of {sorted(SEVERITIES)}"
    row = conn.execute(
        "SELECT id FROM ni_findings WHERE watcher=? AND title=? AND status='open' "
        "AND (item_id = ? OR (item_id IS NULL AND ? IS NULL)) LIMIT 1",
        [watcher, title, item_id, item_id],
    ).fetchone()
    if row is not None:
        return None
    fid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ni_findings (id, created_at, watcher, severity, title, item_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'open')",
        [fid, datetime.now(UTC), watcher, severity, title[:300], item_id],
    )
    # Bounded retention: keep the newest _MAX_FINDINGS rows.
    conn.execute(
        "DELETE FROM ni_findings WHERE id IN (SELECT id FROM ni_findings "
        "ORDER BY created_at DESC OFFSET ?)",
        [_MAX_FINDINGS],
    )
    return fid


def list_findings(conn, limit: int = 50, *, status: str | None = "open") -> list[dict]:
    """Newest-first findings for the Health surface (desktop-local route)."""
    assert conn is not None, "conn required"
    limit = max(1, min(int(limit), 200))
    where = "WHERE status = ?" if status else ""
    args: list = [status] if status else []
    rows = conn.execute(
        f"SELECT id, created_at, watcher, severity, title, item_id, status "
        f"FROM ni_findings {where} ORDER BY created_at DESC LIMIT ?",
        [*args, limit],
    ).fetchall()
    return [{"id": r[0], "created_at": r[1].isoformat() if r[1] else None,
             "watcher": r[2], "severity": r[3], "title": r[4],
             "item_id": r[5], "status": r[6]} for r in rows]


def resolve_finding(conn, finding_id: str) -> bool:
    """Mark one finding resolved (Health surface tap). Returns True if it existed."""
    assert conn is not None and finding_id, "args required"
    row = conn.execute(
        "UPDATE ni_findings SET status='resolved' "
        "WHERE id=? AND status='open' RETURNING id",
        [finding_id],
    ).fetchone()
    return row is not None


def watch_create(store: ni.NIStore) -> dict:
    """One W-CREATE pass. Returns a summary dict for the scheduler's log line.

    Bounded by ``ni._MAX_ITEMS``; every check wrapped so one bad record never
    stops the pass (feeds-contract isolation).
    """
    assert store is not None, "store required"
    conn = store.conn
    summary = {"swept": 0, "filed": 0, "high": 0}

    # 1. Stalled builds — the sweep acts, the watcher records the pattern.
    try:
        swept = ni_flow.sweep_stranded_flows(store)
        summary["swept"] = swept
        if swept and file_finding(
                conn, "create", "warn",
                f"{swept} card build(s) stalled and were stopped this pass"):
            summary["filed"] += 1
    except Exception as exc:  # isolation: sweep failure never stops the pass
        log.warning("ni_watch: stale sweep failed: %s", exc)

    # 2 + 3. Terminal-record audit: dead-end law + failure clustering.
    cutoff = datetime.now(UTC) - timedelta(hours=_CLUSTER_WINDOW_HOURS)
    clusters: dict[str, int] = {}
    try:
        items = store.list_items()
    except Exception as exc:
        log.warning("ni_watch: list_items failed: %s", exc)
        return summary
    for item in items:  # bounded by ni._MAX_ITEMS
        try:
            record = ni_flow._flow_read(store, item["id"])
            if record is None:
                continue
            state = str(record.get("state") or "")
            if state not in ("failed", "unsupported"):
                continue
            shell = bool(item["spec"].get("_shell"))
            surface = ni_master.terminal_surface(state, record, shell=shell)
            dead_end = not surface.get("reason") or not (
                surface.get("question") or surface.get("reopen"))
            if dead_end and file_finding(
                    conn, "create", "high",
                    "a finished-with-failure card offers no reason or way out",
                    item_id=item["id"]):
                summary["filed"] += 1
                summary["high"] += 1
            when = record.get("updated_at") or record.get("created_at")
            ts = None
            if isinstance(when, str):
                try:
                    ts = datetime.fromisoformat(when)
                except ValueError:
                    ts = None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts is None or ts >= cutoff:
                klass = ni_master.error_class_of(
                    record.get("error") if isinstance(record.get("error"), str) else None)
                if klass not in ("none", "declined"):
                    clusters[klass] = clusters.get(klass, 0) + 1
        except Exception as exc:  # one bad record never stops the audit
            log.warning("ni_watch: record audit failed for %s: %s",
                        item.get("id"), exc)
    for klass, count in sorted(clusters.items()):
        if count >= _CLUSTER_MIN and file_finding(
                conn, "create", "warn",
                f"{count} cards hit the same '{klass}' failure in "
                f"the last {_CLUSTER_WINDOW_HOURS}h — possible class break"):
            summary["filed"] += 1
    return summary
