# Extreme Sprint V2 Firecracker System

## Goal

Extreme Sprint V2 is designed to increase controlled trading attempts during the small-account sprint phase. It does not simply lower all filters. Instead, it adds a separate "firecracker" path that can open small probe positions when a symbol has strong abnormal activity but is not yet clean enough for a full sprint entry.

## New Signal Layers

### Firecracker Recall

The scanner now gives extra priority to symbols with:

- Large 24h absolute move.
- Sufficient 24h quote volume.
- High trade activity.
- WebSocket trigger priority when available.

In `extreme_sprint`, manual symbols still receive a score boost, but they no longer permanently outrank high-scoring abnormal movers.

### Derivatives Confirmation

For top extreme candidates, the scanner checks:

- Open interest history.
- Open interest growth percentage.
- Funding rate crowding.

Price movement with rising OI receives a score bonus. Large price movement without OI support receives a penalty and lower risk multiplier.

### Spot Proxy Confirmation

Because direct spot CVD is not yet integrated, V2 uses a low-cost proxy:

- Recent volume spike.
- Current candle movement.

Volume-confirmed movement gets a small bonus. Sharp movement without volume confirmation is penalized.

### Squeeze Detection

V2 adds a volatility compression signal:

- Bollinger-style width compared with ATR channel width.
- Breakout from the compressed range.

This can help identify symbols that are preparing to move before a normal breakout fully triggers.

## Probe Entry

`extreme_probe` is a new entry type.

It can pass when:

- Extreme mode V2 is enabled.
- Firecracker score is high enough.
- The candidate is not a liquidity trap or spike wick.
- Derivatives risk multiplier is positive.
- Cost ratio is above the probe threshold.
- Recent backtest is not severely negative.

Probe entries:

- Use a small risk multiplier.
- Are capped by `extreme_probe_max_risk_pct`.
- Do not receive high-score or super-score large-position amplification.
- Can run even when the normal quality pool is only `observe`, as long as V2 conditions pass.

## Full Sprint Entries

Normal standard/preemptive/momentum entries still use the existing stricter path. High-score amplification remains reserved for non-probe entries only.

## New Main Config

- `extreme_v2_enabled`
- `extreme_firecracker_enabled`
- `extreme_probe_enabled`
- `extreme_derivatives_enabled`
- `extreme_spot_proxy_enabled`
- `extreme_squeeze_enabled`
- `extreme_probe_risk_multiplier`
- `extreme_probe_max_risk_pct`
- `extreme_oi_check_top_symbols`

## Safety Notes

This increases the number of possible small test entries. It does not guarantee faster account growth and can still lose money quickly. The intended behavior is:

- More attempts through small probe positions.
- Full-size sprint only when multiple confirmations align.
- No high-score amplification on probe positions.
