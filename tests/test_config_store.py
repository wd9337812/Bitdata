from __future__ import annotations

import importlib


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
