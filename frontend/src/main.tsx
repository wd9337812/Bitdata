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

type StatusData = { config: Record<string, any>; state: Record<string, any>; account: Record<string, any>; market_stream?: Record<string, any>; opportunity_queue?: Record<string, any>; runtime?: Record<string, any>; target_progress?: Record<string, any>; stage_profile?: Record<string, any>; product_completion?: Record<string, any> };
type DecisionsData = { growth_scan?: { mode: Record<string, any>; candidates: any[]; best?: any; funnel?: any }; stage2_grid: any[]; auth_error?: string };
type MarketData = { symbols: any[] };
type SnapshotData = { snapshots: any[] };
type LogsData = { events: any[] };
type LiveLearningData = { scores: any[] };
type SimulationData = Record<string, any>;
type ReportData = Record<string, any>;

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
  ["yolo_scalp", "极限梭哈：50-300U，满仓短打，快进快出，风险极高"],
  ["extreme_sprint", "极限冲刺：300-10000U，强信号放大仓位，保留硬风控"],
  ["attack", "进攻增长：10000-100000U，多币种轮动进攻"],
  ["balanced", "稳健过渡：100000U 以后降低回撤，准备网格"],
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
  const [simulation, setSimulation] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
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
        setSimulation(await api<SimulationData>("/api/simulation/stage"));
        setReport(await api<ReportData>("/api/reports/latest"));
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

  return { status, decisions, market, snapshots, logs, liveLearning, health, simulation, report, error, refresh };
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

function ProtectionAuditPanel({ audit }: { audit?: any }) {
  const positions = audit?.positions || [];
  if (!audit?.enabled && positions.length === 0) return null;
  const protectedCount = positions.filter((item: any) => item.protected || item.repair_status === "repaired").length;
  const first = positions[0] || {};
  const statusText = positions.length === 0 ? "暂无持仓" : audit?.protected ? "已保护" : "需关注";
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>持仓保护</h2>
          <p>系统只检查当前持仓币的条件止盈止损，缺失时会自动补单。</p>
        </div>
      </div>
      <div className="metrics">
        <MetricCard title="保护状态" value={statusText} sub={audit?.checked_at ? new Date(audit.checked_at).toLocaleString("zh-CN") : "等待下一轮审计"} tone={audit?.protected ? "positive" : positions.length ? "negative" : ""} />
        <MetricCard title="持仓数量" value={`${protectedCount} / ${positions.length}`} sub="已保护 / 当前持仓" />
        <MetricCard title="最近持仓" value={first.symbol || "-"} sub={first.direction ? `${first.direction} · ${first.status || "-"}` : "暂无"} />
        <MetricCard title="保护单" value={`${fmt(first.stop_count, 0)} 止损 / ${fmt(first.take_profit_count, 0)} 止盈`} sub={first.repair_status === "repaired" ? "本轮已自动修复" : "来自 Binance 条件单"} />
      </div>
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
  const funnel = data.decisions?.growth_scan?.funnel || {};
  const account = status?.account || {};
  const state = status?.state || {};
  const config = status?.config || {};
  const stream = status?.market_stream || {};
  const opportunityQueue = status?.opportunity_queue || {};
  const runtime = status?.runtime || {};
  const target = status?.target_progress || {};
  const stageProfile = status?.stage_profile || {};
  const completion = status?.product_completion || {};

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
            <ProtectionAuditPanel audit={runtime?.protection_audit} />
            <TargetProgressPanel target={target} />
            <ProductCompletionPanel completion={completion} stageProfile={stageProfile} simulation={data.simulation} report={data.report} />
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
          <section className="stack">
            <ScanSummary funnel={funnel} candidates={candidates} stream={stream} opportunityQueue={opportunityQueue} runtime={runtime} />
            <div className="panel">
              <div className="panel-head">
                <div>
                  <h2>机会漏斗</h2>
                  <p>系统先大范围召回，再逐层粗排、精排和竞价，只有最值得的币才消耗 K 线回测和盘口深度。</p>
                </div>
              </div>
              <FunnelPanel funnel={funnel} />
            </div>
            <div className="panel">
              <div className="panel-head">
                <div>
                  <h2>候选币排名</h2>
                  <p>系统会优先执行最高分且通过过滤的信号。当前回测窗口：{mode.recent_days || "-"} 天。</p>
                </div>
              </div>
              <CandidateTable rows={candidates} />
            </div>
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

function ScanSummary({ funnel, candidates, stream, opportunityQueue, runtime }: { funnel: any; candidates: any[]; stream: any; opportunityQueue: any; runtime: any }) {
  const passed = candidates.filter((item) => item.passed).length;
  const conclusion = passed > 0
    ? `发现 ${passed} 个可执行信号，系统会按风控选择最高优先级。`
    : "本轮暂无可执行开仓，系统继续盯盘等待触发。";
  const degraded = funnel?.rank?.degraded ? "本轮已按 VPS 时间预算自动降级，优先分析最高分币。" : "本轮未触发扫描降级。";
  const queue = funnel?.opportunity_queue || {};
  const queueSymbols = (queue.symbols || opportunityQueue.events?.map((item: any) => item.symbol) || []).slice(0, 6).join("、");
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>本轮扫描结论</h2>
          <p>
            已扫描 {fmt(funnel?.recall?.count, 0)} 个币，重点分析 {fmt(funnel?.rank?.count, 0)} 个，
            发现 {fmt(funnel?.candidates?.count, 0)} 个候选。{conclusion}
          </p>
          <p>{degraded}</p>
        </div>
      </div>
      <div className="metrics">
        <MetricCard title="实时快车道" value={`${fmt(runtime?.fast_lane?.elapsed_seconds, 2)} 秒`} sub={(runtime?.fast_lane?.symbols || []).join("、") || "等待 WebSocket 机会"} tone={(runtime?.fast_lane?.elapsed_seconds || 0) <= 5 ? "positive" : undefined} />
        <MetricCard title="后台全量扫描" value={`${fmt(runtime?.background_scan?.elapsed_seconds ?? funnel?.elapsed_seconds, 2)} 秒`} sub="后台更新，不阻塞实时机会" />
        <MetricCard title="WebSocket 状态" value={stream.connected ? "实时盯盘中" : "未连接"} sub={stream.last_error || `数据年龄 ${fmt(stream.age_seconds, 0)} 秒`} tone={stream.connected ? "positive" : "negative"} />
        <MetricCard title="实时订阅币数" value={`${fmt((stream.symbols || []).length, 0)} 个`} sub={`动态目标 ${fmt(stream.intent_count, 0)} 个`} />
        <MetricCard title="实时行情" value={`${fmt(stream.ticker_count, 0)} / ${fmt(stream.depth_count, 0)} / ${fmt(stream.kline_count, 0)}`} sub="Ticker / 盘口 / K线" />
        <MetricCard title="事件队列" value={`${fmt(queue.count ?? opportunityQueue.active_count, 0)} 个`} sub={queueSymbols ? `热币：${queueSymbols}` : "等待 WebSocket 异动"} />
      </div>
    </div>
  );
}

