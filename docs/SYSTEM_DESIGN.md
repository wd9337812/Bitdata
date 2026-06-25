# Two-Stage Futures System Design

## Stage 1: Growth Mode

Purpose: attempt small-account growth using a low-frequency futures continuation strategy.

Defaults:

- Symbols: `SOLUSDT`
- Direction: long only
- Risk per trade: `1%`
- Leverage cap: `2x`
- Max open positions: `1`
- Daily loss stop: `3%`
- Consecutive loss stop: `2`
- Target equity: `10000U`

Execution path:

1. Fetch 4h klines.
2. Build EMA/ATR signal.
3. Run risk checks.
4. Size by fixed account risk.
5. Round quantity/price by Binance exchange filters.
6. In live mode, set leverage, market buy, then place stop-market and take-profit-market close-position orders.

## Stage 2: Grid Mode

Purpose: use lower-leverage grid plans after the account reaches the configured target.

Defaults:

- Symbols: `BTCUSDT`, `ETHUSDT`
- Leverage cap: `1.5x`
- Reserve cash: `25%`
- Grid levels: `20-80`
- Stop on breakout: enabled
- Activation: manual

Grid execution uses a conservative long-grid by default:

- Place buy limit orders below current price.
- Place reduce-only sell limit orders above current price only when an existing long position exists.
- Place opening sell orders only when `allow_short=true`.
- Cancel existing open orders on the symbol before placing a fresh grid.

## Safety Gates

Live orders require all of these:

- `dry_run=false`
- `live_trading_enabled=true`
- `live_trading_confirmation=ENABLE_LIVE_TRADING`
- bot status is `running`
- risk checks pass

Dashboard start also requires the confirmation phrase `START_BOT`.

## Runner

`python -m app.runner` checks the current state and executes the relevant stage:

- `paused`: no action
- `growth`: evaluate Stage 1 symbols and execute the first eligible decision
- `grid`: generate and place/preview Stage 2 grid orders

The runner defaults to a 300-second loop through `BOT_LOOP_SECONDS`.
