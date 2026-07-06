from __future__ import annotations

from app.protection import apply_initial_protection_to_signal, build_protection_plan


def test_long_protection_plan_uses_profile_atr_multipliers():
    signal = {
        "signal": "LONG",
        "last_price": 10.0,
        "atr": 1.0,
        "stop": 9.0,
        "take_profit": 11.5,
        "protection_profile": {"stop_atr": 0.55, "take_profit_atr": 0.75, "max_hold_bars": 3},
    }

    plan = build_protection_plan(signal, {"dynamic_protection_enabled": True}, entry_type="weak_quality_probe")
    updated = apply_initial_protection_to_signal(signal, plan)

    assert plan["label"] == "弱质量试探保护"
    assert plan["initial_stop"] == 9.45
    assert plan["initial_take_profit"] == 10.75
    assert plan["max_hold_bars"] == 3
    assert updated["stop"] == 9.45
    assert updated["take_profit"] == 10.75


def test_short_protection_plan_reverses_stop_and_take_profit():
    signal = {
        "signal": "SHORT",
        "last_price": 100.0,
        "atr": 2.0,
        "stop": 102.0,
        "take_profit": 96.0,
        "protection_profile": {"stop_atr": 0.5, "take_profit_atr": 1.0, "max_hold_bars": 4},
    }

    plan = build_protection_plan(signal, {"dynamic_protection_enabled": True}, entry_type="preemptive")

    assert plan["initial_stop"] == 101.0
    assert plan["initial_take_profit"] == 98.0
    assert plan["fast_invalid"]["price"] > signal["last_price"]
    assert plan["break_even"]["trigger_price"] < signal["last_price"]
    assert plan["trailing"]["trigger_price"] < signal["last_price"]


def test_disabled_dynamic_protection_keeps_original_signal_prices():
    signal = {
        "signal": "LONG",
        "last_price": 10.0,
        "atr": 1.0,
        "stop": 8.0,
        "take_profit": 14.0,
        "protection_profile": {"stop_atr": 0.55, "take_profit_atr": 0.75, "max_hold_bars": 3},
    }

    plan = build_protection_plan(signal, {"dynamic_protection_enabled": False}, entry_type="weak_quality_probe")
    updated = apply_initial_protection_to_signal(signal, plan)

    assert plan["enabled"] is False
    assert updated is signal
    assert updated["stop"] == 8.0
    assert updated["take_profit"] == 14.0
