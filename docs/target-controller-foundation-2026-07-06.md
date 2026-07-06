# Target Controller Foundation

## Purpose

This iteration starts the major "extreme rollover" version described in `extreme-rollover-product-plan-2026-07-06.md`.

The first implementation focuses on foundations:

- Target progress calculation.
- Capital stage labeling.
- Risk multiplier explanation.
- Dashboard visibility.

It does not automatically make live trading more aggressive by default.

## New Target Controller

Added `app/target.py`.

The controller calculates:

- Current target phase: A, B, C, or GRID.
- Capital stage label: S0 to S6.
- Current equity.
- Phase target equity.
- Expected equity on the target curve.
- Target completion ratio.
- Remaining days.
- Required daily return.
- Progress status: `ahead`, `on_track`, `behind`, `critical`, `completed`.
- Suggested target risk multiplier.

Default target path:

- Phase A: `10000U`.
- Phase B: `100000U`.
- Phase C: `1000000U`.
- Each phase: `30` days.

## Safety Behavior

`target_risk_adjustment_enabled` defaults to `false`.

That means:

- Dashboard shows the suggested target risk multiplier.
- The decision object records the target progress.
- Live position size is not automatically increased by target pressure.

When the switch is enabled later, `effective_risk_multiplier` can participate in sizing.

## Position Sizing Explanation

Added `app/position_sizing.py`.

The current output explains:

- Base risk percentage.
- Final risk percentage.
- Signal multiplier.
- Quality multiplier.
- Live credit multiplier.
- Equity guard multiplier.
- Target progress multiplier.
- Market state multiplier.
- Max notional and margin caps.
- Human-readable reasons.

This is a visibility and audit layer first. A later version can move all sizing calculations into this module.

## Dashboard

The overview page now includes `目标进度`.

It shows:

- Current stage.
- Target equity.
- Expected equity on the curve.
- Remaining days.
- Required daily return.
- Target risk multiplier.
- Whether the multiplier is only displayed or already participating in sizing.

## State

When the runner syncs stage, it initializes:

- `target_active_phase`
- `target_phase_a_start_equity`
- `target_phase_a_start_time`

Equivalent keys are used for later phases.

## Verification

Added `tests/test_target.py`.

Covered behavior:

- Behind target suggests a higher multiplier, but does not apply it while disabled.
- Ahead target lowers multiplier when risk adjustment is enabled.
- Target phase state initializes correctly.
- Equity maps to S0/S4/S6 labels.
- Position sizing explanation includes multipliers and caps.

