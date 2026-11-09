# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""SQLite storage (WAL). Every PayPal call is preceded by a committed row holding its idempotency ids."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS lost (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,       -- UNPAID, OPEN, MATCHED, RETURNED, EXPIRED, CANCELLED
    owner_name      TEXT NOT NULL,
    owner_email     TEXT NOT NULL,
    owner_token     TEXT NOT NULL,       -- sha256 of the owner's access token
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,       -- public
    private_details TEXT NOT NULL,       -- never shown to anyone; only used to check a finder's answer
    place           TEXT NOT NULL,
    lost_at         REAL NOT NULL,
    attributes      TEXT NOT NULL DEFAULT '{}',  -- AI-extracted (category, brand, colours, marks)
    reward_cents    INTEGER NOT NULL,
    fee_cents       INTEGER NOT NULL,
    order_id        TEXT,
    capture_id      TEXT,
    paid_cents      INTEGER NOT NULL DEFAULT 0,   -- captured from the owner (reward + fee)
    refunded_cents  INTEGER NOT NULL DEFAULT 0,
    paid_out_cents  INTEGER NOT NULL DEFAULT 0,
    expires_at      REAL,
    sample          INTEGER NOT NULL DEFAULT 0,   -- 1 = sample report (demo data), labelled in the UI
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS found (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,       -- OPEN, MATCHED, RETURNED, CLOSED
    finder_name     TEXT NOT NULL,
    finder_email    TEXT NOT NULL,       -- PayPal email for the reward
    finder_token    TEXT NOT NULL,
    description     TEXT NOT NULL,
    place           TEXT NOT NULL,
    found_at        REAL NOT NULL,
    attributes      TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id          TEXT PRIMARY KEY,
    owner_kind  TEXT NOT NULL CHECK (owner_kind IN ('lost', 'found')),
    owner_id    TEXT NOT NULL,
    mime        TEXT NOT NULL,
    sha         TEXT NOT NULL,              -- sha256 of the bytes (same photo reused in another report = flag)
    data        BLOB NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id               TEXT PRIMARY KEY,
    lost_id          TEXT NOT NULL REFERENCES lost(id),
    found_id         TEXT NOT NULL REFERENCES found(id),
    score            INTEGER NOT NULL,
    reasons          TEXT NOT NULL,
    status           TEXT NOT NULL,   -- PROPOSED, CHECKING, VERIFIED, HANDED_OVER, REJECTED, REVIEW
    finder_question  TEXT, finder_answer TEXT, finder_verdict TEXT, finder_reason TEXT,
    owner_question   TEXT, owner_answer  TEXT, owner_verdict  TEXT, owner_reason  TEXT,
    code_hash        TEXT,            -- handover code (sha256); shown only to the owner
    code_attempts    INTEGER NOT NULL DEFAULT 0,
    reward_cents     INTEGER,         -- agreed at handover (may be less than offered)
    flags            TEXT NOT NULL DEFAULT '[]',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE(lost_id, found_id)
);
CREATE TABLE IF NOT EXISTS settlements (
    id              TEXT PRIMARY KEY,  -- PayPal-Request-Id / sender_batch_id / custom_id
    lost_id         TEXT NOT NULL,
    match_id        TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('payout', 'refund')),
    amount_cents    INTEGER NOT NULL,
    receiver        TEXT,
    state           TEXT NOT NULL,     -- SENDING, SUBMITTED, ITEMS_PENDING, DONE, UNCERTAIN, FAILED
    paypal_id       TEXT,              -- payout_batch_id or refund id
    item_status     TEXT,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_payout_per_case ON settlements(lost_id) WHERE kind='payout';
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    case_id  TEXT,
    kind     TEXT NOT NULL,
    data     TEXT NOT NULL DEFAULT '{}'
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def one(self, sql: str, *args: Any) -> Any:
        return self._conn().execute(sql, args).fetchone()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self._conn().execute(sql, args).fetchall()

    def event(self, kind: str, case_id: str | None = None, conn: sqlite3.Connection | None = None, **data: Any) -> None:
        args = (time.time(), case_id, kind, json.dumps(data, default=str, sort_keys=True))
        sql = "INSERT INTO events(at, case_id, kind, data) VALUES (?,?,?,?)"
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.tx() as c:
                c.execute(sql, args)
