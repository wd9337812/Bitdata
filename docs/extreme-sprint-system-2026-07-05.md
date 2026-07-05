# Extreme Sprint Iteration - 2026-07-05

## Goal

This iteration adds an explicit `extreme_sprint` mode for small-account tournament growth. It is intentionally more aggressive than `tournament_sprint`, but still keeps hard execution and drawdown guards.

## Main Changes

- Added `extreme_sprint` as a separate growth mode.
- Requires both:
  - `growth_mode = extreme_sprint`
  - `extreme_sprint_confirmation = ENABLE_EXTREME_SPRINT`
- Uses faster default protection:
  - standard: `0.75 ATR` stop, `1.05 ATR` take profit, `4` bars max hold
  - preemptive: `0.65 ATR` stop, `0.90 ATR` take profit, `3` bars max hold
  - momentum: `0.70 ATR` stop, `1.00 ATR` take profit, `3` bars max hold
- Adds score-based risk amplification:
  - score >= `110`: `1.35x`
  - score >= `135`: `1.75x`
- Adds minimum order pre-filter before a candidate can pass.
- Adds WebSocket kline trigger events so fast-moving symbols enter the next scan with higher priority.
- Adds market state classification:
  - trend breakout
  - trend continuation
  - spike wick
  - liquidity trap
  - chop
- Adds equity high-watermark guard:
  - risk is scaled down as drawdown deepens
  - extreme mode pauses new entries at `40%` high-watermark drawdown by default
- Adds live credit time decay:
  - recent trades carry more weight
  - stale records carry less weight

## Safety Notes

`extreme_sprint` is a high-risk mode. It is designed for the user's stated goal of aggressive small-account growth, not stable income. The system keeps:

- live trading confirmation
- extreme sprint confirmation
- minimum order checks
- daily loss limits
- equity high-watermark guard
- max open position limits
- Binance order filter checks at final execution

## Deployment Notes

Before deployment to VPS, check:

- current positions
- open orders
- account equity
- bot status

Do not cancel or close live positions during deployment unless explicitly requested.
