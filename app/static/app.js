const fmt = (value, digits = 2) => Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });

async function getJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(detail);
  }
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
  renderTable("#market", ["币种", "价格", "24h%", "成交额(十亿U)", "资金费率%"], data.symbols.map((row) => [
    row.symbol,
    fmt(row.last, 4),
    fmt(row.change_pct, 2),
    fmt(row.volume_usdt_b, 3),
    fmt(row.funding_pct, 5),
  ]));
}

async function loadStatus() {
  const data = await getJson("/api/status");
  const state = data.state;
  const account = data.account;
  document.querySelector("#status").innerHTML = [
    ["运行状态", state.bot_status],
    ["当前阶段", state.stage],
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
      <h3>${item.symbol} <span class="badge">${item.signal}</span></h3>
      <p>原因：${item.reason}</p>
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
  renderTable("#backtest", ["币种", "交易数", "胜笔", "胜率", "未杠杆净收益", "单笔均值", "Profit Factor"], rows);
}

async function loadDecisions() {
  const data = await getJson("/api/decisions");
  const stage1Rows = data.stage1.map((item) => [
    "阶段一",
    item.symbol,
    item.action,
    item.signal?.reason ?? "-",
    item.risk?.reason ?? "-",
    item.quantity ? fmt(item.quantity, 5) : "-",
    item.estimated_notional ? fmt(item.estimated_notional, 2) : "-",
  ]);
  const gridRows = data.stage2_grid.map((item) => [
    "阶段二网格",
    item.symbol ?? "-",
    item.status,
    item.reason ?? "grid_ready",
    `${item.lower ? fmt(item.lower, 4) : "-"} / ${item.upper ? fmt(item.upper, 4) : "-"}`,
    item.levels ?? "-",
    item.deploy_equity ? fmt(item.deploy_equity, 2) : "-",
  ]);
  renderTable("#decisions", ["阶段", "币种", "动作", "信号/原因", "风控/区间", "数量/层数", "名义/投入"], stage1Rows.concat(gridRows));
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
      body: JSON.stringify({
        action: button.dataset.action,
        confirmation: button.dataset.confirmation || "",
      }),
    });
    await refreshAll();
  });
});
document.querySelector("#executeStage1").addEventListener("click", async () => {
  const config = await getJson("/api/config");
  const symbol = (config.stage1_symbols?.[0] || "SOLUSDT").toUpperCase();
  const result = await getJson(`/api/execute/stage1?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
  document.querySelector("#executeResult").textContent = JSON.stringify(result, null, 2);
  await refreshAll();
});
document.querySelector("#executeGrid").addEventListener("click", async () => {
  const config = await getJson("/api/config");
  const symbol = (config.stage2_symbols?.[0] || "BTCUSDT").toUpperCase();
  const result = await getJson(`/api/execute/grid?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
  document.querySelector("#executeResult").textContent = JSON.stringify(result, null, 2);
  await refreshAll();
});
refreshAll().catch((err) => {
  document.body.insertAdjacentHTML("beforeend", `<pre class="panel">${err.message}</pre>`);
});
