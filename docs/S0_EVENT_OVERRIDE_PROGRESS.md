# S0 事件接管方案：开发进度 TODO

最后更新：2026-08-05 03:10（北京时间）

## Phase 0：底仓层

- [x] 风险档过拟合审计（bootstrap）：22%/25% 档未通过 → 回退 20% 固定
- [x] 部署 20% 固定 + 2x 并验证（VPS `risk=20, tier=off`）
- [x] AKE 止盈升级 3.5R（现有持仓）

## Phase 1：事件通道研究

- [x] 新币上市 72h 动量延续回测 → 正确协议下拒绝（去头部后转负）
- [x] 事件候选证据盘点：资金费/量价/OI/泵回落/基差全部拒绝或数据不足
- [x] 下载 2020-2025 Binance 免费资金费历史（完成：2020-2023 54.4 万行、2024-2025 131 万行）
- [x] 资金费拥挤反转跨年回测（全部样本外年份 PF<1 → 拒绝）
- [ ] 量价爆发延续补测（视数据可行性）
- [x] 尾部事件 MFE 数据管线与规则基线（单一规则提升有限且跨年不稳定）
- [x] 尾部事件 LightGBM v1（方向无关标签：排序有效但交易错配方向，v1 拒绝）
- [x] 尾部事件 LightGBM v2（方向性标签：PF<1.2 且硬停止，拒绝）
- [x] 尾部事件 LightGBM v3（方向性标签 + 追踪退出：单年不达标，拒绝）
- [x] 尾部事件 v4（回踩限价入场 ± 追踪：2024/2025 仍负，拒绝）
- [x] 用户选择：数据增强
- [x] 下载 2021-2026 OI/持仓/taker 比率 daily metrics（100 币，99/100 完成）
- [x] v5 特征接入代码就绪（OI 变化 / taker 比率 / toptrader 比率）
- [x] v5 训练（88/100 覆盖）：新特征重要性低，合并 PF 1.13-1.16、硬停止 → 拒绝
- [x] 数据源核查：确认 Binance Vision 1m 月度路径正确（此前跨年下载器从未实现），启动 100 币 × 2020-01~2026-07 1m 下载（checksum + parquet + 断点续传）
- [x] Bybit / OKX 免费公开数据源验证（Bybit MT4 1m/15m 2020-2024 共 23 币、spot/trading 近月 CSV；OKX 仅 2026 round3）
- [x] 1m 跨年数据首轮下载完成（100 币，9396 万行；但发现选币口径错误：漏掉 BTC/ETH/SOL 等主力）
- [x] 选币口径修正：改用 universe.json（真实成交量 top 120），已启动 top 60 流动性币补充下载
- [ ] 1m 跨年数据最终完成（top 60 流动性币补齐后：120 币宇宙 + 原有 100 币）
- [ ] Phase 1 高杠杆账户级回放：15m/30m/60m 决策、3x-10x 全仓、5U 硬止损；开发期选型 → 样本外验证 → bootstrap → 影子盘
- [x] Phase 1 高杠杆回放器 v1（scripts/research_s0_phase1_highlev_replay.py）：1m 决策+入场、全仓、可配杠杆/风险%/止损/止盈/时间止损、手续费滑点、单仓串行、adverse-first、爆仓路径；5 个单元测试通过，7 月 VPS 数据冒烟可跑（30 笔）
- [x] Phase 1 早期基线（54 币、20,914 笔）：15m 突破 + 5x 全仓 + 2% 止损 + 1R，2020-2026 每年 PF 0.70-0.83 全负；仅 2025-2026 新上市 meme 币局部为正 → 简单突破无跨年 edge，待全量后继续规则搜索
- [x] 1m 跨年数据最终完成：154 币、1.976 亿行（100 首轮 + top 60 流动性补充，校验通过）
- [x] Phase 1 v1 规则基线（top 60 流动性宇宙，共 11.3 万笔）：breakout/range_break/vol_shock/pullback 全部跨年负期望（PF 0.74-0.83，无任何一年 ≥1）→ v1 规则集整体拒绝，不进参数搜索
- [x] 修正宇宙 daily metrics 已重拉（top 60，1819 万行）：OI/taker/toptrader，供拥挤反转候选使用
- [x] 1m 事件家族批量审计（2026-08-05）：BTC 异动→ETH（3 个 OOS 全部否决）、日内截面动量（216 配置全负）、资金费快速反转（PF 0.67）、OI 强增仓快退（72 配置最好 0.90）→ 全部拒绝，留档 `docs/s0-phase1-1m-family-rejections-2026-08-05.md`
- [x] 日内截面反转（买最弱币）开发期 PF 0.635 → 家族关闭
- [x] 30d 动量快退（仓库既有审计）：PF 0.954、bootstrap 42.5% → 已拒绝，不重复
- [x] 跨所价差事件（Binance↔Bybit 永续 1m，23 币 4445 万行）：价格止损 324 配置最好 PF 0.751；价差回归退出 PF 0.354 → 单腿赌价差回归在结构上不成立，拒绝（`docs/s0-cross-exchange-dislocation-rejection-2026-08-05.md`）
- [x] 双腿跨所套利压力测试：基准 PF 3.90 → Bybit 腿延迟 1 分钟 PF 0.63、成本 ×2 PF 0.36 → 同分钟成交伪影，拒绝（`docs/s0-cross-exchange-pair-rejection-2026-08-05.md`）
- [x] 亚分钟验证（Binance 10s aggTrades + Bybit 逐笔，SOL/DOGE/ADA 2026-06）：10s 价差 std 0.015-0.048%、0.2% 事件为 0；低阈值双腿回测 PF 0.004 → 跨所家族正式关闭（`docs/s0-subminute-cross-exchange-validation-2026-08-05.md`）
- [x] 阻塞期工程：前向影子晋级检查器（30 笔/8 币/压力 PF/去前三/bootstrap）与双腿执行脚手架（dry-run，无 API key 不误下单）→ `docs/s0-forward-shadow-gate-and-dual-leg-scaffold-2026-08-05.md`
- [x] 30d 动量“让利润跑”（2.5 ATR / 3.5R-4R / 7 天）1m 路径跨年审计：小时级 PF 1.8-1.9 衰减为 1.5-1.7，年度稳定/去集中度/bootstrap 全部未过 → 拒绝（`docs/s0-30d-minute-letwin-rejection-2026-08-05.md`）
- [x] 30d letwin 全量 1m 重验（154 币全路径，无窗口截断）：可交易信号 17-20 笔，最优 PF 1.43、去前三 PF 0.14 → 结论不变且在高流动性宇宙上更弱
- [x] 免注册替代数据源：OKX/Gate/KuCoin 匿名公共行情客户端（无需注册）+ 双腿脚手架 `--venue`；Binance↔OKX 双腿 2026H1：基准 PF 1.90、延迟 1 分钟 PF 1.39、成本×2 PF 0.97 → 延迟鲁棒但成本脆弱，需用户确认可注册的第二交易所（`docs/s0-okx-registration-free-alternative-2026-08-05.md`）
- [x] 免注册数据源矩阵（VPS 实测）：Gate/KuCoin/Bitget/MEXC 无跨年 1m 公共历史；Hyperliquid 1m 仅 2 天/日线全史/钱包免 KYC；跨年第二所唯一免注册档案仍是 Bybit 公共站（下载免注册）→ `docs/s0-registration-free-data-matrix-2026-08-05.md`
- [x] 30d 动量 + 跨所价差方向过滤（免注册 Bybit 公共数据，Binance 单腿）：全样本 PF 1.16→1.77；严格切分后改善集中在 2024-2026（开发期去前三 0.51 负）→ 未过协议，研究状态（`docs/s0-30d-dislocation-filter-2026-08-05.md`）
- [x] OKX 注册完成：key/secret 已验证（50105 仅缺 passphrase）；OKX v5 签名客户端 + 双腿执行接线 + 前向影子记录器就绪，运行手册 `docs/s0-okx-forward-shadow-runbook-2026-08-05.md`
- [x] 回归事件研究主线：OKX 双腿套利降级为 Phase 2 备选；新增多事件实时前向影子（funding_extreme / volume_breakout / btc_impulse），公共数据、无资金、VPS 可启动（`docs/s0-forward-event-monitor-2026-08-05.md`）
- [x] 1h 量价爆发延续（方案原定义：量 5 倍 + taker 失衡 0.15 + 30d 同向）：104 笔，2021/2022/2026 全负，2026 盲测 PF 0.69 → 拒绝（`docs/s0-volume-explosion-rejection-2026-08-05.md`）
- [x] 资金费极端 + 动量衰竭反转（方案原定义）：2053 笔，所有窗口压力 PF 0.43-0.92 全负 → 拒绝（`docs/s0-funding-exhaustion-rejection-2026-08-05.md`）；方案三类事件全部测完并拒绝
- [x] 30d 动量 + 加速&广度确认（预注册共现）：dev PF 1.19→1.51、OOS 1.14→1.62，0/5 分钟延迟下去前三 PF>1、bootstrap 0.93-0.96；15 分钟延迟六年全正 → 研究候选（未全过门槛，进前向影子分支）（`docs/s0-30d-confluence-candidate-2026-08-05.md`）
- [x] 前向影子加入 momentum_confirmed 每日分支（30d 选择器 + 加速/广度确认），VPS 实测广度 0.29% 低于触发带正确不出信号
- [x] 四类事件前向影子已在 VPS 启动运行（PID 237564，公共数据、无资金、无下单），开始积累真实未来样本
- [x] 共现过滤扩展：确认过滤对 3.5R/4R 档案无效（去前三 0.65-0.86 全负），候选收敛为“冻结 2.5R + 加速&广度（短延迟）”，等待前向影子
- [x] 15m 尾部 MFE 点火原型（8 币、量能+taker 失衡）：9440 笔、PF 0.65、每年全负 → 拒绝（`docs/s0-15m-mfe-baseline-rejection-2026-08-05.md`）
- [x] 15m 尾部 LightGBM 识别器（按用户新方向加入山寨币重点池）：主流币开发期 PF 0.79；山寨池 2024-2026 全负（0.48-0.74）→ 拒绝，15m 尾部 ML 路径关闭（`docs/s0-15m-tail-lgbm-rejection-2026-08-05.md`）
- [x] 前向监控器新增 new_listing 事件（exchangeInfo 差集，记录新币上市 72h 波动），观察池扩至 42 币含 12 高波动山寨
- [x] 新币上市历史校准：2024+ 共 105 个新上市，72h 最大上行中位数 +33%（最大 +418%），最大下行中位数 -22.5% → 山寨新币波动大的判断量化确认，监控器 new_listing 分支开始积累
- [x] 新币首小时动量规则历史预注册回测：70 个合格，压力 PF 1.01、去前三 0.88、2025 负 → 与 72h 动量结论一致，无交易 edge（`docs/s0-new-listing-first-hour-rejection-2026-08-05.md`）
- [x] Phase 1 穷尽审计总档：30+ 家族全部拒绝，唯一正期望家族仅前向影子（`docs/s0-phase1-event-channel-exhaustive-audit-2026-08-05.md`）
- [x] VPS 前向影子检查：30d 动量 2 笔、24h 截面动量 18 笔（未达 30 笔/8 币门槛）
- [ ] Phase 1 下一候选：30 日横截面动量（唯一历史正期望家族）高杠杆事件化：信号日 3x-10x、1m 快速入场 + 5U 硬止损 + 让利润跑（2020-2022 选型 / 2023+ 验证 / bootstrap / 影子盘）
- [ ] Phase 1 下一候选：修正宇宙重拉 daily metrics（OI/taker/toptrader）→ 拥挤反转 + 1m 快速离场；空头与 ATR 自适应止损；大盘异动事件（2020-2022 选型 / 2023+ 验证 / bootstrap / 影子盘）
- [ ] Phase 1 状态：无合格事件候选；事件通道保持未启用，底仓 20% 固定继续（待新数据/新思路）

## Phase 2：事件实盘

- [ ] 通过候选的影子验证（≥50 笔、PF≥1.2、无 5U）
- [ ] 30% 资金小仓试运行（20 笔）
- [ ] 全量 + 接管日志/回滚验收

## 前端与工程

- [x] v0.31.7 “S0 事件接管状态”卡片
- [x] Bug/性能/API 频控审计（699 测试、零错误、API 2.6%）
- [ ] 事件通道上线后的完整验收（保护单、接管成本账、回滚测试）

## 当前阻塞

规则型事件候选全部被拒；尾部事件 v5 增强也已拒绝。当前在补 1m 跨年数据，
下一步做凉兮式全仓高杠杆短打的账户级回放（数据到位并验证后）。
