-- Schema from spec section 10.1. Times are UTC ISO 8601 text.

CREATE TABLE IF NOT EXISTS incidents (
    id                 INTEGER PRIMARY KEY,
    uid                TEXT UNIQUE NOT NULL,      -- e.g. 2026-11-02T13-05-22Z_0001
    event_type         TEXT NOT NULL,             -- first/primary trigger
    source             TEXT NOT NULL,
    severity           TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    start_time_utc     TEXT NOT NULL,             -- footage start
    first_trigger_utc  TEXT NOT NULL,
    last_trigger_utc   TEXT NOT NULL,
    end_time_utc       TEXT,                      -- footage end
    print_filename     TEXT,
    print_progress     REAL,
    active_toolhead    TEXT,
    diagnostic_summary TEXT,
    evidence_level     TEXT,
    recording_path     TEXT,
    recording_status   TEXT NOT NULL DEFAULT 'recording',
    recording_notes    TEXT,
    actual_pre_s       REAL,
    actual_post_s      REAL,
    protected          INTEGER NOT NULL DEFAULT 0,
    user_note          TEXT
);

CREATE TABLE IF NOT EXISTS incident_events (
    id           INTEGER PRIMARY KEY,
    incident_id  INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    ts_utc       TEXT NOT NULL,                   -- Guardian receive time
    printer_ts   REAL,                            -- printer event time, if any
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    severity     TEXT,
    is_trigger   INTEGER NOT NULL DEFAULT 0,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    metadata     TEXT                             -- JSON
);

CREATE TABLE IF NOT EXISTS printer_snapshots (
    id              INTEGER PRIMARY KEY,
    incident_id     INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    ts_utc          TEXT NOT NULL,
    printer_state   TEXT,
    temperatures    TEXT,                         -- JSON
    toolhead_state  TEXT,                         -- JSON
    print_progress  REAL,
    raw_data        TEXT                          -- JSON
);

CREATE TABLE IF NOT EXISTS toolchange_records (
    id             INTEGER PRIMARY KEY,
    print_filename TEXT,
    requested_tool TEXT,
    previous_tool  TEXT,
    actual_tool    TEXT,
    started_utc    TEXT NOT NULL,
    ended_utc      TEXT,
    duration_ms    INTEGER,
    result         TEXT,       -- ok | timeout | wrong_tool | error | interrupted
    transitions    TEXT,       -- JSON list of {state, ts}
    error          TEXT,
    incident_id    INTEGER REFERENCES incidents(id)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id        INTEGER PRIMARY KEY,
    ts_utc    TEXT NOT NULL,
    component TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL,
    metadata  TEXT
);

CREATE TABLE IF NOT EXISTS notification_log (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE SET NULL,
    ts_utc      TEXT NOT NULL,
    channel     TEXT NOT NULL,       -- email | webhook | ntfy
    kind        TEXT NOT NULL,       -- incident_created | clip_ready | health | test
    status      TEXT NOT NULL,       -- sent | failed | skipped
    attempts    INTEGER NOT NULL DEFAULT 1,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_time     ON incidents(first_trigger_utc);
CREATE INDEX IF NOT EXISTS idx_incidents_type     ON incidents(event_type, severity);
CREATE INDEX IF NOT EXISTS idx_incidents_file     ON incidents(print_filename);
CREATE INDEX IF NOT EXISTS idx_events_incident    ON incident_events(incident_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_snapshots_incident ON printer_snapshots(incident_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_toolchange_time    ON toolchange_records(started_utc);
