from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_cross_sectional_momentum import (
    PROFILES,
    rank_signals,
    simulate_minute,
)

DEFAULT_RESEARCH_DIR = (
    ROOT / "data" / "research" / "s0_cross_sectional_momentum_v1"
)
DEFAULT_MINUTE_DIR = ROOT / "data" / "research" / "s0_public_1m" / "parquet"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
PROFILE = "momentum_24h_5pct"
EXECUTION_MODE = "minute_delay_1m"

WINDOWS = {
    "development": ("2026-02-01", "2026-04-01"),
    "validation": ("2026-04-01", "2026-06-01"),
    "test": ("2026-06-01", "2026-07-01"),
    "final_july": ("2026-07-01", "2026-08-01"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the S0 cross-sectional momentum candidate for concentration, "
            "listing-age, regime, latency, cost, and block-bootstrap risk."
        )
    )
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    parser.add_argument("--minute-dir", type=Path, default=DEFAULT_MINUTE_DIR)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260731)
    return parser.parse_args()


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "symbols": 0,
            "profit_factor": 0.0,
            "net_pct_points": 0.0,
            "mean_net_pct": 0.0,
            "max_drawdown_pct_points": 0.0,
        }
    wins = float(frame.net_pct.clip(lower=0).sum())
    losses = float(-frame.net_pct.clip(upper=0).sum())
    cumulative = frame.net_pct.cumsum()
    drawdown = cumulative.cummax() - cumulative
    return {
        "trades": int(len(frame)),
        "symbols": int(frame.symbol.nunique()),
        "win_rate": round(float(frame.net_pct.gt(0).mean() * 100), 4),
        "profit_factor": round(wins / losses, 4) if losses > 0 else 999.0,
        "net_pct_points": round(float(frame.net_pct.sum()), 6),
        "mean_net_pct": round(float(frame.net_pct.mean()), 6),
        "max_drawdown_pct_points": round(float(drawdown.max()), 6),
    }


