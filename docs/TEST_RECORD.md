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
