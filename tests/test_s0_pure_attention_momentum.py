import pandas as pd

from scripts.benchmark_s0_pure_attention_momentum import (
    qualifies,
    select_trades,
    symbol_frame,
)


def test_symbol_frame_uses_removed_hour_and_next_open_execution(tmp_path) -> None:
    rows = 30
    frame = pd.DataFrame(
        {
            "symbol": ["TESTUSDT"] * rows,
            "open_time": pd.Series(range(rows), dtype="int64") * 3_600_000,
            "open": [100.0 + value for value in range(rows)],
            "close": [100.0 + value for value in range(rows)],
            "quote_volume": [1_000_000.0] * rows,
        }
    )
    path = tmp_path / "TESTUSDT.parquet"
    frame.to_parquet(path, index=False)

    result = symbol_frame(path).iloc[0]

    assert result.stale_return == (101.0 / 100.0 - 1.0)
    assert result.gross_pct == (126.0 / 125.0 - 1.0) * 100.0


def test_select_trades_chooses_most_negative_stale_return() -> None:
    frame = pd.DataFrame(
        {
            "available_ms": [1] * 20,
            "symbol": [f"S{index}USDT" for index in range(20)],
            "stale_return": [-0.01 - index / 1000 for index in range(20)],
            "liquidity_24h": [30_000_000.0] * 20,
            "gross_pct": [0.1] * 20,
        }
    )

    result = select_trades(frame)

    assert result.symbol.tolist() == ["S19USDT"]


def test_qualification_requires_every_year_and_diversification() -> None:
    annual = {
        str(year): {"profit_factor": 1.1, "net_pct_points": 1.0}
        for year in range(2021, 2027)
    }
    stress = {
        "overall": {"profit_factor": 1.2},
        "annual": annual,
        "without_top_3_symbols": {"net_pct_points": 2.0},
    }
    assert qualifies(stress)

    stress["annual"]["2023"]["net_pct_points"] = -1.0
    assert not qualifies(stress)
