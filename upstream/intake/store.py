"""SQLite storage for live reports (stdlib only)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..reports import Report

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supply (
    cycle INTEGER PRIMARY KEY,
    start_min REAL NOT NULL,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    household TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    supply_start REAL NOT NULL,
    t_obs REAL NOT NULL,
    received_at TEXT NOT NULL,
    turbidity REAL,
    chlorine REAL,
    answer TEXT,
    source TEXT NOT NULL,
    level TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    flags TEXT NOT NULL DEFAULT '[]',
    reading TEXT,
    photo TEXT,
    sha256 TEXT,
    phash TEXT,
    taken_at TEXT
);
CREATE INDEX IF NOT EXISTS reports_cycle ON reports(cycle);
CREATE TABLE IF NOT EXISTS wa_sessions (
    sender TEXT PRIMARY KEY,
    report_id TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
            self._db.commit()

    # ----------------------------------------------------------- supply
    def add_supply(self, start_min: float, started_at: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(cycle), -1) AS c FROM supply").fetchone()
            cycle = int(row["c"]) + 1
            self._db.execute("INSERT INTO supply(cycle, start_min, started_at) VALUES (?, ?, ?)",
                             (cycle, start_min, started_at))
            self._db.commit()
        return cycle

    def current_supply(self) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM supply ORDER BY cycle DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- reports
    def next_report_id(self, cycle: int) -> str:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) AS n FROM reports WHERE cycle = ?", (cycle,)).fetchone()["n"]
        return f"L{cycle:03d}-{int(n) + 1:03d}"

    def add_report(self, r: Report, received_at: str, photo: str | None = None, sha256: str | None = None,
                   phash: str | None = None, taken_at: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO reports(id, household, cycle, supply_start, t_obs, received_at, turbidity, chlorine,"
                " answer, source, level, reasons, flags, reading, photo, sha256, phash, taken_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.id, r.household, r.cycle, r.supply_start, r.t_obs, received_at, r.turbidity, r.chlorine,
                 r.answer, r.source, r.level, json.dumps(r.reasons), json.dumps(r.flags),
                 json.dumps(r.reading) if r.reading is not None else None, photo, sha256, phash, taken_at))
            self._db.commit()

    def update_levels(self, reports: list[Report]) -> None:
        with self._lock:
            self._db.executemany("UPDATE reports SET level = ?, reasons = ? WHERE id = ?",
                                 [(r.level, json.dumps(r.reasons), r.id) for r in reports])
            self._db.commit()

    def set_answer(self, report_id: str, answer: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE reports SET answer = ? WHERE id = ?", (answer, report_id))
            self._db.commit()
        return cur.rowcount > 0

    def rows(self, min_cycle: int | None = None) -> list[dict]:
        q = "SELECT * FROM reports"
        args: tuple = ()
        if min_cycle is not None:
            q += " WHERE cycle >= ?"
            args = (min_cycle,)
        q += " ORDER BY t_obs, id"
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def reports(self, min_cycle: int | None = None) -> list[Report]:
        out = []
        for d in self.rows(min_cycle):
            out.append(Report(
                id=d["id"], household=d["household"], cycle=d["cycle"], supply_start=d["supply_start"],
                t_obs=d["t_obs"], turbidity=d["turbidity"], chlorine=d["chlorine"], answer=d["answer"],
                source=d["source"], level=d["level"], reasons=json.loads(d["reasons"]),
                flags=json.loads(d["flags"]), reading=json.loads(d["reading"]) if d["reading"] else None))
        return out

    def find_photo(self, sha256: str | None = None, taken_at: str | None = None) -> list[dict]:
        clauses, args = [], []
        if sha256:
            clauses.append("sha256 = ?")
            args.append(sha256)
        if taken_at:
            clauses.append("taken_at = ?")
            args.append(taken_at)
        if not clauses:
            return []
        with self._lock:
            return [dict(r) for r in self._db.execute(
                f"SELECT id, household, cycle, sha256, phash, taken_at FROM reports WHERE {' OR '.join(clauses)}",
                tuple(args)).fetchall()]

    def phashes(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT id, household, cycle, phash FROM reports WHERE phash IS NOT NULL").fetchall()]

    # -------------------------------------------------------- whatsapp
    def set_session(self, sender: str, report_id: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO wa_sessions(sender, report_id) VALUES (?, ?)",
                             (sender, report_id))
            self._db.commit()

    def session(self, sender: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT report_id FROM wa_sessions WHERE sender = ?", (sender,)).fetchone()
        return row["report_id"] if row else None

    def reset(self) -> None:
        with self._lock:
            self._db.executescript("DELETE FROM reports; DELETE FROM supply; DELETE FROM meta; DELETE FROM wa_sessions;")
            self._db.commit()
