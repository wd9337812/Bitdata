# 7 日动量 + 市场宽度带跨年审计拒绝

日期：2026-08-04

## 假设

把 30 日形成期缩短为 7 日、持有 1 日、2 ATR 止损 / 2R 止盈，并叠加 1.5%-7.5% 绝对市场宽度带，检验是否能成为 S0 的补充高频路线。

## 审计口径

- 数据：Binance USD-M 连续小时路径 + 30 日候选信号，2021-2025 独立审计、2026 仅作上下文。
- 成本：0.36% 基础、0.60% 压力两档。
- 资格线：整体 PF > 1.2、交易数 >= 100、五年每年 PF > 1.0 且净收益为正、盲区样本为正、去掉头部 3 币后仍为正、bootstrap 正概率 >= 0.95。

## 结果

0.60% 压力成本下，独立期（2021-2025）整体 PF 0.878、净收益 -172.18 点数，bootstrap 正概率 0.175；去掉头部 3 币后 PF 0.727。五年中 2022-2025 净收益全部为负。结论为 `reject_before_minute_replay`。

## 决策

- 拒绝 7 日 + 宽度带候选，不进入分钟级重放，也不参与实盘准入。
- 保留审计脚本 `scripts/audit_s0_7d_breadth_cross_year.py` 与测试，便于后续复算。
- 2026 单一年份为正（PF 1.34）不能覆盖独立年份的负期望。

## 留档

- 脚本：`scripts/audit_s0_7d_breadth_cross_year.py`
- 测试：`tests/test_audit_s0_7d_breadth_cross_year.py`
- 数据产物：`data/research/s0_7d_breadth_cross_year/report.json`
