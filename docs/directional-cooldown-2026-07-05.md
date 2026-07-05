# Directional Cooldown Upgrade - 2026-07-05

## Goal

The previous cooldown behavior could block a whole symbol after an entry, even when the next valid signal was the opposite direction. This was too coarse for fast tournament mode. The new design makes cooldown directional and lets live-credit risk multipliers carry most of the punishment.

## Rules

- Cooldown scope is now symbol + direction, for example `LABUSDT:LONG` and `LABUSDT:SHORT`.
- Legacy whole-symbol cooldown is disabled by default through `legacy_symbol_cooldown_blocks=false`.
- Entry cooldown defaults to `symbol_cooldown_minutes=0`; loss-driven cooldown comes from live trade learning after Binance fills are synced.
- Live-credit cooldown is not a hard ban unless the credit score reaches the fuse threshold.
- Strong candidates can penetrate an active cooldown, but position risk remains capped by the cooldown multiplier.

## Default Sprint Values

- Normal loss cooldown: 15 minutes, max risk multiplier `0.60x`
- Quick stop cooldown: 0.5 hours, max risk multiplier `0.40x`
- Two consecutive same-direction losses: 1 hour, max risk multiplier `0.25x`
- Three consecutive same-direction losses: 3 hours, max risk multiplier `0.10x`
- Fuse score: `<= 2`, blocks entries until natural recovery
- Cooldown bypass: enabled when score is at least `95` and cost ratio is at least `18`

## Implementation Notes

- `app/risk.py` supports `symbol_direction_cooldowns` and keeps legacy symbol cooldown behind an explicit switch.
- `app/runner.py` writes directional entry cooldowns only when `symbol_cooldown_minutes` is greater than zero.
- `app/live_learning.py` stores `last_hold_seconds` in `symbol_live_scores` to distinguish quick stops from normal losses.
- `apply_live_credit_to_candidate` now includes a structured `cooldown` object and `cooldown_bypass` flag in `live_credit_adjustment`.

## Safety

This change does not modify signal generation, scanner ranking, order placement, stop loss, take profit, or position rotation logic. It only changes how cooldown state and live-credit risk caps affect candidates.
