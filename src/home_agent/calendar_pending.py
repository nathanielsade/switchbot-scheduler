import json
import sqlite3
from contextlib import closing


class CalendarPending:
    """One staged, unconfirmed calendar change per chat (SQLite). Each stage gets a fresh autoincrement
    id — the same-turn confirm guard compares it to a turn-start snapshot. Thread-safe: connection-per-op."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_calendar_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "payload_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.commit()

    def stage(self, chat_id, payload, created_at):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM pending_calendar_changes WHERE chat_id = ?", (chat_id,))
            cur = conn.execute(
                "INSERT INTO pending_calendar_changes (chat_id, payload_json, created_at) "
                "VALUES (?, ?, ?)", (chat_id, json.dumps(payload), created_at))
            conn.commit()
            return cur.lastrowid

    def current(self, chat_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id, payload_json, created_at FROM pending_calendar_changes WHERE chat_id = ?",
                (chat_id,)).fetchone()
        return None if not row else {"id": row[0], "payload": json.loads(row[1]), "created_at": row[2]}

    def clear(self, chat_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM pending_calendar_changes WHERE chat_id = ?", (chat_id,))
            conn.commit()
