const fmt = (value, digits = 2) => Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });

const statusText = { running: "运行中", paused: "已暂停" };
const stageText = { growth: "阶段一：滚仓增长", grid: "阶段二：合约网格" };
const signalText = { LONG: "做多", WAIT: "等待" };
const actionText = { OPEN_LONG: "开多", WAIT: "等待" };
const modeText = { conservative: "稳健", balanced: "均衡", attack: "进攻", tournament: "锦标赛" };
const strategyText = { default: "稳健回踩", attack: "进攻回踩/动量", breakout: "突破" };
const resultModeText = { dry_run: "模拟执行", live: "实盘执行", blocked: "已阻止", none: "无操作" };
const reasonText = {
  allowed: "允许执行",
  passed: "通过过滤",
  filters_not_passed: "过滤未通过",
  no_candidate_passed: "没有候选币通过",
  no_signal: "没有交易信号",
  filters_not_aligned: "条件未满足",
  not_enough_data: "K线数据不足",
  trend_pullback_recovered: "趋势回踩后收回",
  attack_pullback_or_momentum: "进攻回踩或动量",
  breakout: "突破信号",
  bot_paused: "机器人已暂停",
  cooldown_active: "冷却中",
  consecutive_loss_limit: "连续亏损达到上限",
  daily_loss_limit: "每日亏损达到上限",
  max_drawdown_limit: "最大回撤达到上限",
  max_open_positions: "持仓数量达到上限",
  tournament_stop_equity: "低于锦标赛停止权益",
  account_unavailable: "账户不可用",
  volatility_too_low: "波动太低",
  volatility_too_high: "波动太高",
  grid_ready: "网格计划可用",
};

const zh = (dict, value) => dict[value] || value || "-";
const zhStatus = (value) => zh(statusText, value);
const zhStage = (value) => zh(stageText, value);
const zhSignal = (value) => zh(signalText, value);
const zhAction = (value) => zh(actionText, value);
const zhReason = (value) => zh(reasonText, value);

function renderExecutionResult(result) {
  const lines = [];
  if (result.mode) lines.push(`模式：${resultModeText[result.mode] || result.mode}`);
  if (result.message) lines.push(`说明：${result.message}`);
  if (result.symbol) lines.push(`币种：${result.symbol}`);
  if (typeof result.count === "number") lines.push(`订单数量：${result.count}`);
  const order = result.order || result.entry_order || null;
  if (order) lines.push(`订单：${JSON.stringify(order, null, 2)}`);
  if (result.orders) lines.push(`拟挂单：${JSON.stringify(result.orders, null, 2)}`);
  if (result.placed) lines.push(`已挂单：${JSON.stringify(result.placed, null, 2)}`);
  return lines.join("\n\n") || JSON.stringify(result, null, 2);
}

async function getJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderTable(target, headers, rows) {
  document.querySelector(target).innerHTML = `
    <table>
      <thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody>
    </table>
  `;
}

async function loadMarket() {
  const data = await getJson("/api/market");
  renderTable("#market", ["币种", "价格", "24小时涨跌", "成交额（十亿U）", "资金费率"], data.symbols.map((row) => [
    row.symbol,
    fmt(row.last, 4),
    `${fmt(row.change_pct, 2)}%`,
    fmt(row.volume_usdt_b, 3),
    `${fmt(row.funding_pct, 5)}%`,
  ]));
}

async function loadStatus() {
  const data = await getJson("/api/status");
  const state = data.state;
  const account = data.account;
  const mode = data.config?.growth_mode || "-";
  document.querySelector("#status").innerHTML = [
    ["运行状态", zhStatus(state.bot_status)],
    ["当前阶段", zhStage(state.stage)],
    ["增长模式", modeText[mode] || mode],
    ["账户权益", account.equity ?? "未配置API"],
    ["可用余额", account.available_balance ?? "未配置API"],
    ["未实现盈亏", account.unrealized_pnl ?? "未配置API"],
    ["连续亏损", state.consecutive_losses],
    ["最后错误", state.last_error || "-"],
  ].map(([label, value]) => `<div class="metric">${label}<strong>${typeof value === "number" ? fmt(value, 4) : value}</strong></div>`).join("");
}

async function loadSignals() {
  const data = await getJson("/api/signals");
  document.querySelector("#signals").innerHTML = data.signals.map((item) => `
    <article class="signal ${item.signal === "LONG" ? "long" : ""}">
      <h3>${item.symbol} <span class="badge">${zhSignal(item.signal)}</span></h3>
      <p>原因：${zhReason(item.reason)}</p>
      <p>价格：${fmt(item.last_price, 4)}</p>
      <p>EMA：${fmt(item.ema_fast, 4)} / ${fmt(item.ema_slow, 4)}</p>
      <p>ATR：${fmt(item.atr, 4)}</p>
      ${item.stop ? `<p>止损：${fmt(item.stop, 4)} · 止盈：${fmt(item.take_profit, 4)}</p>` : ""}
    </article>
  `).join("");
}