function ProductCompletionPanel({ completion, stageProfile, simulation, report }: { completion: any; stageProfile: any; simulation: any; report: any }) {
  const modules = completion?.modules || [];
  const reportText = String(report?.markdown || "");
  const reportFirstLine = reportText.split("\n").find((line) => line.startsWith("- ")) || "等待生成每日学习报告";
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>九大模块验收</h2>
          <p>目标、机会队列、动态保护、统一仓位、阶段模式、扫描漏斗、目标面板、阶段模拟、每日报告都已接入。</p>
        </div>
      </div>
      <div className="metrics">
        <MetricCard title="完成度" value={`${fmt(completion?.completed || 0, 0)} / ${fmt(completion?.total || 9, 0)}`} sub={completion?.complete ? "九大模块可验收" : "仍有缺口"} tone={completion?.complete ? "positive" : "negative"} />
        <MetricCard title="当前阶段" value={`${stageProfile?.stage || "-"} ${stageProfile?.label || ""}`} sub={stageProfile?.risk_posture || "-"} />
        <MetricCard title="建议模式" value={stageProfile?.recommended_mode || "-"} sub={`基础风险 ${fmt(stageProfile?.base_risk_pct, 2)}%`} />
        <MetricCard title="模拟终值" value={`${fmt(simulation?.final_equity, 4)} U`} sub={`目标缺口 ${fmt(simulation?.target_gap_pct, 2)}%`} tone={simulation?.target_hit ? "positive" : ""} />
        <MetricCard title="模拟交易频率" value={`${fmt(simulation?.trades_per_day, 2)} 次/天`} sub={`最大回撤 ${fmt(simulation?.max_drawdown_pct, 2)}%`} />
        <MetricCard title="学习报告" value={report?.path ? "已生成" : "生成中"} sub={reportFirstLine.replace("- ", "")} />
      </div>
      <div className="chip-row">
        {modules.map((item: any) => <span className="chip" key={item.key}>{item.label}</span>)}
      </div>
    </div>
  );
}

