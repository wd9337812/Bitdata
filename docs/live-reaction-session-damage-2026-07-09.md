# Live Reaction Session Damage Upgrade - 2026-07-09

## 背景

2026-07-08 夜间实盘出现典型的极限剥头皮问题：部分币种先连续盈利，随后在同币种同方向连续回吐，导致账户权益快速回撤。原有实时反应层已经能处理一亏降仓、两连亏暂停、连盈防追尾，但对“单笔亏损占权益过大”和“当日盈利被吐回去”记忆不够长。

## 本次目标

- 保持极限模式的进攻性，不把策略整体改保守。
- 对同币种同方向的实盘伤害更敏感。
- 把无效下单原因拆成中文可解释字段。
- 不增加高频 REST 压力，所有新增判断基于本地 `live_trade_records` 重算。

## 新增风控

### 当日最大单笔伤害

按 `symbol + direction + UTC day` 统计最大单笔亏损占当前权益比例。

- `live_reaction_single_loss_cooldown_equity_pct=6`：延长降仓观察。
- `live_reaction_single_loss_ban_equity_pct=10`：暂停同币种同方向。
- `live_reaction_single_loss_cooldown_minutes=60`
- `live_reaction_single_loss_ban_minutes=180`

### 盈利回吐保护

按当日累计净盈亏曲线统计峰值利润与当前累计净盈亏之间的回吐比例。

- `live_reaction_giveback_min_profit_usdt=1`：峰值盈利超过该值才启用回吐判断。
- `live_reaction_giveback_cooldown_pct=50`：回吐一半后降仓。
- `live_reaction_giveback_ban_pct=80`：大幅回吐后暂停同向。
- `live_reaction_giveback_cooldown_minutes=60`
- `live_reaction_giveback_ban_minutes=180`

### 当日同向净亏

默认从 `20%` 下调到 `15%`，用于小账户阶段更快阻断重复伤害。

## 学习链路

`sync_live_reaction_from_binance` 在同步成交并写入本地数据库后，会立即调用 `rebuild_symbol_scores` 刷新实盘信用分。这个步骤不增加 Binance 请求，只增加轻量 SQLite 读写。

## 可解释性

`effective_order_viability` 现在返回：

- `reason_labels`
- `reason_details`
- `summary`

用于解释：

- 低于系统有效下单额
- 收益/手续费滑点成本比不足
- 扣费后预期净利润不足

Dashboard 的实时风控页新增：

- 最大单笔伤害
- 盈利回吐

## 性能边界

新增计算只使用已同步的本地成交记录，默认回看窗口仍受 `live_reaction_sync_lookback_minutes` 和 `live_reaction_sync_max_symbols` 控制。没有增加每轮扫描的 K 线、盘口或私有账户 REST 调用。
