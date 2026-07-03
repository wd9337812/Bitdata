# 观察池降仓风控迭代 - 2026-07-03

## 背景

实盘中出现一轮约 10U 回撤，主要由 `TLMUSDT` 观察池高分标准突破单贡献：

- `TLMUSDT` 做多，约 96.30U 名义价值。
- 约 51 秒后触发止损。
- 实现亏损约 6.07U，含手续费净亏约 6.16U。

复盘结论：信号本身并非完全无效，问题在于观察池机会层对低价、高波动、深度一般的币种给了过大的单笔风险。

## 影子回测结论

把严格硬过滤规则套到历史候选池后：

- 历史通过候选：158。
- 严格规则通过候选：154。
- 总机会保留率约 97.47%。
- 观察池标准突破机会从 4 笔降到 0 笔。

因此不采用硬过滤作为主方案。更合适的方案是保留观察池机会，但对高风险结构做动态降仓。

## 本次策略调整

默认参数：

- `observe_breakout_risk_multiplier`: `0.35 -> 0.22`
- `observe_low_price_threshold`: `0.01`
- `observe_low_price_risk_multiplier`: `0.75`
- `observe_high_atr_pct`: `3.0`
- `observe_high_atr_risk_multiplier`: `0.75`
- `observe_extreme_atr_pct`: `3.0`
- `observe_extreme_depth_notional_usdt`: `5000`
- `observe_consecutive_loss_count`: `2`
- `observe_consecutive_loss_risk_multiplier`: `0.5`

规则含义：

- 观察池标准突破仍允许执行，但基础仓位折扣降为 0.22。
- 低价币再次降仓。
- 高 ATR 币再次降仓。
- 最近账户连续亏损达到阈值后，观察池仓位再降一档。
- 仅在高 ATR 且深度极弱时硬拦截。

连续亏损统计改用 Binance `income` 收益流水中的 `REALIZED_PNL`，避免每轮扫描逐币调用 `userTrades`，降低 API 频控压力。

## 预期影响

按本次回撤样本估算：

- `TLMUSDT` 类似结构的亏损会从约 6.16U 降到约 2U 到 2.5U。
- 观察池机会不会被整体关闭。
- 普通正式池、小仓池、抢跑逻辑不受这次规则直接影响。

## 验证

本地测试：

```bash
pytest -q
python -m compileall app
```

结果：

- `38 passed`
- `compileall` 通过
