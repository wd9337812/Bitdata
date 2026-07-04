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

type StatusData = { config: Record<string, any>; state: Record<string, any>; account: Record<string, any> };
type DecisionsData = { growth_scan?: { mode: Record<string, any>; candidates: any[]; best?: any }; stage2_grid: any[]; auth_error?: string };
type MarketData = { symbols: any[] };
type SnapshotData = { snapshots: any[] };
type LogsData = { events: any[] };
type LiveLearningData = { scores: any[] };

const menu = [
  { id: "overview", label: "总览", icon: Activity },
  { id: "scan", label: "多币种扫描", icon: CandlestickChart },
  { id: "learning", label: "实盘学习", icon: Zap },
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
  ["conservative", "稳健：4小时，信号少，控制回撤优先"],
  ["balanced", "均衡：1小时，信号和稳定性折中"],
  ["attack", "进攻：15分钟，小资金进攻模式"],
  ["tournament", "锦标赛：5分钟，高风险机会模式"],
];

modeOptions.push(["tournament_sprint", "锦标赛冲刺：5分钟，更高频、更高风险"]);

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
  "WLDUSDT",
  "PEPEUSDT",
];

function useData() {
  const [status, setStatus] = useState<StatusData | null>(null);
  const [decisions, setDecisions] = useState<DecisionsData | null>(null);
  const [market, setMarket] = useState<MarketData | null>(null);
  const [snapshots, setSnapshots] = useState<any[]>([]);
  const [logs, setLogs] = useState<any[]>([]);
  const [liveLearning, setLiveLearning] = useState<any[]>([]);
  const [health, setHealth] = useState<any>(null);
  const [error, setError] = useState("");

  async function refresh(light = false, throwOnError = false) {
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
        const learningRes = await api<LiveLearningData>("/api/live-learning?limit=100");
        setLiveLearning(learningRes.scores || []);
      }
      setError("");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      if (throwOnError) throw new Error(message);
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

  return { status, decisions, market, snapshots, logs, liveLearning, health, error, refresh };
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
  const [actionNotice, setActionNotice] = useState("");
  const data = useData();
  const status = data.status;
  const candidates = data.decisions?.growth_scan?.candidates || [];
  const best = data.decisions?.growth_scan?.best || candidates[0];
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

  async function handleRefresh() {
    try {
      await data.refresh(false, true);
      setActionError("");
      setActionNotice("刷新成功，页面数据已更新。");
    } catch (err) {
      setActionNotice("");
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function control(action: string, confirmation = "") {
    try {
      await api("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, confirmation }),
      });
      setActionError("");
      setActionNotice(action === "start" ? "启动成功，机器人已进入运行状态。" : "暂停成功，机器人已停止自动执行。");
      await data.refresh();
    } catch (err) {
      setActionNotice("");
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
      setActionNotice("配置已保存。");
      await data.refresh();
    } catch (err) {
      setActionNotice("");
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function testBinanceApi() {
    try {
      const result = await api<any>("/api/config/test-binance", { method: "POST" });
      setActionError("");
      setActionNotice(`Binance API 测试通过，账户权益 ${fmt(result.account?.equity, 4)} U。`);
      await data.refresh();
    } catch (err) {
      setActionNotice("");
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  async function syncLiveLearning() {
    try {
      const result = await api<any>("/api/live-learning/sync", { method: "POST" });
      setActionError("");
      setActionNotice(`实盘信用分同步成功：归因 ${result.records || 0} 笔交易，生成 ${result.scores?.length || 0} 个币种方向评分。`);
      await data.refresh();
    } catch (err) {
      setActionNotice("");
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
            <button className="secondary" onClick={handleRefresh}>刷新</button>
            <button className="success" onClick={() => control("start", "START_BOT")}><Play size={16} />启动</button>
            <button className="danger" onClick={() => control("pause")}><Square size={16} />暂停</button>
          </div>
        </header>

        {(data.error || actionError || data.decisions?.auth_error) && (
          <div className="alert"><AlertTriangle size={18} />{data.error || actionError || data.decisions?.auth_error}</div>
        )}
        {actionNotice && <div className="notice">{actionNotice}</div>}

        {active === "overview" && (
          <section className="stack">
            <div className="metrics">
              <MetricCard title="账户权益" value={`${fmt(account.equity, 4)} U`} sub={account.equity ? "来自 Binance 合约账户" : "未配置 API"} />
              <MetricCard title="可用余额" value={`${fmt(account.available_balance, 4)} U`} />
              <MetricCard title="未实现盈亏" value={`${fmt(account.unrealized_pnl, 4)} U`} tone={Number(account.unrealized_pnl) >= 0 ? "positive" : "negative"} />
              <MetricCard title="机器人状态" value={statusLabel[state.bot_status] || "-"} sub={stageLabel[state.stage] || "-"} />
              <MetricCard title="交易模式" value={config.dry_run ? "模拟交易" : "实盘模式"} tone={config.dry_run ? "" : "negative"} />
              <MetricCard
                title="Binance API"
                value={data.health?.ok ? "正常" : "异常"}
                sub={data.health?.offsetMs !== undefined ? `时间偏差 ${fmt(data.health.offsetMs, 0)} ms` : data.health?.error}
              />
            </div>
            <SignalExplain best={best} />
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
                <p>系统会优先执行最高分且通过过滤的信号。当前回测窗口：{mode.recent_days || "-"} 天。</p>
              </div>
            </div>
            <CandidateTable rows={candidates} />
          </section>
        )}

        {active === "learning" && <LiveLearningPanel rows={data.liveLearning} onSync={syncLiveLearning} />}

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
        {active === "config" && <ConfigPanel config={config} onSave={saveConfig} onTestApi={testBinanceApi} />}
        {active === "logs" && <LogsPanel rows={data.logs} />}
      </main>
    </div>
  );
}

function SignalExplain({ best }: { best?: any }) {
  if (!best) {
    return <div className="panel"><h2>当前策略解释</h2><p>还没有扫描结果。</p></div>;
  }
  const signal = best.signal || {};
  return (
    <div className="panel signal-explain">
      <h2>当前策略解释</h2>
      <div className="explain-grid">
        <div><span>最高候选</span><strong>{best.symbol || "-"}</strong></div>
        <div><span>方向</span><strong>{signalLabel(best.direction || signal.signal)}</strong></div>
        <div><span>信号类型</span><strong>{best.entry_type_label || signal.entry_type_label || "观察"}</strong></div>
        <div><span>综合评分</span><strong>{fmt(best.score, 2)}</strong></div>
        <div><span>离触发价</span><strong>{fmt(signal.distance_to_trigger_pct, 3)}%</strong></div>
        <div><span>当前结论</span><strong>{best.passed ? "允许执行" : "继续等待"}</strong></div>
      </div>
      <p>{best.decision_reason || signal.reason || best.reason || "等待下一轮扫描。"}</p>
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
            <th>方向</th>
            <th>信号类型</th>
            <th>评分</th>
            <th>实盘信用</th>
            {!compact && <th>不开仓原因</th>}
            {!compact && <th>距离触发</th>}
            {!compact && <th>胜率</th>}
            {!compact && <th>净收益</th>}
            <th>PF</th>
            <th>成本比</th>
            <th>风险%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.symbol}-${row.direction}-${index}`}>
              <td><span className={row.passed ? "pill ok" : "pill"}>{row.passed ? "通过" : "等待"}</span></td>
              <td className="symbol">{row.symbol}</td>
              <td>{signalLabel(row.direction || row.signal?.signal)}</td>
              <td>{row.entry_type_label || row.signal?.entry_type_label || "-"}</td>
              <td>{fmt(row.score, 2)}</td>
              <td>{row.live_credit ? `${fmt(row.live_credit.score, 1)} · ${row.live_credit.status_label || "-"}` : "-"}</td>
              {!compact && <td className="reason-cell">{row.decision_reason || row.reason}</td>}
              {!compact && <td>{fmt(row.signal?.distance_to_trigger_pct, 3)}%</td>}
              {!compact && <td>{fmt(row.recent?.win_rate, 1)}%</td>}
              {!compact && <td>{fmt(row.recent?.net_pct, 2)}%</td>}
              <td>{fmt(row.recent?.profit_factor, 2)}</td>
              <td>{fmt(row.cost_ratio, 2)}</td>
              <td>{fmt(row.risk_pct, 2)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LiveLearningPanel({ rows, onSync }: { rows: any[]; onSync: () => Promise<void> }) {
  const totalNet = rows.reduce((sum, row) => sum + Number(row.net_pnl || 0), 0);
  const highPower = rows.filter((row) => Number(row.risk_multiplier || 0) >= 1).length;
  const fuse = rows.filter((row) => Number(row.risk_multiplier || 0) <= 0).length;
  return (
    <section className="stack">
      <div className="metrics">
        <MetricCard title="已学习方向" value={String(rows.length)} sub="按币种 + 做多/做空分开统计" />
        <MetricCard title="正常以上倍率" value={String(highPower)} sub="倍率 >= 1，可按锦标赛正常火力执行" />
        <MetricCard title="熔断方向" value={String(fuse)} sub="倍率为 0，等待自然恢复" tone={fuse ? "negative" : ""} />
        <MetricCard title="学习净盈亏" value={`${fmt(totalNet, 4)} U`} tone={totalNet >= 0 ? "positive" : "negative"} />
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>币种实盘信用分</h2>
            <p>信用分按线性倍率控制仓位：倍率 = 信用分 / 50，最高 2x；亏损会降仓，时间会自然恢复到 50 分。</p>
          </div>
          <button className="secondary" onClick={onSync}>同步历史信用分</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>币种</th>
                <th>方向</th>
                <th>信用分</th>
                <th>开仓倍率</th>
                <th>状态</th>
                <th>交易数</th>
                <th>胜率</th>
                <th>净盈亏</th>
                <th>PF</th>
                <th>连续盈亏</th>
                <th>平均持仓</th>
                <th>自然恢复</th>
                <th>冷却结束</th>
                <th>学习备注</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.symbol}-${row.direction}`}>
                  <td className="symbol">{row.symbol}</td>
                  <td>{signalLabel(row.direction)}</td>
                  <td>{fmt(row.score, 1)}{row.raw_score !== undefined && Number(row.recovery_points || 0) > 0 ? `（原 ${fmt(row.raw_score, 1)}）` : ""}</td>
                  <td>{fmt(row.risk_multiplier, 2)}x</td>
                  <td><span className={row.status === "strong" ? "pill ok" : row.status === "penalty" ? "pill bad" : "pill"}>{row.status_label}</span></td>
                  <td>{row.closed_trades}</td>
                  <td>{fmt(row.win_rate, 1)}%</td>
                  <td className={Number(row.net_pnl) >= 0 ? "positive-text" : "negative-text"}>{fmt(row.net_pnl, 4)} U</td>
                  <td>{fmt(row.profit_factor, 2)}</td>
                  <td>{row.consecutive_wins > 0 ? `连赢 ${row.consecutive_wins}` : row.consecutive_losses > 0 ? `连亏 ${row.consecutive_losses}` : "-"}</td>
                  <td>{fmt(Number(row.avg_hold_seconds || 0) / 60, 1)} 分钟</td>
                  <td>{Number(row.recovery_points || 0) > 0 ? `已恢复 +${fmt(row.recovery_points, 1)}` : row.next_recovery_at ? new Date(row.next_recovery_at).toLocaleString("zh-CN") : "-"}</td>
                  <td>{row.penalty_until ? new Date(row.penalty_until).toLocaleString("zh-CN") : "-"}</td>
                  <td className="reason-cell">{(row.notes || []).join("；")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function signalLabel(value: string) {
  if (value === "LONG") return "做多";
  if (value === "SHORT") return "做空";
  return "等待";
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
        <MetricCard title="标准风险" value={`${fmt(config.risk_per_trade_pct)}% / ${fmt(config.attack_risk_per_trade_pct)}% / ${fmt(config.tournament_risk_per_trade_pct)}% / ${fmt(config.tournament_sprint_risk_per_trade_pct)}%`} sub="稳健 / 进攻 / 锦标赛 / 冲刺" />
        <MetricCard title="抢跑风险折扣" value={`${fmt(config.preemptive_risk_multiplier, 2)} / ${fmt(config.short_preemptive_risk_multiplier, 2)}`} sub="做多 / 做空" />
        <MetricCard title="每日亏损上限" value={`${fmt(config.daily_loss_limit_pct)}% / ${fmt(config.attack_daily_loss_limit_pct)}% / ${fmt(config.tournament_daily_loss_limit_pct)}% / ${fmt(config.tournament_sprint_daily_loss_limit_pct)}%`} />
        <MetricCard title="最大回撤" value={`${fmt(config.max_drawdown_pct)}%`} />
        <MetricCard title="最大持仓" value={`${config.max_open_positions} / 冲刺 ${config.tournament_sprint_max_open_positions || 1}`} />
        <MetricCard title="同币冷却" value={`${fmt(config.symbol_cooldown_minutes, 0)} 分钟`} />
      </div>
      <div className="panel">
        <h2>当前持仓</h2>
        <pre>{JSON.stringify(account.positions || [], null, 2)}</pre>
      </div>
    </section>
  );
}

function normalizeSymbols(value: any): string[] {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim().toUpperCase()).filter(Boolean);
  return String(value || "").split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
}

function SymbolMultiPicker({ value, onChange }: { value: any; onChange: (symbols: string[]) => void }) {
  const selected = normalizeSymbols(value);
  const selectedSet = new Set(selected);
  const options = Array.from(new Set([...defaultSymbolOptions, ...selected])).sort();
  const [custom, setCustom] = useState("");

  function toggle(symbol: string) {
    onChange(selectedSet.has(symbol) ? selected.filter((item) => item !== symbol) : [...selected, symbol]);
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
        <small>多选框选择，系统会优先扫描这些币；也可以开启自动发现。</small>
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

function ConfigPanel({ config, onSave, onTestApi }: { config: any; onSave: (payload: any) => Promise<void>; onTestApi: () => Promise<void> }) {
  const [form, setForm] = useState<Record<string, any>>(config);
  useEffect(() => setForm(config), [config]);
  const update = (key: string, value: any) => setForm((prev) => ({ ...prev, [key]: value }));
  const number = (key: string, label: string, hint?: string) => (
    <label>{label}<input type="number" value={form[key] ?? ""} onChange={(event) => update(key, Number(event.target.value))} />{hint && <small>{hint}</small>}</label>
  );
  const text = (key: string, label: string, hint?: string) => (
    <label>{label}<input value={Array.isArray(form[key]) ? form[key].join(",") : form[key] ?? ""} onChange={(event) => update(key, event.target.value)} />{hint && <small>{hint}</small>}</label>
  );
  const password = (key: string, label: string, hint?: string) => {
    const saved = form[key] === "********";
    return (
      <label>{label}<input type="password" autoComplete="new-password" placeholder={saved ? "已保存，留空表示不修改" : ""} value={saved ? "" : form[key] ?? ""} onChange={(event) => update(key, event.target.value)} />{hint && <small>{hint}</small>}</label>
    );
  };
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
          {select("growth_mode", "增长模式", modeOptions, "50U 阶段建议锦标赛；系统也会按权益自动切换。")}
          <SymbolMultiPicker value={form.stage1_symbols} onChange={(symbols) => update("stage1_symbols", symbols)} />
          {number("max_scan_symbols", "最大扫描币种", "2GB VPS 当前建议 40；观察池默认 45")}
          {number("min_24h_volume_usdt", "最低 24h 成交额", "过滤流动性差的币")}
          {toggle("auto_discover_symbols", "自动发现加密币", "只纳入 Binance U 本位永续币")}
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
          {number("tournament_loop_seconds", "锦标赛扫描秒数", "默认 30 秒")}
          {select("tournament_sprint_interval", "锦标赛冲刺周期", intervalOptions)}
          {number("tournament_sprint_loop_seconds", "冲刺扫描秒数", "默认 20 秒；完整决策仍受币种数量和回测耗时影响")}
          {number("tournament_sprint_risk_per_trade_pct", "冲刺基础风险%", "默认 18%，属于高风险小资金冲刺")}
          {number("tournament_sprint_daily_loss_limit_pct", "冲刺每日亏损上限%", "默认 35%，触发后停止新开仓")}
          {number("tournament_sprint_max_open_positions", "冲刺最大持仓数", "100U 前建议 1，100U 后可配置 2")}
          {number("tournament_sprint_second_position_equity", "允许第二持仓权益", "默认 100U")}
          {toggle("tournament_sprint_momentum_enabled", "开启冲刺强动量", "成交和价格快速异动时允许小仓试探")}
          {number("tournament_sprint_standard_min_score", "冲刺标准信号最低评分", "默认 72")}
          {number("tournament_sprint_preemptive_min_score", "冲刺抢跑最低评分", "默认 58")}
          {number("tournament_sprint_momentum_min_score", "冲刺强动量最低评分", "默认 54")}
          {number("tournament_sprint_preemptive_max_distance_pct", "冲刺抢跑最大触发距离%", "默认 0.55")}
          {number("tournament_sprint_preemptive_risk_multiplier", "冲刺做多抢跑风险折扣", "默认 0.35")}
          {number("tournament_sprint_short_preemptive_risk_multiplier", "冲刺做空抢跑风险折扣", "默认 0.25")}
          {number("tournament_sprint_min_expected_profit_cost_ratio", "冲刺最低收益/成本比", "默认 1.35，低于此值不值得付手续费和滑点")}
          {number("attack_loop_seconds", "进攻扫描秒数", "默认 60 秒")}
          {number("balanced_loop_seconds", "均衡扫描秒数", "默认 120 秒")}
          {number("tournament_risk_per_trade_pct", "锦标赛标准风险%")}
          {toggle("preemptive_entries_enabled", "开启抢跑试探", "高分候选接近触发时允许小仓提前进场")}
          {number("preemptive_min_score", "抢跑最低评分", "默认 72")}
          {number("standard_min_score", "标准信号最低评分", "默认 85")}
          {number("preemptive_risk_multiplier", "做多抢跑风险折扣", "默认 0.24")}
          {number("short_preemptive_risk_multiplier", "做空抢跑风险折扣", "默认 0.18")}
          {number("preemptive_max_distance_pct", "抢跑最大触发距离%", "默认 0.35")}
          {toggle("observe_breakout_enabled", "观察池标准突破试单", "高分 observe 币出现标准突破时允许折扣仓位试单")}
          {number("observe_breakout_min_score", "观察池试单最低评分", "默认 105")}
                {number("observe_breakout_min_quality", "观察池试单质量分", "默认 78")}
                {number("observe_breakout_min_cost_ratio", "观察池试单成本比", "默认 20")}
                {number("observe_breakout_risk_multiplier", "观察池试单仓位折扣", "默认 0.22")}
                {number("observe_low_price_threshold", "低价币阈值", "低于该价格会再次降仓，默认 0.01")}
                {number("observe_low_price_risk_multiplier", "低价币仓位折扣", "默认 0.75")}
                {number("observe_high_atr_pct", "高波动 ATR%", "超过该值会再次降仓，默认 3")}
                {number("observe_high_atr_risk_multiplier", "高波动仓位折扣", "默认 0.75")}
                {number("observe_extreme_depth_notional_usdt", "极端弱深度阈值", "高 ATR 且深度低于该值会拦截，默认 5000U")}
                {number("observe_consecutive_loss_count", "连续亏损降档笔数", "默认 2 笔")}
                {number("observe_consecutive_loss_risk_multiplier", "连续亏损仓位折扣", "默认 0.5")}
                {toggle("live_credit_enabled", "启用实盘信用分", "按真实成交奖励有效币种，惩罚连续亏损或快速止损的币种方向")}
                {number("live_credit_history_hours", "信用分历史窗口小时", "默认 96 小时；部署时会用这个窗口初始化")}
                {number("live_credit_score_weight", "信用分排序权重", "默认 0.35；分数越高越偏向近期实盘表现")}
                {number("live_credit_multiplier_divisor", "信用倍率除数", "默认 50；倍率 = 信用分 / 50")}
                {number("live_credit_max_risk_multiplier", "最高信用倍率", "默认 2.0；100 分约等于 2 倍风险")}
                {number("live_credit_fuse_score", "熔断信用分", "默认 2；低于等于该分数才不开仓")}
                {toggle("live_credit_recovery_enabled", "启用自然恢复", "亏损币种会随时间慢慢恢复到默认 50 分")}
                {number("live_credit_recovery_interval_hours", "自然恢复间隔小时", "默认 6 小时")}
                {number("live_credit_recovery_points", "每次恢复分数", "默认 +3 分")}
                {number("live_credit_recovery_cap", "自然恢复上限", "默认 50；超过 50 必须靠实盘盈利")}
                {number("live_credit_loss_cooldown_cap", "普通亏损冷却倍率上限", "默认 0.80")}
                {number("live_credit_two_loss_cooldown_cap", "两连亏冷却倍率上限", "默认 0.25")}
                {number("live_credit_three_loss_cooldown_cap", "三连亏冷却倍率上限", "默认 0.10")}
                {number("live_credit_quick_stop_seconds", "快速止损秒数", "默认 60 秒；低于该持仓时间的亏损会重罚")}
                {number("live_credit_two_loss_cooldown_hours", "同方向两连亏冷却小时", "默认 4 小时")}
                {number("live_credit_tail_win_count", "连续盈利防追尾笔数", "默认 3 笔")}
                {number("live_credit_tail_risk_multiplier", "防追尾仓位倍率", "默认 0.75")}
                {number("symbol_cooldown_minutes", "同币开仓冷却分钟", "默认 15")}
          {toggle("position_rotation_enabled", "持仓轮换", "满仓时，只有明显更强的新信号才会替换当前弱仓")}
          {number("tournament_rotation_min_new_score", "锦标赛轮换最低新评分", "默认 95")}
          {number("tournament_rotation_min_score_delta", "锦标赛轮换最低分差", "默认 12")}
          {number("rotation_min_cost_ratio", "轮换最低收益/成本比", "默认 8")}
          {number("rotation_keep_winner_profit_pct", "盈利仓保护%", "旧仓浮盈超过该值时不轻易轮换")}
          {number("rotation_max_current_loss_pct", "深亏仓保护%", "旧仓浮亏超过该值时交给止损，不强制轮换")}
          {number("estimated_slippage_pct", "估算滑点%")}
          {number("min_expected_profit_cost_ratio", "最低收益/成本比")}
          {toggle("allow_short", "自动评估做空", "做空风险会自动打折，并使用更严格回测门槛")}
          {number("short_min_recent_trades", "做空最少样本")}
          {number("short_min_profit_factor", "做空最低 PF")}
          {number("short_min_net_pct", "做空最低净收益%")}
        </div>
      </details>
      <details className="panel danger-zone">
        <summary>危险配置</summary>
        <div className="form-grid">
          <div className="field-wide account-warning">
            <strong>Binance API 配置</strong>
            <small>只需要开启读取和 U 本位合约交易。不要开启提现、万向划转、现货杠杆、预测交易。建议绑定 VPS IP：203.248.94.70。</small>
          </div>
          <div className="field-wide credential-field">{text("api_key", "Binance API Key", "保存后自动打码显示；不想修改则保持原样。")}</div>
          <div className="field-wide credential-field">{password("api_secret", "Binance API Secret", "已保存过时留空表示不修改。")}</div>
          <div className="field-wide credential-field">{text("binance_base_url", "Binance 合约接口地址", "默认 https://fapi.binance.com")}</div>
          {text("live_trading_confirmation", "实盘确认短语", "必须填写 ENABLE_LIVE_TRADING")}
          {number("max_drawdown_pct", "最大回撤%")}
          {number("tournament_stop_equity", "锦标赛停止权益")}
          {number("tournament_max_leverage", "锦标赛最大杠杆")}
          {number("tournament_max_symbol_margin_pct", "锦标赛保证金上限%")}
        </div>
      </details>
      <div className="button-row">
        <button className="primary" onClick={() => onSave(form)}>保存配置</button>
        <button className="secondary" onClick={onTestApi}>测试 Binance API</button>
      </div>
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
