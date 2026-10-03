"""SQLite storage for memo.db (lightweight, stdlib only)."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'P3',
    due_at       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    source_msg_id TEXT,
    raw_text     TEXT,
    reminded     TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);

CREATE TABLE IF NOT EXISTS processed_messages (
    msg_id TEXT PRIMARY KEY,
    ts     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PRIORITIES = ("P0", "P1", "P2", "P3")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def add_task(db_path, title, priority, due_at=None, source_msg_id=None, raw_text=None):
    ts = now_iso()
    if priority not in PRIORITIES:
        priority = "P3"
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, priority, due_at, status, source_msg_id,"
            " raw_text, reminded, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (title, priority, due_at, "pending", source_msg_id, raw_text, "[]", ts, ts),
        )
        return cur.lastrowid


def get_task(db_path, task_id):
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(db_path, status="pending", priority=None, due_before=None, due_after=None):
    sql = "SELECT * FROM tasks WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if priority:
        sql += " AND priority=?"
        args.append(priority)
    if due_before:
        sql += " AND due_at IS NOT NULL AND due_at<=?"
        args.append(due_before)
    if due_after:
        sql += " AND due_at IS NOT NULL AND due_at>=?"
        args.append(due_after)
    sql += " ORDER BY (due_at IS NULL), due_at ASC, id ASC"
    with connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def update_status(db_path, task_id, status):
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, now_iso(), task_id),
        )
        return cur.rowcount


def set_priority(db_path, task_id, priority):
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET priority=?, updated_at=? WHERE id=?",
            (priority, now_iso(), task_id),
        )


def effective_priority(priority, due_at, now=None):
    """P1 tasks whose deadline is within 48 hours become P0."""
    if priority != "P1" or not due_at:
        return priority
    now = now or datetime.now().astimezone()
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        return priority
    if due <= now + timedelta(hours=48):
        return "P0"
    return priority


def set_reminded(db_path, task_id, reminded):
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET reminded=?, updated_at=? WHERE id=?",
            (json.dumps(reminded), now_iso(), task_id),
        )


def get_reminded(task):
    try:
        return list(json.loads(task.get("reminded") or "[]"))
    except (ValueError, TypeError):
        return []


def find_tasks_by_keyword(db_path, keyword, status="pending", title_only=False):
    kw = f"%{keyword}%"
    if title_only:
        cond, args = "title LIKE ?", [status, kw]
    else:
        cond, args = "(title LIKE ? OR raw_text LIKE ?)", [status, kw, kw]
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE status=? AND {cond}"
            " ORDER BY (due_at IS NULL), due_at ASC, id ASC LIMIT 5",
            args,
        ).fetchall()
        return [dict(r) for r in rows]


def mark_processed(db_path, msg_id):
    """Return True if this message was newly recorded, False if duplicate."""
    try:
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO processed_messages (msg_id, ts) VALUES (?,?)",
                (msg_id, now_iso()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def kv_get(db_path, key, default=None):
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(db_path, key, value):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )