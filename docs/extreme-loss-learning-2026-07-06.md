# Extreme Loss Learning - 2026-07-06

## Background

The live extreme sprint session produced many more trades, but the last 36 hours showed a poor risk profile:

- 36 closed trades
- 8 winners and 28 losers
- Net PnL around -24.89 USDT
- Commission around 3.79 USDT
- Profit factor around 0.35

The main lesson is that the system was finding opportunities, but probe entries were too willing to size up before live proof. Several high-volatility symbols also repeated after losses, which allowed one large loss to erase several small wins.

## Changes

### 1. New-symbol probe cap

Extreme V2 probe entries now use a separate cap when the symbol-direction has no live record.

Default:

- `extreme_probe_new_symbol_max_risk_pct = 2.2`

This keeps the bot learning from new opportunities without letting an unproven coin immediately consume the full probe risk.

### 2. After-loss probe damping

When a symbol-direction already has losses or is in a live-credit cooldown, extreme probe entries are damped again.

Defaults:

- `extreme_probe_loss_risk_multiplier = 0.55`
- `extreme_probe_after_loss_max_risk_pct = 1.2`

This is designed for cases like repeated EPICUSDT/TLMUSDT attempts: the bot can still try if a fresh signal appears, but it must try with smaller size until live behavior improves.

### 3. Derivatives-unconfirmed probe damping

If extreme V2 derivatives checks are enabled but OI/funding does not confirm the move, the probe can still pass, but at reduced risk.

Defaults:

- `extreme_probe_unconfirmed_derivative_risk_multiplier = 0.55`
- `extreme_probe_unconfirmed_derivative_max_risk_pct = 1.5`

This targets "price moves but OI does not follow" setups, which are more likely to be thin continuation or reversal traps.

### 4. Fee-pressure penalty

Live credit now applies an extra risk multiplier when a losing symbol-direction has high commission relative to its net loss.

Defaults:

- `live_credit_fee_pressure_enabled = true`
- `live_credit_fee_pressure_ratio = 0.20`
- `live_credit_fee_pressure_risk_multiplier = 0.75`

This helps small accounts avoid repeatedly paying high fees for low-quality noise trades.

## Expected Behavior

This does not turn off extreme sprint mode. It keeps the high-frequency opportunity search alive, while forcing weaker or less-proven opportunities to enter smaller.

The intended result is:

- similar opportunity discovery,
- fewer oversized first attempts,
- faster risk reduction after losses,
- less fee drag from bad repeated signals,
- better survival odds while the bot keeps collecting live data.

## Verification

Added tests:

- New extreme probe symbol is capped at the new-symbol risk cap.
- Extreme probe after a loss is additionally damped and capped.

Ran:

- `pytest tests/test_live_learning.py tests/test_scanner.py -q`
- `python -m compileall app`