function TargetProgressPanel({ target }: { target: Record<string, any> }) {
  if (!target?.enabled && target?.status !== "completed") return null;
  const tone = target.status === "ahead" || target.status === "completed" ? "positive" : target.status === "critical" ? "negative" : "";
  const statusText: Record<string, string> = {
    ahead: "领先目标",
    on_track: "接近目标",
    behind: "落后目标",
    critical: "严重落后",
    completed: "已完成",
    account_unavailable: "账户不可用",
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>目标进度</h2>
          <p>{target.reason || "系统会根据 30 天滚仓目标计算当前进度；默认只展示，不自动放大实盘仓位。"}</p>
        </div>
      </div>
      <div className="metrics">
        <MetricCard title="当前阶段" value={target.stage || "-"} sub={target.phase_label || "-"} tone={tone} />
        <MetricCard title="目标权益" value={`${fmt(target.target_equity, 2)} U`} sub={`当前完成 ${fmt(Number(target.target_completion_ratio || 0) * 100, 2)}%`} />
        <MetricCard title="目标曲线应到" value={`${fmt(target.expected_equity, 4)} U`} sub={`进度比 ${fmt(Number(target.progress_ratio || 0) * 100, 2)}%`} />
        <MetricCard title="剩余天数" value={`${fmt(target.remaining_days, 2)} 天`} sub={`已过 ${fmt(target.elapsed_days, 2)} 天`} />
        <MetricCard title="今日所需收益" value={`${fmt(target.required_daily_return_pct, 2)}%`} sub="按剩余时间倒推" />
        <MetricCard title="目标风险倍率" value={`${fmt(target.risk_multiplier, 2)}x`} sub={target.risk_adjustment_enabled ? "已参与仓位" : "仅展示，未放大仓位"} tone={target.risk_adjustment_enabled ? tone : ""} />
        <MetricCard title="进度状态" value={statusText[target.status] || target.status || "-"} sub={target.hard_floor_hit ? "已触发硬底线" : `硬底线 ${fmt(target.hard_floor, 4)} U`} tone={tone} />
      </div>
    </div>
  );
}

function SignalExplain({ best }: { best?: any }) {
  if (!best) {
    return <div className="panel"><h2>当前策略解释</h2><p>还没有扫描结果。</p></div>;
  }
  const signal = best.signal || {};
  const protection = best.protection_plan || signal.protection_plan || {};
  const profile = signal.protection_profile || {};
  const protectionLabel = protection.label || signal.entry_type_label || best.entry_type_label || "-";
  const protectionStopAtr = protection.stop_atr ?? profile.stop_atr;
  const protectionTakeAtr = protection.take_profit_atr ?? profile.take_profit_atr;
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
        <div><span>{"\u4fdd\u62a4\u6863\u6848"}</span><strong>{protectionLabel}</strong></div>
        <div><span>{"\u6b62\u635f / \u6b62\u76c8 ATR"}</span><strong>{fmt(protectionStopAtr, 2)} / {fmt(protectionTakeAtr, 2)}</strong></div>
        <div><span>{"\u521d\u59cb\u6b62\u635f"}</span><strong>{fmt(protection.initial_stop ?? signal.stop, 6)}</strong></div>
        <div><span>{"\u521d\u59cb\u6b62\u76c8"}</span><strong>{fmt(protection.initial_take_profit ?? signal.take_profit, 6)}</strong></div>
      </div>
      <p>{best.decision_reason || signal.reason || best.reason || "等待下一轮扫描。"}</p>
    </div>
  );
}

