from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_s0_xmom_regime_candidates import (  # noqa: E402
    BASE_COST,
    STRESS_COST,
    block_bootstrap,
    scope,
)
from scripts.benchmark_s0_cross_sectional_momentum import (  # noqa: E402
    summarize,
)
from scripts.benchmark_s0_point_in_time_breakout import (  # noqa: E402
    DEFAULT_PANEL,
    add_breakout_features,
)
from scripts.benchmark_s0_xmom_point_in_time import (  # noqa: E402
    DEFAULT_DATA,
    load_manifest,
)


DEFAULT_FUNDING = (
    ROOT / "data" / "research" / "binance_um_point_in_time_funding"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_point_in_time_funding"
)


@dataclass(frozen=True)
class FundingCandidate:
    name: str
    setup: str
    minimum_abs_rate_pct: float
    minimum_liquidity_24h: float = 20_000_000
    stop_pct: float = 1.0
    target_pct: float = 1.4
    hold_hours: int = 8


CANDIDATES = (
    FundingCandidate("funding_contrarian_003", "contrarian", 0.03),
    FundingCandidate("funding_contrarian_005", "contrarian", 0.05),
    FundingCandidate("funding_continuation_003", "continuation", 0.03),
    FundingCandidate("funding_continuation_005", "continuation", 0.05),
    FundingCandidate("crowded_reversal_003", "crowded_reversal", 0.03),
    FundingCandidate("crowded_reversal_005", "crowded_reversal", 0.05),
    FundingCandidate("crowded_continuation_003", "crowded_continuation", 0.03),
    FundingCandidate("crowded_continuation_005", "crowded_continuation", 0.05),
    FundingCandidate("squeeze_continuation_003", "squeeze", 0.03),
    FundingCandidate("squeeze_continuation_005", "squeeze", 0.05),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark point-in-time funding crowding and squeeze signals "
            "with frozen temporal validation."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--funding", type=Path, default=DEFAULT_FUNDING)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def merge_funding(
    panel: pd.DataFrame,
    funding_dir: Path,
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, bars in panel.groupby("symbol", sort=False):
        path = funding_dir / f"{symbol}-funding.parquet"
        if not path.exists():
            continue
        funding = pd.read_parquet(path).sort_values("timestamp_ms").copy()
        if funding.empty:
            continue
        # Funding becomes observable after the recorded settlement time.
        funding["funding_available_ms"] = (
            funding.timestamp_ms.astype("int64") + 60_000
        )
        scoped = pd.merge_asof(
            bars.sort_values("available_ms"),
            funding[
                [
                    "funding_available_ms",
                    "timestamp_ms",
                    "last_funding_rate",
                ]
            ].sort_values("funding_available_ms"),
            left_on="available_ms",
            right_on="funding_available_ms",
            direction="backward",
        )
        scoped["symbol"] = symbol
        parts.append(scoped)
    if not parts:
        raise ValueError(f"No funding data found in {funding_dir}")
    result = pd.concat(parts, ignore_index=True)
    result["funding_rate_pct"] = (
        pd.to_numeric(result.last_funding_rate, errors="coerce") * 100
    )
    result["funding_age_hours"] = (
        result.available_ms - result.timestamp_ms
    ) / 3_600_000
    return result.sort_values(["available_ms", "symbol"]).reset_index(drop=True)


def funding_signals(
    panel: pd.DataFrame,
    candidate: FundingCandidate,
    minimum_age_days: int,
) -> pd.DataFrame:
    eligible = panel.loc[
        panel.symbol_age_days.ge(minimum_age_days)
        & panel.liquidity_24h.ge(candidate.minimum_liquidity_24h)
        & panel.funding_age_hours.between(0, 12)
        & panel.funding_rate_pct.abs().ge(candidate.minimum_abs_rate_pct)
    ].copy()
    funding_sign = np.sign(eligible.funding_rate_pct)
    price_sign = np.sign(eligible.ret_6h)
    if candidate.setup in {"crowded_reversal", "crowded_continuation"}:
        eligible = eligible.loc[funding_sign.eq(price_sign)].copy()
    elif candidate.setup == "squeeze":
        eligible = eligible.loc[
            funding_sign.ne(price_sign) & price_sign.ne(0)
        ].copy()
    if candidate.setup == "squeeze":
        eligible["direction"] = np.where(
            eligible.ret_6h.gt(0),
            "LONG",
            "SHORT",
        )
    elif candidate.setup in {"continuation", "crowded_continuation"}:
        eligible["direction"] = np.where(
            eligible.funding_rate_pct.gt(0),
            "LONG",
            "SHORT",
        )
    else:
        eligible["direction"] = np.where(
            eligible.funding_rate_pct.gt(0),
            "SHORT",
            "LONG",
        )
    eligible["funding_strength"] = eligible.funding_rate_pct.abs()
    eligible["strength"] = eligible.groupby(
        "available_ms",
        sort=False,
    ).funding_strength.rank(pct=True)
    return (
        eligible.sort_values(
            ["available_ms", "funding_strength", "liquidity_24h"],
            ascending=[True, False, False],
        )
        .groupby("available_ms", sort=False)
        .head(1)
        .sort_values("available_ms")
        .reset_index(drop=True)
    )


def simulate_funding(
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    candidate: FundingCandidate,
    cost_pct: float,
) -> pd.DataFrame:
    bars = {
        symbol: frame.sort_values("available_ms").reset_index(drop=True)
        for symbol, frame in panel.groupby("symbol", sort=False)
    }
    trades: list[dict[str, Any]] = []
    next_available_ms = -1
    for signal in signals.itertuples(index=False):
        if int(signal.available_ms) < next_available_ms:
            continue
        frame = bars[signal.symbol]
        indices = frame.index[
            frame.available_ms.eq(int(signal.available_ms))
        ].tolist()
        if not indices or indices[0] + 1 >= len(frame):
            continue
        entry_index = indices[0] + 1
        entry = float(frame.loc[entry_index, "open"])
        if not np.isfinite(entry) or entry <= 0:
            continue
        direction = 1.0 if signal.direction == "LONG" else -1.0
        exit_price = float(
            frame.loc[
                min(entry_index + candidate.hold_hours - 1, len(frame) - 1),
                "close",
            ]
        )
        exit_reason = "timeout"
        exit_index = min(
            entry_index + candidate.hold_hours - 1,
            len(frame) - 1,
        )
        for index in range(
            entry_index,
            min(entry_index + candidate.hold_hours, len(frame)),
        ):
            high = float(frame.loc[index, "high"])
            low = float(frame.loc[index, "low"])
            favorable = (
                (high / entry - 1) * 100
                if direction > 0
                else (entry / low - 1) * 100
            )
            adverse = (
                (entry / low - 1) * 100
                if direction > 0
                else (high / entry - 1) * 100
            )
            if adverse >= candidate.stop_pct:
                exit_price = entry * (
                    1 - candidate.stop_pct / 100 * direction
                )
                exit_reason = "stop"
                exit_index = index
                break
            if favorable >= candidate.target_pct:
                exit_price = entry * (
                    1 + candidate.target_pct / 100 * direction
                )
                exit_reason = "target"
                exit_index = index
                break
        gross_pct = direction * (exit_price / entry - 1) * 100
        trades.append(
            {
                "symbol": signal.symbol,
                "direction": signal.direction,
                "entry_ms": int(frame.loc[entry_index, "available_ms"]),
                "exit_ms": int(frame.loc[exit_index, "available_ms"]),
                "gross_pct": float(gross_pct),
                "net_pct": float(gross_pct - cost_pct),
                "exit_reason": exit_reason,
                "funding_rate_pct": float(signal.funding_rate_pct),
            }
        )
        next_available_ms = int(frame.loc[exit_index, "available_ms"])
    return pd.DataFrame(trades)


def evaluate(
    panel: pd.DataFrame,
    candidate: FundingCandidate,
    minimum_age_days: int,
) -> dict[str, Any]:
    signals = funding_signals(panel, candidate, minimum_age_days)
    trades = simulate_funding(signals, panel, candidate, BASE_COST)
    report: dict[str, Any] = {
        "candidate": asdict(candidate),
        "signals": int(len(signals)),
        "windows": {},
    }
    for name, start, end in (
        ("development", "2026-02-01", "2026-04-01"),
        ("validation", "2026-04-01", "2026-06-01"),
        ("test", "2026-06-01", "2026-07-01"),
        ("final_blind", "2026-07-01", "2026-08-01"),
    ):
        selected = scope(trades, start, end)
        stressed = selected.copy()
        if not stressed.empty:
            stressed["net_pct"] = (
                stressed.gross_pct - STRESS_COST
            )
        report["windows"][name] = {
            "base": summarize(selected),
            "stress": summarize(stressed),
            "bootstrap": block_bootstrap(selected),
        }
    return report


def main() -> None:
    args = parse_args()
    starts, manifest = load_manifest(args.data)
    if args.panel.exists():
        panel = pd.read_parquet(args.panel)
    else:
        from scripts.benchmark_s0_xmom_point_in_time import build_panel

        panel = add_breakout_features(build_panel(args.data, starts), ())
        args.panel.parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(args.panel, index=False)
    panel = merge_funding(panel, args.funding)
    reports = [
        evaluate(panel, candidate, args.minimum_age_days)
        for candidate in CANDIDATES
    ]
    development_qualified = [
        item
        for item in reports
        if item["windows"]["development"]["base"]["trades"] >= 30
        and item["windows"]["development"]["base"]["profit_factor"] > 1.05
        and item["windows"]["development"]["stress"]["profit_factor"] > 1.0
    ]
    selected = max(
        development_qualified,
        key=lambda item: item["windows"]["development"]["base"][
            "net_pct_points"
        ],
        default=None,
    )
    qualified = bool(
        selected
        and selected["windows"]["validation"]["base"]["trades"] >= 20
        and selected["windows"]["validation"]["base"]["profit_factor"] > 1.05
        and selected["windows"]["test"]["base"]["profit_factor"] > 1.05
        and selected["windows"]["final_blind"]["base"]["profit_factor"] > 1.05
        and selected["windows"]["final_blind"]["stress"]["profit_factor"] > 1.0
    )
    result = {
        "experiment": "s0_point_in_time_funding",
        "source": {
            "price": manifest.get("source"),
            "funding": str(args.funding),
            "symbols": int(panel.symbol.nunique()),
        },
        "cost_pct": BASE_COST,
        "stress_cost_pct": STRESS_COST,
        "selection": "development_only_2026_02_to_2026_03",
        "candidates": reports,
        "selected_candidate": (
            selected["candidate"]["name"] if selected else None
        ),
        "qualified_for_shadow": qualified,
        "decision": (
            "qualified_for_research_shadow"
            if qualified
            else "research_only_not_eligible"
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
