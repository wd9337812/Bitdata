# 模式化币种质量评分 2.0 - 2026-07-05

## 背景

旧版币种质量评分使用一套固定权重，适合普通锦标赛，但对“锦标赛冲刺”偏保守。新版把评分改成按模式加权，并把质量结果转成仓位倍率，而不是简单一刀切。

## 核心变化

1. 不同模式使用不同权重。
   - 稳健/均衡更看重盘口、5天/10天稳定性。
   - 锦标赛冲刺更看重放量、ATR弹性、3天表现和当前趋势。

2. ATR 按模式评分。
   - 冲刺模式理想 ATR 默认是 `1.2%-7%`。
   - ATR 超过 `7%` 不直接禁用，而是默认仓位乘 `0.6`。
   - ATR 超过 `10%` 时，如果深度和放量不足，直接禁止。

3. 样本少惩罚按模式处理。
   - 冲刺模式默认只扣 `1` 分。
   - 如果放量超过 `2.5倍`、趋势成立、盘口达标，则样本少可豁免。

4. 新增质量仓位倍率。
   最终风险现在包含：

```text
基础风险 × 信号倍率 × 质量倍率 × 实盘信用倍率
```

5. 新增 `observe_hot`。
   冲刺模式下，放量强、趋势成立、盘口达标、质量分达到默认 `45` 的币，会进入热点观察池，可用小仓试探。

## 默认冲刺参数

- `sprint_symbol_trade_score`: `68`
- `sprint_symbol_small_trade_score`: `55`
- `sprint_symbol_hot_observe_score`: `45`
- `sprint_atr_ideal_min_pct`: `1.2`
- `sprint_atr_ideal_max_pct`: `7`
- `sprint_atr_high_pct`: `10`
- `sprint_high_atr_risk_multiplier`: `0.6`
- `sprint_sample_penalty`: `1`
- `sprint_sample_penalty_exempt_spike`: `2.5`
- `sprint_sample_low_risk_multiplier`: `0.75`
- `sprint_hot_observe_risk_multiplier`: `0.35`
- `sprint_extreme_depth_notional_usdt`: `50000`

## Dashboard

多币种扫描表新增：

- 质量池
- 质量分
- 质量倍率
- 质量拆分

用于直接解释一个币为什么能开、为什么降仓、或者为什么只是观察。

## 风险说明

本次迭代会增加冲刺模式下的候选机会，尤其是样本少但放量强的新热点币。风险控制不再主要靠“不开仓”，而是靠质量倍率、信用倍率、止损、每日亏损上限和账户硬停止线共同控制。
