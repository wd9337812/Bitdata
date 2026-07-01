# VPS 时间同步方案

## 背景

Binance 私有接口要求请求时间戳与服务器时间偏差不能过大。若 VPS 时间漂移，会返回：

```text
code -1021: Timestamp for this request is outside of the recvWindow.
```

## 系统层方案

VPS 使用 `chrony` 做时间同步：

```bash
apt update
apt install -y chrony
systemctl enable --now chrony
chronyc tracking
chronyc sources -v
```

目标状态：

- `Leap status` 为 `Normal`
- `System time` 偏差尽量接近 0
- `chronyc sources -v` 有可用时间源

## 程序层方案

- Binance 签名请求默认带 `recvWindow=10000`。
- Dashboard 的 Binance API 卡片显示本地时间与 Binance serverTime 的偏差。
- 启动实盘前，如果时间偏差超过 `3000ms`，禁止启动。
- runner 遇到 `-1021` 时不永久暂停，只记录 `runner_time_sync` 并下一轮重试。

## 处理原则

时间同步异常属于基础设施抖动，不应和下单保护单失败同级处理。下单/保护单异常仍然会暂停机器人；时间偏差异常会等待 chrony 恢复后继续。
