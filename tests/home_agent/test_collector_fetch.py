"""Tests for collector fetch and contract normalization."""
from decimal import Decimal
from home_agent.finance import normalize_contract, make_collector_fetch
from home_agent.config import Config
from finance_fakes import max_contract, contract
import json


def test_normalize_contract_max_multi_card():
    """normalize_contract on a max-shaped two-card payload."""
    data = max_contract()
    txns, snaps, counts = normalize_contract(data)

    # Check snapshots: two accounts
    assert len(snaps) == 2
    assert snaps[0]["source"] == "max"
    assert snaps[0]["account"] == "1"
    assert snaps[0]["balance_agorot"] == 250000
    assert snaps[1]["account"] == "2"
    assert snaps[1]["balance_agorot"] == 500000

    # Check transactions: 3 total (account 1 has 2, account 2 has 1)
    assert len(txns) == 3
    assert all(t["source"] == "max" for t in txns)

    # Check amounts (in agorot)
    amounts = sorted(t["amount_agorot"] for t in txns)
    assert amounts == [-50000, -30000, -15050]

    # Verify account assignments
    acc1_txns = [t for t in txns if t["account"] == "1"]
    acc2_txns = [t for t in txns if t["account"] == "2"]
    assert len(acc1_txns) == 2
    assert len(acc2_txns) == 1

    assert counts["dropped"] == 0


def test_normalize_contract_max_vs_discount_source():
    """Transactions from different sources have correct source field."""
    max_data = max_contract()
    max_txns, _, _ = normalize_contract(max_data)
    assert all(t["source"] == "max" for t in max_txns)

    discount_data = contract()
    discount_txns, _, _ = normalize_contract(discount_data)
    assert all(t["source"] == "discount" for t in discount_txns)


def test_make_collector_fetch_discount_env():
    """make_collector_fetch(config) with default source sets correct discount env."""
    # Build a fake config
    cfg = Config(
        openai_api_key="key",
        telegram_bot_token="token",
        allowed_chat_ids=set(),
        discount_id="ID123",
        discount_password="pass123",
        discount_num="num123",
        finance_node_bin="node",
        finance_collector_script="collector/scrape_discount.js",
        finance_start_days=400,
        db_path="/tmp/test.db",
    )

    # Capture env passed to subprocess
    captured_env = {}
    def fake_run(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        # Return a fake result
        class FakeProc:
            returncode = 0
            stdout = json.dumps(contract())
        return FakeProc()

    # Inject the fake subprocess.run
    import home_agent.finance as finance_module
    import unittest.mock as mock
    with mock.patch("subprocess.run", side_effect=fake_run):
        fetch = make_collector_fetch(cfg, source="discount")
        fetch()

    # Check env
    assert captured_env.get("DISCOUNT_ID") == "ID123"
    assert captured_env.get("DISCOUNT_PASSWORD") == "pass123"
    assert captured_env.get("DISCOUNT_NUM") == "num123"
    assert "FINANCE_START_DATE" in captured_env


def test_make_collector_fetch_max_env():
    """make_collector_fetch(config, 'max') sets correct max env."""
    cfg = Config(
        openai_api_key="key",
        telegram_bot_token="token",
        allowed_chat_ids=set(),
        max_username="user@max.com",
        max_password="maxpass456",
        finance_node_bin="node",
        max_collector_script="collector/scrape_max.js",
        finance_start_days=400,
        db_path="/tmp/test.db",
    )

    captured_env = {}
    def fake_run(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        class FakeProc:
            returncode = 0
            stdout = json.dumps(max_contract())
        return FakeProc()

    import unittest.mock as mock
    with mock.patch("subprocess.run", side_effect=fake_run):
        fetch = make_collector_fetch(cfg, source="max")
        fetch()

    # Check env
    assert captured_env.get("MAX_USERNAME") == "user@max.com"
    assert captured_env.get("MAX_PASSWORD") == "maxpass456"
    assert "FINANCE_START_DATE" in captured_env


def test_make_collector_fetch_finance_start_date_env():
    """make_collector_fetch sets FINANCE_START_DATE in env."""
    cfg = Config(
        openai_api_key="key",
        telegram_bot_token="token",
        allowed_chat_ids=set(),
        discount_id="ID",
        discount_password="pass",
        discount_num="num",
        finance_node_bin="node",
        finance_collector_script="collector/scrape_discount.js",
        finance_start_days=30,  # 30 days
        db_path="/tmp/test.db",
    )

    captured_env = {}
    def fake_run(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        class FakeProc:
            returncode = 0
            stdout = json.dumps(contract())
        return FakeProc()

    import unittest.mock as mock
    with mock.patch("subprocess.run", side_effect=fake_run):
        fetch = make_collector_fetch(cfg, source="discount")
        fetch()

    # Check that FINANCE_START_DATE is set
    assert "FINANCE_START_DATE" in captured_env
    # Should be a valid ISO date format (YYYY-MM-DD)
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", captured_env["FINANCE_START_DATE"])


def test_make_collector_fetch_invalid_source():
    """make_collector_fetch with invalid source raises ValueError."""
    cfg = Config(
        openai_api_key="key",
        telegram_bot_token="token",
        allowed_chat_ids=set(),
    )

    import pytest
    with pytest.raises(ValueError, match="unknown finance source"):
        make_collector_fetch(cfg, source="invalid")
