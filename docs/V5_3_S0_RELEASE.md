# V5.3 S0 Direct-Live Release

## Why this release exists

V5.2 live results on 2026-08-10 were negative after fees and slippage. The
largest issue was not a lack of scans: repeated long entries in the same weak
cohort produced losses larger than several small wins. Its release high-water
mark also triggered a global entry pause at 35% drawdown, leaving a flat S0
account unable to evaluate a new, unrelated opportunity.

V4.5 had the best recorded net result, but only seven closed trades. It is a
useful structural hypothesis, not proof of a profitable system. V5.3 therefore
uses its trend-aligned, structured-entry idea through the existing V5.3 regime
router, rather than copying the small sample as a guaranteed edge.

## Execution rules

- S0 remains single-position, high-concentration execution with about 90% of
  available margin. Dynamic leverage is bounded by the protected stop and the
  configured stressed-risk ceiling.
- Only trend-aligned breakout, pre-breakout, or retest pullback structures can
  enter the V5.3 route. Broad-up selects longs and broad-down selects shorts.
- Repeated symbol/direction/structure episodes remain deduplicated and local
  loss/re-entry safeguards remain active.
- One net loss reduces the next independent opportunity to 0.80x risk, two to
  0.60x. Three losses retain the existing timed cooldown. A global release
  drawdown is recorded for audit but does not itself freeze new V5.3 entries.
- The 5U hard stop, Binance-side take-profit and stop-loss orders, minimum
  notional, liquidity gates, time sync, and API limits remain mandatory.

## Daily profit lock

The setting is a **net-profit percentage of the UTC daily starting equity**.
For example, a 13.1124U start and a 200% target require +26.2248U net profit,
so the account must reach roughly 39.3372U before the lock applies. It is not
a target of 16.22U. The dashboard now shows the start equity, required net
profit, required lock equity, and current equity together.

## Validation

- Targeted risk, daily-lock, continuous-permit, V5 opportunity, and sizing
  tests: 71 passed.
- Full repository suite: 799 passed.
- Production dashboard build: passed.

This release does not promise profitability. The next evaluation must compare
V5.3-only closed live opportunities, independent episodes, post-cost net PnL,
PF, drawdown, and long/short results separately.
