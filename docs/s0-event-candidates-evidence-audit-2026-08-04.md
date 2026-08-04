# S0 事件候选证据盘点（2026-08-04）

## 结论

在“只用开发期选型、样本外验证、扣费后正期望、去掉头部币仍为正”的协议下，**目前没有任何事件候选合格**。事件通道暂不能上线；资金费反转存在数据缺口，补齐数据后可再验证。

## 候选清单与证据

| 事件类型 | 仓库回测结论 | 状态 |
| --- | --- | --- |
| 新币上市 72h 动量延续（本轮补测） | 开发期选出的 stop15_tp30 样本外 PF 1.055、去头部 3 币后 0.981 | 拒绝 |
| 资金费极端拥挤反转 | `s0_point_in_time_funding`：2026 仅半年数据，`research_only_not_eligible` | 数据不足，待补 2020-2025 资金费历史 |
| 1h/5m 量价爆发延续 | `s0_quarter_hour_orderflow_5m`、`s0_exact_quarter_hour_flow`、`s0_daily_order_flow`、`s0_volume_profile_tape` 全部 `rejected_not_positive_expectancy` | 拒绝 |
| OI 出清/增仓突破 | `s0_oi_flush`：`rejected_not_positive_expectancy` | 拒绝 |
| 暴涨回落做空 | `s0_pump_fade`：`rejected_not_positive_expectancy` | 拒绝 |
| 结算前资金费捕获 | `s0_pre_funding_capture`：`rejected_not_positive_expectancy` | 拒绝 |
| 现货/永续基差 | `s0_spot_perp_basis`：`rejected_not_stable_positive_expectancy` | 拒绝 |

## 数据缺口

- Binance 官方资金费归档仅覆盖 2026-01 至 2026-06（581 个币）。
- 资金费拥挤反转要跨年验证，需要补齐 2020-2025 资金费历史（仍为 Binance 免费公开数据，可用 `/fapi/v1/fundingRate` 或 data.binance.vision 月度文件分批下载）。

## 下一步

1. 补齐资金费历史 → 重新审计资金费反转（冻结参数、开发/验证/测试/盲测）。
2. 前端先展示“事件通道未通过验证”状态，避免用户误以为事件通道已上线。
3. 任何候选通过协议后，才进入影子→小仓→全量流程。
