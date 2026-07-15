# Test Record

This file tracks local verification for the two-stage futures system.

## 2026-07-16 v0.9.0 V4 Opportunity Evidence

- Added an independent V4 challenger that ranks triggered opportunities by modeled post-cost expectancy, conservative lower bound and cross-sectional percentile instead of reusing V3 A+/A/B score thresholds.
- V4 evidence is release-scoped and cohort-scoped by market regime, direction and setup, with bounded SQLite reads cached for 60 seconds.
- Shadow trades now distinguish decision, exploration and paired-control evidence. Exploration samples selected V3 rejections, reducing the prior selective-label blind spot.
- Fee and slippage estimates are persisted separately while net PnL continues to deduct the conservative observed round-trip cost floor.
- V3.3 is archived. V3.2 remains the active live baseline; V4 live admission defaults off and cannot silently replace the active strategy.
- Dashboard candidate tables now prioritize V4 rank, conservative net expectancy and cohort sample evidence. PF without any losing denominator is displayed as “no loss sample” instead of 999.
- No Binance REST endpoint, request frequency, WebSocket subscription or order path was added. Hard stop, exchange protection orders and minimum-notional checks are unchanged.
- `python -m pytest -q`: `216 passed` (one existing local `requests` dependency compatibility warning).
- `python -m py_compile ...`: passed.
- `npm run build`: passed.
- Local HTTP smoke on `127.0.0.1:8094`: `/`, `/openapi.json`, `/api/status` and `/api/shadow-trades` returned `200`; OpenAPI reported `0.9.0`.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-15 v0.8.1 Release-Scoped Recovery Evidence

- V3 evidence now reads only the active strategy family/version/role instead of disabling symbol evidence to avoid legacy contamination.
- Current-release signal-type shadow evidence is tracked separately from symbol/direction evidence.
- During recovery, a candidate with established negative symbol/direction or signal-type evidence is skipped without consuming the recovery permit.
- The production PUMPUSDT counterfactual was checked: before the live entry, 19 current-release LONG shadow samples had net PnL -1.1015U and PF 0.599, which would have blocked the later losing live recovery probe.
- No Binance REST request or WebSocket subscription was added; the new evidence query uses the existing 15-second local SQLite cache.
- `python -m pytest -q`: `213 passed` (one existing local `requests` dependency compatibility warning).
- `python -m compileall -q app`: passed.
- `npm run build`: passed.


## 2026-07-15 v0.8.0 Persistent Recovery Permit

- Active V3.2 shadow evidence now issues a persistent, expiring recovery permit after stability confirmation, so recovery evidence and a valid candidate no longer need to occur in the same scan.
- A permit authorizes only one protected live probe; it is consumed only after live entry and exchange protection succeed, and is revoked after a losing probe, hard shadow failure, emergency stop, or protection failure.
- UTC daily-loss state resets once per trading day without resetting lifetime or release-level high-water marks.
- V3.2 release-level equity protection no longer inherits the drawdown peak of a superseded strategy release.
- Independent recovery and equity caps use the stricter absolute cap instead of multiplying into a non-economic order size.
- Dashboard exposes the recovery state, confirmation progress, permit time remaining, and UTC daily baseline in Chinese.
- No new Binance REST or WebSocket subscription is introduced; additional evidence uses the cached local SQLite window and state writes occur only on state transitions.
- `python -m pytest -q`: `211 passed` (one existing local `requests` dependency compatibility warning).
- `python -m compileall -q app`: passed.
- `npm run build`: passed; business, React and chart chunks remain separated.
- Local HTTP smoke on `127.0.0.1:8093`: `/`, `/api/status`, and `/api/config` returned `200`; OpenAPI reported `0.8.0`.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-14 v0.7.2 Evidence Scope Label

- Dashboard explicitly labels whether live guard evidence comes from the active release or the conservative historical fallback.
- `npm run build`: passed.

## 2026-07-14 v0.7.1 Live Release Backfill Hotfix

