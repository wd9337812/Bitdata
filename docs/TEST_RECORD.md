# Test Record

This file tracks local verification for the two-stage futures system.

## 2026-07-23 v0.20.1 S0 Training Lineage

- Added one `opportunity_id` from V4 decision through Binance entry order, exchange protection and reconciled close.
- Added bounded 12-bar one-minute WebSocket history for decision-time features; no new Binance REST request was introduced.
- Live reconciliation now preserves entry/exit order ids, split commissions, funding, direction-aware entry slippage and exit attribution.
- Exact order-id matches are separated from approximate legacy matches. The Dashboard shows training data quality in Chinese.
- Added authenticated quality and dataset endpoints plus `scripts/export_training_dataset.py`.
- Focused regression: `python -m pytest tests/test_training_lineage.py tests/test_shadow_trading.py tests/test_market_stream.py -q`: `18 passed`.
- Complete backend regression: `python -m pytest -q`: `315 passed`; one existing local `requests` dependency compatibility warning remains.
- `python -m compileall app`: passed.
- Frontend `npm run build`: passed; production static assets were regenerated.

## 2026-07-21 v0.16.2 V4.6.2 Evidence Weighting

- Reweighted the live V4 sorter from V4.4+ closed-trade evidence: directed flow, persistent volume, regime fit and medium path now lead the model; saturated alignment and cross-sectional inputs carry less weight.
- Momentum and breakout receive bounded ranking adjustments; pullback and prebreakout are penalized. Smart flow remains a separate, visible, at-most +/-5 point soft adjustment.
- SHORT admission requires stronger flow, regime and medium-path confirmation and receives a 0.65 direction risk multiplier for this release. The 15% stressed risk cap remains unchanged.
- Low-return full-bet positions use a two-bar time exit (about 10 minutes) instead of the prior six-bar default. Exchange stop-loss and take-profit orders remain authoritative.
- V4.6.1 configuration migrates to an isolated V4.6.2 evidence scope, canary id and two-bar time exit without deleting history.
- A one-time migration marker repairs partially persisted multi-container upgrades without overriding a later explicit user change back to six bars.
- Focused regression: `python -m pytest tests/test_config_store.py tests/test_opportunity_v4.py -q`: `24 passed`.
- Complete backend regression: `python -m pytest -q`: `301 passed`; one existing local `requests` dependency compatibility warning remains.
- `python -m compileall -q app`: passed.
- Frontend `npm run build`: passed; generated `index-BdyugdOT.js` and the existing split React/chart chunks.
- Local HTTP smoke on `127.0.0.1:8097`: `/`, `/openapi.json` and all five referenced static assets returned `200`; OpenAPI reported `0.16.2`.

## 2026-07-17 v0.9.2 V4 Protection Hotfix

- A release-scoped streak of four consecutive losing live trades now triggers performance protection even when earlier winners leave aggregate release PnL positive.
- Runtime position protection owns `critical` Binance REST priority, so price fallback, stop inspection and emergency exits do not inherit the background scanner budget.
- Existing Binance `closePosition` stops are retained when an atomic tighten is unavailable; the system no longer retries a rejected second stop or cancels the confirmed hard stop first.
- Shadow test fixtures include version and role in their dedupe keys, matching production evidence isolation.
- V4 ranking, admission, leverage and sizing parameters are unchanged.
- `python -m pytest -q`: `225 passed`.
- `python -m compileall -q app`: passed.
- `npm run build`: passed; production asset names and chunk split remained stable.
- Local HTTP smoke on `127.0.0.1:8095`: `/`, `/openapi.json` and all five referenced static assets returned `200`; OpenAPI reported `0.9.2`.

## 2026-07-16 v0.9.0 V4 Opportunity Evidence

- Added an independent V4 challenger that ranks triggered opportunities by modeled post-cost expectancy, conservative lower bound and cross-sectional percentile instead of reusing V3 A+/A/B score thresholds.
- V4 evidence is release-scoped and cohort-scoped by market regime, direction and setup, with bounded SQLite reads cached for 60 seconds.
- Shadow trades now distinguish decision, exploration and paired-control evidence. Exploration samples selected V3 rejections, reducing the prior selective-label blind spot.
- Fee and slippage estimates are persisted separately while net PnL continues to deduct the conservative observed round-trip cost floor.
- V3.3 is archived. V3.2 remains the active live baseline; V4 live admission defaults off and cannot silently replace the active strategy.
- Dashboard candidate tables now prioritize V4 rank, conservative net expectancy and cohort sample evidence. PF without any losing denominator is displayed as “no loss sample” instead of 999.
- No Binance REST endpoint, request frequency, WebSocket subscription or order path was added. Hard stop, exchange protection orders and minimum-notional checks are unchanged.
- `python -m pytest -q`: `216 passed` (one existing local `requests` dependency compatibility warning).
- `python -m py_compile ...`: passed.
- `npm run build`: passed.
- Local HTTP smoke on `127.0.0.1:8094`: `/`, `/openapi.json`, `/api/status` and `/api/shadow-trades` returned `200`; OpenAPI reported `0.9.0`.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-15 v0.8.1 Release-Scoped Recovery Evidence

- V3 evidence now reads only the active strategy family/version/role instead of disabling symbol evidence to avoid legacy contamination.
- Current-release signal-type shadow evidence is tracked separately from symbol/direction evidence.
- During recovery, a candidate with established negative symbol/direction or signal-type evidence is skipped without consuming the recovery permit.
- The production PUMPUSDT counterfactual was checked: before the live entry, 19 current-release LONG shadow samples had net PnL -1.1015U and PF 0.599, which would have blocked the later losing live recovery probe.
- No Binance REST request or WebSocket subscription was added; the new evidence query uses the existing 15-second local SQLite cache.
- `python -m pytest -q`: `213 passed` (one existing local `requests` dependency compatibility warning).
- `python -m compileall -q app`: passed.
- `npm run build`: passed.


## 2026-07-15 v0.8.0 Persistent Recovery Permit

- Active V3.2 shadow evidence now issues a persistent, expiring recovery permit after stability confirmation, so recovery evidence and a valid candidate no longer need to occur in the same scan.
- A permit authorizes only one protected live probe; it is consumed only after live entry and exchange protection succeed, and is revoked after a losing probe, hard shadow failure, emergency stop, or protection failure.
- UTC daily-loss state resets once per trading day without resetting lifetime or release-level high-water marks.
- V3.2 release-level equity protection no longer inherits the drawdown peak of a superseded strategy release.
- Independent recovery and equity caps use the stricter absolute cap instead of multiplying into a non-economic order size.
- Dashboard exposes the recovery state, confirmation progress, permit time remaining, and UTC daily baseline in Chinese.
- No new Binance REST or WebSocket subscription is introduced; additional evidence uses the cached local SQLite window and state writes occur only on state transitions.
- `python -m pytest -q`: `211 passed` (one existing local `requests` dependency compatibility warning).
- `python -m compileall -q app`: passed.
- `npm run build`: passed; business, React and chart chunks remain separated.
- Local HTTP smoke on `127.0.0.1:8093`: `/`, `/api/status`, and `/api/config` returned `200`; OpenAPI reported `0.8.0`.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-14 v0.7.2 Evidence Scope Label

- Dashboard explicitly labels whether live guard evidence comes from the active release or the conservative historical fallback.
- `npm run build`: passed.

## 2026-07-14 v0.7.1 Live Release Backfill Hotfix

- A `legacy` placeholder no longer prevents live records from being matched to their exact open decision and active strategy version.
- Unmatched historical records remain in the conservative safety fallback; no evidence is guessed from symbol or outcome alone.
- Performance-guard caches are isolated by active version and window sizes.
- `python -m pytest -q`: `201 passed`.

## 2026-07-14 v0.7.0 V3.3 Paired Shadow

- V3.2 remains the only active live release; V3.3 is registered as a shadow-only challenger and V3.1 is archived.
- Active and challenger policies can record the same symbol, direction, entry type and time bucket with one shared opportunity ID.
- The global performance guard reads only the active V3.2 release when versioned evidence exists; archived profits cannot hide a bad current release.
- V3.3 reuses the existing scan snapshot and cached 1h shortlist, so it adds no order calls and no full-universe REST fan-out.
- Dashboard review now separates current recovery evidence, candidate validation and archived history in Chinese.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `200 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; output remains split into business, React and chart chunks.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-13 v0.6.0 V3.2 Evidence Guard

- Severe live losses and peak-equity drawdown independently close new-entry admission; A+ can no longer bypass the global guard.
- V3.2 blocks countertrend, panic-chase and overextended entries, keeps pre-breakout entries shadow-only, and requires medium-horizon confirmation for A+.
- Strategy evidence is isolated by strategy version and calibrated from bounded live/shadow samples with conservative net expectancy.
- SQLite schema setup runs once per process/database, compact telemetry payloads and two-day decision retention prevent unbounded growth.
- Dashboard exposes V3.2 evidence, calibration status and cached database storage metrics in Chinese.
- `python -m pytest -q`: `196 passed`.
- `npm run build`: passed; output split into business, React and chart chunks with no large-chunk warning.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-13 v0.5.0 V3.1 and Dashboard

- V3.1 medium-horizon challenger remains shadow-only and shares V3 market inputs.
- V3 and V3.1 shadow trades can coexist for the same symbol, direction, and signal.
- Dynamic stop test verifies the replacement stop is confirmed before the old stop is cancelled.
- Binance algo-order cancellation is regression-tested against the current `algoId`-only request contract.
- Manual V3.1 validation is cost-aware and reports an out-of-sample segment.
- Dashboard builds with five primary navigation items and a simplified active-settings view.
- Desktop and 390px mobile layouts were inspected in a running local build; no page-level horizontal overflow or browser console errors were found.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `189 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-12 v0.4.1 V3 Liquidity Admission Hotfix

