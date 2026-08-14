# V5.5.2: real-execution mirror and timed local re-entry

Date: 2026-08-15

## Why this release exists

The V5.5 review found that a recalled shadow candidate could receive an
opportunity id before the final live decision created its own id. That made
many live fills impossible to pair exactly with their corresponding shadow
records. It also found that the legacy local-loss recovery condition could
keep a cohort unavailable until it accumulated a large new shadow sample.

## Changes

1. Every accepted V5.5.2 live order writes one `execution_mirror` record with
   the final live `opportunity_id` after the order is accepted by Binance.
2. When Binance reconciliation closes that opportunity, the mirror receives
   the actual gross PnL, commissions, funding, net PnL and exit price.
3. Execution mirrors are diagnostic-only. They do not enter shadow admission,
   local circuit recovery, rank, PF, permit, credit or position sizing.
4. A two-live-loss local cohort first has a 90-minute hard cooldown. After it
   expires, only a RETEST or ARMED fresh structure with strong path, anti-chase,
   volume and at least three confirmations can make one half-risk probe.
5. The currently weak `LONG + breakout` exploration route is mirror-only. It
   continues to collect evidence but does not use real account risk.

## Safety boundaries unchanged

- 5U equity hard stop remains active.
- Binance-side stop loss and take profit remain mandatory for every position.
- Minimum notional, liquidity hard gates, rate limiting and time sync remain
  mandatory.
- The S0 account risk cap is not increased by this release.

## Verification

- `python -m compileall app`
- `pytest -q tests/test_shadow_trading.py tests/test_training_lineage.py tests/test_opportunity_v4.py`
  - 55 passed
- `npm run build` in `frontend`

## Observability

`/api/shadow` now exposes `active_release.execution_mirror`. It is an exact
real-execution comparison series and must be read separately from the eligible
decision-shadow evidence used by the strategy.