function FunnelPanel({ funnel }: { funnel: any }) {
  const stages = [
    ["recall", "大召回"],
    ["coarse", "粗排"],
    ["rank", "精排"],
    ["auction", "竞价"],
    ["candidates", "候选"],
  ];
  return (
    <div className="metrics">
      {stages.map(([key, label]) => {
        const item = funnel?.[key] || {};
        const value = key === "candidates"
          ? `${fmt(item.displayed ?? item.count ?? 0, 0)} / ${fmt(item.count ?? 0, 0)}`
          : `${fmt(item.count ?? 0, 0)} / ${fmt(item.limit ?? 0, 0)}`;
        const sub = key === "auction"
          ? "已查盘口深度 / 竞价预算"
          : key === "rank" && item.degraded
            ? `已按 ${fmt(funnel?.degrade_seconds, 0)} 秒预算降级，计划 ${fmt(item.planned, 0)}`
          : key === "candidates"
            ? "展示候选 / 全部候选"
            : `${item.label || label} / 预算`;
        return <MetricCard key={key} title={label} value={value} sub={sub} />;
      })}
      <MetricCard title="本轮耗时" value={`${fmt(funnel?.elapsed_seconds, 3)} 秒`} sub="用于判断是否需要自动降级" />
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
            {!compact && <th>质量池</th>}
            {!compact && <th>质量分</th>}
            {!compact && <th>质量倍率</th>}
            {!compact && <th>质量拆分</th>}
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
              {!compact && <td>{qualityPoolLabel(row.symbol_quality?.pool)}</td>}
              {!compact && <td>{fmt(row.symbol_quality?.score, 2)}</td>}
              {!compact && <td>{fmt(row.quality_risk_multiplier ?? row.symbol_quality?.quality_risk_multiplier, 2)}x</td>}
              {!compact && <td className="reason-cell">{qualitySummary(row.symbol_quality)}</td>}
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
            <p>信用分用于排序和仓位倍率：亏损会降仓，时间会自然恢复；只有净收益、PF 和手续费占比达标的连续盈利方向才允许加仓到 1x 以上。</p>
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

function qualityPoolLabel(pool?: string) {
  const labels: Record<string, string> = {
    trade: "交易池",
    small_trade: "小仓交易",
    adaptive_live: "实盘加权",
    observe_hot: "热点观察",
    observe: "普通观察",
    disabled: "禁用",
  };
  return labels[pool || ""] || pool || "-";
}

function qualitySummary(quality?: any) {
  if (!quality) return "-";
  const c = quality.components || {};
  const reasons = quality.quality_risk_reasons || [];
  const parts = [
    `放量 ${fmt(c.volume_spike, 1)}`,
    `波动 ${fmt(c.volatility, 1)}`,
    `盘口 ${fmt(c.spread_depth, 1)}`,
    `3天 ${fmt(c.backtest_3d, 1)}`,
    `5天 ${fmt(c.backtest_5d, 1)}`,
  ];
  if (Number(c.false_breakout_penalty || 0) > 0) parts.push(`惩罚 -${fmt(c.false_breakout_penalty, 1)}`);
  if (reasons.length) parts.push(reasons.join("；"));
  return parts.join(" / ");
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
        <MetricCard title="标准风险" value={`${fmt(config.yolo_scalp_risk_per_trade_pct)}% / ${fmt(config.extreme_sprint_risk_per_trade_pct)}% / ${fmt(config.attack_risk_per_trade_pct)}% / ${fmt(config.risk_per_trade_pct)}%`} sub="极限梭哈 / 极限冲刺 / 进攻 / 稳健" />
        <MetricCard title="抢跑风险折扣" value={`${fmt(config.preemptive_risk_multiplier, 2)} / ${fmt(config.short_preemptive_risk_multiplier, 2)}`} sub="做多 / 做空" />
        <MetricCard title="每日亏损上限" value={`${fmt(config.yolo_scalp_daily_loss_limit_pct)}% / ${fmt(config.extreme_sprint_daily_loss_limit_pct)}% / ${fmt(config.attack_daily_loss_limit_pct)}% / ${fmt(config.daily_loss_limit_pct)}%`} sub="极限梭哈 / 极限冲刺 / 进攻 / 稳健" />
        <MetricCard title="最大回撤" value={`${fmt(config.max_drawdown_pct)}%`} />
        <MetricCard title="最大持仓" value={`梭哈 ${config.yolo_scalp_max_open_positions || 1} / 极限 ${config.extreme_sprint_max_open_positions || 1} / 普通 ${config.max_open_positions}`} />
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
          {number("recall_pool_limit", "召回池上限", "默认 600；只做低成本预筛，不会全部回测")}
          {number("coarse_pool_limit", "粗排池上限", "默认 220；用成交额和异动先筛选")}
          {number("rank_pool_limit", "精排池上限", "默认 90；只有这些币会拉K线和轻回测")}
          {number("auction_pool_limit", "竞价池上限", "默认 15；限制盘口深度等高成本检查")}
          {number("scan_degrade_seconds", "扫描预算秒数", "默认 18 秒；超过后停止本轮后续精排")}
          {number("scan_min_rank_symbols", "最低精排币数", "默认 8；先保证最高分币被认真计算，2G VPS 更稳")}
          {number("max_scan_symbols", "候选展示上限", "控制 Dashboard 展示候选数量，不等于实际召回数量")}
          {number("min_24h_volume_usdt", "最低 24h 成交额", "过滤流动性差的币")}
          {toggle("auto_discover_symbols", "自动发现加密币", "只纳入 Binance U 本位永续币")}
          {toggle("market_stream_dynamic_enabled", "动态 WebSocket 机会池", "粗排热点、候选币、持仓币会自动进入实时盯盘池")}
          {number("market_stream_max_symbols", "实时盯盘币数上限", "2G VPS 默认 50；越高越实时，但连接和写入压力越大")}
          {number("market_stream_rebuild_seconds", "实时盯盘重建间隔", "默认 60 秒；避免 WebSocket 频繁重连")}
          {number("stream_hot_symbols_limit", "热点进入盯盘数量", "默认 25；来自漏斗粗排和候选")}
          {toggle("fast_lane_enabled", "WebSocket 实时快车道", "异动事件独立于全量扫描，优先在数秒内完成决策")}
          {number("fast_lane_poll_seconds", "快车道轮询秒数", "默认 2 秒；只读取本地事件队列，不持续消耗 Binance REST")}
          {number("fast_lane_symbol_cooldown_seconds", "同币快车道冷却秒数", "默认 10 秒；合并连续推送，避免重复计算和追单")}
          {number("fast_lane_event_max_age_seconds", "快车道事件有效期", "默认 45 秒；过期异动不再追单")}
          {number("fast_lane_max_symbols", "快车道单次币数", "默认 3；优先最高分异动，避免挤占交易API预算")}
          {number("fast_lane_budget_seconds", "快车道计算预算", "默认 5 秒；超过预算只完成最高优先级币")}
          {number("telemetry_retention_days", "系统明细保留天数", "默认 30 天；成交与实盘学习记录不受影响")}
          {toggle("auto_risk_by_equity", "按权益自动切换风险", "小于 300U 自动极限梭哈；300-10000U 自动极限冲刺；更高权益转进攻/稳健")}
          {toggle("target_controller_enabled", "开启目标进度控制器", "按 30 天到 1万、再 30 天到 10万、再 30 天到 100万计算进度")}
          {toggle("target_risk_adjustment_enabled", "目标进度参与仓位", "默认关闭；开启后系统会根据领先或落后目标曲线调整风险倍率")}
          {number("target_phase_a_equity", "阶段A目标权益", "默认 10000U")}
          {number("target_phase_b_equity", "阶段B目标权益", "默认 100000U")}
          {number("target_phase_c_equity", "阶段C目标权益", "默认 1000000U")}
          {number("target_phase_days", "每阶段天数", "默认 30 天")}
          {toggle("daily_learning_report_enabled", "\u6bcf\u65e5\u5b66\u4e60\u62a5\u544a", "\u6bcf\u5929\u81ea\u52a8\u751f\u6210\u7b56\u7565\u590d\u76d8 Markdown\uff0c\u8bb0\u5f55\u76c8\u4e8f\u3001\u963b\u585e\u539f\u56e0\u3001\u5956\u52b1\u548c\u60e9\u7f5a\u5019\u9009")}
          {number("simulation_trades_per_day", "\u6a21\u62df\u4ea4\u6613\u6b21\u6570/\u5929", "\u9ed8\u8ba4 3\uff1b\u7528\u4e8e\u4f30\u7b97\u6eda\u4ed3\u8def\u5f84\uff0c\u4e0d\u662f\u6536\u76ca\u4fdd\u8bc1")}
          {number("simulation_fee_slippage_pct", "\u6a21\u62df\u624b\u7eed\u8d39\u6ed1\u70b9%", "\u9ed8\u8ba4 0.12%\uff1b\u7528\u4e8e\u9636\u6bb5\u6a21\u62df\u6263\u8d39")}
          {number("simulation_start_equity", "\u6a21\u62df\u8d77\u59cb\u6743\u76ca", "\u9ed8\u8ba4 50U")}
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
          {toggle("quality_mode_weights_enabled", "启用模式化质量评分", "不同模式使用不同权重；冲刺更看重放量、ATR、趋势和实盘表现")}
          {number("tournament_sprint_standard_stop_atr", "冲刺标准止损 ATR", "默认 0.9；只影响新开仓保护单")}
          {number("tournament_sprint_standard_take_profit_atr", "冲刺标准止盈 ATR", "默认 1.4；更快止盈，减少持仓占用")}
          {number("tournament_sprint_standard_max_hold_bars", "冲刺标准最多K线", "默认 6 根 5m K线，回测使用")}
          {number("tournament_sprint_preemptive_stop_atr", "冲刺抢跑止损 ATR", "默认 0.75；抢跑单更快认错")}
          {number("tournament_sprint_preemptive_take_profit_atr", "冲刺抢跑止盈 ATR", "默认 1.0")}
          {number("tournament_sprint_preemptive_max_hold_bars", "冲刺抢跑最多K线", "默认 4 根 5m K线，回测使用")}
          {number("tournament_sprint_momentum_stop_atr", "冲刺动量止损 ATR", "默认 0.8")}
          {number("tournament_sprint_momentum_take_profit_atr", "冲刺动量止盈 ATR", "默认 1.2")}
          {number("tournament_sprint_momentum_max_hold_bars", "冲刺动量最多K线", "默认 5 根 5m K线，回测使用")}
          {toggle("yolo_scalp_enabled", "开启极限梭哈", "高风险开关：必须配合确认短语 ENABLE_YOLO_SCALP 才会生效")}
          {text("yolo_scalp_confirmation", "极限梭哈确认短语", "填写 ENABLE_YOLO_SCALP 后，增长模式选择极限梭哈才会启用")}
          {select("yolo_scalp_interval", "极限梭哈周期", intervalOptions)}
          {number("yolo_scalp_loop_seconds", "极限梭哈扫描秒数", "默认 8 秒；全量漏斗仍受预算和 API 频控保护")}
          {number("yolo_scalp_auto_under_equity", "自动梭哈权益线", "默认 300U；低于该权益且已确认时自动进入极限梭哈")}
          {number("yolo_scalp_risk_per_trade_pct", "极限梭哈基础风险%", "默认 55%；好机会大仓位，舔一口就走，可能快速亏完本金")}
          {number("yolo_scalp_daily_loss_limit_pct", "极限梭哈每日亏损上限%", "默认 65%；触发后停止新开仓")}
          {number("yolo_scalp_max_open_positions", "极限梭哈最大持仓数", "默认 1；满仓短打阶段不建议同时持有多个币")}
          {number("yolo_scalp_standard_min_score", "梭哈标准最低评分", "默认 72；比极限冲刺更宽，靠更快止盈止损控制风险")}
          {number("yolo_scalp_preemptive_min_score", "梭哈抢跑最低评分", "默认 58；允许火药桶和抢跑信号更早试错")}
          {number("yolo_scalp_momentum_min_score", "梭哈动量最低评分", "默认 54；强异动可进入短打")}
          {number("yolo_scalp_min_expected_profit_cost_ratio", "梭哈最低收益/成本比", "默认 0.85；低于此值手续费和滑点可能吃掉毛利")}
          {number("yolo_scalp_standard_stop_atr", "梭哈标准止损 ATR", "默认 0.38；更快认错")}
          {number("yolo_scalp_standard_take_profit_atr", "梭哈标准止盈 ATR", "默认 0.55；更快落袋")}
          {number("yolo_scalp_preemptive_stop_atr", "梭哈抢跑止损 ATR", "默认 0.32")}
          {number("yolo_scalp_preemptive_take_profit_atr", "梭哈抢跑止盈 ATR", "默认 0.48")}
          {number("yolo_scalp_high_score", "梭哈加仓评分", "默认 95；达到后进入高仓位档")}
          {number("yolo_scalp_super_score", "梭哈强加仓评分", "默认 118；达到后进入顶级仓位档")}
          {number("yolo_scalp_high_risk_multiplier", "梭哈加仓倍率", "默认 1.35")}
          {number("yolo_scalp_super_risk_multiplier", "梭哈强加仓倍率", "默认 1.80")}
          {toggle("extreme_sprint_enabled", "开启极限冲刺", "必须配合确认短语 ENABLE_EXTREME_SPRINT 才会生效")}
          {text("extreme_sprint_confirmation", "极限冲刺确认短语", "填写 ENABLE_EXTREME_SPRINT 后，增长模式选择极限冲刺才会启用")}
          {select("extreme_sprint_interval", "极限冲刺周期", intervalOptions)}
          {number("extreme_sprint_loop_seconds", "极限扫描秒数", "默认 15 秒；更快寻找机会")}
          {number("extreme_sprint_risk_per_trade_pct", "极限基础风险%", "默认 28%，高风险冲刺参数")}
          {number("extreme_sprint_daily_loss_limit_pct", "极限每日亏损上限%", "默认 50%，触发后停止新开仓")}
          {number("extreme_sprint_standard_min_score", "极限标准最低评分", "默认 88")}
          {number("extreme_sprint_preemptive_min_score", "极限抢跑最低评分", "默认 72")}
          {number("extreme_sprint_momentum_min_score", "极限动量最低评分", "默认 68")}
          {number("extreme_sprint_min_expected_profit_cost_ratio", "极限最低收益/成本比", "默认 1.1，必须覆盖手续费和滑点")}
          {number("extreme_sprint_high_score", "极限加仓评分", "默认 110；达到后提高仓位倍率")}
          {number("extreme_sprint_super_score", "极限强加仓评分", "默认 135；达到后进一步放大仓位")}
          {number("extreme_sprint_high_risk_multiplier", "极限加仓倍率", "默认 1.35")}
          {number("extreme_sprint_super_risk_multiplier", "极限强加仓倍率", "默认 1.75")}
          {toggle("equity_guard_enabled", "开启权益高点保护", "回撤越深，开仓倍率自动降低；极端回撤暂停")}
          {number("equity_guard_drawdown_1_pct", "权益保护一档回撤%", "默认 10")}
          {number("equity_guard_multiplier_1", "权益保护一档倍率", "默认 0.75")}
          {number("equity_guard_drawdown_2_pct", "权益保护二档回撤%", "默认 18")}
          {number("equity_guard_multiplier_2", "权益保护二档倍率", "默认 0.45")}
          {number("equity_guard_drawdown_3_pct", "权益保护三档回撤%", "默认 25")}
          {number("equity_guard_multiplier_3", "权益保护三档倍率", "默认 0.20")}
          {number("extreme_equity_guard_pause_drawdown_pct", "极限模式暂停回撤%", "默认 40")}
          {toggle("effective_position_sizing_enabled", "启用有效仓位模型", "强信号获得有效仓位；低收益小单直接等待，不再强行凑交易所最低额")}
          {number("effective_min_order_notional_usdt", "最低有效订单 U", "默认 10U；低于该名义价值的订单不执行")}
          {number("effective_min_profit_cost_ratio", "最低收益成本比", "默认 3；预期毛收益至少为手续费与滑点成本的 3 倍")}
          {number("effective_min_net_profit_usdt", "最低预期净利润 U", "默认 0.15U；低于该值的小单不值得承担噪声风险")}
          {number("effective_probe_min_risk_pct", "探路单最低止损风险%", "默认 0.5%；按止损距离反推仓位，不等于保证金比例")}
          {number("effective_standard_min_risk_pct", "标准单最低止损风险%", "默认 0.8%")}
          {number("effective_high_min_risk_pct", "高质量单最低止损风险%", "默认 1.5%")}
          {number("effective_top_min_risk_pct", "顶级信号最低止损风险%", "默认 2%；仍受最大保证金和风险上限约束")}
          {number("effective_guard_probe_min_multiplier", "深回撤探路权益倍率", "默认 0.2；弱机会不会绕过权益保护")}
          {number("effective_guard_standard_min_multiplier", "深回撤标准权益倍率", "默认 0.3")}
          {number("effective_guard_high_min_multiplier", "深回撤高质量权益倍率", "默认 0.4")}
          {number("effective_guard_top_min_multiplier", "深回撤顶级权益倍率", "默认 0.6；只对顶级标准信号生效")}
          {number("extreme_probe_stop_atr", "火药桶探路止损 ATR", "默认 0.9；给 5 分钟噪声更多空间")}
          {number("extreme_probe_take_profit_atr", "火药桶探路止盈 ATR", "默认 1.2")}
          {number("extreme_probe_max_hold_bars", "火药桶最长持有 K 线", "默认 4 根")}
          {toggle("min_order_filter_enabled", "最小下单量前置过滤", "扫描阶段提前跳过下单量不足的币，避免启动后失败")}
          {toggle("market_state_filter_enabled", "行情状态分类过滤", "识别趋势放量、插针、盘口薄等状态")}
          {toggle("websocket_trigger_enabled", "WebSocket 事件触发入场", "实时K线异动会进入下一轮扫描优先级")}
          {number("websocket_trigger_move_pct", "实时触发涨跌幅%", "默认 0.35")}
          {number("websocket_trigger_quote_volume_usdt", "实时触发成交额U", "默认 250000")}

          {toggle("dynamic_protection_enabled", "\u52a8\u6001\u4fdd\u62a4\u8ba1\u5212", "\u5f00\u542f\u540e\u4e0b\u5355\u524d\u751f\u6210\u7edf\u4e00\u4fdd\u62a4\u8ba1\u5212\uff1a\u521d\u59cb\u6b62\u76c8\u6b62\u635f\u3001\u5feb\u901f\u5931\u6548\u3001\u4fdd\u672c\u548c\u79fb\u52a8\u6b62\u76c8\u53c2\u6570\u90fd\u4f1a\u7559\u6863")}
          {toggle("dynamic_protection_runtime_enabled", "\u8fd0\u884c\u65f6\u4fdd\u62a4\u68c0\u67e5", "\u6bcf\u8f6e\u626b\u63cf\u524d\u68c0\u67e5\u5df2\u6709\u6301\u4ed3\u662f\u5426\u89e6\u53d1\u5feb\u901f\u5931\u6548\u3001\u4fdd\u672c\u3001\u79fb\u52a8\u6b62\u76c8\u6216\u65f6\u95f4\u6b62\u635f")}
          {toggle("dynamic_protection_runtime_trade_enabled", "\u8fd0\u884c\u65f6\u4fdd\u62a4\u5b9e\u76d8\u6267\u884c", "\u9ad8\u98ce\u9669\u5f00\u5173\uff1a\u5f00\u542f\u540e\u6ee1\u8db3\u5feb\u901f\u5931\u6548\u6216\u65f6\u95f4\u6b62\u635f\u4f1a\u81ea\u52a8\u5e73\u4ed3\uff0c\u9ed8\u8ba4\u5173\u95ed")}
          {number("runtime_protection_max_hold_bars", "\u8fd0\u884c\u65f6\u6700\u5927\u6301\u4ed3K\u7ebf", "\u9ed8\u8ba4 12 \u6839\uff1b\u8d85\u8fc7\u4e14\u672a\u8fbe\u5230\u6263\u8d39\u540e\u5229\u6da6\u4f1a\u63d0\u793a\u65f6\u95f4\u6b62\u635f")}
          {number("protection_fast_invalid_seconds", "\u5feb\u901f\u5931\u6548\u89c2\u5bdf\u79d2\u6570", "\u9ed8\u8ba4 90 \u79d2\uff1b\u7a81\u7834\u540e\u5f88\u5feb\u53cd\u5411\u65f6\u7528\u4e8e\u540e\u7eed\u98ce\u63a7\u5347\u7ea7")}
          {number("protection_fast_invalid_atr", "\u5feb\u901f\u5931\u6548 ATR", "\u9ed8\u8ba4 0.35\uff1b\u4ef7\u683c\u53cd\u5411\u8d85\u8fc7\u8be5 ATR \u89c6\u4e3a\u4fe1\u53f7\u8d70\u5f31")}
          {number("protection_break_even_trigger_atr", "\u4fdd\u672c\u89e6\u53d1 ATR", "\u9ed8\u8ba4 0.55\uff1b\u76c8\u5229\u5230\u8be5\u8ddd\u79bb\u540e\uff0c\u540e\u7eed\u7248\u672c\u53ef\u628a\u4fdd\u62a4\u7ebf\u63a8\u5230\u6210\u672c\u9644\u8fd1")}
          {number("protection_break_even_buffer_pct", "\u4fdd\u672c\u7f13\u51b2%", "\u9ed8\u8ba4 0.08%\uff1b\u8986\u76d6\u624b\u7eed\u8d39\u548c\u8f7b\u5fae\u6ed1\u70b9")}
          {number("protection_trailing_trigger_atr", "\u79fb\u52a8\u6b62\u76c8\u89e6\u53d1 ATR", "\u9ed8\u8ba4 0.9\uff1b\u8dd1\u51fa\u5229\u6da6\u540e\u624d\u8003\u8651\u8ddf\u8e2a")}
          {number("protection_trailing_distance_atr", "\u79fb\u52a8\u6b62\u76c8\u8ddd\u79bb ATR", "\u9ed8\u8ba4 0.55\uff1b\u8d8a\u5c0f\u8d8a\u5feb\u843d\u888b\uff0c\u8d8a\u5927\u8d8a\u7ed9\u8d8b\u52bf\u7a7a\u95f4")}
          {number("protection_min_profit_after_cost_pct", "\u6263\u8d39\u540e\u6700\u4f4e\u5229\u6da6%", "\u9ed8\u8ba4 0.08%\uff1b\u907f\u514d\u5c0f\u76c8\u5229\u88ab\u624b\u7eed\u8d39\u5403\u6389")}
          {number("sprint_symbol_trade_score", "冲刺交易池分数", "默认 68")}
          {number("sprint_symbol_small_trade_score", "冲刺小仓交易分数", "默认 55")}
          {number("sprint_symbol_hot_observe_score", "冲刺热点观察分数", "默认 45，满足放量和盘口时可小仓试探")}
          {number("sprint_atr_ideal_min_pct", "冲刺ATR理想下限%", "默认 1.2")}
          {number("sprint_atr_ideal_max_pct", "冲刺ATR理想上限%", "默认 7")}
          {number("sprint_atr_high_pct", "冲刺ATR极端阈值%", "默认 10")}
          {number("sprint_high_atr_risk_multiplier", "冲刺高ATR仓位倍率", "默认 0.6")}
          {number("sprint_sample_penalty", "冲刺样本少惩罚", "默认 -1 分")}
          {number("sprint_sample_penalty_exempt_spike", "样本少豁免放量倍数", "默认 2.5")}
          {number("sprint_sample_low_risk_multiplier", "样本少仓位倍率", "默认 0.75")}
          {number("sprint_hot_observe_risk_multiplier", "热点观察仓位倍率", "默认 0.35")}
          {number("sprint_extreme_depth_notional_usdt", "极端波动最低深度", "默认 50000U")}
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
                {toggle("weak_quality_probe_enabled", "开启弱质量试探", "极限模式下，候选分强但质量仍在观察池时，用更小仓位获取实盘样本")}
                {number("weak_quality_probe_min_candidate_score", "弱质量试探最低候选分", "默认 95；必须是很强的候选信号")}
                {number("weak_quality_probe_min_quality_score", "弱质量试探最低质量分", "默认 58；比正式交易池低，但不能太差")}
                {number("weak_quality_probe_min_cost_ratio", "弱质量试探最低成本比", "默认 12；确保手续费和滑点后仍有空间")}
                {number("weak_quality_probe_min_profit_factor", "弱质量试探最低 PF", "默认 0.55；低于此值代表历史表现太弱")}
                {number("weak_quality_probe_min_net_pct", "弱质量试探最低净收益%", "默认 -8；允许小亏历史，但不能持续失血")}
                {number("weak_quality_probe_min_depth_notional_usdt", "弱质量试探最低盘口深度U", "默认 300；盘口太薄不试")}
                {number("weak_quality_probe_max_spread_pct", "弱质量试探最大点差%", "默认 0.12；点差过大不试")}
                {number("weak_quality_probe_base_multiplier", "弱质量试探基础仓位倍率", "默认 0.18；弱观察质量的小仓测试")}
                {number("weak_quality_probe_mid_quality_score", "弱质量试探中档质量分", "默认 65")}
                {number("weak_quality_probe_mid_multiplier", "弱质量试探中档仓位倍率", "默认 0.25")}
                {number("weak_quality_probe_high_quality_score", "弱质量试探高档质量分", "默认 72")}
                {number("weak_quality_probe_high_multiplier", "弱质量试探高档仓位倍率", "默认 0.35")}
                {number("weak_quality_probe_stop_atr", "弱质量试探止损 ATR", "默认 0.55；试错更快")}
                {number("weak_quality_probe_take_profit_atr", "弱质量试探止盈 ATR", "默认 0.75；有利润先落袋")}
                {number("weak_quality_probe_max_hold_bars", "弱质量试探最多K线", "默认 3 根 5m K线；久不动就不恋战")}
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