- Unknown order-book liquidity blocks V3 A+/A live admission.
- A fresh WebSocket depth snapshot completes spread/depth checks without a REST depth request.
- `python -m pytest -q`: `184 passed` (one existing dependency compatibility warning).
- `python -m compileall -q app`: passed.

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

## 2026-07-12 Rolling Performance Guard

- Added a cached account-level performance guard using the latest live and shadow trades. When both windows are negative, new entries pause for 60 minutes and then recover at a default `0.20x` risk multiplier.
- Added symbol/direction and market-direction evidence. Live-credit boosts now require positive live and shadow confirmation; negative agreement caps risk and a recent loss blocks same-direction re-entry for 30 minutes.
- Effective-order cost checks now use the higher of the candidate estimate and the observed 75th-percentile live round-trip cost with a safety multiplier.
- Shadow trades use the same observed cost floor, prevent overlapping symbol/direction/signal samples, and retain observed high/low prices with conservative ambiguous-bar settlement.
- Live-credit evidence now decays with a configurable 12-hour half-life instead of retaining old wins at a fixed weight.
- Fast-lane repeated WAIT records are throttled, compact payloads retain fewer candidates, strategy-run details default to seven-day retention, and closed shadow details default to fourteen-day retention.
- React Dashboard now shows the global trading-protection state, rolling live PF/loss count, and shadow PF in Chinese.
- Binance time health checks are cached for 60 seconds and serve the last valid result when internal REST reserves defer background work.
- `python -m compileall app`: passed.
- `python -m pytest -q`: `176 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- `git diff --check`: passed; Git reported only Windows LF/CRLF conversion notices.

## 2026-07-12 Opportunity Engine V3

- S0-S2 route to the isolated `extreme_v3_roll` strategy family; legacy V2 credit remains read-only for review.
- Added cross-sectional market regimes, relative strength, Donchian/EMA trend structure, normalized volume flow, OI/funding/basis confirmation, liquidity checks, observed cost admission, and A+/A/B opportunity tiers.
- Removed repeated rolling backtests from the live V3 path; validation remains available through deterministic tests and offline replay.
- Added graduated account recovery and a constrained A+ canary path that does not bypass hard equity, daily-loss, order viability, or protection controls.
- Shadow trades now use local WebSocket prices and 1m high/low data, retain V3 strategy/regime/score fields, and subscribe active shadow symbols without additional REST polling.
- React Dashboard shows V3 market state, tier counts, component scores, observed cost, and isolated V3 live credit in Chinese.
- `python -m compileall -q app`: passed.
- `python -m pytest -q`: `182 passed` (one existing local `requests` dependency compatibility warning).
- `npm run build`: passed; Vite reported only the existing large-chunk advisory.
- Local HTTP smoke on `127.0.0.1:8091`: `/` returned 200 and `/api/live-learning` exposed the isolated `v3_scores` collection.
# v0.9.3 V4 决策影子证据口径修复（2026-07-17）

- 假设：用于研究漏判的 `exploration` 影子不属于当前实盘决策策略，不能参与 `risk_off` 的恢复 PF 和恢复许可证判断。
- 代码：V4 风险保护仅查询当前版本、`active` 角色、`decision` 证据；探索影子继续保留在数据库和复盘统计中。
- 定向回归：`python -m pytest tests/test_performance_guard.py -q`，12 passed。
- 完整后端：`python -m pytest -q`，226 passed。
- Python 编译：`python -m compileall -q app tests`，通过。
- 前端：`npm run build`，通过。
- 本地静态冒烟：首页、主脚本、React、图表和 CSS 资源均返回 HTTP 200。
- VPS 已部署提交 `b7660c5`；机器人、S0/V4 路由、WebSocket、REST 频控和 Dashboard 正常。

# v0.9.4 权益回撤恢复死锁修复（2026-07-17）

- 线上复现：权益约 19.63U、无持仓；高点回撤约 13.26%，V4 最近 20 笔决策影子净收益 1.264U、PF 1.821，但恢复许可仍停在 `revoked`。
- 根因：高点回撤被同时作为永久 `emergency_stop` 传入恢复控制器，使影子确认通道永远不可达。
- 修复：高点回撤继续触发 `risk_off` 和冷却；仅 5U 硬停止保持无条件撤销。冷却后仍须连续三份当前 V4 决策影子正证据才签发一次 `0.4x` 许可。
- 定向回归：`python -m pytest tests/test_performance_guard.py tests/test_recovery_controller.py -q`，19 passed。
- 完整后端：`python -m pytest -q`，228 passed。
- Python 编译：`python -m compileall -q app tests`，通过。
- 前端：`npm run build`，通过。
- 本地静态冒烟：首页及 5 个 JS/CSS 资源全部返回 HTTP 200。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。

## 2026-07-20 V4.4 VPS 实盘部署验收

- 发布提交 `f63cab7`，标签 `v0.14.0`；部署前线上提交为 `6d7f028`，机器人正常运行。
- 部署前 Binance 实盘权益约 `21.9221U`，空仓、普通挂单 0、条件单 0；停止 runner 后再次复核仍为空仓且无残留订单，没有主动平仓或撤销保护单。
- 配置与状态备份为 `/opt/bitdata/backups/v0.14.0-20260719T200547Z`；VPS 原生 `bash -n deploy.sh` 与 `bash -n ops/bitdata-maintenance.sh` 均通过。
- 线上配置原子升级为 V4.4：S0 风险上限 15%、保证金计划 90%、动态杠杆 3x-10x、单仓；常规扫描 120 币、WebSocket 监控 150 币。
- 新版本许可证精确绑定 `extreme_v4_roll@v4.4`，状态 `waiting_candidate`、倍率 `1.0x`、已用 `0/6`、亏损 `0`；旧 V4.3.2 许可证没有参与 V4.4 放行。
- 部署后 Dashboard 与 runner 均为 `0.14.0` / `f63cab7`，容器重启 0、OOM 0；机器人保持实盘运行。
- 公共 WebSocket 在线，覆盖 150 币、4 条连接、451 个流；私有账户流在线。REST 无冷却或 429，交易所时间偏差约 `-76ms`。
- Dashboard 首页、OpenAPI 和 5 个版本化 JS/CSS 资源全部 HTTP 200；近三分钟无 429/500/502、数据库锁、异常栈或错误日志。
- 完整扫描与实时快车道均已运行，后台扫描约 10-19 秒；最新候选使用 `v4.4`，因当前为 `shadow_only` 未开仓，证明新版本没有绕过准入硬门制造交易。
- 上线约 20 分钟后 V4.4 已完成 1 个盈利许可证机会，并建立第 2 个机会：`ESPORTSUSDT SHORT`。该仓明确记录为 `s0_full_bet_v44`，保证金利用率 90%、动态杠杆 5x、压力风险约 14.77%，未追加。
- 第 2 个机会建立后 Binance 普通挂单 0，条件单 2：有效 `STOP_MARKET` 1 张、`TAKE_PROFIT_MARKET` 1 张；保护审计状态 `protected`，机器人继续运行，未发现错误日志。
- VPS 部署前：权益约 19.63U，无持仓、普通挂单或条件单。
- VPS 已部署提交 `5f338be`；机器人为 `running`，S0 路由为 `extreme_v4_roll@v4.0`，公共/私有 WebSocket 正常，REST 未限流。
- 部署后：Binance 仍无持仓、普通挂单或条件单；Dashboard 首页及 5 个静态资源全部返回 HTTP 200，近 3 分钟无 429/500/502、数据库锁或异常日志。
- 恢复状态已由不可达的 `revoked` 转为 `accumulating`。当时最近 20 笔 V4 决策影子净收益 -0.202U、PF 0.9255，证据未达正收益要求，因此没有立即签发试单许可。

# v0.10.0 / V4.1 状态自适应与新策略许可证（2026-07-17）

- 假设：V4.0 的统一质量分层失真，需要按市场状态、方向、信号与入场阶段拆分；新版本不能被旧版本 `risk_off` 永久锁死，但试单不得绕过账户和交易所安全边界。
- 后端：决策影子独立去重；探索/对照证据隔离；动态成本和保守净期望硬门；回踩优先；三档状态化保护；精确版本限时、限次许可证及 `0.4x -> 0.7x -> 1.0x` 晋级。
- 前端：中文展示 V4.1 状态路线、成本比、准入等级、恢复许可证与新策略试运行许可证。
- 数据库：新增 V4.1 版本/证据类型索引；未清空任何历史数据。
- 完整后端：`python -m pytest -q`，232 passed。
- Python 编译：`python -m compileall -q app`，通过。
- 前端：`npm run build`，通过，生成版本化静态资源。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。
- 本地 HTTP 冒烟：主页、`openapi.json`、`/api/status` 及 5 个版本化 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.10.0`。
- 发布提交：`13a5bfb`，分支 `codex/two-stage-live-system`。
- VPS 部署前：权益 `19.63063606U`，机器人运行中，无持仓、普通挂单或条件单；公共/私有 WebSocket 正常，REST 无冷却。
- VPS 配置仅更新 40 个 V4.1 专属字段，原 Binance API 与其他用户配置保持不变；旧配置及状态已备份到 `/opt/bitdata/backups/v0.10.0-*`。
- VPS 部署后：提交 `13a5bfb`，路由 `extreme_v4_roll@v4.1`，权益 `19.63068912U`，无持仓、普通挂单或条件单；两个容器重启计数均为 0。
- 新策略试运行许可证已签发：精确绑定 `extreme_v4_roll@v4.1`，24 小时有效，一级 `0.4x`，最多 3 次；当时处于强制冷却，状态为 `waiting_candidate`，未绕过冷却或主动下单。
- 部署后公共/私有 WebSocket 正常，REST 交易所限额使用率约 `7.67%`、无 429 冷却；Dashboard、API 及全部静态资源为 HTTP 200，近 8 分钟无 429/500/502、数据库锁、异常栈或错误日志。

