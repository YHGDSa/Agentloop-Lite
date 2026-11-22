"""SQLite access: connection policy, the four-table schema, and API wiring.

The schema is the runtime's source of truth for auditability: runs hold the
state machine state, steps hold one planner iteration, tool_calls hold every
retry attempt, approvals hold human decisions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
  id                  TEXT PRIMARY KEY,
  goal                TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN
                        ('CREATED','RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')),
  step_count          INTEGER NOT NULL DEFAULT 0,
  max_steps           INTEGER NOT NULL DEFAULT 6,
  result              TEXT,
  error               TEXT,
  approval_expires_at TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  thought     TEXT,
  tool_name   TEXT,
  args_json   TEXT,
  observation TEXT,
  error_type  TEXT,
  latency_ms  INTEGER,
  created_at  TEXT NOT NULL,
  UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  step_id         INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  attempt         INTEGER NOT NULL,
  status          TEXT NOT NULL,
  error_type      TEXT,
  observation     TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE (idempotency_key, attempt)
);

CREATE TABLE IF NOT EXISTS approvals (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_id    INTEGER REFERENCES steps(id),
  decision   TEXT NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tool_calls_key ON tool_calls(idempotency_key);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open one connection with foreign keys on and rows addressable by name.

    ``check_same_thread=False`` is needed because FastAPI runs sync dependencies
    (the connection is opened there) in a worker thread while async endpoints
    such as ``POST /runs`` use it on the event loop. Each connection still
    belongs to exactly one request, so there is no shared state across threads.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the schema if needed; safe to call on every process start."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one short-lived connection per request."""
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()
