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

## Current Risk Notes

- Stage 1 live order path supports market entry plus protective stop and take-profit orders.
- Stage 2 can place conservative grid limit orders. In default long-only mode, it does not open shorts.
- Binance API keys should be IP-restricted and never include withdrawal permission.
