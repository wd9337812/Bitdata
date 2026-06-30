import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CandlestickChart,
  FileText,
  LineChart,
  Play,
  Settings,
  Shield,
  Square,
  Wallet,
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
};

type MarketData = { symbols: any[] };
type SnapshotData = { snapshots: any[] };
type LogsData = { events: any[] };

const menu = [
  { id: "overview", label: "总览", icon: Activity },
  { id: "scan", label: "多币种扫描", icon: CandlestickChart },
  { id: "pnl", label: "收益曲线", icon: LineChart },
  { id: "risk", label: "风控中心", icon: Shield },
  { id: "config", label: "配置", icon: Settings },
  { id: "logs", label: "日志", icon: FileText },
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
        const decisionsRes = await api<DecisionsData>("/api/decisions");
        setDecisions(decisionsRes);
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
        time: new Date(item.ts).toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "2-digit", day: "2-digit" }),
        equity: item.equity,
        available: item.available_balance,
        unrealized: item.unrealized_pnl,
      })),
    [data.snapshots],
  );

  async function control(action: string, confirmation = "") {
    await api("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, confirmation }),
    });
    await data.refresh();
  }

  async function saveConfig(payload: Record<string, any>) {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await data.refresh();
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <Bot size={28} />
          <div>
            <strong>Bitdata</strong>
            <span>合约策略控制台</span>
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
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <h1>{menu.find((item) => item.id === active)?.label}</h1>
            <p>当前模式：{modeLabel[mode.mode] || mode.mode || modeLabel[config.growth_mode] || "-"} · Binance：{data.health?.ok ? "正常" : "异常"}</p>
          </div>
          <div className="top-actions">
            <button className="secondary" onClick={() => data.refresh()}>刷新</button>
            <button className="success" onClick={() => control("start", "START_BOT")}><Play size={16} />启动</button>
            <button className="danger" onClick={() => control("pause")}><Square size={16} />暂停</button>
          </div>
        </header>

        {data.error && <div className="alert"><AlertTriangle size={18} />{data.error}</div>}

        {active === "overview" && (
          <section className="stack">
            <div className="metrics">
              <MetricCard title="账户权益" value={`${fmt(account.equity, 4)} U`} sub={account.equity ? "来自 Binance 账户" : "未配置 API，显示为空"} />
              <MetricCard title="可用余额" value={`${fmt(account.available_balance, 4)} U`} />
              <MetricCard title="未实现盈亏" value={`${fmt(account.unrealized_pnl, 4)} U`} tone={Number(account.unrealized_pnl) >= 0 ? "positive" : "negative"} />
              <MetricCard title="机器人状态" value={statusLabel[state.bot_status] || "-"} sub={stageLabel[state.stage] || "-"} />
              <MetricCard title="实盘开关" value={config.dry_run ? "模拟交易" : "实盘模式"} tone={config.dry_run ? "" : "negative"} />
              <MetricCard title="Binance API" value={data.health?.ok ? "正常" : "异常"} sub={data.health?.ok ? "公开接口 200" : data.health?.error} />
            </div>
            <div className="panel">
              <h2>最高分候选</h2>
              <CandidateTable rows={candidates.slice(0, 5)} compact />
            </div>
            <div className="panel">
              <h2>市场行情</h2>
              <MarketTable rows={data.market?.symbols || []} />
            </div>
          </section>
        )}

        {active === "scan" && (
          <section className="panel">
            <div className="panel-head">
              <div>
                <h2>候选币排名</h2>
                <p>只执行最高分且通过过滤的信号。当前周期：{mode.interval || "-"}，回测窗口：{mode.recent_days || "-"} 天。</p>
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
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="time" minTickGap={32} />
                  <YAxis />
                  <Tooltip />
                  <Legend />
                  <Line type="monotone" dataKey="equity" name="账户权益" stroke="#2563eb" dot={false} />
                  <Line type="monotone" dataKey="available" name="可用余额" stroke="#16a34a" dot={false} />
                  <Line type="monotone" dataKey="unrealized" name="未实现盈亏" stroke="#dc2626" dot={false} />
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
              <td>{row.symbol}</td>
              <td>{modeLabel[row.mode] || row.mode}</td>
              {!compact && <td>{row.strategy}</td>}
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

function MarketTable({ rows }: { rows: any[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>币种</th><th>价格</th><th>24h</th><th>成交额</th><th>资金费率</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol}>
              <td>{row.symbol}</td>
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

function ConfigPanel({ config, onSave }: { config: any; onSave: (payload: any) => Promise<void> }) {
  const [form, setForm] = useState<Record<string, any>>(config);
  useEffect(() => setForm(config), [config]);
  const update = (key: string, value: any) => setForm((prev) => ({ ...prev, [key]: value }));
  const number = (key: string, label: string) => (
    <label>{label}<input type="number" value={form[key] ?? ""} onChange={(event) => update(key, Number(event.target.value))} /></label>
  );
  const text = (key: string, label: string) => (
    <label>{label}<input value={Array.isArray(form[key]) ? form[key].join(",") : form[key] ?? ""} onChange={(event) => update(key, key.endsWith("symbols") || key === "symbols" ? event.target.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean) : event.target.value)} /></label>
  );
  const check = (key: string, label: string) => (
    <label className="check"><input type="checkbox" checked={Boolean(form[key])} onChange={(event) => update(key, event.target.checked)} />{label}</label>
  );
  return (
    <section className="stack">
      <div className="panel">
        <h2>基础配置</h2>
        <div className="form-grid">
          {text("growth_mode", "增长模式")}
          {text("stage1_symbols", "手动候选币")}
          {number("max_scan_symbols", "最大扫描币种")}
          {number("min_24h_volume_usdt", "最低24h成交额")}
          {check("auto_discover_symbols", "自动发现加密币")}
          {check("auto_risk_by_equity", "按权益自动切换风险")}
          {check("dry_run", "模拟交易")}
          {check("live_trading_enabled", "允许实盘交易")}
        </div>
      </div>
      <details className="panel">
        <summary>进阶参数</summary>
        <div className="form-grid">
          {text("conservative_interval", "稳健周期")}
          {text("balanced_interval", "均衡周期")}
          {text("attack_interval", "进攻周期")}
          {text("tournament_interval", "锦标赛周期")}
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
          {check("allow_short", "允许做空")}
          {text("live_trading_confirmation", "实盘确认短语")}
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
