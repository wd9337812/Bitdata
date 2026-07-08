# 极限梭哈短打模式留档 - 2026-07-08

## 背景

用户目标是小资金阶段用滚仓方式冲刺：先在 50U-300U 阶段追求更高交易反馈和更高资金利用率，之后再进入 300U-10000U 的极限冲刺，最终逐步过渡到进攻、稳健和网格。

旧模式的极限冲刺仍偏向“强信号放大仓位”，但 50U 附近订单太小，手续费和滑点会吃掉毛利，导致看起来有信号却仓位偏小、反馈偏慢。本次新增 `yolo_scalp`，专门服务 50U-300U 的极限短打。

## 模式定位

`yolo_scalp` 不是稳定盈利模式，而是高风险试错模式：

- 权益低于 `yolo_scalp_auto_under_equity`，且已开启确认短语时，自动进入极限梭哈。
- 默认只开 1 个仓位，避免多个弱小仓同时消耗保证金和手续费。
- 使用更短的 ATR 止盈止损，目标是“好机会大仓位，吃一口就走”。
- 保留 Binance 端止盈止损保护单、运行时保护、最小下单量、盘口深度、手续费成本比、每日亏损上限和换仓成本检查。

## 关键默认参数

- `yolo_scalp_enabled = false`
- `yolo_scalp_confirmation = ENABLE_YOLO_SCALP`
- `yolo_scalp_auto_under_equity = 300`
- `yolo_scalp_loop_seconds = 8`
- `yolo_scalp_risk_per_trade_pct = 55`
- `yolo_scalp_daily_loss_limit_pct = 65`
- `yolo_scalp_max_open_positions = 1`
- 标准单：止损 `0.38 ATR`，止盈 `0.55 ATR`
- 抢跑单：止损 `0.32 ATR`，止盈 `0.48 ATR`
- 动量单：止损 `0.35 ATR`，止盈 `0.60 ATR`

## 阶段切换

新的阶段推荐：

- S0 `50-300U`：`yolo_scalp`
- S1 `300-10000U`：`extreme_sprint`
- S2 `10000-100000U`：`attack`
- S3 `100000-1000000U`：`balanced`
- S4 `1000000U+`：`grid`

手动选择 `yolo_scalp` 时必须同时满足：

- `yolo_scalp_enabled = true`
- `yolo_scalp_confirmation = ENABLE_YOLO_SCALP`

否则会回退到 `balanced`，避免误触高风险模式。

## 信用学习变化

信用分仍会影响排序和仓位，但本次加了一个限制：信用分高不等于直接加仓。

只有满足以下条件的币种方向，才允许信用倍率放大到 1x 以上：

- 历史成交数达到 `live_credit_boost_min_closed_trades`
- 净收益达到 `live_credit_boost_min_net_pnl_usdt`
- PF 达到 `live_credit_boost_min_profit_factor`
- 手续费/净收益比不超过 `live_credit_boost_max_fee_to_net_ratio`

如果不满足，信用倍率封顶在 `live_credit_unqualified_boost_cap`。如果当前方向净亏且手续费压力大，会继续降低仓位，避免“频繁交易但毛利被手续费吃掉”。

## 前端变化

配置中心的增长模式简化为四个阶段：

- 极限梭哈
- 极限冲刺
- 进攻增长
- 稳健过渡

旧的锦标赛、锦标赛冲刺等后端模式仍保留兼容，但不再作为新手主选择暴露。进阶参数里新增极限梭哈的开关、确认短语、扫描秒数、风险、止盈止损和加仓阈值。

## 测试

本次新增/更新测试覆盖：

- 50U 阶段推荐 `yolo_scalp`
- `yolo_scalp` 必须确认短语才能启用
- `yolo_scalp` 使用独立短打止盈止损参数
- 未盈利达标的信用高分方向不会盲目加仓
- 盈利、PF 和手续费达标的连续盈利方向才允许加仓

验证结果：

- `python -m py_compile app/config_store.py app/live_learning.py app/models.py app/position_sizing.py app/risk.py app/scanner.py app/stage_modes.py app/trading_engine.py`
- `pytest -q`：117 passed
- `npm run build`：成功，Vite 仅提示单页 bundle 偏大，不影响部署

## 风险说明

`yolo_scalp` 可能带来更多开仓尝试，也可能更快亏损本金。它适合用户明确接受高波动和高失败概率的滚仓冲刺目标，不适合作为稳定收益策略。系统不会保证 30 天到 10000U，只是把执行逻辑调整为更贴近该目标的高风险形态。
