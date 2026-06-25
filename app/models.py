from __future__ import annotations

from pydantic import BaseModel, Field


class TradingConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    stage1_symbols: list[str] = Field(default_factory=lambda: ["SOLUSDT"])
    stage2_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    interval: str = "4h"
    limit: int = Field(default=1000, ge=100, le=1500)
    stage1_target_equity: float = Field(default=10000.0, gt=0)
    stage2_activation: str = "manual"
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=5)
    daily_loss_limit_pct: float = Field(default=3.0, ge=0.1, le=20)
    max_drawdown_pct: float = Field(default=15.0, ge=1, le=80)
    max_consecutive_losses: int = Field(default=2, ge=1, le=20)
    cooldown_hours: int = Field(default=24, ge=1, le=168)
    max_open_positions: int = Field(default=1, ge=1, le=20)
    max_symbol_margin_pct: float = Field(default=35.0, ge=1, le=100)
    stage1_max_leverage: float = Field(default=2, ge=1, le=10)
    stage2_max_leverage: float = Field(default=1.5, ge=1, le=5)
    allow_short: bool = False
    grid_enabled: bool = True
    grid_min_levels: int = Field(default=20, ge=5, le=200)
    grid_max_levels: int = Field(default=80, ge=5, le=300)
    grid_reserve_cash_pct: float = Field(default=25.0, ge=0, le=80)
    grid_stop_on_breakout: bool = True
    dry_run: bool = True
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    binance_base_url: str = "https://fapi.binance.com"
    api_key: str = ""
    api_secret: str = ""


class ExecutePayload(BaseModel):
    symbol: str
    side: str = "BUY"
    quantity: float = Field(gt=0)
    reduce_only: bool = False


class BotControlPayload(BaseModel):
    action: str
    confirmation: str = ""
