# 极限梭哈盘口剥头皮引擎

日期：2026-07-09

## 背景

原来的 `yolo_scalp` 更像 5m 突破策略的激进版本：通过 K 线、回测表现、币种质量、火药桶和实盘信用分筛选机会。它安全边界较多，但在极限冲刺阶段会出现长时间无仓，和“高频剥头皮、快进快出”的目标不完全一致。

本次迭代把 `yolo_scalp` 的最极限入口改为盘口剥头皮优先，普通突破、抢跑、火药桶仍保留作为背景信号和备用入口。

## 新增模块

- `app/scalp_engine.py`
  - 读取 WebSocket `depth5`、`ticker`、`1m kline` 和机会事件。
  - 计算点差、盘口深度、买卖盘失衡、1m 异动、扣费后净空间。
  - 产出三类新信号：
    - `orderbook_impact`：盘口冲击
    - `volume_scalp`：放量剥头皮
    - `imbalance_probe`：失衡试探

## 入场条件

盘口剥头皮信号必须同时满足：

- 点差不超过 `yolo_scalp_orderbook_max_spread_pct`
- depth5 深度不低于 `yolo_scalp_orderbook_min_depth_notional_usdt`
- 同向盘口失衡不低于 `yolo_scalp_orderbook_min_imbalance`
- WebSocket 事件或 1m K 线出现实时异动
- 预期利润覆盖手续费、滑点和最低净利
- 不处于流动性陷阱或插针状态
- 仍会经过实盘信用分、实时反应风控、最小下单量和有效订单检查

## 仓位

`position_sizing` 新增盘口剥头皮分层：

- 盘口冲击：默认 45%-85% 风险区间
- 放量剥头皮：默认 35%-75% 风险区间
- 失衡试探：默认 18%-40% 风险区间

最终仓位仍受以下模块约束：

- 实盘信用分
- 实时反应风控
- 权益保护
- 最大杠杆和最大名义价值
- 最小下单量补齐规则
- 手续费/滑点后的净利润检查

## 出场

盘口剥头皮信号使用百分比保护：

- 默认硬止损：`yolo_scalp_orderbook_stop_pct = 0.20%`
- 默认止盈：手续费、滑点、目标净利驱动，且不低于 `yolo_scalp_orderbook_min_take_profit_pct`
- 默认最长持仓：`yolo_scalp_orderbook_max_hold_seconds = 120`

运行时保护已支持秒级最长持仓，避免剥头皮单被 5m K 线窗口拖成慢单。

## API 与性能

- WebSocket 增加 `1m kline` 订阅，不增加 REST 权重。
- depth、ticker 优先使用 WebSocket 快照。
- REST 仍只用于账户、下单、必要 K 线历史和兜底。
- 扫描漏斗、快车道、REST 频控保持原架构。
- Dashboard 增加盘口点差、盘口失衡、扣费后空间、剥头皮信号数量。

## 影响范围

- 只改变 `yolo_scalp` 最极限模式。
- 其他模式的开仓逻辑、网格逻辑、持仓轮换逻辑不变。
- 已有止盈止损补单、安全审计、实时伤害学习继续生效。