# v0.10.1 V4.1 许可证冷却与存储维护（2026-07-17）

- 线上复现：V4.1 许可证已签发，但没有当前版本已平仓交易时，`pause_until` 每次检查都会按“当前时间 + 60 分钟”后移，造成永久冷却。
- 修复：冷却只锚定真实最近平仓时间；新版本无平仓样本时，许可证可直接等待合格候选。
- 新增回归：V4.1 在高点回撤 `risk_off`、无当前版本实盘样本时，状态为 `strategy_canary_1`、允许候选、风险倍率 `0.4x`，且不生成虚假 `pause_until`。
- VPS 存储审计：活动库约 2.14GB，其中约 538MB 为 SQLite 可回收页；另有两个约 2.14GB 的未压缩历史快照。两个容器合计内存约 287MB，系统可用内存约 1.2GB。
- 新增每日 systemd 存储维护：压缩非活动数据库快照、保留 30 天压缩归档并清理 7 天以上 Docker 悬空镜像/构建缓存；明确排除活动库，不自动执行长锁 `VACUUM`。
- 定向回归：`python -m pytest tests/test_performance_guard.py tests/test_strategy_canary.py -q`，16 passed。
- 完整后端：`python -m pytest -q`，233 passed。
- Python 编译：`python -m compileall -q app tests`，通过。
- 前端：`npm run build`，通过。
- Shell 语法：Git Bash `bash -n deploy.sh ops/bitdata-maintenance.sh`，通过。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。
- VPS 发布提交：`0d7c1e6`；原生 Bash 语法、Docker 构建和 systemd 定时器安装通过。
- 修复后许可证：`strategy_canary_1`、`allowed=true`、`0.4x`、`pause_until=null`，最多 3 次，未主动开仓。
- 首次维护：磁盘使用率 68% -> 55%，可用空间 9.3GB -> 14GB；旧快照保留为压缩文件，活动库未处理。
- 部署后：机器人运行中，权益约 19.6309U，无持仓或挂单；公共/私有 WebSocket 正常，REST 无冷却，Dashboard/OpenAPI/全部静态资源 HTTP 200，版本 `0.10.1`。

# v0.10.2 4G VPS 与 150 币实时行情池（2026-07-17）

- 资源假设：4G 内存足以扩大公共 WebSocket 覆盖；2 核 CPU 和 REST 预算不适合把 150 个币全部送入每轮 K 线回放、精排和盘口竞价。
- 后端：实时池流动性门槛与实盘发现门槛分离；150 币按每组 75 币拆成两组 K 线流和两组轻盘口流，全市场 ticker 只订阅一次。
- 前端：中文显示实时币数、连接数和订阅数，并在专家设置说明 4G VPS 的 120-150 币建议值。
- 安全边界：未修改 V4.1 评分、准入、仓位、杠杆、许可证、5U 硬停止或交易所保护单；REST 精排、深度检查和快车道并发预算保持不变。
- Binance 公共行情实测：150 个 U 本位永续币、4 条连接、451 个订阅均成功握手并收到首包；测试不使用账户 API，也不发送订单。
- 定向回归：`python -m pytest tests/test_market_stream.py -q`，12 passed。
- 完整后端：`python -m pytest -q`，235 passed。
- Python 编译：`python -m compileall -q app tests`，通过。
- 前端：`npm run build`，通过，生成版本化静态资源。
- Shell 语法：Git Bash `bash -n deploy.sh ops/bitdata-maintenance.sh`，通过。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。
- 本地 HTTP 冒烟：首页和 OpenAPI 返回 HTTP 200，OpenAPI 版本为 `0.10.2`，5 个版本化 JS/CSS 资源全部返回 HTTP 200。
- VPS 实施提交：`a7b95b2`；配置更新为 150 币、每连接 75 币、实时池最低 24h 成交额 500 万 U，REST 精排和交易风险参数未变。
- 线上实测：150 币、4 条连接、451 个订阅；150/150 个币的 1m/5m K 线新鲜，15 秒内盘口覆盖 149/150。
- VPS 性能：runner 六次 CPU 采样平均约 54%、峰值 120.7%（2 核），内存约 92-97MiB；系统可用内存约 3.0GiB、负载约 0.70、磁盘可用约 14GB。
- 部署后 REST 交易所额度使用约 5.2%、无冷却；Dashboard/OpenAPI/5 个静态资源全部 HTTP 200，版本 `0.10.2`，近端日志无 429/500/502、数据库锁或异常栈。
- 实盘安全：部署前后均为空仓，普通挂单和条件单均为 0；机器人运行，路由仍为 `extreme_v4_roll@v4.1`，许可证有效且使用次数仍为 0。

# v0.11.0 V4.2 自适应双通道准入与 V3 归档（2026-07-17）

