from __future__ import annotations

import importlib

from app.models import TradingConfig


def test_masked_credentials_do_not_overwrite_saved_values(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    import app.config_store as config_store

    importlib.reload(config_store)
    config_store.save_config({"api_key": "real-key-123456", "api_secret": "real-secret-123456"})
    saved = config_store.save_config({"api_key": "real...3456", "api_secret": ""})
    raw = config_store.load_config(include_secret=True)

    assert raw["api_key"] == "real-key-123456"
    assert raw["api_secret"] == "real-secret-123456"
    assert saved["api_key"] == "real...3456"
    assert saved["api_secret"] == "********"


def test_credentials_are_trimmed_before_save(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    import app.config_store as config_store

    importlib.reload(config_store)
    config_store.save_config({"api_key": " key-with-space \n", "api_secret": "\tsecret-with-space "})
    raw = config_store.load_config(include_secret=True)

    assert raw["api_key"] == "key-with-space"
    assert raw["api_secret"] == "secret-with-space"


def test_v32_safety_defaults_match_api_model():
    import app.config_store as config_store

    model = TradingConfig().model_dump()
    for key in (
        "telemetry_retention_days",
        "strategy_run_retention_days",
        "performance_guard_pause_minutes",
        "opportunity_v3_canary_bypass_enabled",
        "opportunity_v3_strategy_version",
        "position_rotation_enabled",
        "position_rotation_shadow_enabled",
    ):
        assert model[key] == config_store.DEFAULT_CONFIG[key]
