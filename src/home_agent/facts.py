import sqlite3
from contextlib import closing
from datetime import datetime

from .tools import Tool


class FactStore:
    """Durable, append-only family fact store (SQLite). Thread-safe: a fresh connection per
    operation (PTB runs handlers off-thread), mirroring memory.Conversation. Append-only:
    facts are never deleted — forget() flips status to 'forgotten'."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS facts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, fact TEXT NOT NULL,"
                " author TEXT, created_at TEXT, status TEXT NOT NULL DEFAULT 'active')"
            )
            conn.commit()

    def add(self, subject, fact, author, created_at) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO facts (subject, fact, author, created_at) VALUES (?, ?, ?, ?)",
                (subject, fact, author, created_at),
            )
            conn.commit()
            return cur.lastrowid

    def _rows(self, conn, where, params):
        sql = ("SELECT id, subject, fact, author, created_at FROM facts "
               "WHERE status='active'" + where + " ORDER BY id DESC")
        return [
            {"id": i, "subject": s, "fact": f, "author": a, "created_at": c}
            for i, s, f, a, c in conn.execute(sql, params).fetchall()
        ]

    def active(self) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, "", ())

    def find_active(self, query) -> list[dict]:
        like = f"%{query}%"
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(
                conn,
                " AND (subject LIKE ? COLLATE NOCASE OR fact LIKE ? COLLATE NOCASE)",
                (like, like),
            )

    def forget(self, fact_id) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE facts SET status='forgotten' WHERE id=?", (fact_id,))
            conn.commit()


def _now():
    return datetime.now().astimezone()


_REMEMBER_SCHEMA = {"type": "function", "function": {
    "name": "remember",
    "description": (
        "Store a durable family fact, ONLY when the user explicitly asks you to remember something "
        "(e.g. 'תזכור ש…', 'remember that…'). Never store facts on your own initiative. Give a short "
        "'subject' label (e.g. 'gate code', 'passports') and the 'fact' detail. Report back briefly."
    ),
    "parameters": {"type": "object", "properties": {
        "subject": {"type": "string", "description": "A short label for the fact, e.g. 'gate code', 'passports'."},
        "fact": {"type": "string", "description": "The detail to remember, e.g. 'in the safe', 'the code is five six seven eight'."},
    }, "required": ["subject", "fact"], "additionalProperties": False}}}


def _remember_impl(args, *, store, sender, now_fn) -> str:
    subject = (args.get("subject") or "").strip()
    fact = (args.get("fact") or "").strip()
    if not fact:
        return "there was nothing to remember — tell me the detail."
    store.add(subject, fact, sender, now_fn().isoformat())
    label = f"{subject}: {fact}" if subject else fact
    return f"remembered — {label}"


_RECALL_SCHEMA = {"type": "function", "function": {
    "name": "recall",
    "description": (
        "List everything you have been told to remember (family facts), newest first. Call this whenever "
        "the user asks about something that might have been saved — where something is kept, a code, a "
        "password, a date. When values conflict, prefer the most recent. Answer the user from what you find."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def _format_fact(row) -> str:
    label = f"{row['subject']} — {row['fact']}" if row.get("subject") else row["fact"]
    author = row.get("author") or "unknown"
    date = (row.get("created_at") or "")[:10]
    return f"{label} ({author}, {date})"


def _recall_impl(args, *, store) -> str:
    rows = store.active()
    if not rows:
        return "I have not been told to remember anything yet."
    return "\n".join(_format_fact(r) for r in rows)


def build_memory_tools(store, *, sender, now_fn=None) -> list[Tool]:
    now_fn = now_fn or _now
    return [
        Tool(name="remember", schema=_REMEMBER_SCHEMA,
             impl=lambda a: _remember_impl(a, store=store, sender=sender, now_fn=now_fn)),
        Tool(name="recall", schema=_RECALL_SCHEMA, impl=lambda a: _recall_impl(a, store=store)),
    ]
