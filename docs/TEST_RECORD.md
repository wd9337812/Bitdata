# Test Record

This file tracks local verification for the two-stage futures system.

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
