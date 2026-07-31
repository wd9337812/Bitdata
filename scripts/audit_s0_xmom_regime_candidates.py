from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_s0_cross_sectional_momentum import (
    COST_PCT,
    Profile,
    simulate,
    summarize,
)
from scripts.benchmark_s0_xmom_point_in_time import (
    DEFAULT_DATA,
    load_manifest,
    profile,
    selected_signals,
)


DEFAULT_PANEL = (
    ROOT
    / "data"
    / "research"
    / "s0_xmom_point_in_time_july_audit"
    / "point_in_time_panel.parquet"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "research" / "s0_xmom_regime_candidates"
)
BASE_COST = COST_PCT
STRESS_COST = 0.24
Filter = Callable[[pd.DataFrame], pd.Series]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select a small, interpretable cross-sectional momentum entry "
            "filter on development data, then freeze it for later windows."
        )
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-age-days", type=int, default=30)
    return parser.parse_args()


def directional_positive(
    frame: pd.DataFrame,
    column: str,
) -> pd.Series:
    return np.where(
        frame.direction.eq("LONG"),
        frame[column],
        -frame[column],
    ) > 0


def candidate_filters() -> dict[str, Filter]:
    # This intentionally stays small. Each rule corresponds to a common
    # momentum implementation concern and is not a threshold grid.
    return {
        "baseline": lambda frame: pd.Series(True, index=frame.index),
        "mature_90d": lambda frame: frame.symbol_age_days.ge(90),
        "liquid_20m": lambda frame: frame.liquidity_24h.ge(20_000_000),
        "liquid_100m": lambda frame: frame.liquidity_24h.ge(100_000_000),
        "persistent_6h": lambda frame: pd.Series(
            directional_positive(frame, "ret_6h"),
            index=frame.index,
        ),
        "persistent_6h_72h": lambda frame: pd.Series(
            directional_positive(frame, "ret_6h")
            & directional_positive(frame, "ret_72h"),
            index=frame.index,
        ),
        "market_majority_55": lambda frame: np.where(
            frame.direction.eq("LONG"),
            frame.market_positive_share.ge(0.55),
            frame.market_positive_share.le(0.45),
        ),
        "quality_core": lambda frame: (
            frame.symbol_age_days.ge(90)
            & frame.liquidity_24h.ge(20_000_000)
            & directional_positive(frame, "ret_6h")
        ),
        "quality_core_majority": lambda frame: (
            frame.symbol_age_days.ge(90)
            & frame.liquidity_24h.ge(20_000_000)
            & directional_positive(frame, "ret_6h")
            & np.where(
                frame.direction.eq("LONG"),
                frame.market_positive_share.ge(0.55),
                frame.market_positive_share.le(0.45),
            )
        ),
        "liquid_persistent": lambda frame: (
            frame.liquidity_24h.ge(100_000_000)
            & directional_positive(frame, "ret_6h")
        ),
    }