- A `legacy` placeholder no longer prevents live records from being matched to their exact open decision and active strategy version.
- Unmatched historical records remain in the conservative safety fallback; no evidence is guessed from symbol or outcome alone.
- Performance-guard caches are isolated by active version and window sizes.
- `python -m pytest -q`: `201 passed`.

## 2026-07-14 v0.7.0 V3.3 Paired Shadow

- V3.2 remains the only active live release; V3.3 is registered as a shadow-only challenger and V3.1 is archived.
- Active and challenger policies can record the same symbol, direction, entry type and time bucket with one shared opportunity ID.
- The global performance guard reads only the active V3.2 release when versioned evidence exists; archived profits cannot hide a bad current release.
- V3.3 reuses the existing scan snapshot and cached 1h shortlist, so it adds no order calls and no full-universe REST fan-out.
- Dashboard review now separates current recovery evidence, candidate validation and archived history in Chinese.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `200 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; output remains split into business, React and chart chunks.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-13 v0.6.0 V3.2 Evidence Guard

- Severe live losses and peak-equity drawdown independently close new-entry admission; A+ can no longer bypass the global guard.
- V3.2 blocks countertrend, panic-chase and overextended entries, keeps pre-breakout entries shadow-only, and requires medium-horizon confirmation for A+.
- Strategy evidence is isolated by strategy version and calibrated from bounded live/shadow samples with conservative net expectancy.
- SQLite schema setup runs once per process/database, compact telemetry payloads and two-day decision retention prevent unbounded growth.
- Dashboard exposes V3.2 evidence, calibration status and cached database storage metrics in Chinese.
- `python -m pytest -q`: `196 passed`.
- `npm run build`: passed; output split into business, React and chart chunks with no large-chunk warning.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-13 v0.5.0 V3.1 and Dashboard

- V3.1 medium-horizon challenger remains shadow-only and shares V3 market inputs.
- V3 and V3.1 shadow trades can coexist for the same symbol, direction, and signal.
- Dynamic stop test verifies the replacement stop is confirmed before the old stop is cancelled.
- Binance algo-order cancellation is regression-tested against the current `algoId`-only request contract.
- Manual V3.1 validation is cost-aware and reports an out-of-sample segment.
- Dashboard builds with five primary navigation items and a simplified active-settings view.
- Desktop and 390px mobile layouts were inspected in a running local build; no page-level horizontal overflow or browser console errors were found.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `189 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-12 v0.4.1 V3 Liquidity Admission Hotfix

- Unknown order-book liquidity blocks V3 A+/A live admission.
- A fresh WebSocket depth snapshot completes spread/depth checks without a REST depth request.
- `python -m pytest -q`: `184 passed` (one existing dependency compatibility warning).
- `python -m compileall -q app`: passed.

## Manual Checks

- Python syntax: `python -m compileall app`
- Unit tests: `pytest`
- HTTP smoke test: start `python -m app.main`, request `/`, `/api/market`, `/api/decisions`

## 2026-06-25 Local Verification

- `python -m compileall app`: passed
- `pytest -q`: `6 passed`
- HTTP smoke test on `127.0.0.1:8091`: `/`, `/api/market`, `/api/status`, `/api/decisions` all returned `200`

## 2026-06-28 Multi-Symbol Optimization

- Added multi-symbol crypto-only scanner.
- Added conservative, balanced, attack, and tournament growth modes.
- Added tests for crypto-only discovery and automatic small-account mode selection.
- Added mode-specific intervals and fee/slippage filters for high-frequency growth modes.

## 2026-06-30 React Dashboard

- Added Vite + React + TypeScript dashboard.
- Added SQLite telemetry for equity snapshots and event logs.
- Verified `npm run build`, `python -m compileall app`, `pytest -q`, and HTTP smoke tests for `/`, `/api/health/binance`, `/api/equity/snapshots`, `/api/logs`.

## Current Risk Notes

- Stage 1 live order path supports market entry plus protective stop and take-profit orders.
- Stage 2 can place conservative grid limit orders. In default long-only mode, it does not open shorts.
- Binance API keys should be IP-restricted and never include withdrawal permission.

## 2026-07-10 Automatic Stage Strategy

