from home_agent.finance_store import FinanceStore


def test_coverage(tmp_path):
    s = FinanceStore(str(tmp_path / "f.db"))
    s.record_coverage("max", "1743", "2025-08-01", "2026-08-04", "2026-08-04T10:00")
    assert s.is_covered("max", "1743", "2026-04-01", "2026-06-30")
    assert not s.is_covered("max", "1743", "2024-01-01", "2024-02-01")  # before window
    assert not s.is_covered("max", "1743", "2026-04-01", "2026-09-01")  # past coverage_end
    assert s.covered_cards("max", "2026-04-01", "2026-06-30") == {"1743"}
