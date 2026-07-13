from __future__ import annotations

from app.v31_validation import compare_v31_to_plain_breakout


def _bars(count: int = 500):
    rows = []
    price = 1.0
    for index in range(count):
        close = price * (1.002 if index % 80 < 60 else 0.999)
        rows.append([index * 3_600_000, str(price), str(max(price, close) * 1.001), str(min(price, close) * 0.999), str(close), "1000", (index + 1) * 3_600_000 - 1, "1000", 10, "600", "600", "0"])
        price = close
    return rows


def test_v31_validation_is_split_and_cost_aware():
    result = compare_v31_to_plain_breakout(_bars(), cost_pct=0.12)

    assert result["v31"]["status"] == "ok"
    assert result["v31"]["cost_pct"] == 0.12
    assert "out_of_sample" in result["v31"]
    assert result["plain_donchian"]["path_filter"] is False
