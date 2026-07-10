from __future__ import annotations

from datetime import datetime, timezone

from app.config_store import load_config, save_config


def test_manual_stage_override_gets_default_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    saved = save_config({"stage_manual_mode": "yolo_scalp", "stage_manual_until": "", "stage_manual_default_hours": 6})

    expires = datetime.fromisoformat(saved["stage_manual_until"])
    assert expires.tzinfo is not None
    assert 5.9 <= (expires - datetime.now(timezone.utc)).total_seconds() / 3600 <= 6.0


def test_auto_stage_mode_clears_old_manual_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_config({"stage_manual_mode": "yolo_scalp", "stage_manual_until": "", "stage_manual_default_hours": 6})

    save_config({"stage_manual_mode": "auto"})

    assert load_config()["stage_manual_until"] == ""
