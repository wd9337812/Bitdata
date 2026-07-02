# Live performance risk adjustment - 2026-07-02

## Reason

INUSDT SHORT produced several completed live trades in the previous session and the combined realized PnL was positive. The scanner previously treated symbol quality as a static gate, so a symbol could show a current valid signal and strong recent backtest PF/net return while still being blocked by the generic simulation win-rate or depth thresholds.

## Change

The scanner now reads recent Binance futures user trades for the same symbol and position side. If recent live performance passes a small sample gate, the candidate can move from `observe` to `adaptive_live`.

Default gate:

- Window: 36 hours
- Minimum closed trades: 2
- Minimum net realized PnL after USDT commissions: 0.5 USDT
- Minimum live profit factor: 1.05
- Minimum adaptive depth: 1,500 USDT
- Minimum simulated net return: 5%
- Check trigger: current signal is active, or current candidate score is at least 70

The adjustment is same-symbol and same-direction only. A profitable INUSDT SHORT history does not relax rules for INUSDT LONG or any other symbol.

To protect Binance private API frequency limits, user-trade history is checked only for high-potential candidates. Low-score observe symbols keep using public market data only.

## Risk handling

`adaptive_live` does not use full tournament risk. It applies `live_performance_risk_multiplier`, default `0.6`.

For the current 50-100U tournament profile:

- Base tournament risk: 15%
- SHORT multiplier: 0.5
- Adaptive live multiplier: 0.6
- Effective risk: 4.5%

Hard controls still apply after this gate:

- Bot must be running
- Live mode must be enabled
- Max open positions
- Daily loss limit
- Max drawdown
- Symbol cooldown
- Exchange min-notional and precision filters
- Stop-loss and take-profit order placement

## Validation

Added tests covering a same-symbol/same-direction positive live history that upgrades an otherwise blocked candidate to `adaptive_live` with reduced risk.
