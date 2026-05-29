#!/usr/bin/env python3
"""Storage layer — the single interface for all persisted state, profile-keyed.

Replaces git-as-database (overrides.json, state.json, feedback_log.jsonl, the
plans/ files, nutrition_log/*.md) with one SQLite database at data/app.db. All
SQL lives here; callers never see it. Everything is keyed by profile_id so the
same database can hold two users without collision.

Backend: SQLite, one writer at a time (the cron runs serially). Connections are
opened per call — fine for this volume and avoids stale-handle issues in tests.

Test/override hook: set the FITNESS_DB_PATH env var (e.g. to a temp file) to
redirect the database. ":memory:" will NOT work because each call opens a fresh
connection; use a temp file path in tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent
_DEFAULT_DB = _ROOT / "data" / "app.db"
_TZ = ZoneInfo("Europe/Amsterdam")


# ──────────────────────────────────────────────────────────────────────────
# Connection + schema
# ──────────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    return Path(os.environ.get("FITNESS_DB_PATH") or _DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Default rollback journal (not WAL): the cron is a single serial writer, so we
    # gain nothing from WAL's concurrent readers, and DELETE mode leaves a single
    # app.db file after each commit — far cleaner to commit to git.
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (
            profile_id TEXT PRIMARY KEY,
            data       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS overrides (
            profile_id TEXT NOT NULL,
            iso_date   TEXT NOT NULL,
            record     TEXT NOT NULL,
            PRIMARY KEY (profile_id, iso_date)
        );
        CREATE TABLE IF NOT EXISTS pending_choice (
            profile_id TEXT PRIMARY KEY,
            payload    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            entry      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nutrition_item (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            day_iso    TEXT NOT NULL,
            logged_at  TEXT NOT NULL,
            item       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nutrition_day
            ON nutrition_item (profile_id, day_iso);
        CREATE TABLE IF NOT EXISTS nutrition_day (
            profile_id      TEXT NOT NULL,
            day_iso         TEXT NOT NULL,
            first_logged_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, day_iso)
        );
        CREATE TABLE IF NOT EXISTS adaptation (
            profile_id TEXT PRIMARY KEY,
            data       TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────

def get_state(profile_id: str) -> dict:
    """Return the operational state dict for the profile, or {} if unset."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM state WHERE profile_id = ?", (profile_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else {}


def set_state(profile_id: str, state: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO state (profile_id, data) VALUES (?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET data = excluded.data",
            (profile_id, json.dumps(state)),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# Overrides
# ──────────────────────────────────────────────────────────────────────────

def get_overrides(profile_id: str) -> dict[str, dict]:
    """Return {iso_date: override_record} for the profile."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT iso_date, record FROM overrides WHERE profile_id = ?",
            (profile_id,),
        ).fetchall()
    return {r["iso_date"]: json.loads(r["record"]) for r in rows}


def set_override(profile_id: str, iso_date: str, override: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO overrides (profile_id, iso_date, record) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_id, iso_date) DO UPDATE SET record = excluded.record",
            (profile_id, iso_date, json.dumps(override)),
        )
        conn.commit()


def delete_override(profile_id: str, iso_date: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM overrides WHERE profile_id = ? AND iso_date = ?",
            (profile_id, iso_date),
        )
        conn.commit()


def clean_old_overrides(profile_id: str, before_iso: str) -> int:
    """Delete overrides for dates strictly before before_iso. Returns count removed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM overrides WHERE profile_id = ? AND iso_date < ?",
            (profile_id, before_iso),
        )
        conn.commit()
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────
# Week-plan pending choice
# ──────────────────────────────────────────────────────────────────────────

def get_pending_choice(profile_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload FROM pending_choice WHERE profile_id = ?", (profile_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def set_pending_choice(profile_id: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pending_choice (profile_id, payload) VALUES (?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET payload = excluded.payload",
            (profile_id, json.dumps(payload)),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# Nutrition
# ──────────────────────────────────────────────────────────────────────────

def append_nutrition(profile_id: str, day_iso: str, items: list[dict]) -> None:
    """Append food items to a day. Records first_logged_at on the first write."""
    if not items:
        return
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO nutrition_day (profile_id, day_iso, first_logged_at) "
            "VALUES (?, ?, ?) ON CONFLICT(profile_id, day_iso) DO NOTHING",
            (profile_id, day_iso, now),
        )
        conn.executemany(
            "INSERT INTO nutrition_item (profile_id, day_iso, logged_at, item) "
            "VALUES (?, ?, ?, ?)",
            [(profile_id, day_iso, now, json.dumps(it)) for it in items],
        )
        conn.commit()


def read_day(profile_id: str, day_iso: str) -> dict | None:
    """Return {"items": [...], "first_logged_at": iso} for a day, or None if empty."""
    with _connect() as conn:
        items = conn.execute(
            "SELECT item FROM nutrition_item WHERE profile_id = ? AND day_iso = ? "
            "ORDER BY seq",
            (profile_id, day_iso),
        ).fetchall()
        if not items:
            return None
        day = conn.execute(
            "SELECT first_logged_at FROM nutrition_day WHERE profile_id = ? AND day_iso = ?",
            (profile_id, day_iso),
        ).fetchone()
    return {
        "items": [json.loads(r["item"]) for r in items],
        "first_logged_at": day["first_logged_at"] if day else None,
    }


def weekly_nutrition(profile_id: str, end_iso: str, days: int) -> dict:
    """Aggregate raw per-day macro totals for the `days`-day window ending end_iso.

    Returns {"days": [{"date": iso, "logged": bool, "totals": {...} | None}]}.
    Pattern detection and target comparisons live in nutrition_logger.
    """
    from datetime import date, timedelta

    end = date.fromisoformat(end_iso)
    out = []
    for offset in range(days):
        d = (end - timedelta(days=days - 1 - offset)).isoformat()
        day = read_day(profile_id, d)
        if day and day["items"]:
            totals = {
                "protein_g": sum(i.get("protein_g", 0) for i in day["items"]),
                "carbs_g": sum(i.get("carbs_g", 0) for i in day["items"]),
                "fat_g": sum(i.get("fat_g", 0) for i in day["items"]),
                "kcal": sum(i.get("kcal", 0) for i in day["items"]),
            }
            out.append({"date": d, "logged": True, "totals": totals})
        else:
            out.append({"date": d, "logged": False, "totals": None})
    return {"days": out}


# ──────────────────────────────────────────────────────────────────────────
# Feedback / audit log
# ──────────────────────────────────────────────────────────────────────────

def append_feedback(profile_id: str, entry: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO feedback (profile_id, created_at, entry) VALUES (?, ?, ?)",
            (profile_id, _now_iso(), json.dumps(entry)),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# Adaptation / taper / weekly counters
# ──────────────────────────────────────────────────────────────────────────

def get_adaptation(profile_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM adaptation WHERE profile_id = ?", (profile_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else {}


def set_adaptation(profile_id: str, fields: dict) -> None:
    """Merge `fields` into the stored adaptation dict (shallow update)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM adaptation WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        current = json.loads(row["data"]) if row else {}
        current.update(fields)
        conn.execute(
            "INSERT INTO adaptation (profile_id, data) VALUES (?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET data = excluded.data",
            (profile_id, json.dumps(current)),
        )
        conn.commit()
