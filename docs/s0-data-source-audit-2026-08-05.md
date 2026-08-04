# S0 数据源核查：1m 跨年数据与可替换免费数据源

日期：2026-08-05

## 结论摘要

1. 之前 1m 跨年数据缺失的真正原因不是 Binance 没有数据，而是仓库里没有“月度 1m 归档”下载器：
   - 正确路径：`data/futures/um/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM}.zip`
   - 已验证 BTCUSDT / SOLUSDT / DOGEUSDT 的 2020/2021 月度包存在，且带官方 `.CHECKSUM`。
   - 现有 `download_binance_um_event_1m.py` 用的是 daily 路径（`daily/klines/{SYMBOL}/1m/...`），所以事件窗口的 1m 数据一直是好的；缺的是跨年全量。
2. Binance Vision 仍是最优主数据源：官方、免费、月度归档、带 checksum、覆盖 2019-09 至今。
3. 已启动 100 币 × 2020-01 ~ 2026-07 的 1m 月度归档下载：逐币下载 → 校验 → 合并 parquet → 删除 zip，
   断点续传（manifest），后台运行，避免把本地磁盘（C 盘仅剩约 17GB）塞爆。
4. 可替换/交叉验证数据源（均免费官方）：
   - Bybit `public.bybit.com`：MT4 K 线目录有 23 个主流币的 1m/15m 月度文件（2020-2024）；
     `spot/` 和 `trading/` 还有近月 CSV（2025-2026 及逐日成交）。
   - OKX：仓库已有 `s0_okx_1m_round3`（12 币，2026-01~2026-07）；官方 data-download 需登录，公开批量接口有限。
   - Binance Vision aggTrades：逐笔聚合成交，免费但体积大（BTC 单月约 1.4GB），适合做 tape 级特征，不适合全量常备。

## 之前路径问题定位

- 跨年 1m 下载从未落地：`git log` 中没有可用的 monthly 1m 下载器；
  仓库内 `monthly/klines` 只被 1h/5m 构建器正确使用（`{SYMBOL}/1h/`、`{SYMBOL}/5m/`）。
- 若按“`monthly/klines/{SYMBOL}/{SYMBOL}-1m-{月}.zip`”访问会 404（少了 `/1m/` 段），
  这正是此前“1m 跨年数据不可行”印象的来源。

## 新下载器设计（scripts/download_s0_cross_year_1m.py）

- 币种：按资金费历史覆盖长度取 top 100（与 metrics 下载器同一选币逻辑）。
- 月份：2020-01 ~ 2026-07（月度归档稳定，当前月用 daily 兜底）。
- 完整性：每个 zip 拉取 `.CHECKSUM` 做 SHA256 校验（可 `--no-checksum` 关闭）。
- 磁盘：每个币种先落 `raw/{SYMBOL}/`，全部月份到齐后合并写 `parquet/{SYMBOL}.parquet`，
  成功即删除 raw；manifest 记录 missing/failed，下次自动跳过已完成币种。
- 冒烟测试：BTCUSDT 2020-01/2020-02，86,398 根 1m K 线，校验通过，parquet 5.2MB，raw 已清理。

## 为什么这对 Phase 1 是关键

凉兮式全仓高杠杆短打要求的是 15m/30m/60m 级别的入场与出场，而不是 1h 追单。
1m 跨年数据到位后，才能做：

- 账户级回放：按真实权益、手续费、滑点、全仓保证金逐笔回放；
- 高杠杆档位（3x-10x）与 5U 硬止损的压力测试；
- 开发期（2020-2022）选型 → 样本外（2023+）独立验证 → bootstrap 防过拟合 → 影子盘，
  全部通过后才允许推到 VPS 部署。

## 验收与留档

- 下载进度：`data/research/binance_um_1m_cross_year/manifest.json`
- 日志：`download.out.log` / `download.err.log`
- 数据源优先级：Binance Vision（主）→ Bybit 官方公开（交叉验证）→ OKX（有限，仅做参考）

## 选币口径修正（2026-08-05 03:10）

首轮 100 币选币用了 `binance_um_point_in_time_funding` 的行数排序，但这个目录只覆盖
2026-01~06，且部分币有重复 funding 记录，导致选出的“top 100”几乎全是次新 meme 币，
BTC/ETH/SOL/DOGE/XRP/BNB 等全部漏掉。

修正：

- 下载器默认改读 `data/research/s0_public_1m/universe.json`（按真实 24h 成交量排序的 120 币清单），
  支持 `--universe` 显式指定，`--funding` 仅作兜底。
- 已启动 top 60 流动性币的 1m 跨年下载（与首轮 100 币合并，断点续传）。
- 影响说明：v5 增强（OI/taker/toptrader）是在 meme-skewed 100 币上训练的，其拒绝结论仍然有效
  （增强特征重要性低）；后续如重做增强，必须用修正后的流动性币宇宙重新下载 metrics。