- 策略：保留 V4 核心通道，新增只接收顺势动量或确认回踩的受限探索通道；默认排名前 25%、质量分 55、融合期望 0.04%、保守下界 -0.03%、成本比 1.60、动量确认至少 2/4、风险倍率 0.4x。
- 隔离：恐慌和逆势候选继续只做影子；当前 V4 执行链不再运行 V3 完整质量评分、校准或 V3.3 配对影子。
- 归档：V3 历史证据和回滚实现保留，当前 API、Dashboard 和紧凑遥测不再暴露 V3 实验；版本注册自动标记为 `archived/retired`。
- 架构：新增中性 `market-structure-v1` 适配层，V4.2 明确记录 `legacy_v3_quality_used_for_live=false`；点差和深度改用通用 execution 配置名，并兼容旧配置别名。
- API 与性能：未增加 Binance REST、订单或深度预算；V4.2 复用现有 WebSocket、K 线和中周期缓存，跳过旧 V3 实验可减少 CPU 与 SQLite 写入。
- 定向回归：V4.2、scanner、影子、版本归档和遥测共 `65 passed`。
- 完整后端：`python -m pytest -q`，`239 passed`；包含旧配置点差/深度无损迁移回归。
- 前端：`npm run build`，通过，生成版本化生产静态资源。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。
- 本地 HTTP 冒烟：首页与 5 个版本化 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.11.0`，旧 `/api/validation/v31` 已不再暴露。
- 发布提交：V4.2 主提交 `d59afb0`；中性影子命名与旧名去重修复最终提交 `1387b4a`，分支 `codex/two-stage-live-system`。
- VPS 部署前两次安全复核均为空仓、普通挂单 0、条件单 0；权益约 `19.63U`，机器人运行，5U 硬停止未触发。
- 配置与状态备份：`/opt/bitdata/backups/v0.11.0-20260717T134315Z`；仅迁移 V4.2、通用执行门槛和精确版本许可证字段，API 密钥、150 币实时池、阶段风险和杠杆未改。
- 部署后：路由 `extreme_v4_roll@v4.2`，S0 仍为单仓、10% 单笔风险上限、5x 杠杆上限；新版本许可证状态 `waiting_candidate`，`0.4x`、最多 3 次、已用 0 次，不处于强制冷却。
- 线上连接：150 币、4 条公共 WebSocket、451 个订阅；私有账户流已初始化并实时连接。REST 观察时交易所额度使用约 `40.96%`，无 429 冷却或错误。
- 线上静态资源：首页及 5 个版本化 JS/CSS 全部 HTTP 200，OpenAPI `0.11.0`；容器重启计数均为 0，近端日志无 429/500/502、数据库锁、异常栈或错误。
- V4.2 新影子实测使用中性 `breakout/prebreakout/pullback`，payload 包含 `market_structure` 且不再写旧 `market_state`；开放影子没有同 `opportunity_id + evidence_type` 重复组。
- 最终实盘安全：权益约 `19.6283U`，无持仓、普通挂单或条件单；机器人持续运行，许可证等待第一个符合 V4.2 条件的候选，不曾因部署主动开仓或平仓。

# v0.12.0 V4.3 事件证据与动态准入（2026-07-18）

- 假设：150 币实时监控已经覆盖足够的召回范围，V4.2 开仓少的主要瓶颈是重复影子快照、信号别名分裂、方向背景过度否决局部新形态，以及小账户固定盘口深度门槛，而不是继续无差别扩大币池。
- 事件证据：同币种、方向、标准化形态、市场状态和阶段在 30 分钟内按一个行情事件计数；价格移动一个风险距离或结构变化后可重新计入。
- 分层准入：局部同形态证据决定硬阻断，大方向历史只按 PF 将仓位降为 `0.85x / 0.70x / 0.55x`；小样本使用分层收缩，PF 999 不直接放行。
- 盘口：按最终通道风险估算订单名义价值，默认深度要求为 `max(750U, 订单 × 12.5)`、上限 `50000U`，订单不得超过可见盘口 `8%`，点差门槛保持不变；未新增 Binance REST 深度请求。
- 路由：保留核心与 `0.4x` 受限探索；V4.2 回放持续负期望的 `broad_down + SHORT` 暂只做 V4.3 影子，不用“顺势”标签掩盖负样本。
- 历史反事实：V4.2 事件去重后 `3727` 个事件、PF `0.665`、净结果 `-132.736`；V4.3 历史路由筛选得到 `92` 个事件、胜率 `55.4%`、PF `1.794`、净结果 `+9.517`、成本 `2.208`，前后时序两段 PF `1.374 / 1.842`。该结果是样本内策略筛选，只用于签发受限许可证，不视为样本外盈利证明。
- 定向回归：V4.3 信号别名、事件去重、结构重置、动态盘口、方向证据降仓和广泛下跌追空隔离测试通过。
- Python 编译：`python -m compileall -q app tests`，通过。
- 完整后端：`python -m pytest -q`，`243 passed`；保留一个本机 `requests` 依赖兼容警告。
- 前端：`npm run build`，通过，生成 `index-DlDVjN-l.js` 等版本化生产资源。
- 本地 HTTP 冒烟：首页、OpenAPI 和 5 个 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.12.0`。
- 发布提交与标签：`e332d53`、`v0.12.0`；分支 `codex/two-stage-live-system` 已推送至 GitHub。
- VPS 配置与状态备份：`/opt/bitdata/backups/v0.12.0-20260718T062321Z`；未复制 2.1GB 活动数据库，避免部署时额外占用磁盘，历史数据库原地保留且迁移仅新增索引。
- 部署切换前存在受保护的 `REUSDT` 空仓；安全门确认币安端同时存在 `STOP_MARKET` 与 `TAKE_PROFIT_MARKET` 后才允许切换。部署未手工开仓、平仓或撤销保护单。
- VPS 配置只写入 29 个 V4.3 专属版本、事件证据和动态盘口字段；5U 硬停止、S0 单笔风险上限 10%、5x 杠杆、单仓上限、API 密钥和 150 币实时池均保持不变。
- 部署后路由为 `extreme_v4_roll@v4.3`，机器人运行；权益约 `19.5755U`，`REUSDT` 空仓 `182` 张，止盈 `0.4011`、止损 `0.4069` 均为交易所端 `NEW` 状态，保护审计无缺失。
- 新版启动后约一分钟已记录 11 个独立 V4.3 影子事件；`idx_shadow_v4_episode` 索引存在，旧版本历史未清空或混入 V4.3 准入证据。
- 线上公共 WebSocket 为 4 条连接、451 个订阅，私有账户流已初始化并连接；REST 无 429 冷却，NTP 同步正常，维护定时器处于 active。
- Dashboard、OpenAPI 和 5 个版本化 JS/CSS 资源全部 HTTP 200，OpenAPI 版本 `0.12.0`；两个容器重启计数均为 0，近端日志无 429/500/502、数据库锁、异常栈或错误。

# v0.12.1 / V4.3.1 局部熔断与许可证恢复（2026-07-19）

- 修复核心试运行可能绕过融合局部证据的问题：许可证候选也必须满足扣费后融合净期望和保守下界。
- 新增按市场状态、方向、标准形态和入场阶段划分的局部熔断；同组合连续两笔实盘净亏损后转影子验证，不冻结其他组合。
- 全局保护拆分为软观察与硬冷却：默认分别为 `4 笔 / 8% / 12% / 20 分钟` 和 `5 笔 / 12% / 15% / 60 分钟`。
- 许可证两次亏损后不再永久停用；观察 60 分钟并获得至少 8 笔、3 个币种、净正收益、PF≥1.15 的新合格影子证据后，以 `0.30x / 2 次` 自动再签发。
- `shadow_only` 和诊断影子不再参与全局恢复或许可证签发；不同策略版本证据保持隔离。
- 后端完整回归：`python -m pytest -q`，`251 passed`，仅保留本机 `requests` 依赖兼容警告。
- 前端生产构建：`npm run build` 通过，生成新的版本化静态资源。
- 本地 HTTP 冒烟：首页、OpenAPI 与 5 个版本化 JS/CSS 资源全部返回 HTTP 200，OpenAPI 版本 `0.12.1`。
- `git diff --check` 通过，仅有 Windows LF/CRLF 转换提示。

## 2026-07-19 v0.12.2 跨进程状态锁与 V4.3.1 部署验收

- 首次 V4.3.1 部署提交 `b648f80`，随后在实盘验收中发现 Dashboard 与 runner 并发保存 `state.json` 时可能覆盖局部熔断持仓归因；交易所保护单始终完整，不属于裸仓故障。
- 新增 Linux `fcntl.flock` / Windows `msvcrt.locking` 跨进程排他锁，状态读取、合并、`fsync` 和原子替换全部在同一锁周期内完成；策略、仓位、杠杆、许可证和保护阈值未改。
- 并发回归：6 个独立 Python 进程各连续保存 20 次，最终所有进程字段均保留。
- Python 编译：`python -m compileall -q app tests`，通过。
- 完整后端：`python -m pytest -q`，`252 passed`；仅保留本地 `requests` 依赖兼容警告。
- 前端：在 `frontend/` 执行 `npm run build`，通过。
- 本地 HTTP：主页 200、OpenAPI `0.12.2`、5 个版本化 JS/CSS 资源全部 200。
- VPS 备份：`/opt/bitdata/backups/v0.12.1-20260718T191752Z` 与 `/opt/bitdata/backups/v0.12.2-20260718T193435Z`；API 密钥和用户配置原样保留。
- VPS 提交 `52fef20`，应用 `0.12.2`，策略 `extreme_v4_roll@v4.3.1`，机器人 `running`，S0 实盘模式，5U 硬停止保持不变。
- 部署验收时 `BANKUSDT` 多单 120 张；止盈 `0.11488`、止损 `0.10798` 均为 Binance 端 `NEW`，保护审计为完整。未手工开仓、平仓、撤单或重建正常保护单。
- 已从真实开仓日志回填局部组合 `quiet:LONG:pullback:RETEST`；多个并发扫描周期后字段仍保留，证明跨进程覆盖问题已修复。
- 公共/私有 WebSocket 正常，REST 无冷却或 429，时间偏差 `-96ms`；Dashboard/API/静态资源全部 200；近端日志无 429/500/502、数据库锁、异常栈或错误。

# v0.13.0 / V4.3.2 连续质量仓位与受保护追加（2026-07-19）

