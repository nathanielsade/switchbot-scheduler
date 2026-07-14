import sqlite3
from contextlib import closing


class FinanceStore:
    """Local finance data (SQLite), connection-per-op like shopping_store. Money is integer agorot.
    `transactions` is durable current state (upsert by fingerprint); `account_snapshots` is append;
    `category_rules` are soft-deletable (status flip)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS transactions ("
                " source TEXT NOT NULL, account TEXT NOT NULL, identifier TEXT,"
                " fingerprint TEXT NOT NULL, txn_date TEXT NOT NULL, processed_date TEXT,"
                " amount_agorot INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'ILS',"
                " description TEXT NOT NULL, status TEXT NOT NULL, category_override TEXT,"
                " raw_json TEXT, imported_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                " PRIMARY KEY (source, account, fingerprint));"
                "CREATE TABLE IF NOT EXISTS account_snapshots ("
                " source TEXT NOT NULL, account TEXT NOT NULL, scraped_at TEXT NOT NULL,"
                " balance_agorot INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS category_rules ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_pattern TEXT NOT NULL,"
                " category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            )
            conn.commit()

    def upsert_transactions(self, rows):
        inserted = updated = 0
        with closing(sqlite3.connect(self.db_path)) as conn:
            for r in rows:
                exists = conn.execute(
                    "SELECT 1 FROM transactions WHERE source=? AND account=? AND fingerprint=?",
                    (r["source"], r["account"], r["fingerprint"])).fetchone()
                conn.execute(
                    "INSERT INTO transactions (source, account, identifier, fingerprint, txn_date,"
                    " processed_date, amount_agorot, currency, description, status, raw_json)"
                    " VALUES (:source,:account,:identifier,:fingerprint,:txn_date,:processed_date,"
                    " :amount_agorot,:currency,:description,:status,:raw_json)"
                    " ON CONFLICT(source, account, fingerprint) DO UPDATE SET"
                    " status=excluded.status, amount_agorot=excluded.amount_agorot,"
                    " txn_date=excluded.txn_date, processed_date=excluded.processed_date,"
                    " description=excluded.description, raw_json=excluded.raw_json,"
                    " imported_at=CURRENT_TIMESTAMP", r)
                if exists:
                    updated += 1
                else:
                    inserted += 1
            conn.commit()
        return inserted, updated

    def record_snapshot(self, source, account, scraped_at, balance_agorot):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO account_snapshots (source, account, scraped_at, balance_agorot)"
                         " VALUES (?,?,?,?)", (source, account, scraped_at, balance_agorot))
            conn.commit()

    def current_balance_agorot(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT a.balance_agorot FROM account_snapshots a WHERE a.rowid ="
                " (SELECT b.rowid FROM account_snapshots b"
                "  WHERE b.source=a.source AND b.account=a.account"
                "  ORDER BY b.scraped_at DESC, b.rowid DESC LIMIT 1)").fetchall()
        return sum(r[0] for r in rows)

    def sum_amounts(self, from_date, to_date):
        with closing(sqlite3.connect(self.db_path)) as conn:
            income = conn.execute(
                "SELECT COALESCE(SUM(amount_agorot),0) FROM transactions"
                " WHERE amount_agorot>0 AND txn_date BETWEEN ? AND ?", (from_date, to_date)).fetchone()[0]
            expense = conn.execute(
                "SELECT COALESCE(SUM(amount_agorot),0) FROM transactions"
                " WHERE amount_agorot<0 AND txn_date BETWEEN ? AND ?", (from_date, to_date)).fetchone()[0]
        return income, expense

    def _rows(self, conn, where="", params=()):
        cols = "source,account,identifier,fingerprint,txn_date,processed_date,amount_agorot,currency,description,status"
        q = f"SELECT {cols} FROM transactions {where}"
        return [dict(zip(cols.split(","), row)) for row in conn.execute(q, params).fetchall()]

    def transactions_between(self, from_date, to_date):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, "WHERE txn_date BETWEEN ? AND ? ORDER BY txn_date", (from_date, to_date))

    def search(self, from_date=None, to_date=None, min_abs=None, max_abs=None,
               direction=None, query=None, limit=50):
        clauses, params = [], []
        if from_date: clauses.append("txn_date >= ?"); params.append(from_date)
        if to_date: clauses.append("txn_date <= ?"); params.append(to_date)
        if min_abs is not None: clauses.append("ABS(amount_agorot) >= ?"); params.append(min_abs)
        if max_abs is not None: clauses.append("ABS(amount_agorot) <= ?"); params.append(max_abs)
        if direction == "income": clauses.append("amount_agorot > 0")
        elif direction == "expense": clauses.append("amount_agorot < 0")
        if query:
            # Forgiving match: any word of the query as a substring (the model may pass a phrase
            # like "הפקדות לפיקדון" whose exact string isn't in the description "הפקדה לפיקדון").
            tokens = [t for t in query.split() if len(t) >= 2] or [query]
            clauses.append("(" + " OR ".join(["description LIKE ?"] * len(tokens)) + ")")
            params.extend(f"%{t}%" for t in tokens)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return self._rows(conn, f"{where} ORDER BY txn_date DESC LIMIT ?", (*params, limit))

    def add_rule(self, merchant_pattern, category):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rid = conn.execute("INSERT INTO category_rules (merchant_pattern, category) VALUES (?,?)",
                               (merchant_pattern, category)).lastrowid
            conn.commit()
        return rid

    def active_rules(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT id, merchant_pattern, category FROM category_rules"
                                " WHERE status='active' ORDER BY id").fetchall()
        return [{"id": i, "merchant_pattern": p, "category": c} for i, p, c in rows]

    def remove_rule(self, rule_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute("UPDATE category_rules SET status='removed'"
                               " WHERE id=? AND status='active'", (rule_id,))
            conn.commit()
        return cur.rowcount > 0