def summarize_windows(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, (start, end) in WINDOWS.items():
        mask = frame.entry_time.ge(pd.Timestamp(start, tz="UTC")) & frame.entry_time.lt(
            pd.Timestamp(end, tz="UTC")
        )
        result[name] = summarize(frame.loc[mask])
    return result


def load_exchange_info(cache_path: Path) -> dict[str, int]:
    try:
        response = requests.get(EXCHANGE_INFO_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except (requests.RequestException, ValueError):
        if not cache_path.exists():
            raise
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    return {
        str(item["symbol"]): int(item.get("onboardDate") or 0)
        for item in payload.get("symbols", [])
        if item.get("symbol")
    }


def enrich_trades(
    trades: pd.DataFrame,
    panel: pd.DataFrame,
    onboard_dates: dict[str, int],
) -> pd.DataFrame:
    selected = trades[
        trades.profile.eq(PROFILE) & trades.execution_mode.eq(EXECUTION_MODE)
    ].copy()
    features = panel[
        ["available_ms", "symbol", "ret_24h", "liquidity_24h"]
    ].copy()
    breadth = panel.groupby("available_ms", sort=False).ret_24h.median()
    btc_returns = (
        panel.loc[panel.symbol.eq("BTCUSDT")]
        .set_index("available_ms")
        .ret_24h
    )
    selected = selected.merge(
        features,
        left_on=["signal_ms", "symbol"],
        right_on=["available_ms", "symbol"],
        how="left",
        validate="many_to_one",
    )
    selected["market_breadth_24h"] = selected.signal_ms.map(breadth)
    selected["btc_ret_24h"] = selected.signal_ms.map(btc_returns)
    selected["onboard_ms"] = selected.symbol.map(onboard_dates)
    selected["symbol_age_days"] = (
        selected.signal_ms - selected.onboard_ms
    ) / 86_400_000
    selected["entry_time"] = pd.to_datetime(
        selected.entry_ms,
        unit="ms",
        utc=True,
    )
    if selected.onboard_ms.isna().any():
        missing = sorted(selected.loc[selected.onboard_ms.isna(), "symbol"].unique())
        raise ValueError(f"Missing Binance onboardDate for: {missing}")
    required = [
        "ret_24h",
        "liquidity_24h",
        "market_breadth_24h",
        "btc_ret_24h",
        "symbol_age_days",
    ]
    if selected[required].isna().any().any():
        raise ValueError("Momentum audit enrichment produced missing feature values.")
    return selected.sort_values("entry_ms").reset_index(drop=True)


def block_bootstrap(
    frame: pd.DataFrame,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    if frame.empty:
        return {"calendar_weeks": 0, "samples": samples}
    scoped = frame.copy()
    iso = scoped.entry_time.dt.isocalendar()
    scoped["week"] = iso.year.astype(str) + "-W" + iso.week.astype(str).str.zfill(2)
    weekly = scoped.groupby("week", sort=True).net_pct.sum().to_numpy()
    rng = np.random.default_rng(seed)
    draws = rng.choice(
        weekly,
        size=(samples, len(weekly)),
        replace=True,
    ).sum(axis=1)
    return {
        "calendar_weeks": int(len(weekly)),
        "samples": int(samples),
        "probability_net_not_positive": round(float(np.mean(draws <= 0)), 6),
        "net_pct_points_p025": round(float(np.quantile(draws, 0.025)), 6),
        "net_pct_points_p50": round(float(np.quantile(draws, 0.5)), 6),
        "net_pct_points_p975": round(float(np.quantile(draws, 0.975)), 6),
    }


def grouped_summaries(
    frame: pd.DataFrame,
    key: str,
) -> dict[str, dict[str, Any]]:
    return {
        str(value): summarize(scoped)
        for value, scoped in frame.groupby(key, observed=True, sort=True)
    }


def contribution_audit(frame: pd.DataFrame) -> dict[str, Any]:
    contributions = frame.groupby("symbol").net_pct.sum().sort_values(ascending=False)
    positive_gross = float(frame.net_pct.clip(lower=0).sum())
    result: dict[str, Any] = {
        "top_10": {
            symbol: round(float(value), 6)
            for symbol, value in contributions.head(10).items()
        },
        "top_5_share_of_all_positive_trade_pnl": round(
            float(contributions.head(5).sum() / positive_gross),
            6,
        )
        if positive_gross > 0
        else 0.0,
    }
    for count in (1, 3, 5, 10):
        removed = set(contributions.head(count).index)
        result[f"remove_top_{count}"] = summarize(
            frame.loc[~frame.symbol.isin(removed)]
        )
    return result


def evaluate_filter(
    frame: pd.DataFrame,
    predicate: Callable[[pd.DataFrame], pd.Series],
) -> dict[str, Any]:
    scoped = frame.loc[predicate(frame)].copy()
    return {
        "overall": summarize(scoped),
        "windows": summarize_windows(scoped),
    }


def hypothesis_filters(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    filters: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
        "baseline": lambda data: pd.Series(True, index=data.index),
        "listing_age_at_least_14d": lambda data: data.symbol_age_days.ge(14),
        "listing_age_at_least_30d": lambda data: data.symbol_age_days.ge(30),
        "listing_age_at_least_60d": lambda data: data.symbol_age_days.ge(60),
        "absolute_breadth_at_least_0_5pct": (
            lambda data: data.market_breadth_24h.abs().ge(0.005)
        ),
        "absolute_breadth_at_least_1pct": (
            lambda data: data.market_breadth_24h.abs().ge(0.01)
        ),
        "strength_at_least_0_99": lambda data: data.strength.ge(0.99),
    }
    return {
        name: evaluate_filter(frame, predicate)
        for name, predicate in filters.items()
    }


def select_development_hypothesis(
    hypotheses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    baseline = hypotheses["baseline"]["windows"]["development"]
    eligible: list[tuple[str, dict[str, Any]]] = []
    for name, report in hypotheses.items():
        if name == "baseline":
            continue
        development = report["windows"]["development"]
        if (
            development["trades"] >= baseline["trades"] * 0.75
            and development["profit_factor"] > baseline["profit_factor"]
            and development["net_pct_points"] > baseline["net_pct_points"]
            and development["max_drawdown_pct_points"]
            <= baseline["max_drawdown_pct_points"]
        ):
            eligible.append((name, development))
    if not eligible:
        return {
            "selected": None,
            "reason": "No hypothesis improved all development-only guardrails.",
        }
    selected, development = max(
        eligible,
        key=lambda item: (
            item[1]["profit_factor"],
            item[1]["net_pct_points"],
            item[1]["trades"],
        ),
    )
    return {
        "selected": selected,
        "selection_scope": "development_only",
        "guardrails": {
            "minimum_trade_retention": 0.75,
            "profit_factor_must_improve": True,
            "net_must_improve": True,
            "drawdown_must_not_increase": True,
        },
        "development": development,
        "frozen_out_of_sample": {
            key: hypotheses[selected]["windows"][key]
            for key in ("validation", "test", "final_july")
        },
        "warning": (
            "This is a historical hypothesis, not live qualification. "
            "The same data family was inspected during research and requires "
            "fresh real-time shadow evidence."
        ),
    }


def listing_age_latency_stress(
    all_trades: pd.DataFrame,
    panel: pd.DataFrame,
    minute_dir: Path,
    onboard_dates: dict[str, int],
    minimum_age_days: float,
) -> dict[str, Any]:
    profile = next(item for item in PROFILES if item.name == PROFILE)
    signals = rank_signals(panel, profile)
    report: dict[str, Any] = {}
    for delay in range(1, 6):
        mode = f"minute_delay_{delay}m"
        scoped = all_trades.loc[
            all_trades.profile.eq(PROFILE)
            & all_trades.execution_mode.eq(mode)
        ].copy()
        if scoped.empty:
            scoped = simulate_minute(
                signals,
                minute_dir,
                profile,
                execution_delay_minutes=delay,
            )
        scoped["onboard_ms"] = scoped.symbol.map(onboard_dates)
        if scoped.onboard_ms.isna().any():
            raise ValueError(f"Missing onboardDate in {delay}-minute latency replay.")
        scoped["symbol_age_days"] = (
            scoped.signal_ms - scoped.onboard_ms
        ) / 86_400_000
        scoped["entry_time"] = pd.to_datetime(
            scoped.entry_ms,
            unit="ms",
            utc=True,
        )
        scoped = scoped.loc[scoped.symbol_age_days.ge(minimum_age_days)].copy()
        stressed = scoped.copy()
        stressed["net_pct"] = stressed.gross_pct - 0.24
        report[f"{delay}m"] = {
            "base_cost_0_12pct": {
                "overall": summarize(scoped),
                "windows": summarize_windows(scoped),
            },
            "stressed_cost_0_24pct": {
                "overall": summarize(stressed),
                "windows": summarize_windows(stressed),
            },
        }
    return report


def main() -> None:
    args = parse_args()
    research_dir = args.research_dir
    trades = pd.read_parquet(research_dir / "trades.parquet")
    panel = pd.read_parquet(research_dir / "hourly_panel.parquet")
    onboard_dates = load_exchange_info(research_dir / "exchange_info_snapshot.json")
    enriched = enrich_trades(trades, panel, onboard_dates)
    enriched["listing_age_bucket"] = pd.cut(
        enriched.symbol_age_days,
        bins=[-np.inf, 7, 14, 30, 60, 90, 180, 365, np.inf],
        labels=["<7d", "7-14d", "14-30d", "30-60d", "60-90d", "90-180d", "180-365d", "365d+"],
    )
    enriched["month"] = enriched.entry_time.dt.strftime("%Y-%m")

    hypotheses = hypothesis_filters(enriched)
    selection = select_development_hypothesis(hypotheses)
    selected_name = selection.get("selected")
    selected_age_days = (
        30.0 if selected_name == "listing_age_at_least_30d" else 0.0
    )
    report = {
        "candidate": f"{PROFILE}@{EXECUTION_MODE}",
        "evidence_status": "historical_candidate_requires_realtime_shadow",
        "overall": summarize(enriched),
        "windows": summarize_windows(enriched),
        "by_month": grouped_summaries(enriched, "month"),
        "by_direction": grouped_summaries(enriched, "direction"),
        "by_listing_age": grouped_summaries(enriched, "listing_age_bucket"),
        "contribution": contribution_audit(enriched),
        "weekly_block_bootstrap": {
            "overall": block_bootstrap(enriched, args.bootstrap_samples, args.seed),
            "windows": {
                name: block_bootstrap(
                    enriched.loc[
                        enriched.entry_time.ge(pd.Timestamp(start, tz="UTC"))
                        & enriched.entry_time.lt(pd.Timestamp(end, tz="UTC"))
                    ],
                    args.bootstrap_samples,
                    args.seed + index + 1,
                )
                for index, (name, (start, end)) in enumerate(WINDOWS.items())
            },
        },
        "hypotheses": hypotheses,
        "development_only_selection": selection,
        "latency_stress": json.loads(
            (research_dir / "delay_stress.json").read_text(encoding="utf-8")
        ),
        "selected_hypothesis_latency_and_cost_stress": (
            listing_age_latency_stress(
                trades,
                panel,
                args.minute_dir,
                onboard_dates,
                minimum_age_days=selected_age_days,
            )
            if selected_name == "listing_age_at_least_30d"
            else {}
        ),
        "limitations": [
            "The historical universe is the 119-symbol research universe, not every contract listed at each historical timestamp.",
            "Minute-bar replay cannot model queue position or sub-minute adverse selection.",
            "The live shadow uses Binance rolling 24-hour ticker change as a low-cost proxy, while this replay uses completed-hour returns.",
            "Historical selection discovery and repeated inspection create data-snooping risk; fresh real-time shadow evidence is mandatory.",
            "Positive percentage-point totals do not prove positive account growth under 30% per-trade risk or prevent ruin.",
        ],
    }
    (research_dir / "robustness_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    enriched.to_parquet(research_dir / "audited_trades.parquet", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