- Added S0-S4 automatic equity routing with 5% upward hysteresis, 10% downward hysteresis, three confirmations, expiring manual override, and flat-position handoff.
- Added hard stage risk caps after all signal, credit, drawdown, and legacy scalp multipliers.
- Isolated live credit for `extreme_v2_roll`, `orderbook_scalp`, and `grid_stable`.
- Added cross-process Binance request-weight and order-count budgets from `exchangeInfo` and response headers.
- Added private account/order WebSocket with REST snapshot fallback; listen keys are never persisted.
- Added stage-gated local L2 order books with sequence-gap rebuild, book ticker microprice, and rolling aggregate-trade flow.
- Added S4 grid plus low-risk scalp overlay with grid-symbol exclusion.
- Added Chinese stage route, API budget, and public/private stream status to the React Dashboard.
- Desktop and 390px mobile layouts were inspected in a running local build; mobile page width stayed inside the viewport.
- `python -m pytest -q`: `159 passed` (one local `requests` dependency compatibility warning).
- `python -m compileall -q app`: passed.
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.
- Latest local HTTP smoke on `127.0.0.1:8091`: `/`, `/api/status`, `/api/live-learning`, and `/api/health/binance` returned `200`; status returned S0, `extreme_sprint`, five profiles, and a valid REST budget.
- Binance Futures live stream probe passed: `public` delivered depth/book ticker data, `market` delivered aggregate trades, and the in-process trade-flow snapshot contained positive notional plus imbalance.
- VPS rollout feedback raised the default REST priority budgets to 40% background, 55% normal, 75% realtime, and 90% critical after the old 13.75% background ceiling deferred most deep checks at only 22% exchange usage. Explicit caller budgets keep the legacy 55% background split.
- Separated the background full-funnel cadence from the strategy execution cadence: full scans now have a configurable 30-second minimum while the WebSocket fast lane remains at two seconds.

## 2026-07-12 Rolling Performance Guard

- Added a cached account-level performance guard using the latest live and shadow trades. When both windows are negative, new entries pause for 60 minutes and then recover at a default `0.20x` risk multiplier.
- Added symbol/direction and market-direction evidence. Live-credit boosts now require positive live and shadow confirmation; negative agreement caps risk and a recent loss blocks same-direction re-entry for 30 minutes.
- Effective-order cost checks now use the higher of the candidate estimate and the observed 75th-percentile live round-trip cost with a safety multiplier.
- Shadow trades use the same observed cost floor, prevent overlapping symbol/direction/signal samples, and retain observed high/low prices with conservative ambiguous-bar settlement.
- Live-credit evidence now decays with a configurable 12-hour half-life instead of retaining old wins at a fixed weight.
- Fast-lane repeated WAIT records are throttled, compact payloads retain fewer candidates, strategy-run details default to seven-day retention, and closed shadow details default to fourteen-day retention.
- React Dashboard now shows the global trading-protection state, rolling live PF/loss count, and shadow PF in Chinese.
- Binance time health checks are cached for 60 seconds and serve the last valid result when internal REST reserves defer background work.
- `python -m compileall app`: passed.
- `python -m pytest -q`: `176 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-12 Opportunity Engine V3

- S0-S2 route to the isolated `extreme_v3_roll` strategy family; legacy V2 credit remains read-only for review.
- Added cross-sectional market regimes, relative strength, Donchian/EMA trend structure, normalized volume flow, OI/funding/basis confirmation, liquidity checks, observed cost admission, and A+/A/B opportunity tiers.
- Removed repeated rolling backtests from the live V3 path; validation remains available through deterministic tests and offline replay.
- Added graduated account recovery and a constrained A+ canary path that does not bypass hard equity, daily-loss, order viability, or protection controls.
- Shadow trades now use local WebSocket prices and 1m high/low data, retain V3 strategy/regime/score fields, and subscribe active shadow symbols without additional REST polling.
- React Dashboard shows V3 market state, tier counts, component scores, observed cost, and isolated V3 live credit in Chinese.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `182 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- Local HTTP smoke on `127.0.0.1:8091`: `/` returned 200 and `/api/live-learning` exposed the isolated `v3_scores` collection.
