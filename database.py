"""Database layer for Flow Telegram Bot — optimized for speed."""

import sqlite3
import os
from datetime import datetime, timedelta, date
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "flow.db")

# Connection pool for faster access
_pool: dict[int, sqlite3.Connection] = {}


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                focus_min     INTEGER DEFAULT 25,
                short_min     INTEGER DEFAULT 5,
                long_min      INTEGER DEFAULT 15,
                sessions_long INTEGER DEFAULT 4,
                auto_break    INTEGER DEFAULT 0,
                auto_focus    INTEGER DEFAULT 0,
                sound         INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                text         TEXT NOT NULL,
                completed    INTEGER DEFAULT 0,
                sessions     INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT (datetime('now')),
                completed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                task_id      INTEGER,
                duration     INTEGER NOT NULL,
                type         TEXT DEFAULT 'focus',
                started_at   TEXT NOT NULL,
                completed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS reminders (
                user_id      INTEGER PRIMARY KEY,
                remind_time  TEXT DEFAULT '09:00',
                remind_on    INTEGER DEFAULT 1,
                streak_alert INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_date
                ON sessions(user_id, completed_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_user
                ON tasks(user_id, completed);
        """)


# ── User ─────────────────────────────────────────────────────

def ensure_user(user_id: int, username: str = None, first_name: str = None):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, username, first_name),
            )


def get_user(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_user_setting(user_id: int, key: str, value):
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))


# ─ Tasks ─────────────────────────────────────────────────────

def add_task(user_id: int, text: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, text) VALUES (?, ?)", (user_id, text)
        )
        return cur.lastrowid


def get_tasks(user_id: int, include_completed: bool = True) -> list[dict]:
    with get_db() as conn:
        if include_completed:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY completed ASC, created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_task(user_id: int, task_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def complete_task(user_id: int, task_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET completed = 1, completed_at = datetime('now') WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def uncomplete_task(user_id: int, task_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET completed = 0, completed_at = NULL WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def delete_task(user_id: int, task_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))


def increment_task_sessions(user_id: int, task_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET sessions = sessions + 1 WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


# ── Sessions ──────────────────────────────────────────────────

def record_session(user_id: int, duration: int, session_type: str = "focus", task_id: int = None, started_at: str = None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, task_id, duration, type, started_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, task_id, duration, session_type, started_at or datetime.utcnow().isoformat()),
        )


def get_today_stats(user_id: int) -> dict:
    """Get today's session count and minutes in a SINGLE query."""
    today = date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as count, COALESCE(SUM(duration), 0) as minutes
               FROM sessions WHERE user_id = ? AND type = 'focus' AND date(completed_at) = ?""",
            (user_id, today),
        ).fetchone()
        return {"count": row["count"] or 0, "minutes": row["minutes"] or 0}


def get_recent_sessions(user_id: int, limit: int = 10) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, t.text as task_text FROM sessions s
               LEFT JOIN tasks t ON s.task_id = t.id
               WHERE s.user_id = ? AND s.type = 'focus'
               ORDER BY s.completed_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_weekly_data(user_id: int) -> list[dict]:
    """Get all 7 days in a SINGLE query."""
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    today = date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT date(completed_at) as day, COALESCE(SUM(duration), 0) as minutes
               FROM sessions WHERE user_id = ? AND type = 'focus'
               AND date(completed_at) >= ? AND date(completed_at) <= ?
               GROUP BY date(completed_at)""",
            (user_id, week_ago, today),
        ).fetchall()
    # Build the full 7-day array, filling gaps
    data = {dict(r)["day"]: dict(r)["minutes"] for r in rows}
    days = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        days.append({"date": d.isoformat(), "minutes": data.get(d.isoformat(), 0), "day_name": d.strftime("%a")})
    return days


def get_streak(user_id: int) -> int:
    """Calculate streak in a SINGLE query by finding consecutive days."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT date(completed_at) as day FROM sessions
               WHERE user_id = ? AND type = 'focus'
               ORDER BY day DESC""",
            (user_id,),
        ).fetchall()

    if not rows:
        return 0

    # Get today and work backwards
    day_set = {dict(r)["day"] for r in rows}
    today = date.today().isoformat()

    # Check if today or yesterday has a session
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    if today not in day_set and yesterday not in day_set:
        return 0

    streak = 0
    check = date.today() if today in day_set else date.today() - timedelta(days=1)

    while check.isoformat() in day_set:
        streak += 1
        check -= timedelta(days=1)

    return streak


def get_total_stats(user_id: int) -> dict:
    """Get all-time sessions and minutes in ONE query."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as sessions, COALESCE(SUM(duration), 0) as minutes FROM sessions WHERE user_id = ? AND type = 'focus'",
            (user_id,),
        ).fetchone()
        return {"sessions": row["sessions"] or 0, "minutes": row["minutes"] or 0}


# ── Reminders ────────────────────────────────────────────────

def get_reminder(user_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
        # Default
        return {"user_id": user_id, "remind_time": "09:00", "remind_on": 1, "streak_alert": 1}


def set_reminder_time(user_id: int, time_str: str):
    with get_db() as conn:
        existing = conn.execute("SELECT user_id FROM reminders WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE reminders SET remind_time = ? WHERE user_id = ?", (time_str, user_id))
        else:
            conn.execute("INSERT INTO reminders (user_id, remind_time) VALUES (?, ?)", (user_id, time_str))


def set_reminder_on(user_id: int, on: bool):
    with get_db() as conn:
        existing = conn.execute("SELECT user_id FROM reminders WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE reminders SET remind_on = ? WHERE user_id = ?", (1 if on else 0, user_id))
        else:
            conn.execute("INSERT INTO reminders (user_id, remind_on) VALUES (?, ?)", (user_id, 1 if on else 0))


def set_streak_alert(user_id: int, on: bool):
    with get_db() as conn:
        existing = conn.execute("SELECT user_id FROM reminders WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE reminders SET streak_alert = ? WHERE user_id = ?", (1 if on else 0, user_id))
        else:
            conn.execute("INSERT INTO reminders (user_id, streak_alert) VALUES (?, ?)", (user_id, 1 if on else 0))


def get_all_reminders() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE remind_on = 1").fetchall()
        return [dict(r) for r in rows]


def get_streak_alert_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT r.user_id, u.username, u.first_name FROM reminders r JOIN users u ON r.user_id = u.user_id WHERE r.streak_alert = 1").fetchall()
        return [dict(r) for r in rows]
