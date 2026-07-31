# S0 点时策略研究留档（2026-07-31）

## 结论

本轮没有找到达到实盘或研究影子准入要求的正期望候选，线上策略不变。

旧的 119 币种研究集只包含当前仍可见的合约，存在幸存者偏差。它产生的高
PF 不能用于实盘判断。本轮重新构建了包含下架合约的 Binance USD-M 点时
数据集，并将 2026 年 7 月日线归档补至 7 月 30 日。

## 数据完整性

- 价格：Binance 官方 USD-M 1 小时归档。
- 宇宙：650 个历史 USDT 永续合约，包含后来下架的合约。
- 时间：2026-01-01 至 2026-07-30。
- 价格归档：月包与日包均校验 SHA-256，下载失败 0。
- 资金费率：581 个历史合约、526,777 行，官方月包校验失败 0。
- 7 月资金费率月包尚未发布；逐日资金费率 URL 不存在，因此不得把 7 月
  资金费结果当作完整盲测。
- 基础往返成本：0.12%；压力成本：0.24%。

## 已拒绝候选

### 1. 横截面动量 V2

在旧 119 币种集合中结果为正，但在 652 币点时集合中：

- 2026 年 7 月，基础成本 PF 0.996，净收益 -2.87 个百分点。
- 压力成本 PF 0.974，净收益 -17.03 个百分点。

结论：旧结果主要受幸存者集合影响，不可恢复。

### 2. 动量市场状态过滤

仅在 2–3 月开发窗口选择流动性、币龄、市场宽度和动量持续性过滤，
冻结后测试：

- 4–5 月 PF 0.920，净收益 -110.9 个百分点。
- 6 月 PF 0.976，净收益 -14.8 个百分点。
- 7 月 PF 0.912，净收益 -55.9 个百分点。

结论：过滤器没有产生稳定的样本外优势。

### 3. 时间序列通道突破

测试 24/48/72 小时通道、成交量确认、下一根 K 线开盘成交和 ATR 保护。
开发段最好的 48 小时成交量突破，基础 PF 1.036，压力 PF 0.997，未达到
进入冻结验证的开发门槛。

### 4. 短周期冲击反转

测试 1/2/4 小时波动率标准化冲击后的反向交易。开发段所有候选均为负，
最好 PF 约 0.919。

### 5. 正则化状态模型

LightGBM Huber 回归不使用币种身份，只使用已完成 K 线、流动性、币龄、
横截面排名、BTC 和市场宽度状态：

- 4–5 月校准 PF 1.134。
- 6 月 PF 1.482，净收益 +84.50 个百分点。
- 7 月 PF 0.989，净收益 -1.42 个百分点；压力 PF 0.966。

模型方向几乎全部偏空，表现存在明显状态漂移。扩展月度走步训练后，5–7
月合并 PF 0.972，压力 PF 0.948，拒绝。

### 6. 跨交易所迁移

旧 Binance + OKX 模型在 OKX 验证段 PF 1.073，但未触碰测试仅 PF 1.036，
成本提升至 0.16% 后转负；Binance 测试 PF 0.812。验证时选出的市场分组
在两家交易所测试段均失败。

### 7. 资金费率拥挤与挤仓

资金费率使用结算时间后 1 分钟才可见，信号在下一根 K 线开盘成交。
开发窗口为 2–3 月，4–5 月验证，6 月测试：

- 资金费反向：PF 约 0.35 / 0.30 / 0.19。
- 资金费同向：PF 约 0.56 / 0.49 / 0.48。
- 拥挤同向延续：PF 约 0.64 / 0.55 / 0.52。
- 逆资金费挤仓延续：PF 约 0.32 / 0.32 / 0.20。

结论：资金费率可作为模型上下文特征，但不能独立决定方向或实盘准入。

## 工程与实盘决定

- 不部署任何本轮候选。
- 不修改 V5.2、仓位、许可证、止盈止损或 5U 硬停止线。
- 不清空历史实盘、影子、模型或回测数据。
- 新研究必须继续使用点时币种宇宙、下一根 K 线成交、手续费/滑点后收益、
  冻结时间窗口和压力成本。
- 后续优先研究能跨市场状态工作的条件组合或相对价值信号；不得继续通过
  扩大阈值搜索来拟合已失败的价格动量、反转或资金费率单因子。

## 可复现命令

```powershell
.\.venv\Scripts\python.exe scripts\build_binance_um_point_in_time_1h.py
.\.venv\Scripts\python.exe scripts\extend_binance_um_point_in_time_daily_1h.py
.\.venv\Scripts\python.exe scripts\build_binance_um_point_in_time_funding.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_xmom_point_in_time.py
.\.venv\Scripts\python.exe scripts\audit_s0_xmom_regime_candidates.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_point_in_time_breakout.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_point_in_time_reversal.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_point_in_time_state_model.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_point_in_time_state_model_walkforward.py
.\.venv\Scripts\python.exe scripts\benchmark_s0_point_in_time_funding.py
```
