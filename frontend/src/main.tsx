import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Bot,
  CandlestickChart,
  FileText,
  LineChart,
  Play,
  Settings,
  Shield,
  Square,
  Zap,
} from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart as ReLineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, fmt, modeLabel, stageLabel, statusLabel } from "./lib/api";
import "./styles.css";

type StatusData = {
  config: Record<string, any>;
  state: Record<string, any>;
  account: Record<string, any>;
};

type DecisionsData = {
  growth_scan?: { mode: Record<string, any>; candidates: any[]; best?: any };
  stage2_grid: any[];
  auth_error?: string;
};

type MarketData = { symbols: any[] };
type SnapshotData = { snapshots: any[] };
type LogsData = { events: any[] };

const menu = [
  { id: "overview", label: "总览", icon: Activity },
  { id: "scan", label: "多币种扫描", icon: CandlestickChart },
  { id: "pnl", label: "收益曲线", icon: LineChart },
  { id: "risk", label: "风控中心", icon: Shield },
  { id: "config", label: "配置中心", icon: Settings },
  { id: "logs", label: "系统日志", icon: FileText },
];

const intervalOptions = [
  ["5m", "5分钟"],
  ["15m", "15分钟"],
  ["1h", "1小时"],
  ["4h", "4小时"],
];

const modeOptions = [
  ["conservative", "稳健：4小时，信号少，回撤控制优先"],
  ["balanced", "均衡：1小时，信号和稳定性折中"],
  ["attack", "进攻：15分钟，小资金冲刺模式"],
  ["tournament", "锦标赛：5分钟，高风险机会模式"],
];

const defaultSymbolOptions = [
  "BTCUSDT",
  "ETHUSDT",
  "SOLUSDT",
  "BNBUSDT",
  "XRPUSDT",
  "DOGEUSDT",
  "ADAUSDT",
  "LINKUSDT",
  "AVAXUSDT",
  "SUIUSDT",
  "AAVEUSDT",
  "LABUSDT",
  "ENAUSDT",
  "WIFUSDT",
  "PEPEUSDT",
];

function useData() {
  const [status, setStatus] = useState<StatusData | null>(null);
  const [decisions, setDecisions] = useState<DecisionsData | null>(null);
  const [market, setMarket] = useState<MarketData | null>(null);
  const [snapshots, setSnapshots] = useState<any[]>([]);
  const [logs, setLogs] = useState<any[]>([]);
  const [health, setHealth] = useState<any>(null);
  const [error, setError] = useState("");

  async function refresh(light = false) {
    try {
      const [statusRes, marketRes, snapshotRes, logsRes, healthRes] = await Promise.all([
        api<StatusData>("/api/status"),
        api<MarketData>("/api/market"),
        api<SnapshotData>("/api/equity/snapshots?limit=500"),
        api<LogsData>("/api/logs?limit=100"),
        api<any>("/api/health/binance"),
      ]);
      setStatus(statusRes);
      setMarket(marketRes);
      setSnapshots(snapshotRes.snapshots || []);
      setLogs(logsRes.events || []);
      setHealth(healthRes);
      if (!light) {
        setDecisions(await api<DecisionsData>("/api/decisions"));
      }
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    refresh();
    const fast = window.setInterval(() => refresh(true), 10000);
    const slow = window.setInterval(() => refresh(), 60000);
    return () => {
      window.clearInterval(fast);
      window.clearInterval(slow);
    };
  }, []);

  return { status, decisions, market, snapshots, logs, health, error, refresh };
}

function MetricCard({ title, value, sub, tone }: { title: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className={`metric-card ${tone || ""}`}>
      <span>{title}</span>
      <strong>{value}</strong>
      {sub && <small>{sub}</small>}
    </div>
  );
}

