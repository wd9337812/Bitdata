# 多事件类型实时前向影子（2026-08-05）

## 定位

回归事件研究主线：Phase 0（30d 动量底仓）继续运行；Phase 1 事件通道需要
“未来样本”证据。本监控器把方案里的三类事件候选同时挂到真实行情上，
**只记录、不下单、不需要任何交易所资金**。

## 监控的事件类型

| 类型 | 触发 | 方向 | 对应方案 |
| --- | --- | --- | --- |
| funding_extreme | 最新资金费 \|r\|≥0.05%/8h 且相对历史 z≥2 | 正→空、负→多（拥挤反转） | 方案 1.2-2 |
| volume_breakout | 最新 1h 成交量相对 200h z≥3 且收阳/收阴 | 阳→多、阴→空（量价爆发） | 方案 1.2-3 |
| btc_impulse | BTC 4h 收益 z≥3 | 跟随方向（大盘异动） | 事件家族扩展 |
| momentum_confirmed | 每日 30d 截面动量 + 加速&广度确认（预注册共现候选） | 选币方向 | 30d 共现候选 |
| new_listing | exchangeInfo 差集检测新上市 USD-M 合约 | 记录 72h 绝对波动 | 山寨新币事件 |

每个事件记录入场价格；24 小时后用真实收盘价结算 `raw_return_pct` 与
`mfe_pct`（最大有利波动），写入 `records.jsonl`。积累到每个类型 ≥50 笔闭合
后再按协议评估（PF≥1.2、无 5U、去集中度、bootstrap）。

momentum_confirmed 按冻结 30d 动量规则每日评估：广度绝对值必须在 2%-10%，
BTC 与广度同向；选中币需同时满足加速与广度确认才记 `momentum_confirmed`，
否则记 `momentum_unconfirmed`（诊断用）。2026-08-05 实测：市场广度 +0.29%
低于触发带，正确不出信号（该类型一年约 15 次）。

new_listing 通过 Binance exchangeInfo 差集检测新上市 USD-M 合约，记录上市后
72 小时的绝对收益与最大波动（不预设方向），直接积累“山寨新币单事件”样本。

## 启动（VPS，nohup，非 cron）

```bash
cd /opt/bitdata
mkdir -p data/research/s0_forward_event_monitor
nohup python3 scripts/forward_event_monitor.py \
  --state data/research/s0_forward_event_monitor/state.json \
  --records data/research/s0_forward_event_monitor/records.jsonl \
  --loop-interval 300 \
  > data/research/s0_forward_event_monitor/monitor.log 2>&1 &
echo $! > data/research/s0_forward_event_monitor/monitor.pid
```

查看：`tail -f .../monitor.log`；停止：`kill $(cat .../monitor.pid)`。
状态持久化，重启续跑；每 5 分钟一轮，公共 API 权重极小。

## 运行状态（2026-08-05）

- VPS 上已启动：PID `237564`，`state.json`/`records.jsonl` 已建立；
- 首轮执行完成：0 事件触发（当日市场广度 +0.29% 低于 30d 动量触发带，正常）；
- 每 5 分钟一轮，日志追加 `monitor.log`；停止用 `kill $(cat monitor.pid)`。

## 与 OKX 套利的关系

OKX 双腿套利**降级为 Phase 2 备选**（跨所价差毛利薄，不适合第一桶金 1000x）；
其代码与凭据保留，但不占 Phase 1 主线。OKX 公共行情仍可作免注册数据源。

## 验收

- 单元测试 3 个（z 分数、量价事件、到期结算）。
- VPS 端到端实测通过（4 币扫描，0 事件触发属正常）。
