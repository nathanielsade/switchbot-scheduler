import sqlite3
from contextlib import closing


class ScheduleStore:
    """Record of the timers the agent programmed onto the Bots. Source of truth, because Bots
    cannot be read back. Thread-safe: a fresh connection per operation (PTB runs handlers in a
    worker thread), mirroring memory.Conversation."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schedules ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, action TEXT NOT NULL, "
                "time TEXT NOT NULL, days TEXT NOT NULL, once INTEGER NOT NULL DEFAULT 0, "
                "fire_at TEXT, set_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()

    def add(self, device, action, time, days, once, fire_at=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO schedules (device, action, time, days, once, fire_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device, action, time, ",".join(days), 1 if once else 0, fire_at),
            )
            conn.commit()
            return cur.lastrowid

    def list(self, device=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if device is None:
                rows = conn.execute(
                    "SELECT device, action, time, days, once, fire_at FROM schedules "
                    "ORDER BY device, time"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT device, action, time, days, once, fire_at FROM schedules "
                    "WHERE device = ? ORDER BY time",
                    (device,),
                ).fetchall()
        return [{"device": d, "action": a, "time": t,
                 "days": [x for x in dd.split(",") if x], "once": bool(o), "fire_at": f}
                for d, a, t, dd, o, f in rows]

    def remove(self, device, time=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if time is None:
                cur = conn.execute("DELETE FROM schedules WHERE device = ?", (device,))
            else:
                cur = conn.execute(
                    "DELETE FROM schedules WHERE device = ? AND time = ?", (device, time))
            conn.commit()
            return cur.rowcount

    def remove_id(self, row_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (row_id,))
            conn.commit()

    def remove_expired(self, now_iso):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM schedules WHERE once = 1 AND fire_at IS NOT NULL AND fire_at < ?",
                (now_iso,))
            conn.commit()
            return cur.rowcount
