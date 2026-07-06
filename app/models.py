from __future__ import annotations

from pydantic import BaseModel, Field


class TradingConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    stage1_symbols: list[str] = Field(default_factory=lambda: ["SOLUSDT", "LABUSDT", "SUIUSDT", "AAVEUSDT", "WLDUSDT"])
    stage2_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    auto_discover_symbols: bool = True
    recall_pool_limit: int = Field(default=600, ge=1, le=1000)
    coarse_pool_limit: int = Field(default=220, ge=1, le=500)
    rank_pool_limit: int = Field(default=90, ge=1, le=200)
    auction_pool_limit: int = Field(default=15, ge=0, le=50)
    scan_degrade_seconds: int = Field(default=18, ge=5, le=300)
    scan_min_rank_symbols: int = Field(default=8, ge=1, le=200)
    max_scan_symbols: int = Field(default=40, ge=1, le=120)
    min_24h_volume_usdt: float = Field(default=30_000_000, ge=0)
    max_observation_symbols: int = Field(default=45, ge=1, le=150)
    max_trade_pool_symbols: int = Field(default=20, ge=1, le=50)
    depth_check_top_symbols: int = Field(default=10, ge=0, le=50)
    depth_check_min_current_score: float = Field(default=38.0, ge=0, le=200)
    market_stream_enabled: bool = True
    market_stream_dynamic_enabled: bool = True
    market_stream_max_symbols: int = Field(default=50, ge=1, le=200)
    market_stream_auto_discover: bool = True
    market_stream_rebuild_seconds: int = Field(default=60, ge=15, le=1800)
    market_stream_rotation_threshold_pct: float = Field(default=20.0, ge=0, le=100)
    stream_hot_symbols_limit: int = Field(default=25, ge=0, le=200)
    stream_include_positions: bool = True
    stream_include_live_credit: bool = True
    opportunity_queue_enabled: bool = True
    opportunity_queue_ttl_seconds: int = Field(default=240, ge=10, le=3600)
    opportunity_queue_max_events: int = Field(default=120, ge=1, le=1000)
    opportunity_queue_scan_limit: int = Field(default=50, ge=1, le=500)
    opportunity_queue_score_weight: float = Field(default=0.35, ge=0, le=2)
    opportunity_queue_min_score: float = Field(default=20.0, ge=0, le=200)
    symbol_trade_score: float = Field(default=75.0, ge=0, le=100)
    symbol_small_trade_score: float = Field(default=65.0, ge=0, le=100)
    symbol_observe_score: float = Field(default=50.0, ge=0, le=100)
    min_simulated_trades: int = Field(default=5, ge=0, le=100)
    min_simulated_win_rate: float = Field(default=45.0, ge=0, le=100)
    min_simulated_profit_factor: float = Field(default=1.25, ge=0, le=20)
    min_simulated_net_pct: float = Field(default=1.5, ge=-100, le=500)
    live_performance_boost_enabled: bool = True
    live_performance_window_hours: int = Field(default=36, ge=1, le=168)
    live_performance_min_closed_trades: int = Field(default=2, ge=1, le=50)
    live_performance_min_net_pnl_usdt: float = Field(default=0.5, ge=-100, le=1000)
    live_performance_min_profit_factor: float = Field(default=1.05, ge=0, le=20)
    live_performance_min_depth_notional_usdt: float = Field(default=1_500, ge=0)
    live_performance_min_sim_net_pct: float = Field(default=5.0, ge=-100, le=500)
    live_performance_risk_multiplier: float = Field(default=0.6, ge=0.05, le=1)
    live_performance_check_min_score: float = Field(default=70.0, ge=0, le=200)
    live_credit_enabled: bool = True
    live_credit_default_score: float = Field(default=50.0, ge=0, le=100)
    live_credit_history_hours: int = Field(default=96, ge=1, le=720)
    live_credit_sync_seconds: int = Field(default=600, ge=60, le=86400)
    live_credit_score_weight: float = Field(default=0.35, ge=0, le=2)
    live_credit_multiplier_divisor: float = Field(default=50.0, ge=1, le=100)
    live_credit_max_risk_multiplier: float = Field(default=2.0, ge=0.1, le=3)
    live_credit_fuse_score: float = Field(default=2.0, ge=0, le=20)
    live_credit_recovery_enabled: bool = True
    live_credit_recovery_interval_hours: int = Field(default=6, ge=1, le=168)
    live_credit_recovery_points: float = Field(default=3.0, ge=0, le=50)
    live_credit_low_recovery_interval_hours: int = Field(default=12, ge=1, le=168)
    live_credit_low_recovery_points: float = Field(default=2.0, ge=0, le=50)
    live_credit_recovery_cap: float = Field(default=50.0, ge=0, le=100)
    live_credit_loss_cooldown_cap: float = Field(default=0.60, ge=0, le=2)
    live_credit_quick_loss_cooldown_cap: float = Field(default=0.40, ge=0, le=2)
    live_credit_two_loss_cooldown_cap: float = Field(default=0.25, ge=0, le=2)
    live_credit_three_loss_cooldown_cap: float = Field(default=0.10, ge=0, le=2)
    live_credit_quick_stop_seconds: int = Field(default=60, ge=1, le=3600)
    live_credit_loss_cooldown_minutes: int = Field(default=15, ge=0, le=1440)
    live_credit_quick_stop_cooldown_hours: float = Field(default=0.5, ge=0, le=72)
    live_credit_two_loss_cooldown_hours: float = Field(default=1, ge=0, le=168)
    live_credit_three_loss_cooldown_hours: float = Field(default=3, ge=0, le=720)
    live_credit_penalty_cooldown_hours: int = Field(default=12, ge=0, le=720)
    live_credit_cooldown_bypass_enabled: bool = True
    live_credit_cooldown_bypass_min_score: float = Field(default=95.0, ge=0, le=200)
    live_credit_cooldown_bypass_min_cost_ratio: float = Field(default=18.0, ge=0, le=200)
    live_credit_big_win_usdt: float = Field(default=0.7, ge=0, le=1000)
    live_credit_large_win_usdt: float = Field(default=2.0, ge=0, le=1000)
    live_credit_big_loss_usdt: float = Field(default=0.7, ge=0, le=1000)
    live_credit_large_loss_usdt: float = Field(default=2.0, ge=0, le=1000)
    live_credit_min_quality_hold_seconds: int = Field(default=90, ge=0, le=86400)
    live_credit_tail_win_count: int = Field(default=3, ge=1, le=20)
    live_credit_tail_risk_multiplier: float = Field(default=0.75, ge=0.05, le=1)
    live_credit_tail_score_penalty: float = Field(default=3.0, ge=0, le=50)
    live_credit_sync_max_symbols: int = Field(default=20, ge=1, le=100)
    live_credit_time_decay_enabled: bool = True
    live_credit_recent_3h_multiplier: float = Field(default=1.5, ge=0, le=5)
    live_credit_recent_24h_multiplier: float = Field(default=1.0, ge=0, le=5)
    live_credit_old_multiplier: float = Field(default=0.6, ge=0, le=5)
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
    tournament_sprint_interval: str = "5m"
    conservative_recent_days: int = Field(default=30, ge=1, le=120)
    balanced_recent_days: int = Field(default=20, ge=1, le=120)
    attack_recent_days: int = Field(default=10, ge=1, le=60)
    tournament_recent_days: int = Field(default=5, ge=1, le=30)
    tournament_sprint_recent_days: int = Field(default=3, ge=1, le=14)
    conservative_loop_seconds: int = Field(default=300, ge=10, le=3600)
    balanced_loop_seconds: int = Field(default=120, ge=10, le=3600)
    attack_loop_seconds: int = Field(default=60, ge=10, le=3600)
    tournament_loop_seconds: int = Field(default=30, ge=10, le=3600)
    tournament_sprint_loop_seconds: int = Field(default=20, ge=10, le=3600)
    extreme_sprint_loop_seconds: int = Field(default=15, ge=5, le=3600)
    limit: int = Field(default=1000, ge=100, le=1500)
    stage1_target_equity: float = Field(default=10000.0, gt=0)
    stage2_activation: str = "manual"
    target_controller_enabled: bool = True
    target_risk_adjustment_enabled: bool = False
    target_phase_a_equity: float = Field(default=10000.0, ge=1)
    target_phase_b_equity: float = Field(default=100000.0, ge=1)
    target_phase_c_equity: float = Field(default=1000000.0, ge=1)
    target_phase_days: int = Field(default=30, ge=1, le=365)
    target_progress_ahead_multiplier: float = Field(default=0.75, ge=0.01, le=5)
    target_progress_on_track_multiplier: float = Field(default=1.0, ge=0.01, le=5)
    target_progress_behind_multiplier: float = Field(default=1.2, ge=0.01, le=5)
    target_progress_critical_multiplier: float = Field(default=1.5, ge=0.01, le=5)
    target_progress_max_multiplier: float = Field(default=1.8, ge=0.01, le=10)
    target_progress_min_multiplier: float = Field(default=0.5, ge=0.01, le=5)
    target_hard_floor_pct: float = Field(default=35.0, ge=0, le=100)
    target_critical_gap_pct: float = Field(default=35.0, ge=0, le=100)
    target_ahead_gap_pct: float = Field(default=15.0, ge=0, le=100)
    dynamic_protection_enabled: bool = True
    protection_fast_invalid_seconds: int = Field(default=90, ge=0, le=3600)
    protection_fast_invalid_atr: float = Field(default=0.35, ge=0, le=10)
    protection_break_even_trigger_atr: float = Field(default=0.55, ge=0, le=20)
    protection_break_even_buffer_pct: float = Field(default=0.08, ge=0, le=5)
    protection_trailing_trigger_atr: float = Field(default=0.9, ge=0, le=30)
    protection_trailing_distance_atr: float = Field(default=0.55, ge=0, le=20)
    protection_min_profit_after_cost_pct: float = Field(default=0.08, ge=0, le=10)
    growth_mode: str = "balanced"
    tournament_stop_equity: float = Field(default=30.0, ge=0)
    auto_risk_by_equity: bool = True
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=25)
    attack_risk_per_trade_pct: float = Field(default=5.0, ge=0.1, le=25)
    tournament_risk_per_trade_pct: float = Field(default=15.0, ge=0.1, le=50)
    tournament_sprint_risk_per_trade_pct: float = Field(default=18.0, ge=0.1, le=60)
    extreme_sprint_enabled: bool = False
    extreme_sprint_confirmation: str = ""
    extreme_sprint_risk_per_trade_pct: float = Field(default=28.0, ge=0.1, le=100)
    extreme_sprint_max_leverage: float = Field(default=8, ge=1, le=50)
    extreme_sprint_max_symbol_margin_pct: float = Field(default=98.0, ge=1, le=100)
    extreme_sprint_recent_days: int = Field(default=2, ge=1, le=14)
    extreme_sprint_interval: str = "5m"
    extreme_sprint_standard_min_score: float = Field(default=88.0, ge=0, le=200)
    extreme_sprint_preemptive_min_score: float = Field(default=72.0, ge=0, le=200)
    extreme_sprint_momentum_min_score: float = Field(default=68.0, ge=0, le=200)
    extreme_sprint_min_expected_profit_cost_ratio: float = Field(default=1.1, ge=0, le=50)
    extreme_sprint_min_expected_profit_pct: float = Field(default=0.18, ge=0, le=20)
    extreme_sprint_standard_stop_atr: float = Field(default=0.75, ge=0.1, le=10)
    extreme_sprint_standard_take_profit_atr: float = Field(default=1.05, ge=0.1, le=20)
    extreme_sprint_standard_max_hold_bars: int = Field(default=4, ge=1, le=100)
    extreme_sprint_preemptive_stop_atr: float = Field(default=0.65, ge=0.1, le=10)
    extreme_sprint_preemptive_take_profit_atr: float = Field(default=0.9, ge=0.1, le=20)
    extreme_sprint_preemptive_max_hold_bars: int = Field(default=3, ge=1, le=100)
    extreme_sprint_momentum_stop_atr: float = Field(default=0.7, ge=0.1, le=10)
    extreme_sprint_momentum_take_profit_atr: float = Field(default=1.0, ge=0.1, le=20)
    extreme_sprint_momentum_max_hold_bars: int = Field(default=3, ge=1, le=100)
    extreme_sprint_high_score: float = Field(default=110.0, ge=0, le=300)
    extreme_sprint_super_score: float = Field(default=135.0, ge=0, le=300)
    extreme_sprint_high_risk_multiplier: float = Field(default=1.35, ge=0.1, le=5)
    extreme_sprint_super_risk_multiplier: float = Field(default=1.75, ge=0.1, le=5)
    extreme_sprint_max_consecutive_losses: int = Field(default=5, ge=1, le=20)
    extreme_sprint_daily_loss_limit_pct: float = Field(default=50.0, ge=0.1, le=95)
    extreme_v2_enabled: bool = True
    extreme_v2_profile: str = "standard"
    extreme_firecracker_enabled: bool = True
    extreme_firecracker_min_abs_change_pct: float = Field(default=8.0, ge=0, le=200)
    extreme_firecracker_min_quote_volume_usdt: float = Field(default=30_000_000.0, ge=0)
    extreme_firecracker_volume_score_weight: float = Field(default=12.0, ge=0, le=100)
    extreme_firecracker_move_score_weight: float = Field(default=2.4, ge=0, le=20)
    extreme_firecracker_min_score: float = Field(default=55.0, ge=0, le=200)
    extreme_probe_enabled: bool = True
    extreme_probe_min_score: float = Field(default=78.0, ge=0, le=300)
    extreme_probe_min_firecracker_score: float = Field(default=55.0, ge=0, le=200)
    extreme_probe_risk_multiplier: float = Field(default=0.22, ge=0.01, le=1)
    extreme_probe_max_risk_pct: float = Field(default=6.0, ge=0.1, le=100)
    extreme_probe_min_expected_profit_cost_ratio: float = Field(default=1.05, ge=0, le=100)
    weak_quality_probe_enabled: bool = True
    weak_quality_probe_min_candidate_score: float = Field(default=95.0, ge=0, le=300)
    weak_quality_probe_min_quality_score: float = Field(default=58.0, ge=0, le=100)
    weak_quality_probe_min_cost_ratio: float = Field(default=12.0, ge=0, le=200)
    weak_quality_probe_min_profit_factor: float = Field(default=0.55, ge=0, le=20)
    weak_quality_probe_min_net_pct: float = Field(default=-8.0, ge=-100, le=500)
    weak_quality_probe_min_depth_notional_usdt: float = Field(default=300.0, ge=0)
    weak_quality_probe_max_spread_pct: float = Field(default=0.12, ge=0, le=5)
    weak_quality_probe_base_multiplier: float = Field(default=0.18, ge=0.01, le=1)
    weak_quality_probe_mid_quality_score: float = Field(default=65.0, ge=0, le=100)
    weak_quality_probe_high_quality_score: float = Field(default=72.0, ge=0, le=100)
    weak_quality_probe_mid_multiplier: float = Field(default=0.25, ge=0.01, le=1)
    weak_quality_probe_high_multiplier: float = Field(default=0.35, ge=0.01, le=1)
    weak_quality_probe_low_pf_multiplier: float = Field(default=0.7, ge=0.01, le=1)
    weak_quality_probe_negative_net_multiplier: float = Field(default=0.8, ge=0.01, le=1)
    weak_quality_probe_weak_depth_multiplier: float = Field(default=0.7, ge=0.01, le=1)
    weak_quality_probe_stop_atr: float = Field(default=0.55, ge=0.05, le=10)
    weak_quality_probe_take_profit_atr: float = Field(default=0.75, ge=0.05, le=20)
    weak_quality_probe_max_hold_bars: int = Field(default=3, ge=1, le=200)
    extreme_derivatives_enabled: bool = True
    extreme_oi_check_top_symbols: int = Field(default=12, ge=0, le=100)
    extreme_oi_min_growth_pct: float = Field(default=1.5, ge=-100, le=1000)
    extreme_oi_strong_growth_pct: float = Field(default=4.0, ge=-100, le=1000)
    extreme_funding_abs_crowded_pct: float = Field(default=0.05, ge=0, le=5)
    extreme_derivative_confirm_bonus: float = Field(default=10.0, ge=0, le=100)
    extreme_derivative_divergence_penalty: float = Field(default=12.0, ge=0, le=100)
    extreme_spot_proxy_enabled: bool = True
    extreme_spot_proxy_min_volume_spike: float = Field(default=1.1, ge=0, le=20)
    extreme_spot_proxy_bonus: float = Field(default=6.0, ge=0, le=100)
    extreme_spot_proxy_penalty: float = Field(default=8.0, ge=0, le=100)
    extreme_squeeze_enabled: bool = True
    extreme_squeeze_lookback: int = Field(default=20, ge=10, le=200)
    extreme_squeeze_bonus: float = Field(default=8.0, ge=0, le=100)
    daily_loss_limit_pct: float = Field(default=3.0, ge=0.1, le=60)
    attack_daily_loss_limit_pct: float = Field(default=10.0, ge=0.1, le=60)
    tournament_daily_loss_limit_pct: float = Field(default=25.0, ge=0.1, le=80)
    tournament_sprint_daily_loss_limit_pct: float = Field(default=35.0, ge=0.1, le=90)
    equity_guard_enabled: bool = True
    equity_guard_drawdown_1_pct: float = Field(default=10.0, ge=0, le=100)
    equity_guard_multiplier_1: float = Field(default=0.75, ge=0, le=2)
    equity_guard_drawdown_2_pct: float = Field(default=18.0, ge=0, le=100)
    equity_guard_multiplier_2: float = Field(default=0.45, ge=0, le=2)
    equity_guard_drawdown_3_pct: float = Field(default=25.0, ge=0, le=100)
    equity_guard_multiplier_3: float = Field(default=0.20, ge=0, le=2)
    equity_guard_pause_drawdown_pct: float = Field(default=35.0, ge=0, le=100)
    extreme_equity_guard_pause_drawdown_pct: float = Field(default=40.0, ge=0, le=100)
    min_order_notional_buffer_pct: float = Field(default=3.0, ge=0, le=20)
    max_drawdown_pct: float = Field(default=15.0, ge=1, le=80)
    max_consecutive_losses: int = Field(default=2, ge=1, le=20)
    cooldown_hours: int = Field(default=24, ge=1, le=168)
    max_open_positions: int = Field(default=1, ge=1, le=20)
    tournament_sprint_max_open_positions: int = Field(default=1, ge=1, le=5)
    tournament_sprint_second_position_equity: float = Field(default=100.0, ge=0)
    position_rotation_enabled: bool = True
    tournament_rotation_enabled: bool = True
    tournament_sprint_rotation_enabled: bool = True
    extreme_sprint_rotation_enabled: bool = True
    attack_rotation_enabled: bool = True
    balanced_rotation_enabled: bool = True
    conservative_rotation_enabled: bool = False
    tournament_rotation_min_new_score: float = Field(default=95.0, ge=0, le=200)
    tournament_sprint_rotation_min_new_score: float = Field(default=88.0, ge=0, le=200)
    extreme_sprint_rotation_min_new_score: float = Field(default=82.0, ge=0, le=200)
    attack_rotation_min_new_score: float = Field(default=100.0, ge=0, le=200)
    balanced_rotation_min_new_score: float = Field(default=110.0, ge=0, le=200)
    conservative_rotation_min_new_score: float = Field(default=130.0, ge=0, le=200)
    tournament_rotation_min_score_delta: float = Field(default=12.0, ge=0, le=200)
    tournament_sprint_rotation_min_score_delta: float = Field(default=8.0, ge=0, le=200)
    extreme_sprint_rotation_min_score_delta: float = Field(default=8.0, ge=0, le=200)
    attack_rotation_min_score_delta: float = Field(default=18.0, ge=0, le=200)
    balanced_rotation_min_score_delta: float = Field(default=25.0, ge=0, le=200)
    conservative_rotation_min_score_delta: float = Field(default=999.0, ge=0, le=1000)
    rotation_min_cost_ratio: float = Field(default=8.0, ge=0, le=100)
    rotation_min_net_cost_ratio: float = Field(default=2.5, ge=0, le=100)
    rotation_extra_close_cost_pct: float = Field(default=0.08, ge=0, le=5)
    rotation_profit_min_score_delta: float = Field(default=25.0, ge=0, le=200)
    rotation_loss_min_score_delta: float = Field(default=8.0, ge=0, le=200)
    rotation_probe_min_score_delta: float = Field(default=8.0, ge=0, le=200)
    rotation_small_loss_pct: float = Field(default=0.5, ge=0, le=100)
    rotation_keep_winner_profit_pct_extreme: float = Field(default=1.2, ge=0, le=100)
    rotation_unknown_position_score: float = Field(default=75.0, ge=0, le=200)
    rotation_keep_winner_profit_pct: float = Field(default=3.0, ge=0, le=100)
    rotation_max_current_loss_pct: float = Field(default=6.0, ge=0, le=100)
    rotation_cooldown_minutes: int = Field(default=45, ge=0, le=1440)
    max_symbol_margin_pct: float = Field(default=35.0, ge=1, le=100)
    attack_max_symbol_margin_pct: float = Field(default=60.0, ge=1, le=100)
    tournament_max_symbol_margin_pct: float = Field(default=90.0, ge=1, le=100)
    tournament_sprint_max_symbol_margin_pct: float = Field(default=95.0, ge=1, le=100)
    stage1_max_leverage: float = Field(default=2, ge=1, le=10)
    attack_max_leverage: float = Field(default=3, ge=1, le=20)
    tournament_max_leverage: float = Field(default=5, ge=1, le=50)
    tournament_sprint_max_leverage: float = Field(default=5, ge=1, le=50)
    stage2_max_leverage: float = Field(default=1.5, ge=1, le=5)
    min_recent_trades: int = Field(default=2, ge=0, le=50)
    min_profit_factor: float = Field(default=1.2, ge=0, le=10)
    min_expected_profit_cost_ratio: float = Field(default=3.0, ge=0, le=20)
    estimated_slippage_pct: float = Field(default=0.04, ge=0, le=5)
    min_expected_profit_pct: float = Field(default=0.35, ge=0, le=20)
    preemptive_entries_enabled: bool = True
    preemptive_min_score: float = Field(default=72.0, ge=0, le=200)
    preemptive_risk_multiplier: float = Field(default=0.24, ge=0.05, le=1)
    short_preemptive_risk_multiplier: float = Field(default=0.18, ge=0.05, le=1)
    preemptive_max_distance_pct: float = Field(default=0.35, ge=0, le=5)
    tournament_sprint_enabled: bool = True
    tournament_sprint_auto_under_equity: float = Field(default=100.0, ge=0)
    tournament_sprint_standard_min_score: float = Field(default=72.0, ge=0, le=200)
    tournament_sprint_preemptive_min_score: float = Field(default=58.0, ge=0, le=200)
    tournament_sprint_preemptive_max_distance_pct: float = Field(default=0.55, ge=0, le=5)
    tournament_sprint_preemptive_risk_multiplier: float = Field(default=0.35, ge=0.05, le=1)
    tournament_sprint_short_preemptive_risk_multiplier: float = Field(default=0.25, ge=0.05, le=1)
    tournament_sprint_momentum_enabled: bool = True
    tournament_sprint_momentum_min_score: float = Field(default=54.0, ge=0, le=200)
    tournament_sprint_momentum_min_candle_pct: float = Field(default=0.10, ge=0, le=20)
    tournament_sprint_min_expected_profit_cost_ratio: float = Field(default=1.35, ge=0, le=20)
    tournament_sprint_min_expected_profit_pct: float = Field(default=0.22, ge=0, le=20)
    tournament_sprint_standard_stop_atr: float = Field(default=0.9, ge=0.1, le=10)
    tournament_sprint_standard_take_profit_atr: float = Field(default=1.4, ge=0.1, le=20)
    tournament_sprint_standard_max_hold_bars: int = Field(default=6, ge=1, le=100)
    tournament_sprint_preemptive_stop_atr: float = Field(default=0.75, ge=0.1, le=10)
    tournament_sprint_preemptive_take_profit_atr: float = Field(default=1.0, ge=0.1, le=20)
    tournament_sprint_preemptive_max_hold_bars: int = Field(default=4, ge=1, le=100)
    tournament_sprint_momentum_stop_atr: float = Field(default=0.8, ge=0.1, le=10)
    tournament_sprint_momentum_take_profit_atr: float = Field(default=1.2, ge=0.1, le=20)
    tournament_sprint_momentum_max_hold_bars: int = Field(default=5, ge=1, le=100)
    tournament_sprint_short_min_recent_trades: int = Field(default=3, ge=0, le=50)
    tournament_sprint_short_min_profit_factor: float = Field(default=1.05, ge=0, le=10)
    tournament_sprint_short_min_net_pct: float = Field(default=-2.0, ge=-100, le=100)
    tournament_sprint_long_min_profit_factor: float = Field(default=0.85, ge=0, le=10)
    tournament_sprint_long_min_net_pct: float = Field(default=-3.0, ge=-100, le=100)
    tournament_sprint_max_consecutive_losses: int = Field(default=4, ge=1, le=20)
    quality_mode_weights_enabled: bool = True
    sprint_symbol_trade_score: float = Field(default=68.0, ge=0, le=100)
    sprint_symbol_small_trade_score: float = Field(default=55.0, ge=0, le=100)
    sprint_symbol_hot_observe_score: float = Field(default=45.0, ge=0, le=100)
    sprint_atr_ideal_min_pct: float = Field(default=1.2, ge=0, le=100)
    sprint_atr_ideal_max_pct: float = Field(default=7.0, ge=0, le=100)
    sprint_atr_high_pct: float = Field(default=10.0, ge=0, le=100)
    sprint_high_atr_risk_multiplier: float = Field(default=0.6, ge=0, le=1)
    sprint_sample_penalty: float = Field(default=1.0, ge=0, le=20)
    sprint_sample_penalty_exempt_spike: float = Field(default=2.5, ge=0, le=20)
    sprint_sample_low_risk_multiplier: float = Field(default=0.75, ge=0, le=1)
    sprint_hot_observe_risk_multiplier: float = Field(default=0.35, ge=0, le=1)
    sprint_extreme_depth_notional_usdt: float = Field(default=50_000.0, ge=0)
    market_state_filter_enabled: bool = True
    market_state_spike_wick_ratio: float = Field(default=2.2, ge=0, le=20)
    market_state_trap_max_depth_notional_usdt: float = Field(default=800.0, ge=0)
    market_state_min_volume_spike: float = Field(default=1.2, ge=0, le=20)
    min_order_filter_enabled: bool = True
    websocket_trigger_enabled: bool = True
    websocket_trigger_max_events: int = Field(default=80, ge=1, le=500)
    websocket_trigger_move_pct: float = Field(default=0.35, ge=0, le=20)
    websocket_trigger_quote_volume_usdt: float = Field(default=250_000.0, ge=0)
    observe_breakout_enabled: bool = True
    observe_breakout_min_score: float = Field(default=105.0, ge=0, le=200)
    observe_breakout_min_quality: float = Field(default=78.0, ge=0, le=100)
    observe_breakout_min_cost_ratio: float = Field(default=20.0, ge=0, le=200)
    observe_breakout_min_profit_factor: float = Field(default=1.5, ge=0, le=20)
    observe_breakout_min_net_pct: float = Field(default=4.0, ge=-100, le=500)
    observe_breakout_min_depth_notional_usdt: float = Field(default=500.0, ge=0)
    observe_breakout_max_spread_pct: float = Field(default=0.08, ge=0, le=5)
    observe_breakout_risk_multiplier: float = Field(default=0.22, ge=0.05, le=1)
    observe_low_price_threshold: float = Field(default=0.01, ge=0, le=1000)
    observe_low_price_risk_multiplier: float = Field(default=0.75, ge=0.05, le=1)
    observe_high_atr_pct: float = Field(default=3.0, ge=0, le=100)
    observe_high_atr_risk_multiplier: float = Field(default=0.75, ge=0.05, le=1)
    observe_extreme_atr_pct: float = Field(default=3.0, ge=0, le=100)
    observe_extreme_depth_notional_usdt: float = Field(default=5_000.0, ge=0)
    observe_consecutive_loss_count: int = Field(default=2, ge=0, le=20)
    observe_consecutive_loss_risk_multiplier: float = Field(default=0.5, ge=0.05, le=1)
    directional_cooldown_enabled: bool = True
    legacy_symbol_cooldown_blocks: bool = False
    symbol_cooldown_minutes: int = Field(default=0, ge=0, le=1440)
    stop_loss_cooldown_minutes: int = Field(default=30, ge=0, le=1440)
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
