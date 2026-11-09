"""A small SQLite layer. WAL mode, one connection, guarded by a lock.

Calls are short and synchronous. From async code, wrap a call in
asyncio.to_thread if it ever shows up as slow.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from guardian.clock import Clock, SystemClock, to_iso, uid_stamp

# Columns on `incidents` that update_incident is allowed to set.
_INCIDENT_COLUMNS = {
    "event_type", "source", "severity", "start_time_utc", "first_trigger_utc",
    "last_trigger_utc", "end_time_utc", "print_filename", "print_progress",
    "active_toolhead", "diagnostic_summary", "evidence_level", "recording_path",
    "recording_status", "recording_notes", "actual_pre_s", "actual_post_s",
    "protected", "user_note",
}


class Database:
    def __init__(self, path: Path | str, clock: Clock | None = None) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because we guard access with our own lock.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        sql = resources.files("guardian.storage").joinpath("schema.sql").read_text("utf-8")
        with self._lock, self._conn:
            self._conn.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- incidents ----

    def next_uid(self, first_trigger_utc) -> str:
        """Make a uid like 2026-11-02T13-05-22Z_0001. The number is the next row id."""
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM incidents").fetchone()
        return f"{uid_stamp(first_trigger_utc)}_{row['n']:04d}"

    def create_incident(self, **fields: Any) -> int:
        """Insert an incident. Required: uid, event_type, source, severity, and the three
        time columns start_time_utc, first_trigger_utc, last_trigger_utc."""
        unknown = set(fields) - _INCIDENT_COLUMNS - {"uid"}
        if unknown:
            raise ValueError(f"unknown incident columns: {sorted(unknown)}")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO incidents ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            return int(cur.lastrowid)

    def update_incident(self, incident_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - _INCIDENT_COLUMNS
        if unknown:
            raise ValueError(f"unknown incident columns: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE incidents SET {sets} WHERE id = ?", (*fields.values(), incident_id)
            )

    def get_incident(self, incident_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()

    def get_incident_by_uid(self, uid: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM incidents WHERE uid = ?", (uid,)).fetchone()

    def list_incidents(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM incidents"
        args: tuple = ()
        if status:
            sql += " WHERE recording_status = ?"
            args = (status,)
        sql += " ORDER BY first_trigger_utc, id"
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def query_incidents(self, *, event_type: str | None = None, severity: str | None = None,
                        since: str | None = None, until: str | None = None, q: str | None = None,
                        limit: int = 50, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
        """Filtered list, newest first. Returns (rows, total count). All values are bound
        parameters, never pasted into the SQL."""
        where, args = [], []
        if event_type:
            where.append("event_type = ?"); args.append(event_type)
        if severity:
            where.append("severity = ?"); args.append(severity)
        if since:
            where.append("first_trigger_utc >= ?"); args.append(since)
        if until:
            where.append("first_trigger_utc <= ?"); args.append(until)
        if q:
            escaped = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            where.append("print_filename LIKE ? ESCAPE '!'"); args.append(f"%{escaped}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) AS n FROM incidents{clause}", args).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM incidents{clause} ORDER BY first_trigger_utc DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset)).fetchall()
        return rows, int(total)

    def delete_incident(self, incident_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))

    def list_toolchanges(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM toolchange_records ORDER BY started_utc DESC LIMIT ?", (limit,)).fetchall()

    # ---- incident events ----

    def add_event(
        self,
        incident_id: int,
        *,
        ts_utc,
        event_type: str,
        source: str,
        severity: str | None = None,
        is_trigger: bool = False,
        repeat_count: int = 1,
        printer_ts: float | None = None,
        metadata: dict | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO incident_events (incident_id, ts_utc, printer_ts, event_type, source,"
                " severity, is_trigger, repeat_count, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    incident_id, to_iso(ts_utc), printer_ts, event_type, source, severity,
                    int(is_trigger), repeat_count,
                    json.dumps(metadata, sort_keys=True) if metadata is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def bump_event_repeat(self, event_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE incident_events SET repeat_count = repeat_count + 1 WHERE id = ?",
                (event_id,),
            )

    def incident_of_event(self, event_id: int) -> sqlite3.Row:
        """The incident row that owns an event."""
        with self._lock:
            return self._conn.execute(
                "SELECT i.* FROM incidents i JOIN incident_events e ON e.incident_id = i.id"
                " WHERE e.id = ?", (event_id,)
            ).fetchone()

    def list_events(self, incident_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY ts_utc, id",
                (incident_id,),
            ).fetchall()

    # ---- notification log ----

    def log_notification(self, incident_id: int | None, ts_utc: str, channel: str, kind: str,
                         status: str, attempts: int, error: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO notification_log (incident_id, ts_utc, channel, kind, status, attempts, error)"
                " VALUES (?,?,?,?,?,?,?)", (incident_id, ts_utc, channel, kind, status, attempts, error))

    def list_notifications(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT n.*, i.uid AS incident_uid FROM notification_log n"
                " LEFT JOIN incidents i ON i.id = n.incident_id ORDER BY n.id DESC LIMIT ?",
                (limit,)).fetchall()

    # ---- logs ----

    def log(self, component: str, severity: str, message: str, metadata: dict | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO system_logs (ts_utc, component, severity, message, metadata)"
                " VALUES (?,?,?,?,?)",
                (
                    to_iso(self._clock.now_utc()), component, severity, message,
                    json.dumps(metadata, sort_keys=True) if metadata is not None else None,
                ),
            )
