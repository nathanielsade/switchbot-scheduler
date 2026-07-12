from decimal import Decimal
from home_agent.finance import normalize_contract, _to_agorot, _fingerprint, build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, make_fetch


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_to_agorot_uses_decimal_half_up():
    assert _to_agorot("-450.00") == -45000
    assert _to_agorot("0.005") == 1        # ROUND_HALF_UP, not banker's (would be 0)
    assert isinstance(_to_agorot("1.00"), int)


def test_fingerprint_prefers_identifier_else_hash():
    assert _fingerprint("discount", "1", "A1", "2026-07-01", -45000, "x") == "id:A1"
    h = _fingerprint("discount", "1", None, "2026-07-01", -45000, "שופרסל")
    assert h.startswith("h:") and h == _fingerprint("discount", "1", None, "2026-07-01", -45000, " שופרסל ")


def test_normalize_contract_shapes_rows_and_snapshots():
    txns, snaps, counts = normalize_contract(contract())
    assert snaps == [{"source": "discount", "account": "1",
                      "scraped_at": "2026-07-12T18:00:00+03:00", "balance_agorot": 120050}]
    amounts = sorted(r["amount_agorot"] for r in txns)
    assert amounts == [-45000, 100000]
    assert all(r["txn_date"] == "2026-07-01" or r["txn_date"] == "2026-07-02" for r in txns)
    assert counts["dropped"] == 0


def test_sync_finances_imports_and_reports_counts():
    import tempfile, os
    store = FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))
    tools = build_finance_tools(store, fetch_fn=make_fetch(contract()))
    out = _tool(tools, "sync_finances").impl({})
    assert "2" in out  # 2 imported (model-facing count)
    assert store.current_balance_agorot() == 120050


def test_sync_finances_malformed_is_friendly():
    store = FinanceStore(__import__("tempfile").mktemp())
    def boom(): raise ValueError("collector produced no JSON")
    out = _tool(build_finance_tools(store, fetch_fn=boom), "sync_finances").impl({})
    assert "לא הצלחתי" in out or "couldn" in out.lower() or "failed" in out.lower()
