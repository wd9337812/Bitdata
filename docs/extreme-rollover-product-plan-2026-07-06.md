# Extreme Rollover Product Plan

## 1. Product Goal

This document defines the next major version of Bitdata as a target-driven crypto futures rollover system.

The user target is:

- Phase A: grow small capital to `10000U` in 30 days.
- Phase B: grow `10000U` to `100000U` in the next 30 days.
- Phase C: grow `100000U` to `1000000U` in the next 30 days.
- Phase D: after `1000000U`, switch to lower-risk grid and portfolio trading.

This is an extreme-risk compounding goal. The product must not present it as stable profit. The system should make the risk explicit, automate execution discipline, and maximize useful learning from live trades.

## 2. Product Principles

1. Target driven, not static mode driven.
   The bot should know the current target, remaining days, required daily return, and whether the account is ahead or behind schedule.

2. Aggressive before `10000U`, increasingly defensive after larger equity milestones.
   A strategy that is acceptable at `50U` may be unacceptable at `10000U`.

3. More opportunities, but not blind entries.
   Increase attempts through event-driven scanning, small probes, and better routing. Do not remove fee, spread, depth, and minimum-order checks.

4. Live learning matters.
   Real trades should adjust symbol credit, direction confidence, sizing, cooldown, and future ranking.

5. Execution safety is a product feature.
   Every live order must have protection logic, timestamp safety, idempotency, and clear logs.

## 3. Capital Stages

| Stage | Equity Range | Mode | Main Goal | Risk Posture |
| --- | ---: | --- | --- | --- |
| S0 | `<100U` | Extreme Rollover | Find explosive moves and compound fast | Highest risk |
| S1 | `100-500U` | Extreme Sprint Plus | Continue compounding with limited diversification | Very high risk |
| S2 | `500-2000U` | Attack Rotation | Reduce single-trade ruin risk | High risk |
| S3 | `2000-10000U` | Trend/Event Hybrid | Push to the first major target | Medium-high risk |
| S4 | `10000-100000U` | Controlled Momentum | Protect capital while seeking 10x | Medium risk |
| S5 | `100000-1000000U` | Multi-strategy Portfolio | Avoid single-event ruin | Medium-low risk |
| S6 | `>1000000U` | Grid/Portfolio | Stable return, lower leverage | Low-medium risk |

Each stage should have independent defaults for:

- Max open positions.
- Base risk per trade.
- Max leverage.
- Max symbol margin.
- Probe sizing.
- Rotation strictness.
- Daily loss limit.
- Equity guard behavior.
- Stop/take-profit profile.
- Minimum liquidity and cost ratio.

## 4. Optimization Scope

This major version contains 9 product modules.

### 4.1 Goal Progress Controller

Purpose:
Convert the 30-day targets into daily required performance and risk posture.

Inputs:

- Current equity.
- Stage target equity.
- Stage start equity.
- Stage start time.
- Stage deadline.
- Current high watermark.
- Daily realized PnL.
- Consecutive wins/losses.

Outputs:

- `target_required_daily_return_pct`
- `target_progress_ratio`
- `target_status`: `ahead`, `on_track`, `behind`, `critical`
- `target_risk_multiplier`
- `recommended_stage`
- `reason`

Behavior:

- If ahead of target curve, reduce risk multiplier.
- If close to target curve, use default stage risk.
- If behind, increase opportunity-layer priority and risk multiplier within caps.
- If critically behind, allow extreme mode only when equity is still above hard floor.
- If hard drawdown is hit, require cooldown or manual confirmation before continuing.

Dashboard:

- Add target curve chart.
- Add current position on target curve.
- Add required daily return.
- Add "ahead/behind" label.
- Add stage countdown.

Tests:

- Ahead schedule lowers risk.
- Behind schedule raises risk but respects caps.
- Hard stop prevents new entries.

### 4.2 Event-Driven Entry Queue

Purpose:
Move from pure loop scanning to WebSocket-triggered opportunity routing.

Sources:

- WebSocket ticker movement.
- WebSocket kline movement.
- Volume burst.
- Depth/spread changes.
- Existing market stream hot list.
- Manual symbols.
- Symbols with positive live credit.

Pipeline:

1. Event arrives.
2. Normalize event into `OpportunityEvent`.
3. Score event urgency.
4. Push symbol into hot queue with TTL.
5. Runner prioritizes hot queue before normal scan.
6. If a hot symbol passes light checks, run full decision.

Important:

- This is not millisecond HFT.
- REST order placement remains controlled.
- Respect Binance rate limits.

New data structure:

