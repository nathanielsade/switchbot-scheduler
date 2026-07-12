import sqlite3
from contextlib import closing

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