function App() {
  const [active, setActive] = useState("overview");
  const [actionError, setActionError] = useState("");
  const data = useData();
  const status = data.status;
  const candidates = data.decisions?.growth_scan?.candidates || [];
  const mode = data.decisions?.growth_scan?.mode || {};
  const account = status?.account || {};
  const state = status?.state || {};
  const config = status?.config || {};

  const chartData = useMemo(
    () =>
      data.snapshots.map((item) => ({
        time: new Date(item.ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
        equity: item.equity,
        available: item.available_balance,
        unrealized: item.unrealized_pnl,
      })),
    [data.snapshots],
  );

  async function control(action: string, confirmation = "") {
    try {
      await api("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, confirmation }),
      });
      setActionError("");
      await data.refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function saveConfig(payload: Record<string, any>) {
    try {
      await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setActionError("");
      await data.refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Zap size={24} /></div>
          <div>
            <strong>Bitdata</strong>
            <span>智能合约策略控制台</span>
          </div>
        </div>
        <nav>
          {menu.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} className={active === item.id ? "active" : ""} onClick={() => setActive(item.id)}>
                <Icon size={18} />
                {item.label}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <Bot size={16} />
          <span>{config.dry_run ? "模拟交易中" : "实盘模式已开启"}</span>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <div className="eyebrow">LIVE OPS PANEL</div>
            <h1>{menu.find((item) => item.id === active)?.label}</h1>
            <p>
              当前模式：{modeLabel[mode.mode] || modeLabel[config.growth_mode] || "-"} · 周期：{mode.interval || "-"} · Binance：
              {data.health?.ok ? "正常" : "异常"}
            </p>
          </div>
          <div className="top-actions">
            <button className="secondary" onClick={() => data.refresh()}>刷新</button>
            <button className="success" onClick={() => control("start", "START_BOT")}><Play size={16} />启动</button>
            <button className="danger" onClick={() => control("pause")}><Square size={16} />暂停</button>
          </div>
        </header>

        {(data.error || actionError || data.decisions?.auth_error) && (
          <div className="alert"><AlertTriangle size={18} />{data.error || actionError || data.decisions?.auth_error}</div>
        )}

        {active === "overview" && (
          <section className="stack">
            <div className="metrics">
              <MetricCard title="账户权益" value={`${fmt(account.equity, 4)} U`} sub={account.equity ? "来自 Binance 账户" : "未配置 API"} />
              <MetricCard title="可用余额" value={`${fmt(account.available_balance, 4)} U`} />
              <MetricCard title="未实现盈亏" value={`${fmt(account.unrealized_pnl, 4)} U`} tone={Number(account.unrealized_pnl) >= 0 ? "positive" : "negative"} />
              <MetricCard title="机器人状态" value={statusLabel[state.bot_status] || "-"} sub={stageLabel[state.stage] || "-"} />
              <MetricCard title="交易模式" value={config.dry_run ? "模拟交易" : "实盘模式"} tone={config.dry_run ? "" : "negative"} />
              <MetricCard title="Binance API" value={data.health?.ok ? "正常" : "异常"} sub={data.health?.ok ? "公开接口可访问" : data.health?.error} />
            </div>
            <div className="grid-two">
              <div className="panel">
                <h2>最高分候选</h2>
                <CandidateTable rows={candidates.slice(0, 5)} compact />
              </div>
              <div className="panel">
                <h2>市场行情</h2>
                <MarketTable rows={data.market?.symbols || []} />
              </div>
            </div>
          </section>
        )}

        {active === "scan" && (
          <section className="panel">
            <div className="panel-head">
              <div>
                <h2>候选币排名</h2>
                <p>系统只会执行最高分且通过过滤的信号。当前回测窗口：{mode.recent_days || "-"} 天。</p>
              </div>
            </div>
            <CandidateTable rows={candidates} />
          </section>
        )}

        {active === "pnl" && (
          <section className="stack">
            <div className="metrics">
              <MetricCard title="快照数量" value={String(data.snapshots.length)} />
              <MetricCard title="最新权益" value={`${fmt(data.snapshots.at(-1)?.equity, 4)} U`} />
              <MetricCard title="最新可用" value={`${fmt(data.snapshots.at(-1)?.available_balance, 4)} U`} />
            </div>
            <div className="panel chart-panel">
              <h2>权益与盈亏曲线</h2>
              <ResponsiveContainer width="100%" height={360}>
                <ReLineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#14345a" />
                  <XAxis dataKey="time" stroke="#91a7c4" minTickGap={32} />
                  <YAxis stroke="#91a7c4" />
                  <Tooltip contentStyle={{ background: "#07111f", border: "1px solid #22d3ee", color: "#e5f6ff" }} />
                  <Legend />
                  <Line type="monotone" dataKey="equity" name="账户权益" stroke="#38bdf8" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="available" name="可用余额" stroke="#22c55e" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="unrealized" name="未实现盈亏" stroke="#f97316" strokeWidth={2} dot={false} />
                </ReLineChart>
              </ResponsiveContainer>
            </div>
          </section>
        )}

        {active === "risk" && <RiskPanel config={config} state={state} account={account} />}
        {active === "config" && <ConfigPanel config={config} onSave={saveConfig} />}
        {active === "logs" && <LogsPanel rows={data.logs} />}
      </main>
    </div>
  );
}

function CandidateTable({ rows, compact = false }: { rows: any[]; compact?: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>状态</th>
            <th>币种</th>
            <th>模式</th>
            {!compact && <th>策略</th>}
            <th>评分</th>
            <th>信号</th>
            {!compact && <th>胜率</th>}
            {!compact && <th>净收益</th>}
            <th>PF</th>
            <th>成本比</th>
            <th>成交额</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.symbol}-${index}`}>
              <td><span className={row.passed ? "pill ok" : "pill"}>{row.passed ? "通过" : "等待"}</span></td>
              <td className="symbol">{row.symbol}</td>
              <td>{modeLabel[row.mode] || row.mode}</td>
              {!compact && <td>{strategyName(row.strategy)}</td>}
              <td>{fmt(row.score, 2)}</td>
              <td>{row.signal?.signal === "LONG" ? "做多" : "等待"}</td>
              {!compact && <td>{fmt(row.recent?.win_rate, 1)}%</td>}
              {!compact && <td>{fmt(row.recent?.net_pct, 2)}%</td>}
              <td>{fmt(row.recent?.profit_factor, 2)}</td>
              <td>{fmt(row.cost_ratio, 2)}</td>
              <td>{fmt(row.ticker?.volume_usdt_b, 3)}B</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function strategyName(value: string) {
  const names: Record<string, string> = {
    default: "趋势回踩",
    attack: "进攻动量",
    breakout: "突破",
  };
  return names[value] || value || "-";
}

function MarketTable({ rows }: { rows: any[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>币种</th><th>价格</th><th>24h</th><th>成交额</th><th>资金费率</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol}>
              <td className="symbol">{row.symbol}</td>
              <td>{fmt(row.last, 5)}</td>
              <td className={Number(row.change_pct) >= 0 ? "positive-text" : "negative-text"}>{fmt(row.change_pct, 2)}%</td>
              <td>{fmt(row.volume_usdt_b, 3)}B</td>
              <td>{fmt(row.funding_pct, 5)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RiskPanel({ config, state, account }: { config: any; state: any; account: any }) {
  return (
    <section className="stack">
      <div className="metrics">
        <MetricCard title="单笔风险" value={`${fmt(config.risk_per_trade_pct)}% / ${fmt(config.attack_risk_per_trade_pct)}% / ${fmt(config.tournament_risk_per_trade_pct)}%`} sub="稳健 / 进攻 / 锦标赛" />
        <MetricCard title="每日亏损上限" value={`${fmt(config.daily_loss_limit_pct)}% / ${fmt(config.attack_daily_loss_limit_pct)}% / ${fmt(config.tournament_daily_loss_limit_pct)}%`} />
        <MetricCard title="最大回撤" value={`${fmt(config.max_drawdown_pct)}%`} />
        <MetricCard title="连续亏损" value={String(state.consecutive_losses ?? 0)} sub={`上限 ${config.max_consecutive_losses}`} />
        <MetricCard title="最大持仓" value={String(config.max_open_positions)} />
        <MetricCard title="锦标赛停止权益" value={`${fmt(config.tournament_stop_equity)} U`} />
      </div>
      <div className="panel">
        <h2>当前持仓</h2>
        <pre>{JSON.stringify(account.positions || [], null, 2)}</pre>
      </div>
    </section>
  );
}

function normalizeSymbols(value: any): string[] {
  if (Array.isArray(value)) {
    return value.map(String).map((item) => item.trim().toUpperCase()).filter(Boolean);
  }
  return String(value || "")
    .split(",")
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
}

function SymbolMultiPicker({ value, onChange }: { value: any; onChange: (symbols: string[]) => void }) {
  const selected = normalizeSymbols(value);
  const selectedSet = new Set(selected);
  const options = Array.from(new Set([...defaultSymbolOptions, ...selected])).sort();
  const [custom, setCustom] = useState("");

  function toggle(symbol: string) {
    const next = selectedSet.has(symbol)
      ? selected.filter((item) => item !== symbol)
      : [...selected, symbol];
    onChange(next);
  }

  function addCustom() {
    const additions = normalizeSymbols(custom);
    if (!additions.length) return;
    onChange(Array.from(new Set([...selected, ...additions])));
    setCustom("");
  }

  return (
    <div className="field-wide">
      <div className="field-title">
        <span>手动候选币</span>
        <small>多选框选择，系统会在这些币里优先扫描；也可以开启自动发现。</small>
      </div>
      <div className="symbol-picker">
        {options.map((symbol) => (
          <label className={`symbol-check ${selectedSet.has(symbol) ? "checked" : ""}`} key={symbol}>
            <input type="checkbox" checked={selectedSet.has(symbol)} onChange={() => toggle(symbol)} />
            <span>{symbol}</span>
          </label>
        ))}
      </div>
      <div className="custom-symbol-row">
        <input
          value={custom}
          placeholder="添加自定义币种，例如 OPUSDT,ARBUSDT"
          onChange={(event) => setCustom(event.target.value.toUpperCase())}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addCustom();
            }
          }}
        />
        <button type="button" className="secondary" onClick={addCustom}>添加</button>
      </div>
    </div>
  );
}

function ConfigPanel({ config, onSave }: { config: any; onSave: (payload: any) => Promise<void> }) {
  const [form, setForm] = useState<Record<string, any>>(config);
  useEffect(() => setForm(config), [config]);
  const update = (key: string, value: any) => setForm((prev) => ({ ...prev, [key]: value }));
  const number = (key: string, label: string, hint?: string) => (
    <label>{label}<input type="number" value={form[key] ?? ""} onChange={(event) => update(key, Number(event.target.value))} />{hint && <small>{hint}</small>}</label>
  );
  const text = (key: string, label: string, hint?: string) => (
    <label>{label}<input value={Array.isArray(form[key]) ? form[key].join(",") : form[key] ?? ""} onChange={(event) => update(key, event.target.value)} />{hint && <small>{hint}</small>}</label>
  );
  const password = (key: string, label: string, hint?: string) => (
    <label>{label}<input type="password" autoComplete="new-password" value={form[key] ?? ""} onChange={(event) => update(key, event.target.value)} />{hint && <small>{hint}</small>}</label>
  );
  const select = (key: string, label: string, options: string[][], hint?: string) => (
    <label>{label}<select value={form[key] ?? ""} onChange={(event) => update(key, event.target.value)}>{options.map(([value, name]) => <option value={value} key={value}>{name}</option>)}</select>{hint && <small>{hint}</small>}</label>
  );
  const toggle = (key: string, label: string, hint?: string) => (
    <label className="switch-row">
      <span><strong>{label}</strong>{hint && <small>{hint}</small>}</span>
      <input type="checkbox" checked={Boolean(form[key])} onChange={(event) => update(key, event.target.checked)} />
    </label>
  );
  return (
    <section className="stack">
      <div className="panel">
        <h2>基础配置</h2>
        <div className="form-grid">
          {select("growth_mode", "增长模式", modeOptions, "新手建议先用均衡或模拟观察")}
          <SymbolMultiPicker value={form.stage1_symbols} onChange={(symbols) => update("stage1_symbols", symbols)} />
          {number("max_scan_symbols", "最大扫描币种", "2GB VPS 建议 10-15")}
          {number("min_24h_volume_usdt", "最低 24h 成交额", "过滤流动性太差的币")}
          {toggle("auto_discover_symbols", "自动发现加密币", "只纳入 Binance 永续币")}
          {toggle("auto_risk_by_equity", "按权益自动切换风险", "50U 自动锦标赛，100U 后进攻")}
          {toggle("dry_run", "模拟交易", "开启时不会真实下单")}
          {toggle("live_trading_enabled", "允许实盘交易", "还需要确认短语才会实盘")}
        </div>
      </div>
      <details className="panel">
        <summary>进阶参数</summary>
        <div className="form-grid">
          {select("conservative_interval", "稳健周期", intervalOptions)}
          {select("balanced_interval", "均衡周期", intervalOptions)}
          {select("attack_interval", "进攻周期", intervalOptions)}
          {select("tournament_interval", "锦标赛周期", intervalOptions)}
          {number("risk_per_trade_pct", "稳健风险%")}
          {number("attack_risk_per_trade_pct", "进攻风险%")}
          {number("tournament_risk_per_trade_pct", "锦标赛风险%")}
          {number("estimated_slippage_pct", "估算滑点%")}
          {number("min_expected_profit_cost_ratio", "最低收益/成本比")}
          {number("min_profit_factor", "最低 PF")}
        </div>
      </details>
      <details className="panel danger-zone">
        <summary>危险配置</summary>
        <div className="form-grid">
          <div className="field-wide account-warning">
            <strong>Binance API 配置</strong>
            <small>只需要开启“允许读取”和“U 本位合约交易/合约交易”。不要开启提现、万向划转、现货杠杆、预测交易。建议绑定 VPS IP：203.248.94.70。保存后页面只显示打码 Key，Secret 不会明文回显。</small>
          </div>
          <div className="field-wide credential-field">{text("api_key", "Binance API Key", "保存后会自动打码显示；如果不修改，保持打码值即可")}</div>
          <div className="field-wide credential-field">{password("api_secret", "Binance API Secret", "第一次配置时填写完整 Secret；保存后显示 ********")}</div>
          <div className="field-wide credential-field">{text("binance_base_url", "Binance 合约接口地址", "默认 https://fapi.binance.com，一般不用改")}</div>
          {toggle("allow_short", "允许做空", "不建议新手开启")}
          {text("live_trading_confirmation", "实盘确认短语", "必须填写 ENABLE_LIVE_TRADING")}
          {number("max_drawdown_pct", "最大回撤%")}
          {number("tournament_stop_equity", "锦标赛停止权益")}
          {number("tournament_max_leverage", "锦标赛最大杠杆")}
          {number("tournament_max_symbol_margin_pct", "锦标赛保证金上限%")}
        </div>
      </details>
      <button className="primary" onClick={() => onSave(form)}>保存配置</button>
    </section>
  );
}

function LogsPanel({ rows }: { rows: any[] }) {
  return (
    <section className="panel">
      <h2>事件日志</h2>
      <div className="log-list">
        {rows.map((row) => (
          <div className={`log-item ${row.level}`} key={row.id}>
            <span>{new Date(row.ts).toLocaleString("zh-CN")}</span>
            <strong>{row.category}</strong>
            <p>{row.message}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
