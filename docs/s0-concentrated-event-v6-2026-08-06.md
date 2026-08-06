# S0 Concentrated Event V6

## Purpose

V6 keeps the existing adaptive 30-day momentum route as the S0 fallback and adds a higher-risk, event-priority route. It is designed for rare, independently confirmed market events; it is not a promise of profitability and it does not trade Polymarket.

## Executable S-grade event

An event may be considered only when all of the following are present:

1. A public Polymarket probability moves by at least 8 percentage points.
2. The event can be mapped to a Binance USD-M symbol and direction.
3. Binance 5-minute price movement agrees with that direction.
4. Binance 5-minute volume is at least 1.5 standard deviations above its recent baseline.

Funding, social/news language, a probability change, or a Binance volume spike alone are not executable. The monitor uses only public Gamma data and public Binance market data. It never holds a Polymarket account, places a Polymarket order, or treats a prediction price as a trading instruction.

The question direction and the probability-change direction are both required. For example, falling odds for “Will BTC rise?” are treated as bearish rather than blindly long. The same prediction-market source cannot open another concentrated position for six hours.

## S0 risk definition

`50%` means the maximum planned loss of account equity **if the exchange stop is filled**, not 50% margin. The order sizing is the smaller of:

- risk sizing from stop distance plus estimated fee/slippage; and
- 97% available margin multiplied by at most 15x leverage.

The engine also requires a liquidation-buffer proxy of at least 2%, keeps `hard_stop_equity=5U`, and reserves 0.25U above that line. It therefore refuses a trade whose planned stopped loss would cross the hard-stop headroom.

Default exchange protection is a 3% price stop and 2R take profit. Runtime management moves the stop to break-even after one stop-distance of favorable movement, starts trailing after 1.2R, and time-stops a stagnant position at six hours. Exchange-side stop and take-profit placement remains mandatory; a protection placement failure closes the entry.

## Scheduling and API use

The event monitor polls public Polymarket data every 60 seconds. The expensive funding, 1-hour volume, listing, and 30-day momentum scans run no more than once every five minutes. This keeps V6 event detection responsive without multiplying Binance REST usage for the 43-symbol research universe.

## Daily profit lock

The daily profit lock remains enabled. Once its target is reached and the account is flat, it stops **new** entries for the rest of that UTC day. It never force-closes an existing position with exchange-side protection. Hard stop, minimum order rules, time sync, REST limits, and protection auditing remain unchanged.

## Initial operational boundary

V6 event entries have priority only while the account is flat. Existing protected fallback positions keep their protection and management; V6 does not close them merely to chase an event. New-listing open-tail research remains shadow-only until enough forward samples establish real execution costs.
