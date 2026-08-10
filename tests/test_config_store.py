from __future__ import annotations

import importlib
import json

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


def test_strategy_safety_defaults_match_api_model():
    import app.config_store as config_store

    model = TradingConfig().model_dump()
    for key in (
        "telemetry_retention_days",
        "strategy_run_retention_days",
        "performance_guard_pause_minutes",
        "opportunity_v3_canary_bypass_enabled",
        "opportunity_v3_strategy_version",
        "opportunity_v4_strategy_version",
        "opportunity_v4_live_enabled",
        "execution_max_spread_pct",
        "execution_min_depth_notional_usdt",
        "position_rotation_enabled",
        "position_rotation_shadow_enabled",
        "adaptive_30d_live_enabled",
        "adaptive_30d_live_manage_existing_enabled",
        "xmom_shadow_episode_minutes",
    ):
        assert model[key] == config_store.DEFAULT_CONFIG[key]


def test_v42_loads_legacy_execution_limits_without_changing_values(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "opportunity_v3_max_spread_pct": 0.075,
                "opportunity_v3_min_depth_notional_usdt": 4321.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(path))
    import app.config_store as config_store

    importlib.reload(config_store)
    loaded = config_store.load_config(include_secret=True)

    assert loaded["execution_max_spread_pct"] == 0.075
    assert loaded["execution_min_depth_notional_usdt"] == 4321.0


def test_v50_migrates_legacy_live_version_and_canary(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "opportunity_v4_strategy_version": "v4.7.4",
                "opportunity_v44_max_hold_bars": 6,
                "strategy_canary_release_id": "extreme_v4_roll@v4.7.4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(path))
    import app.config_store as config_store

    importlib.reload(config_store)
    loaded = config_store.load_config(include_secret=True)

    assert loaded["opportunity_v4_strategy_version"] == "v5.2"
    assert loaded["strategy_canary_release_id"] == "extreme_v5_roll@v5.2"
    assert loaded["opportunity_v44_max_hold_bars"] == 2
    assert loaded["opportunity_v462_time_exit_migrated"] is True


def test_v51_migrates_v50_runtime_route_and_canary(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "opportunity_v4_strategy_version": "v5.0-s30",
                "strategy_canary_release_id": "extreme_v5_roll@v5.0-s30",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(path))
    import app.config_store as config_store

    importlib.reload(config_store)
    loaded = config_store.load_config(include_secret=True)

    assert loaded["opportunity_v4_strategy_version"] == "v5.2"
    assert loaded["strategy_canary_release_id"] == "extreme_v5_roll@v5.2"
    assert loaded["strategy_canary_level_1_multiplier"] == 0.5
    assert loaded["strategy_canary_level_1_max_opportunities"] == 4


def test_v511_migrates_v51_runtime_with_guarded_first_release(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "opportunity_v4_strategy_version": "v5.1",
                "strategy_canary_release_id": "extreme_v5_roll@v5.1",
                "strategy_canary_level_1_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(path))
    import app.config_store as config_store

    importlib.reload(config_store)
    loaded = config_store.load_config(include_secret=True)

    assert loaded["opportunity_v4_strategy_version"] == "v5.2"
    assert loaded["strategy_canary_release_id"] == "extreme_v5_roll@v5.2"
    assert loaded["opportunity_v511_initial_multiplier"] == 0.5
    assert loaded["strategy_canary_level_1_multiplier"] == 0.5


def test_v462_time_exit_migration_repairs_partially_persisted_upgrade_once(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "opportunity_v4_strategy_version": "v4.6.2",
                "opportunity_v44_max_hold_bars": 6,
                "strategy_canary_release_id": "extreme_v4_roll@v4.6.2",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(path))
    import app.config_store as config_store

    importlib.reload(config_store)
    migrated = config_store.load_config(include_secret=True)
    assert migrated["opportunity_v44_max_hold_bars"] == 2
    assert migrated["opportunity_v462_time_exit_migrated"] is True

    config_store.save_config(migrated)
    config_store.save_config({"opportunity_v44_max_hold_bars": 6})
    explicitly_changed = config_store.load_config(include_secret=True)
    assert explicitly_changed["opportunity_v44_max_hold_bars"] == 6
