# Binance Futures Strategy Dashboard

这是一个 Binance USDT 永续两阶段策略系统，包含 Dashboard、回测、风控、实盘执行保护和 VPS 部署脚本。

> 风险提示：合约交易可能快速亏损本金。本项目不保证收益。默认 `dry_run=true`，不会真实下单。

## 功能

- 当前市场数据：价格、24h 涨跌、成交额、资金费率
- 阶段一：滚仓增长模式
- 阶段二：合约网格模式
- 策略信号、决策、回测
- Binance API 配置
- 实盘开关和多重确认
- 后台 runner 定时执行
- Docker Compose 一键部署到 VPS

## 阶段一：滚仓增长模式

默认参数：

- 交易对：`SOLUSDT`
- 方向：只做多
- 杠杆上限：`2x`
- 单笔风险：账户权益 `1%`
- 每日最大亏损：`3%`
- 连续亏损暂停：`2` 笔
- 最大持仓数：`1`
- 阶段目标：`10000U`

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

把本目录上传到 VPS 后执行：

```bash
chmod +x deploy.sh
./deploy.sh
```

部署完成后打开：

```text
http://YOUR_VPS_IP:8080
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
python -m compileall app
pytest
```
