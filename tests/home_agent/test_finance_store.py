from home_agent.finance_store import FinanceStore


def _store(tmp_path):
    return FinanceStore(str(tmp_path / "fin.db"))


def _row(**kw):
    base = dict(source="discount", account="1", identifier="A1", fingerprint="id:A1",
                txn_date="2026-07-01", processed_date=None, amount_agorot=-45000,
                currency="ILS", description="שופרסל", status="completed", raw_json="{}")
    base.update(kw)
    return base


def test_upsert_inserts_then_dedups(tmp_path):
    s = _store(tmp_path)
    assert s.upsert_transactions([_row()]) == (1, 0)
    assert s.upsert_transactions([_row()]) == (0, 1)  # same fingerprint → update, not duplicate


def test_upsert_pending_to_settled_mutates_row(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([_row(status="pending", amount_agorot=-45000)])
    s.upsert_transactions([_row(status="completed", amount_agorot=-45050)])
    got = s.search()
    assert len(got) == 1 and got[0]["status"] == "completed" and got[0]["amount_agorot"] == -45050


def test_current_balance_sums_latest_snapshot_per_account(tmp_path):
    s = _store(tmp_path)
    s.record_snapshot("discount", "1", "2026-07-01T00:00:00Z", 100000)
    s.record_snapshot("discount", "1", "2026-07-12T00:00:00Z", 120000)  # newer for acct 1
    s.record_snapshot("discount", "2", "2026-07-05T00:00:00Z", 30000)
    assert s.current_balance_agorot() == 150000  # 120000 + 30000


def test_sum_amounts_income_and_expense(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([
        _row(identifier="i", fingerprint="id:i", amount_agorot=1000000, description="משכורת"),
        _row(identifier="e", fingerprint="id:e", amount_agorot=-45000),
    ])
    assert s.sum_amounts("2026-07-01", "2026-07-31") == (1000000, -45000)


def test_search_absolute_amount_and_direction(tmp_path):
    s = _store(tmp_path)
    s.upsert_transactions([
        _row(identifier="a", fingerprint="id:a", amount_agorot=-45000, description="chargeX"),
        _row(identifier="b", fingerprint="id:b", amount_agorot=1000000, description="salary"),
    ])
    hit = s.search(min_abs=45000, max_abs=45000)
    assert len(hit) == 1 and hit[0]["description"] == "chargeX"
    assert len(s.search(direction="income")) == 1


def test_rule_add_list_remove(tmp_path):
    s = _store(tmp_path)
    rid = s.add_rule("שופרסל", "groceries")
    assert [r["merchant_pattern"] for r in s.active_rules()] == ["שופרסל"]
    assert s.remove_rule(rid) is True
    assert s.active_rules() == []
