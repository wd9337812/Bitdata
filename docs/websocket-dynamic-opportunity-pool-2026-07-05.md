# WebSocket 动态机会池迭代留档

日期：2026-07-05

## 目标

把行情数据层从“固定币种 WebSocket + REST 兜底”升级为“漏斗扫描驱动的动态 WebSocket 盯盘池”。本次不改最终开仓逻辑，只优化哪些币被实时盯盘、什么时候减少 REST 压力。

## 保持不变

- 标准突破开仓逻辑不变
- 抢跑试探逻辑不变
- 做多/做空判断不变
- 币种质量评分不变
- 实盘信用分奖励、惩罚、仓位倍率不变
- 止盈止损保护单逻辑不变
- 持仓轮换逻辑不变

## 新增机制

### 扫描意图文件

每轮漏斗扫描结束后，系统写入：

`data/stream_symbols.json`

内容只包含币种列表和来源，不写完整策略细节：

- `positions`：当前持仓币，最高优先级
- `candidates`：本轮候选和交易池币
- `hot`：粗排 Top 热点币
- `live_credit`：近期有实盘信用记录且未熔断的币

### 动态 WebSocket 池

`market_stream.py` 会合并以下来源：

1. 当前持仓币
2. 手动候选币
3. 漏斗候选币
4. 粗排热点币
5. 实盘信用币
6. 自动发现的高成交币

默认 2G VPS 参数：

- `market_stream_dynamic_enabled`: `true`
- `market_stream_max_symbols`: `50`
- `market_stream_rebuild_seconds`: `60`
- `market_stream_rotation_threshold_pct`: `20`
- `stream_hot_symbols_limit`: `25`

如果当前持仓币不在旧订阅池，会强制进入下一轮订阅，不受变化阈值限制。

## Dashboard

多币种扫描页新增“本轮扫描结论”：

- 已扫描多少币
- 重点分析多少币
- 候选多少个
- 是否有可执行信号
- 是否因 VPS 时间预算自动降级
- WebSocket 是否连接、订阅多少币、ticker/depth/kline 数量

工程细节仍保留在“机会漏斗”卡片里。

## 风险控制

这次不会提高开仓频率阈值，不会放宽风控。它的收益是更早让热点币进入实时盯盘，降低 REST 压力，并让 Dashboard 更容易看懂。
