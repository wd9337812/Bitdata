# 极限剥头皮单引擎与独立信用分

日期：2026-07-09

## 背景

极限模式之前同时存在盘口剥头皮、火药桶小仓试探、抢跑、弱质量试探等多条入场路径。这样会让复盘变得混乱：前端显示像是剥头皮，但真实成交可能来自旧滚仓逻辑；旧策略亏损后的信用惩罚也可能压制盘口剥头皮引擎。

本次迭代把 `yolo_scalp` 明确为单一盘口剥头皮引擎：

- 只允许 `orderbook_impact`、`volume_scalp`、`imbalance_probe` 三类盘口剥头皮信号执行。
- 旧极限 V2 的火药桶、抢跑、弱质量试探在 `yolo_scalp_orderbook_only_enabled=true` 时只保留观察，不允许开仓。
- 交易执行层增加二次保险，即使扫描层误传旧 entry type，也会返回 `WAIT / yolo_orderbook_only`。

## 独立信用模型

新增策略族信用表 `symbol_strategy_live_scores`，主键为：

- `symbol`
- `direction`
- `strategy_family`

盘口剥头皮使用 `orderbook_scalp` 策略族。旧实盘信用 `symbol_live_scores` 继续保留，用于复盘旧混合策略表现，但不再影响盘口剥头皮仓位。

归因方式：

- 不增加 Binance API 请求。
- 使用本地 `live_trade_records` 和 `strategy_runs` 做时间窗口匹配。
- 通过 `strategy_runs.payload.decision.entry_type` 判断是否属于盘口剥头皮。

## 剥头皮专用评分参数

盘口剥头皮是快进快出的策略，所以独立信用模型使用更短的节奏：

- `scalp_credit_quick_stop_seconds=35`
- `scalp_credit_win_reward=5.0`
- `scalp_credit_loss_penalty=7.5`
- `scalp_credit_consecutive_loss_penalty=12.0`
- `scalp_credit_fee_drag_penalty=4.5`
- `scalp_credit_penalty_cooldown_hours=1.0`
- `scalp_credit_recovery_interval_hours=3.0`
- `scalp_credit_recovery_points=4.0`

这些参数只作用于 `orderbook_scalp`，不会改变旧滚仓策略的信用分。

## 前端变化

实盘学习页拆成两块：

- 盘口剥头皮专用信用分：极限剥头皮模式实际读取的仓位倍率来源。
- 旧策略混合信用分：只用于复盘旧策略，不参与剥头皮仓位。

配置中心新增：

- 极限只跑盘口剥头皮
- 剥头皮独立信用分
- 剥头皮快速止损秒数
- 剥头皮盈利奖励
- 剥头皮亏损惩罚
- 剥头皮冷却小时

## 性能和频控

本次迭代没有增加 REST 行情请求：

- 入场仍复用现有 WebSocket 行情、盘口深度和漏斗扫描。
- 信用归因完全读取本地 SQLite 历史表。
- Dashboard 新增字段只来自本地接口 `/api/live-learning`。

## 验证

- 后端测试覆盖：
  - yolo 剥头皮信用不再被旧信用软折扣。
  - 交易层拒绝非盘口剥头皮 entry type。
  - 扫描器与交易引擎关键路径不回归。
- 前端构建通过。

