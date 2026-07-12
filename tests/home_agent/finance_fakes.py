def contract(**over):
    """Canonical Collector JSON contract (strings for money)."""
    data = {
        "source": "discount", "scraped_at": "2026-07-12T18:00:00+03:00",
        "accounts": [{
            "account": "1", "balance": "1200.50",
            "transactions": [
                {"identifier": "A1", "date": "2026-07-01T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "-450.00", "chargedCurrency": "ILS", "description": "שופרסל", "status": "completed"},
                {"identifier": None, "date": "2026-07-02T00:00:00.000Z", "processedDate": None,
                 "chargedAmount": "1000.00", "chargedCurrency": "ILS", "description": "משכורת", "status": "completed"},
            ],
        }],
    }
    data.update(over)
    return data


def make_fetch(data):
    calls = {"n": 0}
    def _fetch():
        calls["n"] += 1
        return data
    _fetch.calls = calls
    return _fetch
