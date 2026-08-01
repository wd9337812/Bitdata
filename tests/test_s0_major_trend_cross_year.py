import pandas as pd

from scripts.audit_s0_major_trend_cross_year import extract_trades, metrics


def test_empty_position_path_has_stable_trade_schema() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
            "symbol": [None, None],
            "direction": [0, 0],
            "gross_return": [0.0, 0.0],
            "turnover": [0.0, 0.0],
        }
    )

    trades = extract_trades(daily, 0.0012)
    report = metrics(daily, 0.0012)

    assert list(trades.columns) == ["entry_day", "exit_day", "net_return"]
    assert report["trades"] == 0
    assert report["net_pct"] == 0.0
