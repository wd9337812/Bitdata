# V5.5 Candidate-Level Admission

Date: 2026-08-13

## Problem

V5.4 used the current-version aggregate shadow PF as an important recovery
signal. That is useful as a risk background, but it can become a practical
deadlock: a losing aggregate sample blocks every new live trade, so the system
cannot collect the new, candidate-level evidence that could prove a specific
setup is better.

V5.5 removes that aggregate-PF hard veto. It does not treat a short run of
positive shadow trades as proof either. Admission remains point-in-time and is
isolated to the V5.5 version.

## Routes

### Core full-bet route

The three V5.4 routes are retained unchanged in spirit:

- Broad downtrend: trend-aligned short pullback/retest.
- Quiet market: trend-aligned long pullback/retest.
- Mixed market: trend-aligned long pullback/retest.

The default core requirements are top 20% relative rank, 0.72 cross-sectional
strength, 3.5x gross-profit-to-cost coverage, two confirmations, an executable
order, and the existing exchange liquidity gate. Route risk caps remain 15%,
10%, and 12% respectively, below the S0 global 30% maximum.

### Limited exploration route

This route is for current, non-core trend-aligned candidates in broad up/down,
quiet, mixed, or rotation conditions. It accepts only breakout, pre-breakout,
or pullback/retest structures. Defaults are:

- Top 28% relative rank.
- Quality score at least 54.
- Cross-sectional strength at least 0.64.
- Gross-profit-to-cost coverage at least 2.4x.
- At least three of the five structural confirmations.
- Current trigger, direction-quality, minimum order, liquidity, exhaustion,
  local evidence, and re-entry checks must all pass.
- Maximum protected risk is 8%, even though the S0 allocator can reserve up to
  90% available margin. The route cap always wins over the account-level cap.

Exploration events use the `limited_exploration` evidence lane. V5.5 evidence
does not mix V5.4 or older events, and neither lane is stopped solely because
the total V5.5 shadow PF is below one.

## Safety Boundaries

V5.5 retains the 5 U hard stop, exchange-side take-profit and stop-loss
orders, minimum-notional checks, dynamic liquidity limits, API rate control,
time synchronisation, single-position S0 routing, and missing-protection
repair. It never manually opens positions.

## Validation

- Added unit coverage for the V5.5 capabilities, route policy, and a candidate
  admitted through the limited exploration lane without a global-PF gate.
- Relevant backend tests must pass before deployment.
- Frontend production build must succeed before the dashboard is deployed.