async function loadBacktest() {
  const data = await getJson("/api/backtest");
  const rows = data.results.map(({ summary }) => [
    summary.symbol,
    summary.trades,
    summary.wins,
    `${fmt(summary.win_rate, 2)}%`,
    `${fmt(summary.net_return_unlevered_pct, 2)}%`,
    `${fmt(summary.avg_trade_pct, 3)}%`,
    summary.profit_factor ? fmt(summary.profit_factor, 2) : "-",
  ]);
  renderTable("#backtest", ["币种", "交易笔数", "盈利笔数", "胜率", "未杠杆净收益", "单笔平均收益", "盈亏比"], rows);
}

function stage1Row(item) {
  const signal = item.signal || {};
  const recent = item.recent || item.candidate?.recent || {};
  const ticker = item.ticker || item.candidate?.ticker || {};
  return [
    item.passed ? "通过" : "未通过",
    item.symbol || "-",
    modeText[item.mode] || item.mode || "-",
    strategyText[item.strategy] || item.strategy || "-",
    item.score ?? "-",
    zhSignal(signal.signal),
    zhReason(signal.reason || item.reason),
    recent.trades ?? "-",
    recent.win_rate !== undefined ? `${fmt(recent.win_rate, 1)}%` : "-",
    recent.net_pct !== undefined ? `${fmt(recent.net_pct, 2)}%` : "-",
    recent.profit_factor !== undefined ? fmt(recent.profit_factor, 2) : "-",
    ticker.volume_usdt_b !== undefined ? fmt(ticker.volume_usdt_b, 3) : "-",
  ];
}

async function loadDecisions() {
  const data = await getJson("/api/decisions");
  const candidates = data.growth_scan?.candidates || data.stage1 || [];
  const stage1Rows = candidates.slice(0, 15).map(stage1Row);
  const gridRows = data.stage2_grid.map((item) => [
    "网格",
    item.symbol ?? "-",
    "-",
    "-",
    "-",
    item.status === "READY" ? "可执行" : "等待",
    zhReason(item.reason ?? "grid_ready"),
    item.levels ?? "-",
    "-",
    item.deploy_equity ? fmt(item.deploy_equity, 2) : "-",
    "-",
    `${item.lower ? fmt(item.lower, 4) : "-"} / ${item.upper ? fmt(item.upper, 4) : "-"}`,
  ]);
  renderTable(
    "#decisions",
    ["状态", "币种", "模式", "策略", "评分", "信号", "原因", "近期交易", "胜率", "净收益", "PF", "成交额/区间"],
    stage1Rows.concat(gridRows),
  );
}

async function loadConfig() {
  const config = await getJson("/api/config");
  const form = document.querySelector("#configForm");
  for (const [key, value] of Object.entries(config)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (Array.isArray(value)) input.value = value.join(",");
    else input.value = value ?? "";
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {};
  for (const element of form.elements) {
    if (!element.name) continue;
    if (element.name === "api_secret" && !element.value) continue;
    if (element.type === "checkbox") payload[element.name] = element.checked;
    else if (["symbols", "stage1_symbols", "stage2_symbols"].includes(element.name)) {
      payload[element.name] = element.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
    } else if (element.type === "number") payload[element.name] = Number(element.value);
    else payload[element.name] = element.value;
  }
  await getJson("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadMarket(), loadSignals(), loadDecisions(), loadBacktest(), loadConfig()]);
}

document.querySelector("#refreshBtn").addEventListener("click", refreshAll);
document.querySelector("#configForm").addEventListener("submit", saveConfig);
document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    await getJson("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: button.dataset.action, confirmation: button.dataset.confirmation || "" }),
    });
    await refreshAll();
  });
});
document.querySelector("#executeStage1").addEventListener("click", async () => {
  const result = await getJson("/api/execute/stage1", { method: "POST" });
  document.querySelector("#executeResult").textContent = renderExecutionResult(result);
  await refreshAll();
});
document.querySelector("#executeGrid").addEventListener("click", async () => {
  const config = await getJson("/api/config");
  const symbol = (config.stage2_symbols?.[0] || "BTCUSDT").toUpperCase();
  const result = await getJson(`/api/execute/grid?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
  document.querySelector("#executeResult").textContent = renderExecutionResult(result);
  await refreshAll();
});
refreshAll().catch((err) => {
  document.body.insertAdjacentHTML("beforeend", `<pre class="panel">页面加载失败：${err.message}</pre>`);
});