- 执行模型：核心通道不再使用 A+/A/B 硬分层决定仓位，改为连续置信度映射 `4%-7.5%` 初始风险；受限探索不放大，原有方向证据、局部熔断、许可证、阶段上限和全局安全保护继续向下约束。
- 受保护追加：仅在持仓顺向运行至少 `0.55 ATR`、交易所全仓止损已确认收紧到保本并覆盖成本后，允许同方向追加一次；初始与追加名义风险合计硬上限 `15%`。
- 订单安全：一向持仓使用 `quantity + reduceOnly` 桥接止损原子替换全仓保护；双向持仓保留旧全仓保护，不进行无法原子保障的替换。市场追加成交量无法确认时会锁定本次追加资格，禁止下一轮重复加仓。
- 发布金丝雀：精确绑定 `extreme_v4_roll@v4.3.2`，首 24 小时无条件执行 `0.70x`、最多 5 个机会、2 次净亏损撤销；旧恢复许可证不能绕过。部署时若已有本版本受保护持仓，会接管为第 1 个机会并在平仓前阻断新仓；早于许可证签发的持仓禁止追加。首日追加总风险上限同步缩放为 `10.5%`。窗口到期后首日封顶自动结束，不会永久阻断正常交易。
- 版本回退：从自身权益高点回撤 8% 时只关闭连续放大和追加，回到基础仓位，不触发全局 `risk_off` 或强制冷却。
- Binance 官方契约复核：USD-M `/fapi/v1/algoOrder` 的 `quantity` 不能与 `closePosition=true` 同传；`reduceOnly` 不能用于双向持仓，也不能与 `closePosition=true` 同传。实现按一向/双向模式分别处理。
- Python 编译：`python -m compileall -q app tests`，通过。
- 完整后端：`python -m pytest -q`，`268 passed`；新增首日正常态封顶、持仓接管、两亏撤销、窗口到期、恢复许可证不可旁路及首日追加缩放回归；仅保留本机 `requests` 依赖兼容警告。
- 前端：`npm run build`，通过，生成 `index-BR0H7wJ7.js` 等版本化生产资源。
- 配置模型：V4.3.2 精确版本及 15% 总风险硬上限校验通过。
- 本地 HTTP：首页、OpenAPI、状态接口和 5 个版本化 JS/CSS 资源全部 HTTP 200；OpenAPI 版本 `0.13.0`。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。
- 本机无可用 Bash/WSL，部署脚本将在 VPS 原生 Linux 环境执行 `bash -n` 复核。

## 2026-07-19 V4.3.2 首日许可证修复与 VPS 验收

- 状态机审计发现初版 V4.3.2 只在全局 `risk_off` 时应用试运行许可证，正常状态会显示 `not_required`，不符合“发布后首 24 小时始终封顶”的设计。修复后首日许可证优先于旧恢复许可证，并补齐持仓接管、平仓复位、两亏撤销、窗口到期和追加总风险缩放。
- 修复提交 `dbd101a`；完整后端 `268 passed`，前端生产构建、配置模型、OpenAPI `0.13.0` 和 5 个版本化静态资源本地 HTTP 冒烟全部通过。
- 部署前机器人在旧提交 `f81c703` 正常运行；先暂停 runner，再次查询 Binance 确认权益约 `21.9219U`、空仓、普通挂单 0、条件单 0，未主动平仓或撤单。配置与状态备份为 `/opt/bitdata/backups/v0.13.0-canary-20260719T122700Z`。
- VPS 原生执行 `bash -n deploy.sh`、`bash -n ops/bitdata-maintenance.sh` 和容器重建，最终提交为 `dbd101a`；Dashboard 与 runner 均运行，未重启、未 OOM。
- 首日许可证已自动签发：精确绑定 `extreme_v4_roll@v4.3.2`，状态 `waiting_candidate`，`0.70x`，已用 `0/5`，到期时间 `2026-07-20T12:27:15Z`；全局保护状态正常，机器人保持实盘运行。
- 版本权益回退锁此前已因 V4.3.2 自身高点回撤达到 8% 而触发。当前回撤虽已恢复到约 0%，锁仍按设计保留，因此连续仓位放大和追加暂时关闭；它不触发全局 `risk_off`，基础受限仓位和首日许可证仍可执行。
- 部署后权益约 `21.9221U`，空仓、普通挂单 0、条件单 0；公共 WebSocket 150 币、4 条连接、451 个流，私有 WebSocket 在线。REST 当前约 `139/2400`、无冷却或 429，交易所时间偏差 `-141ms`。
- Dashboard OpenAPI 与 5 个静态资源均为 HTTP 200；首页未携带 Basic Auth 返回 401，符合访问控制预期。近 10 分钟无 429/500/502、数据库锁、异常栈或错误日志。VPS 内存约 4GB，两个容器合计约 221MB，磁盘剩余约 14GB。

## 2026-07-20 V4.4 S0 全仓短打本地验收

- 新增 `s0_full_bet_v44` 独立 sizing：默认使用可用保证金 90%，按止损距离与 1.5 倍压力成本在 3x-10x 中动态选杠杆，单笔压力风险连续控制在 8%-15%。
- 全仓只对已通过 `full_bet_admitted` 的 V4.4 S0 候选生效；旧版本、非 S0、未准入候选不会误触发。
- 连续两次亏损后风险上限降至 8%；仍保留 5U 硬停止、单仓上限、交易所止盈止损、最小下单量、流动性、成本、限频和时间同步硬保护。
- 取消 V4.3.2 同方向追加，换仓要求新增优势覆盖平旧仓和开新仓的手续费、滑点，并至少达到 `0.35R`。
- 快速保护采用约 `0.85 ATR` 初始止损、`1.05R` 止盈、`0.45R` 保本触发和 6 根 K 线超时退出；实时失效退出保持 reduce-only。
- 完整设计与上线边界归档于 `docs/v0.14.0-v44-s0-full-bet-2026-07-20.md`。
- Python 编译：`python -m compileall -q app tests`，通过。
- 完整后端：`python -m pytest -q`，`273 passed`。
- 前端：`npm run build`，通过，生成 `index-Bm_8ODcQ.js` 等版本化生产资源。
- 本地 HTTP：首页、OpenAPI、状态接口及 5 个版本化 JS/CSS 资源全部 HTTP 200；OpenAPI 版本 `0.14.0`。
- `git diff --check`：通过，仅有 Windows LF/CRLF 转换提示。

## 2026-07-20 v0.14.1 / V4.4 证据恢复与漏斗对齐

- 根因复现：V4.4 使用 `full_bet` 准入通道，但恢复白名单只包含 V4.3 系列旧通道，导致当前版本合格决策影子被统计为 0。
- 证据修复：全局恢复、许可证再签发和局部熔断统一使用版本感知资格；V4.4 只接收 `decision + full_bet`，研究影子与旧版本继续隔离，并按 `opportunity_id` 去重。
- 漏斗修复：V4.4 粗排增加实时事件、横截面做多强势和横截面做空强势三条保留路线；没有增加 Binance REST 请求，也没有降低最终准入与安全硬门。
- 交互修复：已撤销许可证显示“等待再签发”；恢复卡片明确展示可计票影子类型和进度；机会中心版本文案改为动态版本。
- 策略版本保持 `v4.4`，应用版本升级为 `0.14.1`，现有 V4.4 精确版本证据不清零。
- 定向回归：证据资格、恢复状态机、许可证、V4 排序和扫描漏斗共 `72 passed`。
- 完整后端：`.venv/Scripts/python.exe -m pytest -q`，`280 passed`。
- 前端：`npm run build` 通过，生成 `index-DHJfINrJ.js` 等版本化生产资源。
- 本地 HTTP：主页、OpenAPI 和 5 个版本化 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.14.1`。
- 完整设计和边界归档于 `docs/v0.14.1-v44-evidence-recovery-2026-07-20.md`。

## 2026-07-20 v0.15.0 / V4.5 S0 连续准入

- V4.5 保留 V4.4 的候选排序、单仓全进全出、动态 `3x-10x` 杠杆、`8%-15%` 压力风险、快速止盈止损与手续费覆盖换仓。
- 普通净亏损不再撤销版本许可证或触发全局、方向、币种冷却；连续 1/2/3 次及以上亏损时仓位倍率为 `0.75x / 0.50x / 0.25x`，仍允许其他合格机会。
- 扣费后有效盈利恢复一级，强盈利或连续两次有效盈利恢复 `1.00x`。当日回撤 `10% / 20%` 时仓位上限为 `0.75x / 0.50x`，达到 `30%` 才暂停新开仓。
- 当前版本影子准入和复盘只统计单账户可执行的非重叠路径；同时出现但真实单仓无法执行的影子保留为研究数据，不控制开仓权限。
- 保留 5U 硬停止、当日回撤暂停、交易所止盈止损、裸仓补保护、最小下单量、流动性、API 限频和时间同步硬门。
- 连续准入不增加 Binance REST 请求，且仅在新成交或状态档位变化时写状态文件。
- Python 编译检查通过；完整后端测试 `287 passed`。
- 前端 `npm run build` 通过，生成 `index-CBOUCDgv.js` 等版本化生产资源。
- 本地 HTTP 冒烟：首页、OpenAPI 与 5 个版本化 JS/CSS 资源全部返回 HTTP 200，OpenAPI 版本为 `0.15.0`。
- 完整设计与部署边界归档于 `docs/v0.15.0-v45-s0-continuous-admission-2026-07-20.md`。

## 2026-07-20 v0.15.0 / V4.5 VPS 实盘部署验收

- 发布提交为 `58d6973`，VPS 工作区、Dashboard 与 runner 均已切换到应用版本 `0.15.0`、策略 `extreme_v4_roll@v4.5`。
- 部署前确认 Binance 实盘为空仓、普通订单 0、条件订单 0；配置、状态与数据库辅助文件备份至 `/opt/bitdata/backups/v0.15.0-20260720T101833Z`，未清理历史数据。
- 部署后通过正式控制接口启动机器人，机器人状态为 `running`；V4.5 连续准入 `allowed=true`，初始仓位倍率 `0.75x`，开仓权限已经恢复。普通亏损只降低后续仓位，不再撤销整张许可证。
- 当日权益基准保留为 `24.65922409U`，验收时真实权益约 `24.6513U`、当日回撤约 `0.027%`，明显低于 `30%` 暂停线；5U 硬停止保持不变。
- 最终安全复核仍为空仓、普通订单 0、条件订单 0、未实现盈亏 0；当前无需保护单。部署过程没有手工开仓、平仓或撤销保护单。
- 公共 WebSocket 已连接，覆盖 150 个币、4 条连接和 451 个流；私有账户流已连接并初始化。Binance REST 无 429 冷却，交易所时间偏差约 `-124ms`。
- Dashboard 首页、OpenAPI 与 5 个版本化 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.15.0`。Dashboard 与 runner 重启次数均为 0，未发生 OOM。
- 部署后日志未发现 429/500/502、数据库锁、异常栈或保护单缺失；系统维护定时器保持 active。

