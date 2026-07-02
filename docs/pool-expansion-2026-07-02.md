# Candidate pool expansion - 2026-07-02

## Context

The VPS is a 2 vCPU / 2 GB RAM instance. Before this change, the live system was scanning 20 symbols with about 72 REST weight per minute and no Binance cooldown. CPU load was moderate, and WebSocket market data was healthy.

## First-stage expansion

Default configuration is increased to:

- `max_observation_symbols`: 35
- `max_scan_symbols`: 30
- `max_trade_pool_symbols`: 15
- `depth_check_top_symbols`: 8
- `market_stream_max_symbols`: 30
- `tournament_loop_seconds`: unchanged at 30 seconds

The WebSocket market data hub now auto-discovers high-volume Binance USDT perpetual coin contracts and fills the stream list up to `market_stream_max_symbols`. Manual stage symbols are still pinned first.

## Depth checks

Depth checks now use `depth_check_min_current_score`, default `38.0`, instead of only checking active signals or very high preemptive scores.

This is meant to give stronger observe candidates a real spread/depth score earlier, without checking every symbol.

## Guardrails

If REST usage approaches local budget or CPU stays high, roll back in this order:

1. Reduce `max_observation_symbols` from 35 to 25.
2. Reduce `max_scan_symbols` from 30 to 20.
3. Reduce `market_stream_max_symbols` from 30 to 20.
4. Raise `tournament_loop_seconds` from 30 to 45.

