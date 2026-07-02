# Tournament drawdown policy - 2026-07-02

## Problem

The growth stage automatically switches accounts below 100 USDT into tournament mode. The previous risk gate still applied the global `max_drawdown_pct` against the historical account high watermark.

For a small aggressive account, that can permanently block new entries after a temporary equity peak. Example: a high watermark near 86 USDT and current equity near 72 USDT triggers the 15% max drawdown lock even when a valid tournament signal appears.

## Change

Tournament mode now ignores the global high-watermark max drawdown gate.

This does not remove risk controls. These gates still apply:

- Bot must be running.
- Equity must stay above `tournament_stop_equity`.
- Daily loss limit still uses `tournament_daily_loss_limit_pct`.
- Consecutive loss limit still applies.
- Symbol cooldown still applies.
- Max open positions still applies.
- Position sizing still uses stop-loss distance.
- Live entries still place protective stop-market and take-profit-market orders.

## Rationale

Tournament mode is intended to grow a small account aggressively. A high-watermark drawdown lock is better suited to conservative, balanced, and grid-style operation. In tournament mode, the more relevant protections are per-trade stop loss, daily loss, consecutive losses, and a hard stop-equity floor.

