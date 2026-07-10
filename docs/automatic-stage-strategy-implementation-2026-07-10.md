# 自动阶段策略实施留档

日期：2026-07-10

版本：`v0.3.0`，分支：`codex/two-stage-live-system`

## 1. 本次交付

本次实现把策略选择从旧的单一 `growth_mode` 升级为账户权益驱动的自动阶段路线：

| 阶段 | 权益 | 实际主策略 | 单笔账户风险硬上限 | 最大持仓 |
| --- | ---: | --- | ---: | ---: |
| S0 | 0-300U | Extreme V2 单仓滚仓 | 10% | 1 |
| S1 | 300-10000U | Extreme V2 增强滚仓 | 7% | 1 |
| S2 | 10000-100000U | Extreme V2 组合滚仓 | 3% | 2 |
| S3 | 100000-1000000U | 盘口剥头皮 | 0.35% | 4 |
| S4 | 1000000U 以上 | 网格加低风险盘口剥头皮 | 网格 0.20%，剥头皮 0.10% | 8 |

风险百分比表示止损触发时的理论账户损失上限，不表示收益目标。阶段风险帽在旧策略仓位下限、信用倍率、信号倍率和权益保护之后再次执行，任何旧参数都不能突破阶段上限。

## 2. 核心实现

- `app/stage_modes.py`：权益区间、滞回、连续确认、手动覆盖、到期恢复和持仓交接。
- `app/trading_engine.py`、`app/position_sizing.py`：阶段参数进入最大持仓、保证金、杠杆、当日亏损和最终风险硬帽。
- `app/live_learning.py`：Extreme V2、盘口剥头皮、网格按策略族独立评分；旧混合信用只展示。
- `app/binance_rate.py`：跨进程统一 REQUEST_WEIGHT、ORDERS_10S、ORDERS_1M 预算与优先级。
- `app/user_stream.py`：私有余额、仓位、订单和算法单事件；REST 快照负责不可由事件准确推导的可用余额。
- `app/order_book.py`、`app/market_stream.py`：S3/S4 精选币本地 L2、更新编号校验、断档重建、微价格和主动成交方向。
- `app/runner.py`：公共行情流、私有用户流、后台漏斗与实时快车道协同；S4 网格与剥头皮叠加执行。
- `frontend/src/main.tsx`：中文阶段路线、实际风险、数据流、REST 额度和策略族学习面板。

## 3. 性能与频控

- 全市场召回优先使用 `!ticker@arr`，不重复请求全市场 24h ticker。
- S0-S2 只维护轻量 depth5；完整 L2、book ticker 和 aggregate trade 只在 S3/S4 启用。按 Binance 当前流分类，深度与最优价使用 `public` 连接，聚合成交使用独立 `market` 连接，盘口快照只初始化一次。
- 默认最多 80 个实时热币、20 个完整订单簿，订单簿快照并发限制为 4。
- 行情先写进程内存，默认每 5 秒持久化一次；事件队列合并同币连续事件。
- 后台、普通、实时和关键请求分别使用交易所额度的保守分层预算，保护单和平仓优先。

## 4. 实盘安全不变量

- 已有持仓时不跨策略切换；空仓后才应用新执行器。
- 每次开仓仍必须创建服务器端止损和止盈，运行时审计负责缺失补单。
- 私有流不持久化 listen key，也不使用事件中的跨钱包余额冒充可用余额。
- 深度更新编号断档时本轮盘口作废并重建，不使用不连续订单簿。
- 网格币种不会同时进入 S4 剥头皮叠加执行器。
- 自动阶段不绕过实盘确认短语、IP 白名单、最小下单量、费用后收益和换仓成本。

## 5. 部署参数

生产默认使用：

```text
stage_routing_enabled=true
stage_manual_mode=auto
stage_switch_up_buffer_pct=5
stage_switch_down_buffer_pct=10
stage_switch_confirmations=3
strategy_family_credit_enabled=true
market_stream_rebuild_seconds=300
user_stream_enabled=true
orderbook_full_stream_enabled=true
```

部署前必须查询真实持仓和保护单；部署后必须确认当前权益映射到正确阶段、私有流或 REST 兜底正常、机器人状态为运行中，并再次核对所有持仓的止盈止损。

## 6. 风险说明

30U 到 100000U 的滚仓目标具有极高的本金归零概率，自动阶段和风险控制不能把它变成可预期收益。系统的目标是让每次决策、费用、保护状态和策略学习可审计，并在资金规模增长后自动降低风险，而不是保证达到目标。
