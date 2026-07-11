from app.state_store import load_state, save_state
from app.trading_engine import build_stage1_decision, close_rotation_position, sync_stage


class RotationCloseClient:
    def __init__(self, positions, reject_close=False, positions_after_reject=None):
        self.positions = positions
        self.reject_close = reject_close
        self.positions_after_reject = positions_after_reject if positions_after_reject is not None else positions
        self.close_calls = 0
        self.account_calls = 0

    def account_live(self):
        self.account_calls += 1
        positions = self.positions if self.account_calls == 1 else self.positions_after_reject
        return {
            "totalWalletBalance": "50",
            "totalUnrealizedProfit": "0",
            "availableBalance": "50",
            "positions": positions,
        }

    def position_side_dual(self):
        return {"dualSidePosition": True}

    def cancel_all_open_orders(self, symbol):
        return {"symbol": symbol, "cancelled": True}

    def cancel_all_open_algo_orders(self, symbol):
        return {"symbol": symbol, "cancelled_algo": True}

    def place_market_order(self, **kwargs):
        self.close_calls += 1
        if self.reject_close:
            raise RuntimeError('Binance signed API 400: {"code":-2022,"msg":"ReduceOnly Order is rejected."}')
        return {"status": "NEW", **kwargs}


def test_close_rotation_skips_when_live_position_is_already_gone():
    client = RotationCloseClient(positions=[])

    result = close_rotation_position(client, {"symbol": "OLDUSDT", "direction": "LONG", "quantity": 2})

    assert result["skipped"] is True
    assert result["reason"] == "position_already_closed"
    assert client.close_calls == 0


def test_close_rotation_recovers_reduce_only_when_position_disappears_after_cancel():
    client = RotationCloseClient(
        positions=[{"symbol": "OLDUSDT", "positionSide": "LONG", "positionAmt": "2"}],
        reject_close=True,
        positions_after_reject=[],
    )

    result = close_rotation_position(client, {"symbol": "OLDUSDT", "direction": "LONG", "quantity": 2})

    assert result["skipped"] is True
    assert result["reason"] == "position_already_closed_after_cancel"
    assert client.close_calls == 1


def test_sync_stage_initializes_extreme_sprint_equity_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "growth_mode": "extreme_sprint",
        "extreme_sprint_enabled": True,
        "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
        "auto_risk_by_equity": False,
        "stage1_target_equity": 10000,
        "stage2_activation": "manual",
        "extreme_sprint_interval": "5m",
        "extreme_sprint_recent_days": 2,
        "extreme_sprint_risk_per_trade_pct": 28,
        "extreme_sprint_max_leverage": 8,
        "extreme_sprint_max_symbol_margin_pct": 98,
    }
    state = {
        **load_state(),
        "equity_high_watermark": 100,
        "equity_guard_mode": "balanced",
        "extreme_sprint_equity_high_watermark": 0,
    }
    save_state(state)

    updated = sync_stage(config, state, {"equity": 60})

    assert updated["equity_high_watermark"] == 100
    assert updated["equity_guard_mode"] == "extreme_sprint"
    assert updated["extreme_sprint_start_equity"] == 60
    assert updated["extreme_sprint_equity_high_watermark"] == 60


def test_sync_stage_keeps_extreme_sprint_high_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    config = {
        "growth_mode": "extreme_sprint",
        "extreme_sprint_enabled": True,
        "extreme_sprint_confirmation": "ENABLE_EXTREME_SPRINT",
        "auto_risk_by_equity": False,
        "stage1_target_equity": 10000,
        "stage2_activation": "manual",
        "extreme_sprint_interval": "5m",
        "extreme_sprint_recent_days": 2,
        "extreme_sprint_risk_per_trade_pct": 28,
        "extreme_sprint_max_leverage": 8,
        "extreme_sprint_max_symbol_margin_pct": 98,
    }
    state = {
        **load_state(),
        "equity_guard_mode": "extreme_sprint",
        "extreme_sprint_start_equity": 60,
        "extreme_sprint_equity_high_watermark": 66,
    }
    save_state(state)

    updated = sync_stage(config, state, {"equity": 62})

    assert updated["extreme_sprint_start_equity"] == 60
    assert updated["extreme_sprint_equity_high_watermark"] == 66


def test_yolo_scalp_rejects_non_orderbook_entry_before_live_execution():
    decision = build_stage1_decision(
        "OLDUSDT",
        [],
        {
            "auto_risk_by_equity": False,
            "growth_mode": "yolo_scalp",
            "yolo_scalp_enabled": True,
            "yolo_scalp_confirmation": "ENABLE_YOLO_SCALP",
            "yolo_scalp_orderbook_only_enabled": True,
        },
        {},
        {"equity": 50},
        scan_candidate={
            "symbol": "OLDUSDT",
            "mode": "yolo_scalp",
            "strategy": "breakout",
            "direction": "LONG",
            "risk_pct": 50,
            "leverage": 8,
            "margin_pct": 98,
            "entry_type": "extreme_probe",
            "signal": {
                "signal": "LONG",
                "entry": 1.0,
                "stop": 0.98,
                "take_profit": 1.02,
                "expected_profit_pct": 2.0,
            },
        },
    )

    assert decision["action"] == "WAIT"
    assert decision["risk"]["reason"] == "yolo_orderbook_only"


def test_primary_risk_reason_is_not_overwritten_by_order_viability():
    decision = build_stage1_decision(
        "SOLUSDT",
        [],
        {
            "auto_risk_by_equity": False,
            "growth_mode": "balanced",
            "hard_stop_equity": 30,
            "risk_warning_equity": 40,
            "effective_position_sizing_enabled": True,
        },
        {"bot_status": "running"},
        {"equity": 29.92, "positions": []},
        scan_candidate={
            "symbol": "SOLUSDT",
            "mode": "balanced",
            "strategy": "default",
            "direction": "LONG",
            "risk_pct": 1,
            "leverage": 2,
            "margin_pct": 35,
            "entry_type": "standard",
            "signal": {
                "signal": "LONG",
                "last_price": 100,
                "stop": 99,
                "take_profit": 102,
                "expected_profit_pct": 2,
            },
        },
    )

    assert decision["action"] == "WAIT"
    assert decision["risk"]["reason"] == "hard_stop_equity"
    assert decision["primary_block_reason"] == "hard_stop_equity"
    assert decision["estimated_notional"] == 0
