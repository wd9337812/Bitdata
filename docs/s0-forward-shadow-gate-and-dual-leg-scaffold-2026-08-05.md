# 阻塞期工程：前向影子晋级检查器 + 双腿执行脚手架（2026-08-05）

目标仍因外部授权/时间阻塞，但本轮完成两项可离线交付的工程：

## 1. 前向影子晋级检查器

`scripts/check_s0_forward_shadow.py`：直接读 VPS `data/bitdata.db`，按
`shadow_trades` 表计算晋级门槛：

- 闭合笔数 ≥ 30、独立币种 ≥ 8、至少 2 种市场状态；
- 0.60% 压力成本后 PF > 1.20；
- 剔除前三贡献币后 PF > 1 且净收益 > 0；
- 周块 bootstrap 正收益概率 ≥ 95%。

输出 JSON 报告，任何一项不满足即 `qualified=false`。

## 2. 双腿跨所执行脚手架（dry-run）

`scripts/bybit_dual_leg_execution.py`：

- 只读 Bybit 公开行情客户端（ticker/last price，无需 API key）；
- 双腿订单模板生成（方向、名义、止损距离）；
- `BybitPrivateClient` 在 `dry_run=False` 且无 API key 时报错，防止误下单；
- 实盘下单函数显式 `NotImplementedError`，必须等策略获批 + 凭据到位才实现。

## 当前前向影子状态（VPS 实测）

| 家族 | 总数 | 闭合 | 币种 | 门槛 |
| --- | ---: | ---: | ---: | ---: |
| adaptive_30d_momentum v2 | 1 | 0 | 1 | 未达 |
| cross_sectional_momentum 24h v2 | 19 | 18 | 6 | 未达（需 30/8） |

门槛检查器部署到 VPS 后，每次闭合交易都会自动给出晋级状态。
