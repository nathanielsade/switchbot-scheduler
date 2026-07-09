import sqlite3


class Conversation:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, "
            "ts TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self.conn.commit()

    def append(self, chat_id: int, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )
        self.conn.commit()

    def load(self, chat_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]
