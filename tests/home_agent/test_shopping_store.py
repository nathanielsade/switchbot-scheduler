from home_agent.shopping_store import ShoppingStore


def test_add_and_pending(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב", quantity="2", note="3%")
    s.add("קפה")
    assert s.pending() == [
        {"item": "חלב", "quantity": "2", "note": "3%"},
        {"item": "קפה", "quantity": None, "note": None},
    ]


def test_same_name_is_one_canonical_item_two_list_rows(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    s.add("חלב")
    assert s.known_items() == ["חלב"]              # one canonical item
    assert len(s.pending()) == 2                    # but two list entries


def test_known_items_sorted(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("קפה")
    s.add("חלב")
    assert s.known_items() == sorted(["קפה", "חלב"])


def test_remove_flips_status_and_is_append_only(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    assert s.remove("חלב") == 1
    assert s.pending() == []                        # no longer pending
    assert s.remove("חלב") == 0                     # nothing pending to remove now
    assert s.remove("לא-קיים") == 0                 # unknown item
    # append-only: the row still exists (as 'removed'), not deleted
    import sqlite3
    n = sqlite3.connect(str(tmp_path / "sh.db")).execute("SELECT COUNT(*) FROM list").fetchone()[0]
    assert n == 1


def test_buy_logs_purchase_and_marks_pending_bought(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.add("חלב")
    s.buy("חלב", "2026-07-09", quantity=1, unit_price=6.9)
    assert s.pending() == []                        # left the list
    assert s.purchases_for("חלב") == [
        {"purchased_on": "2026-07-09", "quantity": 1.0, "unit_price": 6.9, "source": "chat"}
    ]


def test_buy_unlisted_item_still_logs_purchase(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("במבה", "2026-07-09")                      # never on the list
    assert [p["purchased_on"] for p in s.purchases_for("במבה")] == ["2026-07-09"]


def test_usable_from_a_different_thread(tmp_path):
    import threading
    s = ShoppingStore(str(tmp_path / "sh.db"))
    errors = []

    def worker():
        try:
            s.add("חלב")
            assert len(s.pending()) == 1
        except Exception as e:
            errors.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []


def test_purchase_dates_by_item_collapses_same_day(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("חלב", "2026-07-01")
    s.buy("חלב", "2026-07-01")   # same day again — must collapse to one date
    s.buy("חלב", "2026-07-06")
    s.buy("קפה", "2026-07-03")
    d = s.purchase_dates_by_item()
    assert d["חלב"] == ["2026-07-01", "2026-07-06"]   # distinct, ascending
    assert d["קפה"] == ["2026-07-03"]


def test_recent_purchases_newest_first_with_limit(tmp_path):
    s = ShoppingStore(str(tmp_path / "sh.db"))
    s.buy("חלב", "2026-07-01", unit_price=6.9)
    s.buy("קפה", "2026-07-05")
    s.buy("לחם", "2026-07-03")
    recent = s.recent_purchases(limit=2)
    assert [r["item"] for r in recent] == ["קפה", "לחם"]   # newest first, limited
    assert recent[0]["purchased_on"] == "2026-07-05"
