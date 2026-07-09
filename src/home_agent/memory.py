import sqlite3
from contextlib import closing


class Conversation:
    """Per-chat message store backed by SQLite.

    Thread-safe by design: each operation opens and closes its own connection, so a
    single instance can be shared across threads. This matters because
    python-telegram-bot runs message handlers in a worker-thread executor
    (via ``asyncio.to_thread``), while the store is constructed on the main thread —
    a single shared sqlite3 connection would raise ``ProgrammingError`` there.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, "
                "ts TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()

    def append(self, chat_id: int, role: str, content: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
                (chat_id, role, content),
            )
            conn.commit()

    def load(self, chat_id: int, limit: int = 20) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    # Kept for API/back-compat. Connections are now per-operation, so there is
    # nothing persistent to close; the context-manager form is still supported.
    def close(self) -> None:
        pass

    def __enter__(self) -> "Conversation":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
