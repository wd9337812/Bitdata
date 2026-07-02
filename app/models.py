from __future__ import annotations

from pydantic import BaseModel, Field


class TradingConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    stage1_symbols: list[str] = Field(default_factory=lambda: ["SOLUSDT", "LABUSDT", "SUIUSDT", "AAVEUSDT", "WLDUSDT"])
    stage2_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    auto_discover_symbols: bool = True
    max_scan_symbols: int = Field(default=20, ge=1, le=120)
    min_24h_volume_usdt: float = Field(default=30_000_000, ge=0)
    max_observation_symbols: int = Field(default=20, ge=1, le=150)
    max_trade_pool_symbols: int = Field(default=10, ge=1, le=50)
    depth_check_top_symbols: int = Field(default=8, ge=0, le=50)
    market_stream_enabled: bool = True
    symbol_trade_score: float = Field(default=75.0, ge=0, le=100)
    symbol_small_trade_score: float = Field(default=65.0, ge=0, le=100)
    symbol_observe_score: float = Field(default=50.0, ge=0, le=100)
    min_simulated_trades: int = Field(default=5, ge=0, le=100)
    min_simulated_win_rate: float = Field(default=45.0, ge=0, le=100)
    min_simulated_profit_factor: float = Field(default=1.25, ge=0, le=20)
    min_simulated_net_pct: float = Field(default=1.5, ge=-100, le=500)
    new_symbol_observation_minutes: int = Field(default=30, ge=0, le=1440)
    small_trade_risk_multiplier: float = Field(default=0.5, ge=0.05, le=1)
    max_spread_pct: float = Field(default=0.08, ge=0, le=5)
    min_depth_notional_usdt: float = Field(default=20_000, ge=0)
    volume_spike_ratio: float = Field(default=1.8, ge=0, le=20)
    quality_backtest_days: list[int] = Field(default_factory=lambda: [3, 5])
    interval: str = "4h"
    conservative_interval: str = "4h"
    balanced_interval: str = "1h"
    attack_interval: str = "15m"
    tournament_interval: str = "5m"
    conservative_recent_days: int = Field(default=30, ge=1, le=120)
    balanced_recent_days: int = Field(default=20, ge=1, le=120)
    attack_recent_days: int = Field(default=10, ge=1, le=60)
    tournament_recent_days: int = Field(default=5, ge=1, le=30)
    conservative_loop_seconds: int = Field(default=300, ge=10, le=3600)
    balanced_loop_seconds: int = Field(default=120, ge=10, le=3600)
    attack_loop_seconds: int = Field(default=60, ge=10, le=3600)
    tournament_loop_seconds: int = Field(default=30, ge=10, le=3600)
    limit: int = Field(default=1000, ge=100, le=1500)
    stage1_target_equity: float = Field(default=10000.0, gt=0)
    stage2_activation: str = "manual"
    growth_mode: str = "balanced"
    tournament_stop_equity: float = Field(default=30.0, ge=0)
    auto_risk_by_equity: bool = True
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=25)
    attack_risk_per_trade_pct: float = Field(default=5.0, ge=0.1, le=25)
    tournament_risk_per_trade_pct: float = Field(default=15.0, ge=0.1, le=50)
    daily_loss_limit_pct: float = Field(default=3.0, ge=0.1, le=60)
    attack_daily_loss_limit_pct: float = Field(default=10.0, ge=0.1, le=60)
    tournament_daily_loss_limit_pct: float = Field(default=25.0, ge=0.1, le=80)
    max_drawdown_pct: float = Field(default=15.0, ge=1, le=80)
    max_consecutive_losses: int = Field(default=2, ge=1, le=20)
    cooldown_hours: int = Field(default=24, ge=1, le=168)
    max_open_positions: int = Field(default=1, ge=1, le=20)
    max_symbol_margin_pct: float = Field(default=35.0, ge=1, le=100)
    attack_max_symbol_margin_pct: float = Field(default=60.0, ge=1, le=100)
    tournament_max_symbol_margin_pct: float = Field(default=90.0, ge=1, le=100)
    stage1_max_leverage: float = Field(default=2, ge=1, le=10)
    attack_max_leverage: float = Field(default=3, ge=1, le=20)
    tournament_max_leverage: float = Field(default=5, ge=1, le=50)
    stage2_max_leverage: float = Field(default=1.5, ge=1, le=5)
    min_recent_trades: int = Field(default=2, ge=0, le=50)
    min_profit_factor: float = Field(default=1.2, ge=0, le=10)
    min_expected_profit_cost_ratio: float = Field(default=3.0, ge=0, le=20)
    estimated_slippage_pct: float = Field(default=0.04, ge=0, le=5)
    min_expected_profit_pct: float = Field(default=0.35, ge=0, le=20)
    allow_short: bool = True
    short_risk_multiplier: float = Field(default=0.5, ge=0.1, le=1)
    short_min_recent_trades: int = Field(default=5, ge=0, le=50)
    short_min_profit_factor: float = Field(default=1.3, ge=0, le=10)
    short_min_net_pct: float = Field(default=1.0, ge=-50, le=100)
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
