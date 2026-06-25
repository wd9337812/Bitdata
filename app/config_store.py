from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "stage1_symbols": ["SOLUSDT"],
    "stage2_symbols": ["BTCUSDT", "ETHUSDT"],
    "interval": "4h",
    "limit": 1000,
    "stage1_target_equity": 10000.0,
    "stage2_activation": "manual",
    "risk_per_trade_pct": 1.0,
    "daily_loss_limit_pct": 3.0,
    "max_drawdown_pct": 15.0,
    "max_consecutive_losses": 2,
    "cooldown_hours": 24,
    "max_open_positions": 1,
    "max_symbol_margin_pct": 35.0,
    "stage1_max_leverage": 2,
    "stage2_max_leverage": 1.5,
    "allow_short": False,
    "grid_enabled": True,
    "grid_min_levels": 20,
    "grid_max_levels": 80,
    "grid_reserve_cash_pct": 25.0,
    "grid_stop_on_breakout": True,
    "dry_run": True,
    "live_trading_enabled": False,
    "live_trading_confirmation": "",
    "binance_base_url": "https://fapi.binance.com",
    "api_key": "",
    "api_secret": "",
}


def config_path() -> Path:
    return Path(os.getenv("APP_CONFIG_PATH", "./data/config.json"))


def load_config(include_secret: bool = True) -> dict[str, Any]:
    path = config_path()
    config = DEFAULT_CONFIG.copy()
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            config.update(json.load(file))
    if not include_secret:
        config["api_secret"] = "********" if config.get("api_secret") else ""
        config["api_key"] = mask(config.get("api_key", ""))
    return config


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_config(include_secret=True)
    if payload.get("api_secret") == "********":
        payload = {key: value for key, value in payload.items() if key != "api_secret"}
    if "..." in str(payload.get("api_key", "")) or payload.get("api_key") == "****":
        payload = {key: value for key, value in payload.items() if key != "api_key"}
    current.update(payload)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(current, file, ensure_ascii=False, indent=2)
    return load_config(include_secret=False)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"
