from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.benchmark_s0_exact_quarter_hour_flow import (
    Variant,
    build_candidates,
    run,
    select_non_overlapping,
    trade_metrics,
)


def test_candidate_uses_next_10s_bin_for_entry() -> None:
    frame = pd.DataFrame(
        {
            "bin_ms": [0, 10_000, 14_410_000],
            "open": [100.0, 101.0, 103.0],
            "order_imbalance": [0.5, 0.0, 0.0],
            "time": pd.to_datetime([0, 10_000, 14_410_000], unit="ms", utc=True),
            "symbol": ["A", "A", "A"],
        }
    )
    result = build_candidates(frame)
    assert result.loc[0, "entry_price"] == 101.0
    assert result.loc[0, "exit_price_240"] == 103.0


def test_strongest_candidate_and_non_overlap() -> None:
    times = pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC")
    panel = pd.DataFrame(
        {
            "time": list(times) * 2,
            "symbol": ["A"] * 4 + ["B"] * 4,
            "order_imbalance": [0.5] * 4 + [0.2] * 4,
            "entry_price": [100.0] * 8,
            "gross_forward_240": [0.01] * 8,
        }
    )
    trades = select_non_overlapping(panel, Variant(240, 0.1))
    assert list(trades.symbol) == ["A"]
    assert list(trades.direction) == [1]


def test_trade_metrics_apply_stated_cost() -> None:
    trades = pd.DataFrame({"symbol": ["A", "B"], "gross_return": [0.01, -0.005]})
    result = trade_metrics(trades, 0.001)
    assert result["trades"] == 2
    assert result["mean_gross_bps"] == 25.0
    assert result["mean_net_bps"] == 15.0


def test_run_requires_development_validation_and_full_stress(monkeypatch) -> None:
    import scripts.benchmark_s0_exact_quarter_hour_flow as module

    panel = pd.DataFrame({"time": [pd.Timestamp("2026-01-01", tz="UTC")]})
    trades = pd.DataFrame()
    passing = {"trades": 20, "profit_factor": 1.1, "net_pct_points": 1.0}
    failing = {"trades": 20, "profit_factor": 0.9, "net_pct_points": -1.0}
    windows = {
        "all": {"stress": passing},
        "development": {"stress": failing},
        "validation": {"stress": passing},
    }
    monkeypatch.setattr(module, "build_panel", lambda *_: panel)
    monkeypatch.setattr(module, "select_non_overlapping", lambda *_: trades)
    monkeypatch.setattr(module, "evaluate", lambda *_: windows)
    monkeypatch.setattr(module, "THRESHOLDS", (0.0,))
    monkeypatch.setattr(module, "HORIZON_BINS", {240: 1_440})
    report = run(Path("unused"), ["A"], "2026-02-15")
    assert report["accepted_variants"] == []