```json
{
  "symbol": "VANRYUSDT",
  "event_type": "volume_breakout",
  "direction_hint": "LONG",
  "score": 88.5,
  "created_at": "...",
  "expires_at": "...",
  "features": {
    "move_pct": 1.2,
    "quote_volume": 350000,
    "spread_pct": 0.04
  }
}
```

Tests:

- Event adds symbol to hot queue.
- Expired events are ignored.
- Hot queue does not bypass execution filters.

### 4.3 Dynamic Stop And Take-Profit Engine

Purpose:
Replace one-size-fits-all ATR protection with adaptive protection per entry type and market state.

Profiles:

- Standard breakout.
- Preemptive entry.
- Firecracker probe.
- Weak-quality probe.
- Event-speed entry.
- Rotation replacement entry.

Rules:

- Fast invalidation stop: if price fails quickly after entry, exit early.
- Break-even move: when profit reaches a threshold, move protection near break-even.
- Partial take-profit: optional, only when order size supports minimum notional.
- Trailing protection: trail only after profit is larger than fee plus slippage buffer.
- Time stop: exit if no progress after configured bars.
- Liquidity deterioration exit: reduce or close if spread/depth worsens sharply.

Outputs:

- Stop order plan.
- Take-profit order plan.
- Optional break-even update.
- Optional trailing update.
- Exit reason.

Tests:

- Protection always covers live entry.
- Tiny order sizes do not create invalid partial exits.
- Break-even only activates after fee-adjusted profit.
- Time stop does not fight existing reduce-only protection orders.

### 4.4 Unified Position Sizing Engine

Purpose:
Centralize all risk multipliers into one auditable calculation.

Current problem:
Risk is influenced by mode, quality, credit, probes, equity guard, derivatives, and rotation. Some logic is spread across modules.

New formula:

```text
position_risk =
  stage_base_risk
  * signal_multiplier
  * quality_multiplier
  * live_credit_multiplier
  * target_progress_multiplier
  * equity_guard_multiplier
  * market_state_multiplier
  * liquidity_multiplier
```

Caps:

- Per-symbol margin cap.
- Per-stage max notional.
- Exchange min notional.
- Leverage cap.
- Daily loss cap.
- Consecutive loss cap.

Output object:

```json
{
  "allowed": true,
  "risk_pct": 1.2,
  "notional": 24.3,
  "leverage": 8,
  "margin": 3.04,
  "multipliers": {},
  "caps": {},
  "reasons": []
}
```

Tests:

- Every multiplier is visible in reason output.
- Risk cannot exceed configured caps.
- Minimum order filter blocks non-executable signals before live order.

### 4.5 Stage-Based Strategy Mode System

Purpose:
Make modes follow account stage instead of forcing the user to understand all settings.

New modes:

- `extreme_rollover`
- `extreme_sprint_plus`
- `attack_rotation`
- `trend_event_hybrid`
- `controlled_momentum`
- `portfolio_growth`
- `grid_stable`

Each mode defines:

- Signal layers enabled.
- Scanner depth.
- WebSocket hot queue size.
- Base risk.
- Max open positions.
- Position rotation strictness.
- Stop profile.
- Credit score sensitivity.
- Quality score weights.

Dashboard:

- Simple mode card: "当前阶段 / 当前目标 / 风险档位".
- Advanced config hidden behind collapsible panels.
- Warning if user forces a mode that conflicts with equity stage.

Tests:

- Equity maps to the expected stage.
- Manual override works only with confirmation.
- Stage defaults are loaded correctly.

### 4.6 Scan Funnel Performance Upgrade

Purpose:
Increase opportunity coverage while controlling CPU and API usage.

Funnel:

1. Recall: all Binance USDT perpetual crypto symbols.
2. Coarse rank: 24h volume, move, trade count, stream events.
3. Fine rank: Kline, ATR, trend, short backtest.
4. Auction: depth, spread, cost ratio, live credit, derivatives.
5. Execution: only highest actionable candidates.

Optimizations:

- Kline cache with TTL.
- Incremental backtest where possible.
- Separate hot-symbol fast path.
- Time budget per scan.
- Request-weight budget per scan.
- Per-symbol cooldown for expensive checks.
- Dashboard funnel metrics.

Dashboard:

- Recall count.
- Coarse count.
- Fine count.
- Auction count.
- Executable count.
- Scan elapsed time.
- API budget used.
- Cache hit rate.

Tests:

- Large recall does not force full backtest for every symbol.
- API budget stops lower-priority checks first.
- Hot queue symbols are still checked even under degraded scan.

### 4.7 Target Curve Dashboard

Purpose:
Make the dashboard understandable for a non-expert user.

New screens:

