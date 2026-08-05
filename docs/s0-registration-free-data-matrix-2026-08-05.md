# 免注册数据源可用性矩阵（2026-08-05，VPS 实测）

## 需求

用户无法注册 Bybit，要求换“不用注册”的数据源。区分两个层面：

- **数据（行情/历史）**：多数交易所公共 API 免注册；
- **执行（下单）**：所有中心化交易所都强制注册+KYC；去中心化交易所（如
  Hyperliquid）不需要注册，但需要加密钱包。

## 实测矩阵（VPS 外网验证）

| 数据源 | 公共行情（免注册） | 跨年 1m 历史 | 执行（免注册） |
| --- | --- | --- | --- |
| Binance Vision | ✅（官方归档） | ✅ 154 币 2020-2026 | ❌（已有账户） |
| Bybit public.bybit.com | ✅（官方归档，匿名） | ✅ 23 币 2020-2025 | ❌（用户无法注册） |
| OKX 公共 API | ✅ tickers/history-candles | ❌ 仅近期，官方历史需登录 | ❌ |
| OKX round3（仓库） | ✅ | ✅ 12 币 2026-01~07 | ❌ |
| Gate.io 公共 API | ✅ tickers/candles | ❌ 仅最近 1 万根（约 7 天） | ❌ |
| KuCoin 期货公共 API | ✅ tickers/kline | ❌ 忽略旧 startAt，只给近期 | ❌ |
| Bitget v2 公共 API | ✅ candles | ❌ 旧区间返回空 | ❌ |
| MEXC 公共 API | ⚠️ 403（区域拦截） | ❌ | ❌ |
| Hyperliquid 公共 API | ✅ allMids/candleSnapshot | ⚠️ 1m 仅最近 2 天；日线全史 | ✅ 钱包（无 KYC） |

## 结论

1. **跨年第二所 1m 数据**：唯一免注册官方档案是 Bybit `public.bybit.com`
   （下载不需要 Bybit 账户，我们一直在匿名使用）；OKX 只有 2026 H1 已入库。
2. **实时第二所信号**：OKX/Gate/KuCoin/Hyperliquid 公共 API 均可匿名获取
   （客户端已实现并测试）。
3. **执行**：没有任何中心化交易所提供免注册下单。唯一“免注册”执行路径是
   **Hyperliquid + 加密钱包**（无 KYC），但 1m 历史只有最近 2 天，无法做
   跨年回测，只能前向验证。

## 建议

- 若用户可注册 OKX/Gate/KuCoin/Bitget/MEXC 任一：接该所执行 + 免注册公共数据
  做信号，回测用 Bybit 公共档案做跨年代理（三所价差结构相近，需按所校准）。
- 若用户可创建加密钱包（无 KYC）：用 Hyperliquid 做第二腿，先小仓前向影子
  （1m 数据从上线日开始积累），不做历史回测。
- 若两者都不可行：双腿套利无解；降级为 Binance 单腿 + 跨所价差信号过滤，
  但需要重新验证。
