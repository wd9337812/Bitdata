# Binance↔OKX 双腿前向影子：运行手册（2026-08-05）

## 作用

用 OKX 公共行情 + Binance 公共价格（都免注册）实时监控 8 个主流币的跨所价差；
满足 |价差|≥0.20% 且 z≥3 时记录“开仓”，价差回归 50% / 扩大 2 倍 / 超 60 分钟时
记录“平仓”和模拟盈亏。**不碰账户、不下单、不花钱**，只积累真实未来样本。

脚本：`scripts/s0_okx_pair_forward_shadow.py`

## 启动（VPS，手动后台进程，非 cron）

```bash
cd /opt/bitdata
mkdir -p data/research/s0_okx_pair_forward_shadow
nohup python3 scripts/s0_okx_pair_forward_shadow.py \
  --loop-interval 30 \
  --state data/research/s0_okx_pair_forward_shadow/state.json \
  --records data/research/s0_okx_pair_forward_shadow/records.jsonl \
  > data/research/s0_okx_pair_forward_shadow/shadow.log 2>&1 &
echo $! > data/research/s0_okx_pair_forward_shadow/shadow.pid
```

注意：VPS 系统 python3 需要 `requests`（已确认存在）；脚本只用标准库 + requests。

## 查看进度

```bash
tail -f data/research/s0_okx_pair_forward_shadow/shadow.log
wc -l data/research/s0_okx_pair_forward_shadow/records.jsonl
cat data/research/s0_okx_pair_forward_shadow/state.json | python3 -m json.tool
```

每次开仓打印 `OPEN ...`，平仓打印 `CLOSE ... pnl=...%` 并写入 records.jsonl。

## 停止

```bash
kill $(cat data/research/s0_okx_pair_forward_shadow/shadow.pid)
```

状态会持久化，重启后从上次历史继续累积（价差 z 需要约 30 分钟预热）。

## passphrase 到位后的步骤

1. 在 VPS 写入 `/opt/bitdata/secure/okx.env`（chmod 600），内容：
   `OKX_API_KEY=...` / `OKX_API_SECRET=...` / `OKX_API_PASSPHRASE=...`
2. 运行只读验证：`python3 scripts/okx_api_probe.py`（读取环境或文件）。
3. 用 `scripts/bybit_dual_leg_execution.py --venue okx --live` 接 OKX 腿，
   单笔 <5U 小仓影子。
4. 前向影子积累到门槛后按协议晋级。
