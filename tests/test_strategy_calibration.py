from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.live_learning import init_live_learning_schema
from app.strategy_calibration import calibrate_v3_opportunity, clear_calibration_cache
from app.telemetry import connect


def _prepare(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    clear_calibration_cache()
    init_live_learning_schema()


def _seed_v3_losses(count: int, *, version: str = "v3.2") -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for index in range(count):
            opened = now - timedelta(minutes=count - index)
            payload = {
                "decision": {
                    "candidate": {
                        "strategy_family": "extreme_v3_roll",
                        "strategy_version": version,
                        "v3_tier": "A+",
                        "entry_type": "v3_breakout",
                        "opportunity_v3": {"tier": "A+", "market_regime": "broad_down"},
                    }
                }
            }
            conn.execute(
                "INSERT INTO strategy_runs (ts, symbol, action, payload) VALUES (?, 'ALTUSDT', 'OPEN_SHORT', ?)",
                (opened.isoformat(), json.dumps(payload)),
            )
            opened_ms = int(opened.timestamp() * 1000)
            conn.execute(
                """
                INSERT INTO live_trade_records (
                    symbol, direction, open_time, close_time, open_notional, realized_pnl,
                    commission, funding_fee, net_pnl, hold_seconds, created_at
                ) VALUES ('ALTUSDT', 'SHORT', ?, ?, 20, -0.2, 0.02, 0, -0.22, 60, ?)
                """,
                (opened_ms, opened_ms + 60_000, now.isoformat()),
            )
        conn.commit()


def _opportunity() -> dict:
    return {
        "strategy_family": "extreme_v3_roll",
        "tier": "A+",
        "tier_label": "顶级机会",
        "market_regime": "broad_down",
        "risk_multiplier": 1.0,
        "passed": True,
        "canary_eligible": True,
        "blockers": [],
    }


def test_unvalidated_a_plus_uses_a_sized_risk(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)

    result = calibrate_v3_opportunity(
        _opportunity(),
        {"entry_type": "v3_breakout"},
        "SHORT",
        {"opportunity_v3_strategy_version": "v3.2"},
    )

    assert result["risk_multiplier"] == 0.65
    assert result["canary_eligible"] is False
    assert result["calibration"]["validated"] is False


def test_negative_same_version_live_cohort_blocks_entry(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_v3_losses(10)
    clear_calibration_cache()

    result = calibrate_v3_opportunity(
        _opportunity(),
        {"entry_type": "v3_breakout"},
        "SHORT",
        {
            "opportunity_v3_strategy_version": "v3.2",
            "opportunity_v3_calibration_negative_live_trades": 10,
        },
    )

    assert result["calibration"]["negative"] is True
    assert result["passed"] is False
    assert result["risk_multiplier"] == 0


def test_old_strategy_version_does_not_penalize_new_version(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    _seed_v3_losses(10, version="v3.1")
    clear_calibration_cache()

    result = calibrate_v3_opportunity(
        _opportunity(),
        {"entry_type": "v3_breakout"},
        "SHORT",
        {"opportunity_v3_strategy_version": "v3.2"},
    )

    assert result["calibration"]["broad"]["live"]["trades"] == 0
    assert result["calibration"]["negative"] is False