- "目标进度": equity vs required target curve.
- "今天能不能追": required daily return, current daily return, gap.
- "为什么没开仓": top blockers summary.
- "当前最值得盯": top hot events and candidates.
- "风险温度": current risk multiplier, drawdown, daily loss, open exposure.

Dashboard copy must be Chinese.

Example labels:

- `目标进度`
- `距离本阶段目标`
- `剩余天数`
- `今日需要收益`
- `当前风险档位`
- `不开仓主要原因`
- `最强机会`

Tests:

- API returns target progress data.
- Dashboard renders missing data safely.
- No raw Python/JSON-only explanation for normal user views.

### 4.8 Stage Simulation And Backtest

Purpose:
Backtest not just a symbol, but the full compounding path.

Simulation inputs:

- Start equity.
- Target equity.
- Days.
- Stage mode.
- Fee and slippage.
- Position sizing.
- Max open positions.
- Signal layers enabled.

Outputs:

- Final equity.
- Max drawdown.
- Win rate.
- Profit factor.
- Trades per day.
- Average hold time.
- Fee share.
- Ruin probability proxy.
- Best/worst day.

Important:
This should be used for comparison between parameter sets, not as a profit guarantee.

Dashboard:

- Run simulation button.
- Compare current config vs proposed config.
- Show "more trades but worse PF" warnings.

Tests:

- Simulation includes fees and slippage.
- Simulation respects min order notional.
- Simulation reflects max open position constraints.

### 4.9 Daily Learning Report

Purpose:
Turn live trading into systematic improvement.

Report sections:

- Account equity change.
- Trades opened/closed.
- Realized PnL.
- Fees and funding.
- Win/loss by symbol.
- Win/loss by direction.
- Win/loss by entry type.
- Missed opportunities.
- Blocker ranking.
- Symbols to reward.
- Symbols to penalize.
- Suggested config changes.

Storage:

- Save Markdown report in `data/reports/`.
- Dashboard shows latest report.
- Optional future notification hook.

Tests:

- Report generates with no trades.
- Report includes closed trades and blockers.
- Report does not expose API secrets.

## 5. Suggested Implementation Order

This should be one major version, but delivered in safe sub-steps.

### Step 1: Product foundation

- Add stage definitions.
- Add target progress controller.
- Add target dashboard API.
- Add dashboard target curve view.

### Step 2: Risk foundation

- Build unified position sizing engine.
- Route existing risk paths through it.
- Keep default outputs equivalent to current behavior.
- Add regression tests.

### Step 3: Event engine

- Add opportunity event model.
- Add hot queue storage.
- Wire market stream events into hot queue.
- Prioritize hot queue in runner.

### Step 4: Dynamic protection

- Add protection engine.
- Support fast invalidation, break-even, time stop.
- Keep current stop/take-profit as fallback.

### Step 5: Scan performance

- Add request budget and cache metrics.
- Expand recall while protecting API budget.
- Expose scan budget dashboard fields.

### Step 6: Simulation and learning

- Add stage simulation.
- Add daily report generator.
- Add dashboard report view.

### Step 7: Live rollout

- Deploy with dry-run shadow mode first if possible.
- Compare old vs new decisions for at least several scan cycles.
- Enable live only after no execution regressions.

## 6. Live Rollout Guardrails

Before live deployment:

- Existing open positions must be checked.
- Existing stop/take-profit protection orders must be checked.
- New version must not cancel protection orders unless replacing them intentionally.
- Runner must start in `running` only after config validation.
- If API errors occur, bot should pause new entries but not remove protection orders.

Runtime guardrails:

- Never place a live order without a protection plan.
- Never exceed Binance min notional and precision rules.
- Never expose API secret in logs or dashboard.
- Always log skipped trades with reason.
- Always log sizing calculation.

## 7. Acceptance Criteria

The major version is complete when:

- Dashboard shows target progress clearly in Chinese.
- The bot can auto-select stage by equity and target progress.
- Every candidate has a clear blocker or execution reason.
- WebSocket events can push symbols into a hot queue.
- Position sizing returns a single auditable object.
- Dynamic protection handles at least standard, probe, and event-speed entries.
- Scan funnel covers more symbols without exceeding API budget.
- Stage simulation can compare current and proposed configs.
- Daily learning report is generated automatically.
- Full test suite passes.
- VPS deployment preserves existing positions and protection orders.

## 8. Risk Statement

The 90-day target path has a very high probability of large drawdown or total loss. The product should support the user's chosen high-risk objective, but it must keep risk visible, decisions logged, and execution protected.

The system should optimize for:

- More qualified attempts.
- Faster learning.
- Clearer reasons.
- Better execution discipline.
- Controlled failure modes.

It should not claim guaranteed profitability.