def enrich_signals(
    panel: pd.DataFrame,
    minimum_age_days: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    signals, gate = selected_signals(panel, minimum_age_days)
    eligible = panel.loc[
        ~panel.symbol.isin({"BTCUSDT", "ETHUSDT"})
        & panel.liquidity_24h.ge(5_000_000)
    ].copy()
    positive_share = eligible.groupby(
        "available_ms",
        sort=False,
    ).ret_24h.apply(lambda values: float(values.gt(0).mean()))
    signals["market_positive_share"] = signals.available_ms.map(
        positive_share
    )
    return signals.dropna(subset=["market_positive_share"]), gate


def scope(
    frame: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    timestamp = pd.to_datetime(frame.entry_ms, unit="ms", utc=True)
    return frame.loc[
        timestamp.ge(pd.Timestamp(start, tz="UTC"))
        & timestamp.lt(pd.Timestamp(end, tz="UTC"))
    ].copy()


def development_folds(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "fold_1": scope(frame, "2026-02-01", "2026-02-20"),
        "fold_2": scope(frame, "2026-02-20", "2026-03-11"),
        "fold_3": scope(frame, "2026-03-11", "2026-04-01"),
    }


def cost_adjusted(frame: pd.DataFrame, cost_pct: float) -> pd.DataFrame:
    result = frame.copy()
    result["cost_pct"] = cost_pct
    result["net_pct"] = result.gross_pct - cost_pct
    return result


def evaluate_candidate(
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    strategy_profile: Profile,
    rule: Filter,
) -> tuple[dict[str, Any], pd.DataFrame]:
    mask = pd.Series(rule(signals), index=signals.index).fillna(False)
    selected = signals.loc[mask].copy()
    trades = simulate(selected, panel, strategy_profile, cost_pct=BASE_COST)
    stressed = cost_adjusted(trades, STRESS_COST)
    development = scope(trades, "2026-02-01", "2026-04-01")
    development_stress = scope(stressed, "2026-02-01", "2026-04-01")
    folds = development_folds(trades)
    stress_folds = development_folds(stressed)
    fold_report = {
        name: {
            "base": summarize(fold),
            "stress": summarize(stress_folds[name]),
        }
        for name, fold in folds.items()
    }
    positive_base_folds = sum(
        item["base"].get("net_pct_points", 0) > 0
        for item in fold_report.values()
    )
    positive_stress_folds = sum(
        item["stress"].get("net_pct_points", 0) > 0
        for item in fold_report.values()
    )
    dev_base = summarize(development)
    dev_stress = summarize(development_stress)
    qualifies = (
        dev_base.get("trades", 0) >= 30
        and min(
            item["base"].get("trades", 0)
            for item in fold_report.values()
        )
        >= 8
        and positive_base_folds >= 2
        and positive_stress_folds >= 2
        and dev_base.get("profit_factor", 0) >= 1.10
        and dev_stress.get("profit_factor", 0) >= 1.02
    )
    return {
        "signals": int(len(selected)),
        "development": {
            "base": dev_base,
            "stress": dev_stress,
            "positive_base_folds": positive_base_folds,
            "positive_stress_folds": positive_stress_folds,
            "folds": fold_report,
        },
        "development_qualified": bool(qualifies),
    }, trades


def choose_frozen_candidate(
    reports: dict[str, dict[str, Any]],
) -> str | None:
    qualified = [
        (name, item)
        for name, item in reports.items()
        if item.get("development_qualified")
    ]
    if not qualified:
        return None
    qualified.sort(
        key=lambda pair: (
            pair[1]["development"]["stress"].get("profit_factor", 0),
            pair[1]["development"]["stress"].get("net_pct_points", 0),
            pair[1]["development"]["base"].get("trades", 0),
        ),
        reverse=True,
    )
    return qualified[0][0]


def block_bootstrap(
    frame: pd.DataFrame,
    *,
    samples: int = 5_000,
    seed: int = 20260731,
) -> dict[str, Any]:
    if frame.empty:
        return {"samples": 0}
    working = frame.copy()
    working["week"] = pd.to_datetime(
        working.entry_ms,
        unit="ms",
        utc=True,
    ).dt.to_period("W").astype(str)
    blocks = [
        scoped.net_pct.to_numpy(dtype=float)
        for _, scoped in working.groupby("week", sort=True)
    ]
    rng = np.random.default_rng(seed)
    totals = np.empty(samples, dtype=float)
    for index in range(samples):
        chosen = rng.integers(0, len(blocks), size=len(blocks))
        totals[index] = sum(float(blocks[item].sum()) for item in chosen)
    return {
        "samples": samples,
        "weekly_blocks": len(blocks),
        "positive_probability": round(float((totals > 0).mean()), 4),
        "net_pct_points_p05": round(float(np.quantile(totals, 0.05)), 6),
        "net_pct_points_p50": round(float(np.quantile(totals, 0.50)), 6),
        "net_pct_points_p95": round(float(np.quantile(totals, 0.95)), 6),
    }


def frozen_evaluation(trades: pd.DataFrame) -> dict[str, Any]:
    stressed = cost_adjusted(trades, STRESS_COST)
    boundaries = {
        "validation_apr_may": ("2026-04-01", "2026-06-01"),
        "test_june": ("2026-06-01", "2026-07-01"),
        "final_july": ("2026-07-01", "2026-08-01"),
    }
    result: dict[str, Any] = {}
    for name, (start, end) in boundaries.items():
        base = scope(trades, start, end)
        stress = scope(stressed, start, end)
        result[name] = {
            "base": summarize(base),
            "stress": summarize(stress),
        }
        if name == "final_july":
            result[name]["weekly_block_bootstrap_base"] = block_bootstrap(base)
            result[name]["weekly_block_bootstrap_stress"] = block_bootstrap(
                stress
            )
    combined = scope(trades, "2026-04-01", "2026-08-01")
    combined_stress = scope(stressed, "2026-04-01", "2026-08-01")
    result["all_frozen_apr_july"] = {
        "base": summarize(combined),
        "stress": summarize(combined_stress),
    }
    return result


def render_markdown(report: dict[str, Any]) -> str:
    frozen = report.get("frozen_candidate")
    lines = [
        "# S0 点时动量候选审计",
        "",
        "## 结论",
        "",
    ]
    if not frozen:
        lines.append(
            "开发集没有候选满足最低稳定性要求，本策略族不得接入实盘。"
        )
    else:
        final = report["frozen_evaluation"]["final_july"]
        lines.extend(
            [
                f"开发集冻结候选：`{frozen}`。",
                (
                    "7 月独立盲测（0.12% 成本）："
                    f"{final['base']['trades']} 笔，"
                    f"PF {final['base']['profit_factor']}，"
                    f"净收益 {final['base']['net_pct_points']} 个百分点。"
                ),
                (
                    "7 月双倍成本（0.24%）："
                    f"PF {final['stress']['profit_factor']}，"
                    f"净收益 {final['stress']['net_pct_points']} 个百分点。"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## 方法",
            "",
            "- 候选及阈值只在 2026-02 至 2026-03 开发集选择。",
            "- 2026-04 至 2026-06 只做冻结验证。",
            "- 2026-07 是最终独立盲测，不参与选择。",
            "- 使用 Binance 官方点时历史币种集合，包含后续下架合约。",
            "- 入场为下一小时开盘，止损优先，扣除 0.12% 与 0.24% 成本。",
            "",
            "## 发布门槛",
            "",
            "即使开发集合格，若冻结窗口不能维持扣费后正期望，"
            "仍判定为研究失败，不连接实盘。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    _, manifest = load_manifest(args.data)
    panel = pd.read_parquet(args.panel)
    signals, gate = enrich_signals(panel, args.minimum_age_days)
    strategy_profile = profile()
    reports: dict[str, dict[str, Any]] = {}
    trades_by_candidate: dict[str, pd.DataFrame] = {}
    for name, rule in candidate_filters().items():
        candidate_report, trades = evaluate_candidate(
            signals,
            panel,
            strategy_profile,
            rule,
        )
        reports[name] = candidate_report
        trades_by_candidate[name] = trades
    frozen = choose_frozen_candidate(reports)
    frozen_evidence = (
        frozen_evaluation(trades_by_candidate[frozen]) if frozen else {}
    )
    report = {
        "method": (
            "Small predeclared family of interpretable momentum filters. "
            "Selection uses Feb-Mar only; Apr-Jun and July are frozen."
        ),
        "data": {
            "monthly_source": manifest.get("source"),
            "monthly_checksum_verified": manifest.get("checksum_verified"),
            "daily_extension": manifest.get("daily_extension"),
            "panel_rows": int(len(panel)),
            "symbols": int(panel.symbol.nunique()),
            "gate": gate,
        },
        "profile": strategy_profile.__dict__,
        "cost_pct": {
            "base": BASE_COST,
            "stress": STRESS_COST,
        },
        "candidate_selection": reports,
        "frozen_candidate": frozen,
        "frozen_evaluation": frozen_evidence,
        "live_qualified": False,
        "live_qualification_reason": (
            "Offline hourly research cannot qualify live execution by itself."
            if frozen
            else "No development candidate passed the stability gate."
        ),
    }
    if frozen:
        frozen_frame = trades_by_candidate[frozen].copy()
        frozen_frame["candidate"] = frozen
        frozen_frame.to_parquet(
            args.output / "frozen_candidate_trades.parquet",
            index=False,
        )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output / "report.md").write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
