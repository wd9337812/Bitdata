# Binance REST 频控与实时性方案 2026-07-02

## 背景

系统在实盘运行中多次触发 Binance `429/418`。根因不是下单频率过高，而是 runner 和 Dashboard 同时高频轮询 REST：

- runner 每 30 秒执行完整扫描。
- Dashboard 每 10 秒请求状态，原实现会查私有账户。
- Dashboard 每 60 秒请求策略决策，原实现会重新跑一遍完整扫描。
- 多币种 K 线、ticker、depth、account 叠加后触发 IP 级 `REQUEST_WEIGHT` 限制。

Binance 官方限制以 IP 为单位累计 `REQUEST_WEIGHT`，USD-M Futures 默认约 `2400/min`。收到 `429` 后必须退避，继续请求会触发 `418` IP ban。

## 本次上线内容

### 1. 全局 REST 频控器

新增 `app/binance_rate.py`：

- 按 endpoint 估算请求权重。
- 记录本地每分钟估算使用量。
- 读取响应头 `X-MBX-USED-WEIGHT-1M`、`X-MBX-ORDER-COUNT-*`。
- 触发本地预算时直接进入冷却，不继续请求 Binance。
- 遇到 `429/418` 后解析 `Retry-After` 或 `banned until`，写入冷却时间。

默认本地预算：`600 weight/min`，低于 Binance 官方限制，给手动操作和异常重试留余量。

### 2. 短周期新鲜快照

为了避免同一分钟内 dashboard 和 runner 重复请求同一份数据，客户端增加短 TTL 复用：

- `exchangeInfo`: 3600 秒。
- `ticker_24h`: 30 秒。
- `premiumIndex`: 30 秒。
- `klines`: 20 秒。
- `klines_history`: 60 秒。
- `depth`: 10 秒。
- `account`: 45 秒。

这不是长期缓存策略，而是“新鲜快照复用”。策略仍按 runner 扫描节奏刷新，Dashboard 不再自己触发新扫描。

### 3. Dashboard 不再触发策略扫描

`/api/decisions` 改为读取 runner 最近一次写入数据库的策略结果。

`/api/status` 改为读取最近权益快照和状态，不再每次请求 Binance 私有账户。

这样打开 Dashboard 不会额外放大 REST 请求量。

### 4. 限流自动等待恢复

runner 遇到 `BinanceRateLimitError` 后不再永久 `paused`，而是进入：

```text
bot_status = rate_limited
```

冷却结束后自动恢复：

```text
bot_status = running
```

前端状态映射已增加“限流等待中”。

### 5. 默认扫描频率

锦标赛模式默认扫描从 `30 秒` 调整为 `60 秒`。

原因：当前阶段还未接入 WebSocket 行情中心，完整 REST 扫描 30 秒一轮风险过高。等二期 WebSocket 行情中心上线后，可再把触发检查改成更实时。

## 二期上线：WebSocket Market Data Hub

新增 `app/market_stream.py`：

- runner 启动时创建后台 WebSocket 线程。
- 订阅候选币的 `@ticker`、`@kline_5m`、`@depth5@500ms`。
- 持续写入 `data/market_stream.json`。
- `ticker_24h(symbols)` 会优先使用实时 ticker 覆盖 REST 快照。
- `depth(symbol)` 会优先使用实时 depth5。
- `klines/klines_history` 会用实时当前 K 线覆盖最后一根 K 线。
- REST 仍作为初始化和断流兜底。

这使系统从“每轮 REST 拉行情”改为“WebSocket 实时更新 + REST 兜底”。完整策略扫描仍按配置节奏执行，但数据源的新鲜度更高，REST 权重更低。

新增配置：

- `market_stream_enabled = true`

状态接口会返回：

- `market_stream.connected`
- `market_stream.age_seconds`
- `market_stream.ticker_count`
- `market_stream.depth_count`
- `market_stream.kline_count`

如果 WebSocket 断开，runner 会自动重连；策略仍会退回 REST 频控路径。

## 后续目标

后续可以继续把“完整扫描”改成事件驱动：

- 新 K 线收盘触发完整策略评估。
- 临近触发候选用实时盘口和价格做快评。
- REST 仅用于缺口补齐、账户快照、下单、异常恢复。

## 当前安全边界

本次上线优先解决 IP 被 ban 和 Dashboard 放大请求的问题。策略数据仍通过 REST 获取，但已经有全局频控、短 TTL 复用和自动限流恢复保护。
