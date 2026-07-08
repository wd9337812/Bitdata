# 保护审计与极限短打迭代留档

日期：2026-07-08

## 背景

VPS 实盘运行中出现两个需要优先处理的问题：

- Dashboard/巡检只看普通 `openOrders` 和 account 持仓字段，容易漏看 Binance 条件保护单 `openAlgoOrders`。
- `/fapi/v2/account` 的持仓没有 `markPrice`，运行时保护会持续出现 `missing_price`，导致保护逻辑不能完整判断。

同时，极限模式需要把“好机会大仓位、快进快出”接入仓位模型，但不能让普通机会一起放大。

## 本次改动

### 1. 保护单审计与自愈

新增 `app/protection_audit.py`：

- 只审计当前持仓币，不扫全市场。
- 使用 `positionRisk.markPrice` 补齐持仓价格，WebSocket 最新价兜底。
- 检查 `openAlgoOrders` 是否存在反向 `STOP_MARKET` 和 `TAKE_PROFIT_MARKET`。
- 缺止损或止盈时按 fallback 百分比自动补单。
- 发现保护单触发价方向错误时，取消该币条件单并重建。
- 开仓后立即反查保护状态；确认失败则市价平仓。

默认参数：

- `protection_audit_enabled=true`
- `protection_audit_auto_repair_enabled=true`
- `protection_audit_fallback_stop_pct=0.9`
- `protection_audit_fallback_take_profit_pct=1.2`

### 2. runtime protection 价格源修复

`app/runtime_protection.py` 在巡检前用 `positionRisk` 和 WebSocket 补齐 `markPrice`。

预期结果：

- `missing_price` 不再持续刷屏。
- 时间止损、快速失效、保本和移动止盈判断能拿到真实价格。

### 3. 数据库写入降噪

`app/telemetry.py` 新增 `record_event_throttled`：

- 高频普通保护巡检不再每轮写完整 payload。
- 数据库锁维护日志节流到 5 分钟。
- 保护缺失、自动修复、错误仍会留详细日志。

### 4. 实盘学习调权

`app/live_learning.py` 将盈利奖励、亏损惩罚、手续费拖累惩罚改为配置化。

默认方向：

- 扣费后净盈利方向更快加分。
- 连续亏损方向更快降权。
- 手续费拖累明显的方向降低优先级和仓位倍率。

### 5. 极限短打仓位

新增 `extreme_scalp` 档位，只在极限模式且同时满足以下条件时触发：

- 非探路单。
- 综合分达到阈值。
- 质量分、成本比、盘口深度达标。

默认参数：

- `extreme_scalp_high_score=122`
- `extreme_scalp_super_score=145`
- `extreme_scalp_min_quality_score=72`
- `extreme_scalp_min_cost_ratio=12`
- `extreme_scalp_min_depth_notional_usdt=20000`
- `extreme_scalp_max_risk_pct=18`
- `extreme_scalp_stop_atr=0.55`
- `extreme_scalp_take_profit_atr=0.75`
- `extreme_scalp_max_hold_bars=2`

## 性能控制

- 保护审计只对当前持仓币调用 `positionRisk` 和 `openAlgoOrders(symbol)`。
- 快车道不执行保护审计，避免挤占实时信号。
- 高频状态不写大 payload。
- 实盘学习仍按低频同步，不放入高频路径。

## 验收

- `pytest -q` 通过：114 passed。
- 新增测试覆盖：
  - positionRisk 价格兜底。
  - 缺止盈/止损自动补。
  - 已有保护不重复下单。
  - 保护单触发价错误时重建。
  - 极限短打仓位放大与快出保护参数。
