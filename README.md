# Binance Futures Strategy Dashboard

这是一个 Binance USDT 永续两阶段策略系统，包含 Dashboard、回测、风控、实盘执行保护和 VPS 部署脚本。

> 风险提示：合约交易可能快速亏损本金。本项目不保证收益。默认 `dry_run=true`，不会真实下单。

当前版本 `v0.18.0` / 策略 `V4.8`：在相对排名外增加绝对质量底线，并加入趋势衰竭检测和同币同方向结构化再入场。聪明钱只在中周期路径与防追价条件成立时做最多正负 `2` 分的确认，不单独创造信号。局部负证据优先降仓和抬高门槛，仅严重同类影子负期望或连续亏损且结构未重置时阻断该候选，不做全币池停牌。V4.7 仅以有上限的权重作为冷启动先验。15% 压力风险上限、5U 硬停止、交易所保护单、流动性硬门和 API 优先级保持不变。

## 功能

- 当前市场数据：价格、24h 涨跌、成交额、资金费率
- React 管理后台：左侧菜单、总览、扫描、收益曲线、风控、配置、日志
- 权益曲线：runner 写入 SQLite 快照，Dashboard 定时刷新
- 阶段一：滚仓增长模式
- 阶段二：合约网格模式
- 策略信号、决策、回测
- Binance API 配置
- 实盘开关和多重确认
- 后台 runner 定时执行
- Docker Compose 一键部署到 VPS

## 阶段一：多币种滚仓增长模式

默认参数：

- 候选币：手动列表 + 自动发现高成交加密币
- 方向：只做多
- 自动排除股票、黄金等非加密合约
- 只执行评分最高且通过过滤的一个币
- 默认扫描最高成交的 `15` 个加密合约，可在前端调整
- 稳健杠杆上限：`2x`
- 进攻杠杆上限：`3x`
- 锦标赛杠杆上限：`5x`
- 稳健单笔风险：`1%`
- 进攻单笔风险：`5%`
- 锦标赛单笔风险：`15%`
- 每日最大亏损：`3%`
- 连续亏损暂停：`2` 笔
- 最大持仓数：`1`
- 阶段目标：`10000U`

增长模式：

- `conservative`：稳健回踩，默认 `4h`
- `balanced`：均衡回踩，默认 `1h`，默认回测 `20天`
- `attack`：进攻回踩或动量，默认 `15m`，默认回测 `10天`
- `tournament`：小资金锦标赛突破，默认 `5m`，默认回测 `5天`

当 `auto_risk_by_equity=true` 时：

- 小于 `100U`：自动使用锦标赛模式
- `100U-500U`：自动使用进攻模式
- 大于 `500U`：使用你配置的模式

手续费过滤：

- 系统估算开仓手续费、平仓手续费和滑点。
- 只有预期止盈空间大于最低阈值，并且预期收益/成本比达到配置要求时才允许交易。
- 周期越短，手续费占比越高，尤其是 `5m` 模式。

策略规则：

1. 4h 收盘价站上 EMA20，且 EMA20 高于 EMA50。
2. 当前 K 线最低价回踩或跌破 EMA20，并收回 EMA20 上方。
3. ATR / 价格大于 `0.4%`。
4. 下一根 K 线开盘做多。
5. 止损 `1 ATR`，止盈 `1.5 ATR`。
6. 最多持仓 `18` 根 4h K 线。

实盘路径会设置杠杆、市价开仓，并挂 `STOP_MARKET` 止损和 `TAKE_PROFIT_MARKET` 止盈保护单。

## 阶段二：合约网格模式

默认参数：

- 交易对：`BTCUSDT`, `ETHUSDT`
- 杠杆上限：`1.5x`
- 保留现金：`25%`
- 网格层数：`20-80`
- 默认不做空
- 到达目标权益后默认只提醒，必须手动确认切换

网格执行采用保守长网格：

- 无多仓时，只在现价下方挂买单。
- 已有多仓时，才在现价上方挂 reduce-only 卖单。
- 只有配置 `allow_short=true` 时才允许挂开空卖单。
- 执行网格前会撤销该币种已有挂单，再放置新网格。

## 本地运行

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

打开：

```text
http://127.0.0.1:8080
```

后台 runner：

```bash
python -m app.runner
```

## VPS 部署

推荐 Ubuntu 22.04/24.04。首次部署：

```bash
curl -fsSL https://raw.githubusercontent.com/wd9337812/Bitdata/codex/two-stage-live-system/deploy.sh | bash
```

部署完成后打开：

```text
http://YOUR_VPS_IP:8080
```

更新：

```bash
cd /opt/bitdata
./deploy.sh update
```

查看日志：

```bash
cd /opt/bitdata
./deploy.sh logs
```

Compose 会启动两个服务：

- `dashboard`：Web 控制台
- `runner`：后台策略循环，默认每 300 秒运行一次

## 安全配置

建议在 `.env` 里开启 Basic Auth：

```env
BASIC_AUTH_USER=your-user
BASIC_AUTH_PASSWORD=your-strong-password
BOT_LOOP_SECONDS=300
```

Binance API 建议：

- 先用只读 API 运行 24 小时。
- 交易 API 必须限制 IP 为 VPS IP。
- 不要开启提现权限。
- 小资金实盘前先保持 `dry_run=true` 观察日志。

实盘必须同时满足：

- `dry_run=false`
- `live_trading_enabled=true`
- `live_trading_confirmation=ENABLE_LIVE_TRADING`
- Dashboard 状态为 `running`

Dashboard 启动机器人还需要确认短语 `START_BOT`。

## Docker 管理

```bash
docker compose up -d --build
docker compose logs -f
docker compose down
```

配置文件：

```text
data/config.json
```

运行状态：

```text
data/state.json
```

## 测试

```bash
cd frontend
npm install
npm run build
cd ..
python -m compileall app
pytest
```
