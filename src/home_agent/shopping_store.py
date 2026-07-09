import sqlite3
from contextlib import closing


class ShoppingStore:
    """Shared shopping list + append-only purchase history (SQLite). Thread-safe: a fresh
    connection per operation (PTB runs handlers off-thread), mirroring memory.Conversation.
    Append-only: list/purchases rows are never deleted — removing/buying flips a status."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS items ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
                "CREATE TABLE IF NOT EXISTS list ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
                " quantity TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'pending',"
                " added_at TEXT DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT);"
                "CREATE TABLE IF NOT EXISTS purchases ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
                " quantity REAL, unit_price REAL, purchased_on TEXT NOT NULL,"
                " source TEXT NOT NULL, receipt_id TEXT,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            )
            conn.commit()

    def _get_or_create_item(self, conn, name):
        row = conn.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return conn.execute("INSERT INTO items (name) VALUES (?)", (name,)).lastrowid

    def known_items(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return [r[0] for r in conn.execute("SELECT name FROM items ORDER BY name").fetchall()]

    def add(self, name, quantity=None, note=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            item_id = self._get_or_create_item(conn, name)
            conn.execute("INSERT INTO list (item_id, quantity, note) VALUES (?, ?, ?)",
                         (item_id, quantity, note))
            conn.commit()

    def pending(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT i.name, l.quantity, l.note FROM list l JOIN items i ON i.id = l.item_id "
                "WHERE l.status = 'pending' ORDER BY l.id").fetchall()
        return [{"item": n, "quantity": q, "note": nt} for n, q, nt in rows]

    def remove(self, name):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()
            if not row:
                return 0
            cur = conn.execute(
                "UPDATE list SET status='removed', resolved_at=CURRENT_TIMESTAMP "
                "WHERE item_id=? AND status='pending'", (row[0],))
            conn.commit()
            return cur.rowcount

    def buy(self, name, purchased_on, quantity=None, unit_price=None):
        with closing(sqlite3.connect(self.db_path)) as conn:
            item_id = self._get_or_create_item(conn, name)
            conn.execute(
                "UPDATE list SET status='bought', resolved_at=CURRENT_TIMESTAMP "
                "WHERE item_id=? AND status='pending'", (item_id,))
            conn.execute(
                "INSERT INTO purchases (item_id, quantity, unit_price, purchased_on, source) "
                "VALUES (?, ?, ?, ?, 'chat')", (item_id, quantity, unit_price, purchased_on))
            conn.commit()

    def purchases_for(self, name):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT p.purchased_on, p.quantity, p.unit_price, p.source FROM purchases p "
                "JOIN items i ON i.id = p.item_id WHERE i.name = ? ORDER BY p.purchased_on, p.id",
                (name,)).fetchall()
        return [{"purchased_on": d, "quantity": q, "unit_price": u, "source": s}
                for d, q, u, s in rows]
