# Opportunity Event Queue

## Purpose

This iteration adds the first event-driven entry foundation for the extreme rollover product plan.

The goal is to make sudden WebSocket market events affect scan priority faster, without bypassing the existing strategy, fee, depth, quality, credit, sizing, and live execution checks.

## New Module

Added `app/opportunity_queue.py`.

It stores recent opportunity events in:

```text
data/opportunity_events.json
```

Each event contains:

- `symbol`
- `event_type`
- `direction_hint`
- `score`
- `move_pct`
- `quote_volume`
- `interval`
- `source`
- `features`
- `created_at`
- `updated_at`
- `expires_at`

## Event Source

`market_stream.py` now writes a queue event when a kline trigger fires.

The existing `triggers` list remains for backward compatibility. The new queue adds:

- TTL.
- Event score.
- Direction hint.
- Deduplication.
- Scan-priority semantics.

## Scanner Integration

`scan_growth_candidates` now:

1. Reads active opportunity events.
2. Appends event symbols to recall if not already present.
3. Adds coarse-rank bonus through `opportunity_queue_score_weight`.
4. Prioritizes event symbols before ordinary ranked symbols.
5. Shows queue details in `funnel.opportunity_queue`.

Important:

- Event symbols do not automatically open positions.
- They must still pass normal signal, backtest, quality, cost, depth, risk, and execution filters.
- This is a faster routing layer, not a free-pass trading layer.

## Dashboard

The scan page now shows:

- Active event queue count.
- Top hot symbols from the queue.

`/api/status` also exposes `opportunity_queue`, so the dashboard can show event activity without waiting for a full scan response.

## Config

New defaults:

- `opportunity_queue_enabled = true`
- `opportunity_queue_ttl_seconds = 240`
- `opportunity_queue_max_events = 120`
- `opportunity_queue_scan_limit = 50`
- `opportunity_queue_score_weight = 0.35`
- `opportunity_queue_min_score = 20`

## Verification

Added tests for:

- Queue scoring and persistence.
- Low-score event filtering.
- Market stream trigger writing to the queue.
- Scanner prioritizing queue symbols for expensive K-line analysis.