## 2026-07-21 v0.17.0 / V4.7 动态方向校准

- 移除 V4.7 执行路径中的固定做空 `0.65x` 与单边做空硬门，改为多空对称的顺势、中性和逆势校准。
- 新增 12/24 小时当前版本实盘与事件级影子证据统计；实盘权重为影子两倍，V4.6.2 仅以 `0.25` 权重冷启动。
- 动态仓位倍率限制为 `0.55x-1.15x`，单次证据变化不超过 `10%`；门槛微调至少需要 12 个当前版本结束样本。
- 校准只读本地 SQLite、最多读取 4000 行并缓存 300 秒，不新增 Binance REST 请求。
- 最终风险继续截断在配置上限和 `15%` 压力风险硬上限；5U 硬停止、保护单和流动性硬门未变。
- 新增校准单元测试，并保留 V4.6.2 历史回放行为。
- Python 编译检查通过；完整后端测试 `306 passed`。
- 前端 `npm run build` 通过，生成 `index-DnrDXwlN.js` 等版本化生产资源。
- 本地 HTTP 冒烟：首页、OpenAPI 与 5 个 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.17.0`。

## 2026-07-22 v0.18.0 / V4.8 质量、衰竭与结构化再入场

- V4.8 在本轮相对排名之外新增绝对质量、扣费后期望、保守期望与成本比硬门，弱市场中的相对第一名不再自动视为好机会。
- 新增趋势衰竭评分：扩张、冲量、逆向影线、量能衰退、中周期路径失败与聪明钱背离共同决定正常、谨慎降仓或禁止追价。
- 新增同币同方向结构化再入场：一次同结构亏损降仓，两次同结构亏损阻断；回踩、路径、量能与扩张共同确认新结构后恢复。
- 聪明钱仅在置信、中周期路径和防追价条件同时成立时做最多正负 2 分修正；每个候选同时记录去掉聪明钱和 V4.7 旧门槛的同机会反事实。
- V4.7 仅作为有上限的启动先验；V4.8 实盘、影子、局部证据、许可证和自适应证据按版本隔离。
- 不新增 Binance REST 请求；新判断只使用扫描链路已有字段和缓存的本地 SQLite 证据。
- 5U 硬停止、交易所端止盈止损、15% 压力风险上限、最小下单量、流动性硬门、API 限频和时间同步保持不变。
- Python 编译检查通过；完整后端测试 `309 passed`。
- 前端 `npm run build` 通过，生成 `index-CQYgJrWP.js` 等版本化生产资源。
- 本地 HTTP 冒烟：首页、OpenAPI 与 5 个 JS/CSS 资源全部返回 HTTP 200；OpenAPI 版本为 `0.18.0`。
## v0.18.0 / V4.8 VPS deployment verification (2026-07-22)

- Deployed commit `825906f` to `/opt/bitdata`; pre-deployment backup stored at `/opt/bitdata/backups/v0.18.0-20260722T053201Z`.
- Dashboard and runner containers rebuilt successfully. Dashboard root, V4.8 JavaScript asset, and OpenAPI each returned HTTP 200.
- Runtime route verified as `extreme_v4_roll@v4.8`; the live `DEXEUSDT` position was tracked with `s0_full_bet_v48` and remained protected by one exchange stop-loss and one take-profit order.
- Public and private WebSocket streams were connected; REST cooldown was inactive. No recent 429/500/502, database-lock, traceback, or container restart errors were found.
- Post-deployment account check: equity about `28.67 U`, one protected position, zero normal open orders, and two conditional protection orders. The robot remained running.

## 2026-07-27 v0.25.0 / V4.7.4 本地验证

- 修复候选保护档案被信号层半成品字典整体覆盖的问题；新增字段级合并和来源留档测试。
- 持仓监督默认每 5 秒运行，测试确认优先使用 WebSocket 最新价和入场 ATR，二者有效时不调用 REST 价格与 K 线接口。
- 新增 S0 当日净利润锁测试：达到 40% 目标且有持仓时只等待空仓，空仓后锁定新开仓，S1 及以上不受影响。
- 测试确认关闭 S0 普通日亏损停牌后，5U 硬停止和当日净利润锁仍可独立阻断新仓。
- 行情事件与真实执行增加独立编号，测试确认同一行情可关联研究样本、每次 Binance 成交仍保持唯一执行身份。
- MoE v1.4 使用约 214 万条、119 个币的 Binance 候选训练。未触碰测试 115 笔、PF 1.0949、净收益 +7.449 个百分点；0.16% 成本压力 PF 1.0353，只覆盖一个市场状态，因此判定为 `research_only_not_eligible`。
- Python 编译检查通过；完整后端测试 `334 passed`。
- 前端 `npm run build` 通过，生成 `index-BmeJuspv.js` 与版本化 CSS/依赖资源。
- 本地 HTTP 冒烟：首页返回 HTTP 200；桌面与 390px 手机视口均正常，手机页面无横向溢出，浏览器控制台无错误。

## 2026-07-27 v0.25.2 / VPS 全历史回放与 MoE v1.5 研究影子

- 审计 VPS 全部 `793` 笔实盘、`66,507` 笔已结束决策影子和 `36,142` 条机会血缘；实盘历史毛盈亏 `+27.0560U`，手续费 `67.5360U`，资金费 `-3.2814U`，净盈亏 `-43.7614U`。
- 下载 `238` 个 Binance USD-M 币种、`1,672,902` 根真实 1 分钟 K 线；回放从信号后下一根完整 K 线开始，同分钟止盈止损冲突按止损优先，并计入历史手续费和滑点。
- 比较 `48` 组止损、盈亏比和最长持仓组合，共回放 `31,383` 个独立候选；没有组合同时在固定验证段和最终测试段扣费后为正，因此不改 V4.7.4 实盘参数。
- MoE v1.5 使用 `720,000` 条公开 Binance 数据（权重 `0.12`）、`60,129` 条 VPS 影子（权重 `1.0`）和 `107` 条可追溯实盘（权重 `6.0`）本地训练。
- MoE 验证段 `212` 次单仓计划机会，PF `0.7757`、净 `-19.5201` 个百分点；最终测试 `126` 次，PF `0.8346`、净 `-9.1658` 个百分点。候选未通过发布门槛，只允许线上研究影子，不参与实盘准入、仓位或退出。
- 新增运行时特征测试，确认聪明钱、流动性、点差与盘口深度进入 MoE 推理；模型输出始终带有 `affects_live_admission=false`。
- Python 编译检查通过；完整后端测试 `337 passed`。
- 前端 `npm run build` 通过，生成 `index-BB_WEk20.js` 与版本化 CSS/依赖资源。
- 本地 HTTP 冒烟：首页与 `5` 个版本化 JS/CSS 资源全部返回 HTTP 200。
- 完整数据口径、时间切分、回放结果和发布边界归档于 `docs/v0.25.2-vps-history-replay-hybrid-moe-2026-07-27.md`。
- VPS 已部署提交 `4971b75`；部署前后均为空仓、普通订单 `0`、条件订单 `0`，机器人保持运行，S0 仍路由至 `extreme_v4_roll@v4.7.4`。
- MoE v1.5 线上模型加载成功，首个完整扫描周期写入 `36` 条评估影子和 `4` 条选中影子，全部明确 `affects_live_admission=false`。
- 部署后 Dashboard、公共/私有 WebSocket、REST 频控正常，未发现 429/500/502、数据库锁、推理异常或容器重启。

## 2026-08-03 v0.30.6 / BNB 持仓频率挑战影子

- 使用 2020-2023 年训练段比较 BNB 1/2/3/5 日持有和 5/7.5/10/15% 固定止损，冻结选择结果仍为 `5日 + 10%止损`；2024-2026 年只作一次性样本外审计。
- 当前规则样本外 40 笔、收益 `+163.41%`、PF `2.14`、最大回撤 `-17.98%`；双倍成本压力下收益 `+137.25%`、PF `1.95`。
- 新增 `3日 + 15%止损`独立挑战影子。它复用当前日线和 WebSocket 数据，不增加 Binance REST 请求，不参与实盘准入、仓位或退出。
- 修正五日影子每日错误上移止损的问题，使影子、实盘和冻结回测均使用入场固定止损与时间退出。
- 定向测试 `21 passed`；完整后端测试 `662 passed`，仅有 5 条既有 sklearn 收敛警告。
- 前端 `npm run build` 通过，Dashboard 已改为显示当前 BNB V4 五日实盘同版影子和三日高频挑战影子。
- 补充同日重启回填：若五日候选已经评估但三日挑战影子尚未创建，重启后立即补建，不再等待下一 UTC 日；定向测试 `22 passed`，完整后端测试 `664 passed`。
# 2026-08-03 BNB 固定门槛小时级动态可重入审计

- 新增 `scripts/audit_s0_bnb_fixed_gate_hourly.py`，按线上固定市场门槛逐小时回放，
  支持提前止损后下一日信号重新入场，并纳入 BNB 数量步长、最低名义价值、
  15.153U 小账户、15% 风险、2 倍杠杆、5U 硬停止及 0.5U 预留。
- 分离 2020-2022 开发、2023 验证和 2024+ 样本外，测试 0/1/2/4 小时执行延迟
  及基础、1.5 倍、2 倍成本。
- 固定门槛候选均未通过严格开发期稳定性要求，因此维持线上 10.65%。
- 同一固定门槛下重新冻结持有期/止损选择：仅使用 2020-2022 选择出五日/15% 止损；
  2023 验证为 +44.55%、PF 2.11、回撤 -9.35%；2024+ 双倍成本样本外为
  +117.66%、PF 1.99、回撤 -24.47%，且 1/2/4 小时延迟与三档成本压力均通过。
- 发布时账户风险预算仍为 15%，更宽止损会由仓位算法换成更小名义仓位，不提高计划账户风险。
- 同口径复核三日挑战者：样本更多、回撤更低，但历史增长低于五日/15% 主线，继续仅作影子。
- 修正 `benchmark_s0_bnb_holding_frequency.py`：此前报告文字写固定 10.65%，实际使用
  动态历史分位；现改为真正固定门槛，并将 2023 从选择期拆为独立验证期。
- 新增 4 个合成测试，覆盖固定快慢趋势门、止损后次日重入、小时延迟和小账户执行约束。
- V5 定向回归 `45 passed`；项目 `.venv` 完整后端回归 `669 passed`，仅有 5 条既有
  ElasticNet 收敛警告。
- 前端 `npm run build` 通过；本地 FastAPI `0.30.7` 首页、文档及 5 个哈希静态资源
  逐项请求均返回 HTTP 200。

## 2026-08-03 BNB 固定门槛做空候选拒绝

- 扩展小时级动态重入审计，使同一执行器支持多头和空头路径；空头止损使用 K 线最高价，
  跳空按开盘价成交，收益方向按做空反转。
- 新增 `scripts/audit_s0_bnb_fixed_gate_short.py`，只用 2020-2022 选择参数，2023
  独立验证，2024 年至今作样本外，并纳入小账户下单限制、三档成本和四档执行延迟。
- 开发期选出的 `7.5% 门槛 + 5 天持有 + 15% 止损` 收益 -60.92%、PF 0.38；
  2023 验证收益 -12.68%、PF 0.61；2024 年至今双倍成本收益 -28.92%、PF 0.70。
- 做空候选未通过开发、验证、样本外、年度稳定性和执行延迟要求，因此不进入影子或实盘。
- 完整数据口径和拒绝理由见
  `docs/v0.30.7-bnb-fixed-gate-short-rejection-2026-08-03.md`。

## 2026-08-03 v0.31.0 / S0 30日山寨动量主路线

- 全市场重新审计 24小时、30日及不同评估频率。24小时最强/最弱和 12/6 小时高频变体均因开发期或样本外负期望被拒绝。
- 冻结 `30日形成期 + 日频 + 3ATR止损 + 3R止盈 + 7日最长持仓`，使用上市45天、24小时成交额2000万U、至少60币、BTC与山寨广度同向及滚动方向门。
- 新增 `scripts/audit_s0_altcoin_30d_live_candidate.py`。0.60% 往返压力成本下，96笔、46币、PF 2.06，2021-2026 各年净点数均为正。
- 15%风险按年从15U重启均未触发5U；30%风险在单年路径触发5U，因此实盘冻结15%，不使用目标进度放大风险。
- 新增 `s0_xmom_30d_live_v1`：山寨动量为S0主路线，BNB只作无山寨候选时后备；多空方向分别用当前版本影子滚动更新。
- 部署不会盲目关闭现有受保护仓位；只有新鲜山寨候选真实可执行时才换仓。第一笔新策略成功开仓并挂好保护单后才重置S0进度。
- 30日动量仓位明确标记为日线级策略，旧的快速失效、日内保本和追踪止损不会提前干预；退出仅由交易所止损/止盈或7日最长持仓管理。
- 保留S0单日止盈锁、5U硬停止、交易所保护单、合约最小量、时间同步和API限频。
- Dashboard 改为中文显示“30日山寨动量主路线”和“BNB趋势后备”，并展示扫描、方向门和风险配置。
- 项目 `.venv` 完整后端回归 `684 passed`，仅有5条既有 ElasticNet 收敛警告；前端生产构建通过（452个模块）。
- 本地 FastAPI `0.31.0` 首页、OpenAPI 及5个生产哈希静态资源逐项请求均返回 HTTP 200。
- 部署验收后补充 `0.31.1` 展示修正：每日候选窗口外仍明确标识“30日山寨动量主路线已启用”，S0 阶段卡片不再误显示旧 V5 路由与30%风险。

## 2026-08-04 30日动量“更快退出”跨年审计拒绝

- 新增 `scripts/audit_s0_30d_fast_exit_cross_year.py`：保留30日截面选币，改用 2ATR止损/1.5R止盈/最长72小时的快速退出，按 0/5/15 分钟延迟和 0.36%/0.60% 成本重放 2021-2026 真实1分钟K线。
- 多数场景 PF 低于1.2、去掉头部3币后转负；仅 15分钟/0.36% 单场景表面为正，但 0.60% 成本压力下消失，且 2021 年大幅为负。
- 决策 `reject_fast_exit_not_cross_year_robust`：实盘继续使用冻结的 3ATR止损/3R止盈/最长7天。
- 定向测试 `2 passed`；完整留档见 `docs/s0-30d-fast-exit-rejection-2026-08-04.md`。

## 2026-08-04 7日动量 + 市场宽度带跨年审计拒绝

- 新增 `scripts/audit_s0_7d_breadth_cross_year.py`：7日形成、持有1日、2ATR止损/2R止盈，叠加 1.5%-7.5% 绝对市场宽度带，2021-2025 独立审计、2026 仅作上下文。
- 0.60% 压力成本下独立期整体 PF 0.878、净收益 -172.18 点数、bootstrap 正概率 0.175；去掉头部3币后 PF 0.727，2022-2025 逐年为负。
- 决策 `reject_before_minute_replay`：不进入分钟级重放，也不参与实盘准入。
- 定向测试 `2 passed`；完整留档见 `docs/s0-7d-breadth-rejection-2026-08-04.md`。

## 2026-08-04 v0.31.1 S0 30日山寨动量主路线上线检查点

- 2026-08-04 UTC 00:02 首笔 `s0_xmom_30d_live_v1` 实盘开仓：`AKEUSDT LONG`，初始计划风险约 15%、2倍杠杆，交易所 `STOP_MARKET` 止损与 `TAKE_PROFIT_MARKET` 止盈均已挂出，保护档案 `adaptive_30d_daily_v1`（2.5ATR止损/6.25ATR止盈/最长5天）。
- Runner 保持 `running`，最新决策 `WAIT / adaptive_30d_position_already_open`，不会重复开仓；`adaptive_30d_live_effect=s0_primary`。
- 账户快照：总钱包约 `15.13U`、保证金余额约 `14.41U`、未实现盈亏约 `-0.73U`；目标起点 `15.137U`。
- Dashboard 显示“30日山寨动量主路线 / BNB趋势后备”，与实盘策略一致。
- README 同步为 `v0.31.1` 并新增“S0 当前主路线：30日山寨动量”章节，替换旧 28/56 市场趋势 V3 描述。
- 完整检查点留档见 `docs/v0.31.1-s0-altcoin-30d-live-checkpoint-2026-08-04.md`。

## 2026-08-04 v0.31.2 / 30日山寨动量风险预算 20% 升级

- 在冻结的 96 笔、0.60% 压力成本数据上重新做风险敏感性：15% 终值 632.58U，20% 终值 1128.76U，22% 终值 1312.61U，25%/30% 在 2026 年按年重启触发 5U 硬停止。
- 20% 在 2021-2026 每年从 15U 重启均未触发 5U，因此把 `adaptive_30d_live_risk_pct` 默认值从 15% 提升到 20%；22% 因安全边际变薄不采用，25%/30% 明确拒绝。
- BNB 后备 `market_tsmom_live_risk_pct` 同步为 20%（v0.30.8 已审计；VPS 配置此前残留 15%）。
- 更新 `scripts/audit_s0_altcoin_30d_live_candidate.py` 决策逻辑（risk20_promoted / risk25_rejected）与定向测试，前端文案、README 和默认配置同步。
- 完整留档见 `docs/v0.31.2-s0-altcoin-30d-risk20-promotion-2026-08-04.md`。

## 2026-08-04 v0.31.3 / 30日山寨动量阶梯风险（20% → 22%）

- 新增权益阶梯：低于 30U 用 20%，达到 30U 自动升到 22%；全序列终值 1256.75U（平 20% 为 1128.76U），按年重启均未触发 5U。
- 22% 在 2 倍杠杆、90% 保证金下可执行（12.5% 止损约需 1.76 倍名义仓位）；25% 超出保证金上限且触发硬停止，继续拒绝。
- 新增 `adaptive_30d_live_risk_tier_enabled / risk_tier_equity / risk_tier2_pct` 配置，`build_adaptive_30d_live_decision` 按当前权益选择风险档，前端显示当前生效档并可在专家设置调整。
- 新增 `scripts/audit_s0_altcoin_30d_risk_tier.py` 与定向测试；README、前端文案和 TEST_RECORD 同步。
- 完整留档见 `docs/v0.31.3-s0-altcoin-30d-risk-tier-2026-08-04.md`。

## 2026-08-04 30日山寨动量双仓并发审计拒绝

- 重建每日 top-2 候选（612 条信号、608 条路径）后对比：单仓 20% 为 89 笔、PF 1.689、终值 152.00U；双仓各 20% 在 2024 年按年重启触发 5U 硬停止；双仓各 10%（总敞口等于单仓 20%）终值仅 66.11U、PF 1.370。
- 第二名候选显著稀释组合质量，决策 `reject_two_position_not_robust`，实盘继续单仓。
- 新增 `scripts/audit_s0_altcoin_30d_two_position.py` 与定向测试；完整留档见 `docs/s0-altcoin-30d-two-position-rejection-2026-08-04.md`。

## 2026-08-04 v0.31.4 / 30日山寨动量止盈 3.5R 升级

- 退出档案审计（top-1 冻结信号、0.60% 压力成本、20% 风险、15U 逐年重启）：2.5R 终值 43.46U、PF 1.42；3.5R 终值 177.18U、PF 1.80，逐年全部为正且无 5U 硬停止；4R 终值 189.48U 但 2021 年仅剩 15.07U，安全边际薄，不采用。
- 分批止盈/保本后追/1R 追踪止损变体 PF 0.96-1.00 且部分触发硬停止，全部拒绝；策略利润来自大赢家，提前分仓伤害收益。
- 新增实盘独立参数 `adaptive_30d_live_stop_atr=2.5`、`adaptive_30d_live_reward_r=3.5`、`adaptive_30d_live_max_stop_pct=12`，仅影响新入场保护单；现有 AKE 持仓保护单不改。
- 新增 `scripts/benchmark_s0_altcoin_30d_exit_profiles.py` 与实盘 3.5R 定向测试；前端、README、默认配置同步。
- 完整留档见 `docs/v0.31.4-s0-altcoin-30d-tp35-promotion-2026-08-04.md`。

## 2026-08-04 30日山寨动量止盈/止损/持仓天数敏感性网格

- 扩展 `scripts/benchmark_s0_altcoin_30d_exit_profiles.py`：在 3.5R 周围测试 2.0/2.5/3.0 ATR 止损和 5/7 天持仓共 8 个组合。
- 上线组合（3.5R / 2.5 ATR / 5 天）终值 177.18U、PF 1.80，网格中最高；3.0 ATR 止损 5 天触发硬停止，7 天持仓各组合全部更低。
- 结论：当前实盘参数已是该网格最优点，不继续调参；敏感性结果并入 v0.31.4 留档。

## 2026-08-04 v0.31.5 / 当日净利润锁目标对齐回测口径

- 历史审计从未模拟当日净利润锁；40% 锁会在单日大赢约 +70% 后阻断 BNB 后备复利，使实盘系统性低于审计口径。
- 把 `stage_s0_daily_profit_target_pct` 默认值从 40 提高到 200：单日极端连赢超过 +200% 才锁定新仓，5U 硬停止、交易所保护单和已有持仓不受影响。
- 完整留档见 `docs/v0.31.5-s0-daily-profit-lock-align-2026-08-04.md`。

## 2026-08-04 30日山寨动量第三档风险（25%）审计拒绝

- 在 20%/22% 两档阶梯上测试“权益 ≥100U 升 25%”：全序列终值 1150.49U，反而低于两档的 1256.75U；按年重启中 25% 档几乎不触发（2026 年仅到 90.62U）。
- 25% 档需要 3 倍杠杆才可执行，但历史全序列没有带来收益提升，且 25% 平铺在 2026 年按年重启触发 5U；结论：不加第三档，维持 20%→22% 两档阶梯。

## 2026-08-04 30日山寨动量“当日止损后重入”审计拒绝

- 测试 24 小时内止损后按下一根小时 K 线重入同一币种同方向：基线 87 笔、PF 1.75、终值 148.07U；重入后 112 笔、PF 1.52、终值 74.41U。
- 118 个重入候选自身 PF 仅 1.052，基本没有正期望；决策 `reject`，实盘继续一天一信号、不追重。
- 新增 `scripts/benchmark_s0_altcoin_30d_reentry.py`；完整留档见 `docs/s0-altcoin-30d-same-day-reentry-rejection-2026-08-04.md`。

## 2026-08-04 BNB 后备加止盈审计拒绝

- 在 15% 止损/5 天持有路径上测试 1R/1.5R/2R/3R 止盈：无 TP 样本外 PF 1.988、20% 风险终值 44.06U；1R/1.5R 明显更差，2R 略低，3R 与无 TP 相同。
- BNB 趋势骑乘同样依赖让赢家跑完持有窗口，结论：继续“15% 止损 + 5 天持有、无止盈”。
- 新增 `scripts/benchmark_s0_bnb_take_profit.py`；完整留档见 `docs/s0-bnb-take-profit-rejection-2026-08-04.md`。

## 2026-08-04 v0.31.6 / 空头方向门对齐冻结审计

- 冻结审计本来包含 48 笔空头（PF 1.49、逐年为正），但旧 8 笔种子空头 PF 约 1.14，把空头长期挡在门外；实盘只运行了审计策略的一半。
- 把冻结全样本（多空各 48 笔）写入 `HISTORICAL_DIRECTION_SEED`，方向门改为：影子证据不足 8 笔时用冻结全样本放行（多空都交易），达到 8 笔后用最近 8 笔影子滚动接管。
- 空头沿用同一套风控（20%/22% 阶梯、2.5 ATR 止损、3.5R 止盈、5 天、5U 硬停止）；历史 96 笔压力测试已覆盖空头，按年重启无 5U 触发。
- 完整留档见 `docs/v0.31.6-s0-altcoin-30d-short-gate-align-2026-08-04.md`。

## 2026-08-04 S0 当前实盘配置历史等价基线

- 新增 `scripts/audit_s0_altcoin_30d_live_config.py`，用当前线上参数（3.5R、2.5 ATR、5 天、多空、20%/22% 阶梯、0.60% 成本）重放 2021-2026。
- 结果：85 笔（多头 45 / 空头 40）、PF 1.802、净 +338.85 点、阶梯终值 177.51U，逐年为正且无 5U 硬停止。
- 该基线用于后续实盘对照；若实盘明显差于 PF 1.80 路径，优先查执行缺口而不是改参数。
- 完整留档见 `docs/s0-live-config-baseline-2026-08-04.md`。

## 2026-08-04 实盘 vs 基线追踪脚本

- 新增 `scripts/track_s0_live_vs_baseline.py`：读取 VPS SQLite，输出 30 日动量影子闭合统计（多空分开）、最新权益/未实现盈亏和 85 笔 PF 1.80 基线参照。
- 首次运行：权益 14.39U、AKE 持仓未实现 -0.74U、影子仅 1 笔闭合亏损；继续积累样本后再做实盘/基线对照。

## 2026-08-04 S0 实盘运行手册

- 新增 `docs/S0_LIVE_OPERATIONS.md`：固化当前配置、健康检查清单、基线参照、触发条件与已否决候选清单，供后续每轮直接复用。
- 首次体检：AKE 距止损约 9%、标记价 0.0042656、清算价 0.00093、两张 reduce-only 保护单完好；权益 14.44U。

## 2026-08-04 AKE 入场执行质量核对

- 信号参考价 0.0044378，实际成交 0.0044365，滑点约 -0.03%，远好于基线 0.60% 单边成本假设；执行质量正常，基线对照前提成立。
