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


# Groups for the incident list filter. A trailing * means "starts with".
INCIDENT_GROUPS = {
    "toolchange": ["toolchange_*"],
    "printer": ["print_paused_with_exception", "print_paused", "print_error", "print_cancelled", "printer_exception",
                "klipper_error_response", "klippy_*", "filament_feed_error", "heater_*", "print_stalled"],
    "camera": ["bed_not_clear", "spaghetti_detected", "residue_detected", "dirty_nozzle_detected"],
    "ai": ["ai_*"],
    "manual": ["manual"],
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
                        group: str | None = None,
                        limit: int = 50, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
        """Filtered list, newest first. Returns (rows, total count). All values are bound
        parameters, never pasted into the SQL."""
        where, args = [], []
        if event_type:
            where.append("event_type = ?"); args.append(event_type)
        if group:
            types = INCIDENT_GROUPS.get(group)
            if types is None:
                raise ValueError(f"unknown group {group!r}")
            prefixes = [t[:-1] for t in types if t.endswith("*")]
            exact = [t for t in types if not t.endswith("*")]
            parts = [f"event_type IN ({','.join('?' * len(exact))})"] if exact else []
            args += exact
            for pre in prefixes:
                parts.append("event_type LIKE ? ESCAPE '!'"); args.append(pre.replace("_", "!_") + "%")
            where.append("(" + " OR ".join(parts) + ")")
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

    def list_toolchanges(self, limit: int = 100, problems_only: bool = False) -> list[sqlite3.Row]:
        where = " WHERE result IS NOT NULL AND result NOT IN ('ok', 'park_only')" if problems_only else ""
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM toolchange_records{where} ORDER BY started_utc DESC, id DESC LIMIT ?",
                (limit,)).fetchall()

    _TOOLCHANGE_FIELDS = frozenset({
        "print_filename", "requested_tool", "previous_tool", "actual_tool", "started_utc", "ended_utc",
        "duration_ms", "result", "transitions", "error", "incident_id"})

    def create_toolchange(self, **fields: Any) -> int:
        bad = set(fields) - self._TOOLCHANGE_FIELDS
        if bad:
            raise ValueError(f"unknown toolchange fields: {sorted(bad)}")
        cols = sorted(fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO toolchange_records ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [fields[c] for c in cols])
            return int(cur.lastrowid)

    def update_toolchange(self, record_id: int, **fields: Any) -> None:
        bad = set(fields) - self._TOOLCHANGE_FIELDS
        if bad:
            raise ValueError(f"unknown toolchange fields: {sorted(bad)}")
        if not fields:
            return
        cols = sorted(fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE toolchange_records SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [*(fields[c] for c in cols), record_id])

    def recent_ok_toolchange_ms(self, limit: int = 200) -> list[int]:
        """Durations of the most recent good tool changes, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT duration_ms FROM toolchange_records WHERE result = 'ok' AND duration_ms IS NOT NULL"
                " ORDER BY started_utc DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [int(r["duration_ms"]) for r in rows]

    def recent_ok_pair_ms(self, previous: str | None, requested: str, limit: int = 60) -> list[int]:
        """Durations of good changes between these two tools, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT duration_ms FROM toolchange_records WHERE result = 'ok' AND duration_ms IS NOT NULL"
                " AND requested_tool = ? AND previous_tool IS ? ORDER BY started_utc DESC, id DESC LIMIT ?",
                (requested, previous, limit)).fetchall()
        return [int(r["duration_ms"]) for r in rows]

    def toolchange_summary(self, points: int = 500) -> dict[str, Any]:
        """Numbers for the Tool changes page: the last print's changes, counts and chart points."""
        with self._lock:
            last = self._conn.execute(
                "SELECT print_filename FROM toolchange_records WHERE print_filename IS NOT NULL"
                " ORDER BY started_utc DESC, id DESC LIMIT 1").fetchone()
            name = last["print_filename"] if last else None
            if name:
                rows = self._conn.execute(
                    "SELECT started_utc, previous_tool, requested_tool, duration_ms, result FROM toolchange_records"
                    " WHERE print_filename = ? ORDER BY started_utc DESC, id DESC LIMIT ?", (name, points)).fetchall()
                counts = self._conn.execute(
                    "SELECT result, COUNT(*) AS n FROM toolchange_records WHERE print_filename = ? GROUP BY result",
                    (name,)).fetchall()
            else:
                rows, counts = [], []
        pts = [dict(r) for r in reversed(rows)]
        ok = sorted(p["duration_ms"] for p in pts if p["result"] == "ok" and p["duration_ms"])
        recent = [p for p in pts[-50:] if p["duration_ms"] and p["result"] == "ok"]
        slowest = max(recent, key=lambda p: p["duration_ms"]) if recent else None
        return {"print_filename": name, "total": sum(int(c["n"]) for c in counts),
                "counts": {(c["result"] or "open"): int(c["n"]) for c in counts},
                "median_ms": ok[len(ok) // 2] if ok else None, "slowest_recent": slowest, "points": pts}

    # Results that mean the printer or a tool had a problem. park_only and interrupted are left out:
    # the first is a warning, the second means Guardian lost track.
    FAILED_RESULTS = ("error", "timeout", "wrong_tool", "bad_state")

    def toolchange_failures(self, max_points: int = 500, max_prints: int = 30) -> dict[str, Any]:
        """Failed changes by tool and by print, and how often they happen, over everything recorded."""
        import re
        from datetime import datetime

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, print_filename, requested_tool, previous_tool, started_utc, result, error"
                " FROM toolchange_records ORDER BY started_utc, id").fetchall()

        def fault(r) -> str | None:
            """The tool the problem belongs to: the one named in the printer's message, else the one parked
            (park_only) or the one asked for."""
            m = re.search(r"Extruder (extruder\d*)", r["error"] or "")
            return m.group(1) if m else (r["previous_tool"] if r["result"] == "park_only" else r["requested_tool"]) or None

        def when(iso: str) -> datetime:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))

        tools: dict[str, dict[str, int]] = {}
        prints: dict[str, dict[str, Any]] = {}
        points: list[dict[str, Any]] = []
        for r in rows:
            if r["result"] is None:
                continue
            for t in {r["previous_tool"], r["requested_tool"]} - {None}:
                tools.setdefault(t, {"changes": 0, "failed": 0})["changes"] += 1
            name = r["print_filename"] or "Unknown print"
            p = prints.setdefault(name, {"print_filename": name, "changes": 0, "failed": 0, "by_tool": {},
                                         "first_utc": r["started_utc"], "last_utc": r["started_utc"],
                                         "first_failed_utc": None, "last_failed_utc": None})
            p["changes"] += 1
            p["last_utc"] = r["started_utc"]
            if r["result"] in self.FAILED_RESULTS:
                t = fault(r) or "unknown"
                tools.setdefault(t, {"changes": 0, "failed": 0})["failed"] += 1
                p["failed"] += 1
                p["by_tool"][t] = p["by_tool"].get(t, 0) + 1
                p["first_failed_utc"] = p["first_failed_utc"] or r["started_utc"]
                p["last_failed_utc"] = r["started_utc"]
                points.append({"started_utc": r["started_utc"], "tool": t, "result": r["result"],
                               "print_filename": name})
        out_prints = []
        for p in prints.values():
            f = p["failed"]
            p["one_in"] = round(p["changes"] / f) if f else None
            span_min = (when(p["last_utc"]) - when(p["first_utc"])).total_seconds() / 60
            p["failed_per_hour"] = round(f / (span_min / 60), 1) if f and span_min >= 10 else None
            if f >= 2:
                gap = (when(p["last_failed_utc"]) - when(p["first_failed_utc"])).total_seconds() / 60 / (f - 1)
                p["minutes_between"] = round(gap)
            else:
                p["minutes_between"] = None
            out_prints.append(p)
        out_prints.sort(key=lambda p: p["last_utc"], reverse=True)
        return {"tools": [{"tool": t, **v, "percent": round(100 * v["failed"] / v["changes"], 1) if v["changes"] else 0}
                          for t, v in sorted(tools.items())],
                "prints": out_prints[:max_prints], "points": points[-max_points:],
                "total_failed": sum(p["failed"] for p in out_prints),
                "total_changes": sum(p["changes"] for p in out_prints)}

    def toolchange_stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result, COUNT(*) AS n FROM toolchange_records GROUP BY result").fetchall()
        return {(r["result"] or "open"): int(r["n"]) for r in rows}

    def close_open_toolchanges(self, result: str, error: str) -> int:
        """Mark records that never got a result (Guardian stopped mid change)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE toolchange_records SET result = ?, error = ? WHERE result IS NULL", (result, error))
            return cur.rowcount

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
