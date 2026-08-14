import fcntl
import os
import tempfile
from datetime import datetime

from home_agent.finance import build_finance_tools
from home_agent.finance_store import FinanceStore
from finance_fakes import contract, max_contract, make_fetch


def _store():
    return FinanceStore(os.path.join(tempfile.mkdtemp(), "f.db"))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _frozen():
    return datetime(2026, 7, 12, 12, 0, 0)


def test_sync_one_source_failing_does_not_abort_others():
    store = _store()

    def boom():
        raise RuntimeError("max collector exploded")

    fetch_fns = {"discount": make_fetch(contract()), "max": boom}
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns=fetch_fns)
    out = _tool(tools, "sync_finances").impl({})

    # Discount rows were committed despite Max failing.
    assert store.current_balance_agorot() == 120050
    # The report mentions both a success and an error, without aborting.
    assert "discount" in out.lower()
    assert "max" in out.lower()


def test_sync_records_coverage_per_source():
    store = _store()
    fetch_fns = {"discount": make_fetch(contract()), "max": make_fetch(max_contract())}
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns=fetch_fns)
    _tool(tools, "sync_finances").impl({})

    # After sync, the imported Max card/account is covered for a window inside its data.
    assert store.is_covered("max", "1", "2026-07-01", "2026-07-02") is True
    assert store.is_covered("max", "2", "2026-07-01", "2026-07-02") is True
    assert store.is_covered("discount", "1", "2026-07-01", "2026-07-02") is True


def test_sync_lock_wraps_whole_loop_and_refuses_concurrent_run():
    """A held lock refuses the sync wholesale — no fetcher runs (lock is ONCE around the loop)."""
    store = _store()

    called = {"n": 0}

    def counting_fetch():
        called["n"] += 1
        return contract()

    fetch_fns = {"discount": counting_fetch, "max": counting_fetch}
    tools = build_finance_tools(store, now_fn=_frozen, fetch_fns=fetch_fns)

    # Hold the same lock run_finance_sync uses (next to the DB), simulating a concurrent sync.
    lock_path = os.path.join(os.path.dirname(store.db_path), ".finance_sync.lock")
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = _tool(tools, "sync_finances").impl({})

    # Whole sync was refused before touching any source; nothing committed.
    assert called["n"] == 0
    assert store.current_balance_agorot() == 0
    assert "כבר רץ" in out
