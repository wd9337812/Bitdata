from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2020_2023",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h_2024_2025",
    ROOT / "data" / "research" / "binance_um_point_in_time_1h",
)
DEFAULT_OUTPUT = ROOT / "data" / "research" / "s0_pure_attention_momentum"
BASE_ROUND_TRIP_COST_PCT = 0.36
STRESS_ROUND_TRIP_COST_PCT = 0.60
MINIMUM_24H_QUOTE_VOLUME = 20_000_000.0
MINIMUM_UNIVERSE = 20


def symbol_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(
        path,
        columns=["symbol", "open_time", "open", "close", "quote_volume"],
    ).sort_values("open_time")
    frame["available_ms"] = frame.open_time.astype("int64") + 3_600_000
    # At close t, the t-24 to t-23 return is known and will leave the
    # exchange's rolling 24-hour display over the next hour.
    frame["stale_return"] = frame.close.shift(23) / frame.close.shift(24) - 1.0
    # Signal at close t, fill at open t+1, exit at open t+2.
    frame["gross_pct"] = (frame.open.shift(-2) / frame.open.shift(-1) - 1.0) * 100.0
    frame["liquidity_24h"] = frame.quote_volume.rolling(24, min_periods=24).sum()
    return frame.dropna(subset=["stale_return", "gross_pct", "liquidity_24h"])


def build_panel(data_dirs: tuple[Path, ...]) -> pd.DataFrame:
    parts = [
        symbol_frame(path)
        for root in data_dirs
        for path in sorted((root / "parquet").glob("*.parquet"))
    ]
    if not parts:
        raise ValueError("No point-in-time Binance parquet files found")
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values(["available_ms", "symbol"])
        .drop_duplicates(["available_ms", "symbol"], keep="last")
        .reset_index(drop=True)
    )


def select_trades(panel: pd.DataFrame) -> pd.DataFrame:
    eligible = panel.loc[
        panel.liquidity_24h.ge(MINIMUM_24H_QUOTE_VOLUME)
        & panel.stale_return.lt(0)
    ].copy()
    eligible["universe_size"] = eligible.groupby("available_ms").symbol.transform(
        "size"
    )
    eligible = eligible.loc[eligible.universe_size.ge(MINIMUM_UNIVERSE)]
    trades = (
        eligible.sort_values(
            ["available_ms", "stale_return", "liquidity_24h"],
            ascending=[True, True, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .rename(columns={"available_ms": "entry_ms"})
        .reset_index(drop=True)
    )
    trades["direction"] = "LONG"
    return trades


def metrics(trades: pd.DataFrame, cost_pct: float) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "symbols": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "net_pct_points": 0.0,
            "max_drawdown_pct_points": 0.0,
        }
    net = trades.gross_pct - float(cost_pct)
    wins = float(net.loc[net > 0].sum())
    losses = float(-net.loc[net < 0].sum())
    cumulative = net.cumsum()
    drawdown = cumulative.cummax() - cumulative
    return {
        "trades": int(len(trades)),
        "symbols": int(trades.symbol.nunique()),
        "win_rate": round(float(net.gt(0).mean() * 100.0), 4),
        "profit_factor": round(wins / losses, 4) if losses else (999.0 if wins else 0.0),
        "net_pct_points": round(float(net.sum()), 6),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
    }


def report_for(trades: pd.DataFrame, cost_pct: float) -> dict[str, Any]:
    years = pd.to_datetime(trades.entry_ms, unit="ms", utc=True).dt.year
    annual = {
        str(int(year)): metrics(group, cost_pct)
        for year, group in trades.assign(year=years).groupby("year", sort=True)
    }
    top_symbols = (
        trades.assign(net_pct=trades.gross_pct - cost_pct)
        .groupby("symbol")
        .net_pct.sum()
        .nlargest(3)
        .index
    )
    return {
        "overall": metrics(trades, cost_pct),
        "annual": annual,
        "without_top_3_symbols": metrics(
            trades.loc[~trades.symbol.isin(top_symbols)], cost_pct
        ),
    }


def qualifies(stress: dict[str, Any]) -> bool:
    required_years = {str(year) for year in range(2021, 2027)}
    annual = stress["annual"]
    return bool(
        required_years.issubset(annual)
        and stress["overall"]["profit_factor"] > 1.10
        and all(
            annual[year]["profit_factor"] > 1.0
            and annual[year]["net_pct_points"] > 0
            for year in required_years
        )
        and stress["without_top_3_symbols"]["net_pct_points"] > 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-year audit of rolling-24h pure attention momentum."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    trades = select_trades(build_panel(DEFAULT_DATA))
    reports = {
        "gross": report_for(trades, 0.0),
        "base": report_for(trades, BASE_ROUND_TRIP_COST_PCT),
        "stress": report_for(trades, STRESS_ROUND_TRIP_COST_PCT),
    }
    accepted = qualifies(reports["stress"])
    result = {
        "experiment": "s0_pure_attention_momentum",
        "source_method": "Fracassi-Kogan rolling 24-hour pure momentum proxy",
        "execution": "signal at completed hour; next-open entry; one-hour hold",
        "selection": "most negative stale hourly return among liquid contracts",
        "minimum_24h_quote_volume": MINIMUM_24H_QUOTE_VOLUME,
        "minimum_universe": MINIMUM_UNIVERSE,
        "base_round_trip_cost_pct": BASE_ROUND_TRIP_COST_PCT,
        "stress_round_trip_cost_pct": STRESS_ROUND_TRIP_COST_PCT,
        "reports": reports,
        "accepted": accepted,
        "decision": (
            "eligible_for_independent_minute_validation"
            if accepted
            else "rejected_not_stable_positive_expectancy"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(args.output / "trades.parquet", index=False)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
