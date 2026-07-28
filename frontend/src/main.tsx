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
  Route,
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
type StatusData = { config: Record<string, any>; state: Record<string, any>; account: Record<string, any>; market_stream?: Record<string, any>; user_stream?: Record<string, any>; binance_rate?: Record<string, any>; opportunity_queue?: Record<string, any>; runtime?: Record<string, any>; target_progress?: Record<string, any>; stage_profile?: Record<string, any>; stage_route?: Record<string, any>; stage_profiles?: Record<string, any>[]; product_completion?: Record<string, any>; storage?: Record<string, any>; v49_global_adaptive?: Record<string, any>; global_adaptive?: Record<string, any>; s0_moe?: Record<string, any> };
type DecisionsData = { growth_scan?: { mode: Record<string, any>; candidates: any[]; best?: any; funnel?: any }; stage2_grid: any[]; auth_error?: string };
type MarketData = { symbols: any[] };
type SnapshotData = { snapshots: any[] };
type LogsData = { events: any[] };
type LiveLearningData = { scores: any[]; strategy_scores?: any[]; scalp_scores?: any[]; extreme_scores?: any[]; v4_scores?: any[]; v5_scores?: any[]; active_opportunity_scores?: any[] };
type LiveReactionData = { reactions: any[]; recent_trades: any[] };
type ShadowData = { stats: Record<string, any>; by_strategy?: any[]; by_release?: any[]; by_evidence_type?: any[]; by_admission_lane?: any[]; active_release?: Record<string, any>; challenger_release?: Record<string, any>; trades: any[] };
type SimulationData = Record<string, any>;
type ReportData = Record<string, any>;
type TrainingQualityData = {
  schema_version?: string;
  generated_at?: string;
  lineage?: Record<string, number>;
  live?: Record<string, number>;
  shadow?: Record<string, number>;
  high_weight_training_ready?: boolean;
  exact_live_link_rate_pct?: number;
  paired_events?: number;
  message?: string;
};

const menu = [
  { id: "overview", label: "控制台", icon: Activity },
  { id: "scan", label: "机会中心", icon: CandlestickChart },
  { id: "review", label: "交易复盘", icon: LineChart },
  { id: "config", label: "设置", icon: Settings },
  { id: "system", label: "系统", icon: Shield },
];

const intervalOptions = [
  ["5m", "5分钟"],
  ["15m", "15分钟"],
  ["1h", "1小时"],
  ["4h", "4小时"],
];

const stageManualOptions = [
  ["auto", "自动（推荐）：按账户权益选择阶段"],
  ["extreme_sprint", "手动 V5.1 全仓短打"],
  ["yolo_scalp", "手动盘口剥头皮"],
  ["grid", "手动网格"],
  ["attack", "手动进攻模式（兼容旧配置）"],
  ["balanced", "手动均衡模式（兼容旧配置）"],
  ["conservative", "手动稳健模式（兼容旧配置）"],
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
  const [liveLearning, setLiveLearning] = useState<LiveLearningData>({ scores: [], strategy_scores: [], scalp_scores: [], extreme_scores: [], v4_scores: [] });
  const [liveReaction, setLiveReaction] = useState<LiveReactionData | null>(null);
  const [shadow, setShadow] = useState<ShadowData>({ stats: {}, trades: [] });
  const [health, setHealth] = useState<any>(null);
  const [simulation, setSimulation] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [trainingQuality, setTrainingQuality] = useState<TrainingQualityData | null>(null);
  const [error, setError] = useState("");

  async function refresh(light = false, throwOnError = false) {
    try {
      if (light) {
        const live = await api<{ status: StatusData; decisions: DecisionsData }>("/api/dashboard/live");
        setStatus(live.status);
        setDecisions(live.decisions);
        setError("");
        return;
      }
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
      setDecisions(await api<DecisionsData>("/api/decisions"));
      const learningRes = await api<LiveLearningData>("/api/live-learning?limit=100");
      setLiveLearning({
        scores: learningRes.scores || [],
        strategy_scores: learningRes.strategy_scores || [],
        scalp_scores: learningRes.scalp_scores || [],
        extreme_scores: learningRes.extreme_scores || [],
        v4_scores: learningRes.v4_scores || [],
      });
      setLiveReaction(await api<LiveReactionData>("/api/live-reaction?limit=100"));
      setShadow(await api<ShadowData>("/api/shadow-trades?limit=100"));
      setSimulation(await api<SimulationData>("/api/simulation/stage"));
      setReport(await api<ReportData>("/api/reports/latest"));
      setTrainingQuality(await api<TrainingQualityData>("/api/training-data/quality"));
      setError("");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      if (throwOnError) throw new Error(message);
    }
  }

  useEffect(() => {
    refresh();
    const fast = window.setInterval(() => refresh(true), 2000);
    const slow = window.setInterval(() => refresh(), 30000);
    return () => {
      window.clearInterval(fast);
      window.clearInterval(slow);
    };
  }, []);

  return { status, decisions, market, snapshots, logs, liveLearning, liveReaction, shadow, health, simulation, report, trainingQuality, error, refresh };
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

function freshnessText(value?: string, ageSeconds?: number) {
  if (!value) return "等待首次同步";
  const age = Number(ageSeconds);
  const ageLabel = Number.isFinite(age) ? `${fmt(age, 0)} 秒前` : new Date(value).toLocaleString("zh-CN");
  return `${ageLabel} · ${new Date(value).toLocaleTimeString("zh-CN")}`;
}

function ProtectionAuditPanel({ audit, projection, supervisor }: { audit?: any; projection?: any; supervisor?: any }) {
  const positions = audit?.positions || [];
  if (!audit?.enabled && positions.length === 0) return null;
  const protectedCount = positions.filter((item: any) => item.protected || item.repair_status === "repaired").length;
  const first = positions[0] || {};
  const fresh = !projection?.stale && supervisor?.healthy !== false;
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
        <MetricCard title="账户数据" value={fresh ? "实时" : "已陈旧"} sub={`${projection?.source === "private_websocket" ? "私有 WebSocket" : "REST 兜底"} · ${freshnessText(projection?.as_of, projection?.stream_age_seconds)}`} tone={fresh ? "positive" : "negative"} />
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
  const userStream = status?.user_stream || {};
  const binanceRate = status?.binance_rate || {};
  const opportunityQueue = status?.opportunity_queue || {};
  const runtime = status?.runtime || {};
  const performanceGuard = runtime?.risk_status?.performance_guard || {};
  const continuousPermit = performanceGuard.continuous_permit || {};
  const recoveryPermit = performanceGuard.recovery_permit || {};
  const strategyCanary = performanceGuard.strategy_canary_permit || {};
  const localCircuit = performanceGuard.local_circuit || {};
  const activeLocalCircuitBlocks = Number(
    funnel?.opportunity_v4?.local_circuit_blocked
      ?? Object.values(localCircuit.cohorts || {}).filter((item: any) => item?.blocked_at).length,
  );
  const activeStrategyVersion = String(
    performanceGuard.active_strategy_version
      || config.opportunity_v4_strategy_version
      || "v5.1",
  );
  const activeStrategyFamily = String(
    performanceGuard.active_strategy_family
      || (activeStrategyVersion.toLowerCase().startsWith("v5.") ? "extreme_v5_roll" : "extreme_v4_roll"),
  );
  const activeStrategyLabel = activeStrategyVersion.toLowerCase().startsWith("v5.")
    ? activeStrategyVersion.toUpperCase()
    : activeStrategyVersion.toUpperCase();
  const currentPerformanceScope = `${activeStrategyFamily}@${activeStrategyVersion}`;
  const moe = status?.s0_moe || {};
  const moeOnline = moe.online_shadow?.current_version || {};
  const moeSelected = moeOnline.selected || {};
  const moeEvaluated = moeOnline.evaluated || {};
  const moeRetraining = moe.retraining || {};
  const moeCandidate = moe.candidate || {};
  const moeCandidateStress = moeCandidate.cost_stress?.["1.5x"]?.test || {};
  const dailyProfitLock = state?.s0_daily_profit_lock_status || {};
  const runtimeProtection = runtime?.runtime_protection || {};
  const runtimeProtectionActions = runtimeProtection.actions || [];
  const activeRuntimeProtection = runtimeProtectionActions[0] || {};
  const globalAdaptive = status?.global_adaptive || status?.v49_global_adaptive || {};
  const globalAdaptiveStats = globalAdaptive.stats_24h || {};
  const globalAdaptiveState = !globalAdaptive.enabled
    ? "未启用"
    : !globalAdaptive.ready
      ? "样本积累中"
      : globalAdaptive.negative
        ? "全局收紧"
          : globalAdaptive.positive
            ? "全局正向"
            : "保持基础门槛";
  const globalDirectionBias = String(globalAdaptive.global_direction_bias || "NEUTRAL");
  const globalSmartFlow = globalAdaptive.smart_flow_global || {};
  const accountProjection = runtime?.account_projection || {};
  const liveEvidenceIsCurrent = performanceGuard.live_evidence_scope === currentPerformanceScope;
  const recoveryEligibleLanes = performanceGuard.recovery_requirements?.eligible_admission_lanes || [];
  const recoveryEvidenceLabel = recoveryEligibleLanes.includes("full_bet") ? "全仓决策影子" : "实盘准入决策影子";
  const target = status?.target_progress || {};
  const stageProfile = status?.stage_profile || {};
  const rawStageRoute = status?.stage_route || {};
  const recoveryStatusLabel: Record<string, string> = {
    accumulating: "积累影子证据",
    confirming: "确认恢复稳定性",
    waiting_candidate: "持证等待候选",
    probe_open: "恢复试单持仓中",
    cooldown: "强制冷却中",
    soft_observation: "软保护观察中",
    hard_cooldown: "硬保护冷却中",
    revoked: "恢复资格已撤销",
    normal: "正常实盘",
    level_2: "二级试运行",
    validated: "已完成试运行验证",
    exhausted: "试运行次数已用完",
    expired: "试运行许可证已过期",
    inactive: "未启用",
    not_required: "当前无需许可证",
  };
  const canaryReasonLabel: Record<string, string> = {
    new_strategy_release_canary: "新版本已签发限次试运行资格",
    waiting_for_canary_candidate: "等待同版本合格候选",
    protected_canary_position_open: "受保护试运行持仓已建立",
    canary_live_evidence_positive: "试运行实盘证据为正，已升二级",
    canary_live_evidence_validated: "试运行实盘证据达标",
    canary_loss_budget_exhausted: "试运行亏损预算已用完",
    waiting_for_reissue_shadow_evidence: "等待新影子证据自动再签发",
    shadow_evidence_reissued_canary: "新影子证据达标，已自动再签发",
    canary_opportunity_budget_exhausted: "试运行机会次数已用完",
    canary_permit_expired: "试运行许可证已过期",
    mandatory_cooldown: "仍在强制冷却期",
    emergency_safety_stop: "触发账户硬安全停止",
    global_guard_normal: "当前无需穿透风险背景",
    release_not_authorized: "当前策略版本未获授权",
  };
  const stageRoute = Object.keys(rawStageRoute).length > 0 ? rawStageRoute : stageProfile;
  const stageProfiles = status?.stage_profiles || [];
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
              当前模式：{modeLabel[stageRoute.mode] || modeLabel[mode.mode] || modeLabel[config.growth_mode] || "-"} · 阶段：{stageRoute.stage || "-"} · Binance：
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
              <MetricCard
                title={`${activeStrategyLabel} 全局自适应`}
                value={globalAdaptiveState}
                sub={`${fmt(globalAdaptiveStats.shadow_trades, 0)} 影子 / ${fmt(globalAdaptiveStats.live_trades, 0)} 实盘 · ${fmt(globalAdaptiveStats.symbols, 0)} 币种 · PF ${fmt(globalAdaptiveStats.profit_factor, 2)}`}
                tone={globalAdaptive.negative ? "negative" : globalAdaptive.ready ? "positive" : ""}
              />
              <MetricCard
                title="全局市场与聪明钱"
                value={`${globalDirectionBias === "LONG" ? "偏多" : globalDirectionBias === "SHORT" ? "偏空" : "中性"} · ${globalAdaptive.market_regime_state === "trend" ? "趋势" : "震荡/待定"}`}
                sub={`聪明钱 ${globalSmartFlow.available ? (globalSmartFlow.bias || "中性") : "数据不足"} · 72小时样本 ${fmt(globalSmartFlow.sample_count, 0)} · 仅全局修正，不按币种调参`}
                tone={globalDirectionBias === "NEUTRAL" ? "" : "positive"}
              />
              <MetricCard
                title="账户投影"
                value={accountProjection.stale ? "需要刷新" : accountProjection.source === "private_ws" ? "实时" : "REST 兜底"}
                sub={accountProjection.age_seconds != null ? `${accountProjection.source || "账户流"} · 数据 ${fmt(accountProjection.age_seconds, 0)} 秒前 · 下单前仍会复核` : "下单前强制读取最新账户余额"}
                tone={accountProjection.stale ? "negative" : "positive"}
              />
              <MetricCard title="自动阶段" value={`${stageRoute.stage || "-"} ${stageRoute.label || ""}`} sub={`单笔风险 ${fmt(stageRoute.risk_pct, 2)}% · 最多 ${fmt(stageRoute.max_open_positions, 0)} 仓`} />
              <MetricCard title="私有账户流" value={userStream.connected ? "实时连接" : "REST 兜底"} sub={userStream.last_error || `账户数据年龄 ${fmt(userStream.account_age_seconds, 0)} 秒`} tone={userStream.connected ? "positive" : ""} />
              <MetricCard
                title="权益安全线"
                value={runtime?.risk_status?.warning_active ? "低于预警线" : "正常"}
                sub={`预警 ${fmt(config.risk_warning_equity, 2)}U · 硬停止 ${fmt(config.hard_stop_equity, 2)}U`}
                tone={runtime?.risk_status?.warning_active ? "negative" : "positive"}
              />
              <MetricCard
                title={`${performanceGuard.active_strategy_version || "当前版本"} 实盘保护`}
                value={performanceGuard.status_label || "等待统计"}
                sub={`${performanceGuard.guard_level === "hard" ? "硬保护" : performanceGuard.guard_level === "soft" ? "软保护" : "正常"} · ${liveEvidenceIsCurrent ? "当前版本实盘证据" : "历史安全兜底"} · PF ${fmt(performanceGuard.live?.profit_factor, 2)} · 连亏 ${fmt(performanceGuard.tail_losses, 0)} 笔 · 高点回撤 ${fmt(performanceGuard.peak_drawdown_pct, 2)}%`}
                tone={performanceGuard.allowed ? "positive" : "negative"}
              />
              <MetricCard
                title="软 / 硬保护边界"
                value={continuousPermit.enabled ? `当前仓位 ${fmt(continuousPermit.risk_multiplier, 2)}x` : `${fmt(performanceGuard.soft_thresholds?.consecutive_losses, 0)} / ${fmt(performanceGuard.hard_thresholds?.consecutive_losses, 0)} 笔连亏`}
                sub={continuousPermit.enabled ? `普通亏损只递进降仓；S0 普通日亏损停牌${stageRoute.daily_loss_stop_enabled ? "已开启" : "已关闭"}，5U 硬停止和交易所保护单始终生效` : `软观察 ${fmt(performanceGuard.soft_thresholds?.observation_minutes, 0)} 分钟，硬冷却 ${fmt(performanceGuard.hard_thresholds?.cooldown_minutes, 0)} 分钟 · 回撤线 ${fmt(performanceGuard.soft_thresholds?.peak_drawdown_pct, 0)}% / ${fmt(performanceGuard.hard_thresholds?.peak_drawdown_pct, 0)}%`}
                tone={performanceGuard.guard_level === "hard" ? "negative" : ""}
              />
              <MetricCard
                title={continuousPermit.enabled ? `${performanceGuard.active_strategy_version || "当前版本"} 单账户影子证据` : `${performanceGuard.active_strategy_version || "当前版本"} 决策影子恢复证据`}
                value={`决策影子 PF ${fmt(performanceGuard.shadow_tail?.profit_factor, 2)}`}
                sub={continuousPermit.enabled ? `只统计单仓账户可依次执行的非重叠影子；并行候选只用于研究，不控制全局开仓 · ${fmt(performanceGuard.shadow_tail?.trades, 0)} 笔` : `${recoveryEvidenceLabel} ${fmt(performanceGuard.shadow_tail?.trades, 0)} / ${fmt(performanceGuard.recovery_requirements?.shadow_trades, 0)} 笔 · 仅影子和探索影子只用于研究，不参与恢复放行 · ${performanceGuard.reason || "只用于判断当前实盘版本是否恢复"}`}
                tone={performanceGuard.shadow_bad ? "negative" : ""}
              />
              <MetricCard
                title={continuousPermit.enabled ? "盈利恢复规则" : "实盘恢复流程"}
                value={continuousPermit.enabled ? `${fmt(continuousPermit.consecutive_effective_wins, 0)} 次连续有效盈利` : recoveryStatusLabel[recoveryPermit.status] || recoveryPermit.status || "等待状态"}
                sub={
                  continuousPermit.enabled
                    ? continuousPermit.recovery_rule
                    : recoveryPermit.status === "waiting_candidate"
                    ? `资格剩余 ${fmt(Number(recoveryPermit.seconds_remaining || 0) / 60, 0)} 分钟，遇到合格候选才会消耗`
                    : recoveryPermit.status === "confirming"
                      ? `稳定确认 ${fmt(recoveryPermit.qualified_closes, 0)} / ${fmt(performanceGuard.recovery_requirements?.confirm_closes, 0)} 次`
                      : recoveryPermit.reason || "影子PF达标后会生成限时恢复资格"
                }
                tone={recoveryPermit.status === "waiting_candidate" ? "positive" : recoveryPermit.status === "normal" ? "positive" : "negative"}
              />
              <MetricCard
                title={continuousPermit.enabled ? `${performanceGuard.active_strategy_version || "当前版本"} 连续准入` : `${performanceGuard.active_strategy_version || "当前版本"} 策略试运行许可证`}
                value={continuousPermit.enabled ? (continuousPermit.allowed ? "允许合格机会开仓" : "暂停新开仓") : recoveryStatusLabel[strategyCanary.status] || strategyCanary.status || "等待状态"}
                sub={
                  continuousPermit.enabled
                    ? continuousPermit.reason
                    : strategyCanary.status === "revoked" && strategyCanary.recovery
                    ? `再签发：${fmt(strategyCanary.recovery.trades, 0)} / ${fmt(strategyCanary.recovery.required_trades, 0)} 笔，${fmt(strategyCanary.recovery.symbols, 0)} / ${fmt(strategyCanary.recovery.required_symbols, 0)} 个币，PF ${fmt(strategyCanary.recovery.profit_factor, 2)}，观察还需 ${fmt(Number(strategyCanary.recovery.seconds_until_observation_complete || 0) / 60, 0)} 分钟`
                    : strategyCanary.enabled
                    ? `${strategyCanary.release_id || "-"} · ${fmt(strategyCanary.used_opportunities, 0)} / ${fmt(strategyCanary.max_opportunities, 0)} 次 · 当前 ${fmt(strategyCanary.risk_multiplier, 2)}x · ${canaryReasonLabel[strategyCanary.reason] || "只对同版本合格候选生效"}`
                    : "未授权当前版本；不会穿透全局风险保护"
                }
                tone={strategyCanary.allowed ? "positive" : ""}
              />
              <MetricCard
                title={continuousPermit.enabled ? "局部组合证据" : "局部组合熔断"}
                value={`${activeLocalCircuitBlocks} 个组合`}
                sub={continuousPermit.enabled ? "先按资金流、量能和市场状态预排，再为决策短名单补齐聪明钱" : "显示本轮仍被阻断的组合；只冻结同市场状态、方向、形态和入场阶段"}
                tone={activeLocalCircuitBlocks > 0 ? "negative" : "positive"}
              />
              <MetricCard
                title={`${performanceGuard.active_strategy_version || "当前版本"} 版本风险线`}
                value={continuousPermit.enabled ? (continuousPermit.allowed ? "连续准入可用" : "安全条件暂停") : performanceGuard.release_equity_guard?.fallback_active ? "已停止新开仓" : "全仓短打可用"}
                sub={continuousPermit.enabled ? `S0 普通日亏损停牌${stageRoute.daily_loss_stop_enabled ? "开启" : "关闭"} · 账户权益到 ${fmt(config.hard_stop_equity, 2)}U 时硬停止` : `版本高点 ${fmt(performanceGuard.release_equity_guard?.peak_equity, 4)}U · 回撤 ${fmt(performanceGuard.release_equity_guard?.drawdown_pct, 2)}% / ${fmt(performanceGuard.release_equity_guard?.fallback_threshold_pct ?? 35, 2)}%`}
                tone={continuousPermit.enabled ? (continuousPermit.allowed ? "positive" : "negative") : performanceGuard.release_equity_guard?.fallback_active ? "negative" : "positive"}
              />
              <MetricCard
                title="当日权益基准"
                value={`${fmt(state.daily_start_equity, 4)} U`}
                sub={`${state.daily_session_date || "等待初始化"}（UTC），跨日自动重置`}
              />
              <MetricCard
                title="S0 当日净利润锁"
                value={
                  dailyProfitLock.active
                    ? "已锁定新开仓"
                    : dailyProfitLock.pending_flat
                      ? "达到目标，等待空仓"
                      : `${fmt(dailyProfitLock.progress_pct, 1)}%`
                }
                sub={
                  dailyProfitLock.enabled
                    ? `已实现 ${fmt(dailyProfitLock.realized_net_pnl, 4)}U / 目标 ${fmt(dailyProfitLock.target_profit_usdt, 4)}U（${fmt(dailyProfitLock.target_pct, 0)}%）· UTC 次日重置`
                    : "当前阶段未启用"
                }
                tone={dailyProfitLock.active ? "positive" : dailyProfitLock.pending_flat ? "negative" : ""}
              />
            </div>
            <div className="panel decision-guide">
              <div className="panel-head">
                <div>
                  <h2>系统现在怎么决定开仓</h2>
                  <p>把页面上容易混淆的两类数据分开：币种指标决定“它有没有机会”，全局指标决定“当前市场允许多大胆”。</p>
                </div>
              </div>
              <div className="chip-row">
                <span className="chip">1. 币种信号：方向、排名、净期望、资金流</span>
                <span className="chip">2. 全局校准：近24小时统一调整门槛</span>
                <span className="chip">3. 硬门检查：成本、盘口、最小下单量</span>
                <span className="chip">4. 执行保护：最新余额 + 止盈止损</span>
              </div>
            </div>
            <ProtectionAuditPanel audit={runtime?.protection_audit} projection={runtime?.account_projection} supervisor={runtime?.account_supervisor} />
            <div className="metrics">
              <MetricCard
                title={`${activeStrategyLabel} 快速退出监督`}
                value={runtimeProtectionActions.length > 0 ? (activeRuntimeProtection.reason_label || activeRuntimeProtection.reason || "已执行检查") : `${fmt(config.runtime_protection_supervisor_seconds || 5, 0)} 秒监督中`}
                sub={runtimeProtectionActions.length > 0
                  ? `${activeRuntimeProtection.symbol || "-"} · 价格 ${activeRuntimeProtection.price_source || "等待"} · ATR ${activeRuntimeProtection.atr_source || "等待"}`
                  : `未兑现 ${fmt(config.opportunity_v473_stagnation_seconds || 180, 0)} 秒 · 最长 ${fmt((config.opportunity_v473_max_hold_seconds || 480) / 60, 1)} 分钟 · WebSocket 价格优先`}
                tone={runtimeProtection.error ? "negative" : "positive"}
              />
              <MetricCard
                title="S0 MoE 研究影子"
                value={moe.runtime_model_enabled ? "在线记录中" : "未启用"}
                sub={moe.runtime_model_enabled ? `${moe.version || "未知版本"} · ${fmt(moe.active_gates, 0)} 个研究门槛 · 只记录，不影响实盘` : "模型不会自动训练，也不会接管实盘"}
                tone={moe.runtime_model_enabled ? "positive" : ""}
              />
              <MetricCard
                title="线上选中影子结果"
                value={Number(moeSelected.closed || 0) > 0
                  ? `${fmt(moeSelected.closed, 0)} 笔 · PF ${fmt(moeSelected.profit_factor, 2)}`
                  : "等待首批影子平仓"}
                sub={`当前模型已评估 ${fmt(moeEvaluated.total, 0)} 笔 · 净收益 ${fmt(moeSelected.net_pnl, 4)}U`}
                tone={Number(moeSelected.net_pnl || 0) > 0 ? "positive" : undefined}
              />
              <MetricCard
                title={`${moeCandidate.version || "MoE"} 本地训练`}
                value={moeCandidate.decision === "shadow_candidate" ? "通过离线测试" : "样本外未通过"}
                sub={`${fmt(moeCandidate.untouched_test?.trades, 0)} 笔 · PF ${fmt(moeCandidate.untouched_test?.profit_factor, 2)} · 1.5倍成本 PF ${fmt(moeCandidateStress.profit_factor, 2)} · 不影响实盘`}
                tone={moeCandidate.decision === "shadow_candidate" ? "positive" : "negative"}
              />
              <MetricCard
                title="下一次本地训练"
                value={moeRetraining.ready ? "样本量已就绪" : `${fmt(moeRetraining.selected_closed, 0)} / ${fmt(moeRetraining.required_selected_closes, 0)} 笔`}
                sub={`${fmt(moeRetraining.selected_regimes, 0)} / ${fmt(moeRetraining.required_regimes, 0)} 种市场状态 · 样本达标不等于模型可放行`}
              />
            </div>
            <TargetProgressPanel target={target} />
            <SignalExplain best={best} />
            <div className="panel">
              <h2>最高分候选</h2>
              <CandidateTable rows={candidates.slice(0, 5)} compact />
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
                  <p>系统先大范围召回，再逐层粗排、精排和竞价；当前版本先判断币种结构，再对决策短名单补齐聪明钱。</p>
                </div>
              </div>
              <FunnelPanel funnel={funnel} />
            </div>
            <V4OpportunityPanel funnel={funnel} performanceGuard={performanceGuard} />
            <div className="panel">
              <div className="panel-head">
                <div>
                  <h2>候选币排名</h2>
                  <p>当前版本是实盘排序器；多空按结构、市场状态和当前版本证据判断，普通亏损只降仓，不会因为单笔亏损全局停牌。</p>
                </div>
              </div>
              <CandidateTable rows={candidates} />
            </div>
          </section>
        )}

        {active === "review" && <ReviewPanel chartData={chartData} snapshots={data.snapshots} learning={data.liveLearning} shadow={data.shadow} reaction={data.liveReaction} onSync={syncLiveLearning} />}
        {active === "config" && <ConfigPanel config={config} onSave={saveConfig} onTestApi={testBinanceApi} />}
        {active === "system" && (
          <section className="stack">
            <StageRoutePanel route={stageRoute} profiles={stageProfiles} stream={stream} userStream={userStream} rate={binanceRate} equity={account.equity} storage={data.status?.storage || {}} />
            <TrainingDataQualityPanel quality={data.trainingQuality} />
            <LogsPanel rows={data.logs} />
          </section>
        )}
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
        <MetricCard title="扫描守护" value={runtime?.scan_supervisor?.healthy === false ? "正在恢复" : "正常"} sub={runtime?.scan_supervisor?.restart_reason || `超时线 ${fmt(runtime?.scan_supervisor?.timeout_seconds, 0)} 秒`} tone={runtime?.scan_supervisor?.healthy === false ? "negative" : "positive"} />
        <MetricCard title="全量数据时间" value={freshnessText(runtime?.background_scan?.updated_at)} sub="候选池与排名更新时间" />
        <MetricCard title="实时机会时间" value={freshnessText(runtime?.fast_lane?.updated_at)} sub="WebSocket 异动触发更新时间" />
        <MetricCard title="实时快车道" value={`${fmt(runtime?.fast_lane?.elapsed_seconds, 2)} 秒`} sub={(runtime?.fast_lane?.symbols || []).join("、") || "等待 WebSocket 机会"} tone={(runtime?.fast_lane?.elapsed_seconds || 0) <= 5 ? "positive" : undefined} />
        <MetricCard title="后台全量扫描" value={`${fmt(runtime?.background_scan?.elapsed_seconds ?? funnel?.elapsed_seconds, 2)} 秒`} sub="后台更新，不阻塞实时机会" />
        <MetricCard title="WebSocket 状态" value={stream.connected ? "实时盯盘中" : "未连接"} sub={stream.last_error || `${fmt(stream.symbols?.length, 0)} 币 · ${fmt(stream.connection_count, 0)} 条连接 · 数据年龄 ${fmt(stream.age_seconds, 0)} 秒`} tone={stream.connected ? "positive" : "negative"} />
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
        <MetricCard title="建议模式" value={modeLabel[stageProfile?.recommended_mode] || stageProfile?.recommended_mode || "-"} sub={`基础风险 ${fmt(stageProfile?.base_risk_pct, 2)}%`} />
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

function entryTypeLabel(item: any, fallback = "观察") {
  const signal = item?.signal || {};
  const mode = String(item?.mode || signal.mode || "");
  const entryType = String(item?.entry_type || signal.entry_type || "");
  if (mode === "yolo_scalp") {
    const labels: Record<string, string> = {
      standard: "标准剥头皮",
      extreme_scalp: "强势剥头皮",
      preemptive: "抢跑剥头皮",
      momentum: "动量剥头皮",
      observe_standard: "抢跑剥头皮",
      small_standard: "抢跑剥头皮",
      extreme_probe: "火药桶剥头皮",
      weak_quality_probe: "小单探路",
      orderbook_impact: "盘口冲击",
      volume_scalp: "放量剥头皮",
      imbalance_probe: "失衡试探",
    };
    return labels[entryType] || item?.entry_type_label || signal.entry_type_label || fallback;
  }
  return item?.entry_type_label || signal.entry_type_label || fallback;
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
  const scalp = best.scalp_signal || {};
  const structure = best.market_structure || {};
  const v4 = best.opportunity_v4 || {};
  const regimePolicy = v4.regime_policy || {};
  const recoveryComponents = regimePolicy.recovery_components || {};
  const recoveryPassed = Object.values(recoveryComponents).filter(Boolean).length;
  const recoveryTotal = Object.keys(recoveryComponents).length;
  const positionConfidence = v4.position_confidence || {};
  const localCircuit = v4.local_circuit || {};
  const smartFlow = best.smart_flow || {};
  const adaptive = v4.adaptive_calibration || positionConfidence.adaptive_calibration || {};
  const moe = v4.moe || {};
  const isGlobalCalibration = ["adaptive_v49_global", "adaptive_v410_global", "adaptive_v411_global"].includes(adaptive.schema);
  return (
    <div className="panel signal-explain">
      <h2>当前策略解释</h2>
      <div className="explain-grid">
        <div><span>最高候选</span><strong>{best.symbol || "-"}</strong></div>
        <div><span>方向</span><strong>{signalLabel(best.direction || signal.signal)}</strong></div>
        <div><span>信号类型</span><strong>{entryTypeLabel(best)}</strong></div>
        <div><span>综合评分</span><strong>{fmt(best.score, 2)}</strong></div>
        <div><span>离触发价</span><strong>{fmt(signal.distance_to_trigger_pct, 3)}%</strong></div>
        <div><span>当前结论</span><strong>{best.passed ? "允许执行" : "继续等待"}</strong></div>
        <div><span>MoE 影子建议</span><strong>{!moe.enabled ? "未评估" : !moe.active_gate ? "当前场景未验证" : moe.passed ? "模型建议记录机会" : "模型优势不足"}</strong></div>
        <div><span>MoE 预测/门槛</span><strong>{moe.active_gate ? `${fmt(moe.model_edge, 3)} / ${fmt(moe.edge_floor, 3)}` : "不参与实盘准入"}</strong></div>
        <div><span>{`${String(v4.strategy_version || "v5.1").toUpperCase()} 本轮排名`}</span><strong>{v4.enabled ? `前 ${fmt((1 - Number(v4.rank_percentile || 0)) * 100, 0)}%` : "-"}</strong></div>
        <div><span>{`${String(v4.strategy_version || "v5.1").toUpperCase()} 准入通道`}</span><strong>{v4.admission_lane === "full_bet" ? "全仓短打" : "仅影子"}</strong></div>
        <div><span>{isGlobalCalibration ? "校准范围" : "方向关系"}</span><strong>{isGlobalCalibration ? "全局 · 不按币种" : adaptive.relation === "aligned" ? "顺势" : adaptive.relation === "countertrend" ? "逆势" : adaptive.relation === "neutral" ? "中性" : "等待校准"}</strong></div>
        <div><span>{isGlobalCalibration ? "全局风险倍率" : "动态仓位倍率"}</span><strong>{adaptive.enabled ? `${fmt(adaptive.risk_multiplier, 2)}x` : "未启用"}</strong></div>
        <div><span>12 / 24小时样本</span><strong>{adaptive.enabled ? `${fmt(adaptive.stats_12h?.shadow_trades ?? adaptive.stats_12h?.current_trades, 0)} 影子 / ${fmt(adaptive.stats_24h?.live_trades ?? adaptive.stats_24h?.current_trades, 0)} 实盘` : "-"}</strong></div>
        <div><span>机会置信度</span><strong>{positionConfidence.applied ? `${fmt(Number(positionConfidence.confidence || 0) * 100, 1)}%` : "不适用"}</strong></div>
        <div><span>计划止损风险</span><strong>{positionConfidence.target_initial_risk_pct != null ? `${fmt(positionConfidence.target_initial_risk_pct, 2)}%` : "等待准入"}</strong></div>
        <div><span>保证金方式</span><strong>{v4.admission_lane === "full_bet" ? "约 90% · 单仓全进全出" : "不下单"}</strong></div>
        <div><span>动态杠杆范围</span><strong>{v4.admission_lane === "full_bet" ? "3x - 10x" : "-"}</strong></div>
        <div><span>保守净期望</span><strong>{v4.enabled ? `${fmt(v4.lower_expected_net_pct, 3)}%` : "未形成当前版本触发"}</strong></div>
        <div><span>收益成本比</span><strong>{v4.enabled ? `${fmt(v4.cost_ratio, 2)}x` : "未形成当前版本触发"}</strong></div>
        <div><span>3 分钟兑现检查</span><strong>{profile.stagnation_seconds ? `${fmt(profile.stagnation_seconds, 0)} 秒内至少 ${fmt(profile.stagnation_min_profit_pct, 3)}%` : "不适用"}</strong></div>
        <div><span>最长持仓</span><strong>{profile.max_hold_seconds ? `${fmt(profile.max_hold_seconds / 60, 1)} 分钟` : `${fmt(profile.max_hold_bars, 0)} 根 K 线`}</strong></div>
        <div><span>当前版本全局24小时</span><strong>{adaptive.ready ? (adaptive.adjustment_action || "已就绪") : "样本积累中"}</strong></div>
        <div><span>全局市场状态</span><strong>{adaptive.market_regime_state === "trend" ? "趋势市场" : "震荡或数据不足"} · {adaptive.global_direction_bias === "LONG" ? "全局偏多" : adaptive.global_direction_bias === "SHORT" ? "全局偏空" : "方向中性"}</strong></div>
        <div><span>全局聪明钱代理</span><strong>{adaptive.smart_flow_global?.available ? `${adaptive.smart_flow_global.bias || "中性"} · ${fmt(Number(adaptive.smart_flow_global.average_score || 0), 3)} · ${fmt(Number(adaptive.smart_flow_global.sample_count || 0), 0)} 样本` : "当前版本暂无可用聚合数据"}</strong></div>
        <div><span>全局有效门槛</span><strong>{adaptive.effective_thresholds ? `前 ${fmt((1 - Number(adaptive.effective_thresholds.rank_percentile || 0)) * 100, 0)}% · 成本比 ${fmt(adaptive.effective_thresholds.cost_ratio, 2)}x` : "保持基础门槛"}</strong></div>
        <div><span>趋势衰竭</span><strong>{v4.exhaustion?.enabled ? `${fmt(Number(v4.exhaustion.score || 0) * 100, 0)}% · ${v4.exhaustion.blocked ? "禁止追价" : v4.exhaustion.caution ? "谨慎" : "健康"}` : "未启用"}</strong></div>
        <div><span>同币再入场</span><strong>{v4.reentry_policy?.state === "blocked" ? "等待结构重置" : v4.reentry_policy?.state === "reset" ? "新结构已确认" : v4.reentry_policy?.state === "caution" ? "降仓观察" : "正常"}</strong></div>
        <div><span>五项确认</span><strong>{v4.enabled ? `${fmt(v4.v44_confirmations, 0)} / ${fmt(v4.v44_confirmations_required, 0)}` : "-"}</strong></div>
        <div><span>旧版本证据</span><strong>仅作 0.25 权重启动先验</strong></div>
        <div><span>当前版本局部证据</span><strong>{localCircuit.blocked ? "仅作风险背景" : "正常记录"}</strong></div>
        <div><span>市场状态</span><strong>{structure.market_regime_label || best.market_state?.label || "-"}</strong></div>
        <div><span>聪明钱方向</span><strong>{smartFlow.available ? (smartFlow.bias === "LONG" ? "偏多" : smartFlow.bias === "SHORT" ? "偏空" : "中性") : "等待数据"}</strong></div>
        <div><span>聪明钱修正</span><strong>{smartFlow.available ? `${Number(best.smart_flow_score_delta || 0) >= 0 ? "+" : ""}${fmt(best.smart_flow_score_delta, 2)} 分 · 置信 ${fmt(Number(smartFlow.confidence || 0) * 100, 0)}%` : "不影响准入"}</strong></div>
        <div><span>真实收益/成本</span><strong>{v4.enabled ? `${fmt(v4.cost_ratio, 2)}x` : fmt(best.cost_ratio, 2)}</strong></div>
        <div><span>{"\u4fdd\u62a4\u6863\u6848"}</span><strong>{protectionLabel}</strong></div>
        <div><span>{"\u6b62\u635f / \u6b62\u76c8 ATR"}</span><strong>{fmt(protectionStopAtr, 2)} / {fmt(protectionTakeAtr, 2)}</strong></div>
        <div><span>市场子阶段</span><strong>{
          regimePolicy.market_phase === "panic_recovery"
            ? "恐慌恢复候选"
            : regimePolicy.market_phase === "panic_decelerating"
              ? "恐慌正在减速"
              : regimePolicy.market_phase === "panic_expansion"
                ? "恐慌仍在扩散"
                : "常规市场"
        }</strong></div>
        <div><span>恢复条件</span><strong>{recoveryTotal ? `${recoveryPassed} / ${recoveryTotal} 项` : "不适用"}</strong></div>
        <div><span>盘口点差</span><strong>{scalp.enabled ? `${fmt(scalp.spread_pct, 3)}%` : "-"}</strong></div>
        <div><span>盘口失衡</span><strong>{scalp.enabled ? fmt(scalp.directed_imbalance, 3) : "-"}</strong></div>
        <div><span>扣费后空间</span><strong>{scalp.enabled ? `${fmt(scalp.net_profit_pct, 3)}%` : "-"}</strong></div>
        <div><span>最长持仓</span><strong>{profile.max_hold_seconds ? `${fmt(profile.max_hold_seconds, 0)} 秒` : `${protection.max_hold_bars || profile.max_hold_bars || "-"} 根K线`}</strong></div>
        <div><span>{"\u521d\u59cb\u6b62\u635f"}</span><strong>{fmt(protection.initial_stop ?? signal.stop, 6)}</strong></div>
        <div><span>{"\u521d\u59cb\u6b62\u76c8"}</span><strong>{fmt(protection.initial_take_profit ?? signal.take_profit, 6)}</strong></div>
      </div>
      {scalp.enabled && !scalp.passed && scalp.blockers?.length ? (
        <p>盘口剥头皮未通过：{scalp.blockers.slice(0, 4).join("；")}</p>
      ) : null}
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
      <MetricCard
        title={funnel?.opportunity_v4?.enabled ? `${funnel?.opportunity_v4?.strategy_version || "V4"} 实盘候选` : "剥头皮信号"}
        value={funnel?.opportunity_v4?.enabled
          ? `${fmt(funnel?.opportunity_v4?.admitted, 0)} 个`
          : `${fmt(funnel?.extreme_v2?.scalp, 0)} 个`}
        sub={funnel?.opportunity_v4?.enabled ? `${funnel?.opportunity_v4?.strategy_version || "V4"} 当前版本全仓短打准入` : "盘口冲击 / 放量剥头皮 / 失衡试探"}
      />
      <MetricCard
        title="市场自适应"
        value={funnel?.adaptive_market?.label || "等待行情"}
        sub={`中位波动 ${fmt(funnel?.adaptive_market?.median_abs_change_pct, 2)}% · 75分位 ${fmt(funnel?.adaptive_market?.p75_abs_change_pct, 2)}%`}
      />
      <MetricCard title="本轮耗时" value={`${fmt(funnel?.elapsed_seconds, 3)} 秒`} sub="用于判断是否需要自动降级" />
    </div>
  );
}

function V4OpportunityPanel({ funnel, performanceGuard }: { funnel: any; performanceGuard: any }) {
  const structure = funnel?.market_structure || {};
  const v4 = funnel?.opportunity_v4 || {};
  const smartFlow = funnel?.smart_flow || {};
  if (!v4.enabled) return null;
  const strategyLabel = String(v4.strategy_version || "v5.1").toUpperCase();
  const v5Active = String(v4.strategy_version || "").toLowerCase().startsWith("v5.");
  const topBlockers = Object.entries(v4.blocked_categories || v4.blocked_reasons || {})
    .sort((left: any, right: any) => Number(right[1]) - Number(left[1]))
    .slice(0, 3);
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>{strategyLabel} 事件证据与全仓短打</h2>
          <p>先形成当前版本基础排名，再对决策短名单补齐聪明钱并最终重排；合格后使用单仓全进全出，旧版本信用和全局 PF 不参与准入。</p>
        </div>
      </div>
      <div className="metrics">
        <MetricCard title="市场状态" value={structure.market_label || "等待行情"} sub={`上涨广度 ${fmt(Number(structure.breadth_positive || 0) * 100, 1)}% · 离散 ${fmt(structure.dispersion_pct, 2)}%`} />
        <MetricCard title="真实成本线" value={`${fmt(structure.observed_cost_floor_pct, 3)}%`} sub="手续费、资金费和历史滑点的保守估计" />
        <MetricCard title="有实时触发" value={`${fmt(v4.shadow_ready, 0)} 个`} sub="已经形成方向和止盈止损，可进入后续筛选" />
        <MetricCard title="结构可交易" value={`${fmt(v4.structure_ready, 0)} 个`} sub="方向、入场形态与当前市场结构有效" />
        <MetricCard title="盘口可执行" value={`${fmt(v4.liquidity_ready, 0)} 个`} sub="按本次预计下单额动态检查深度与点差" />
        <MetricCard title="聪明钱覆盖" value={`${fmt(smartFlow.available, 0)} / ${fmt(smartFlow.shortlisted, 0)} 个`} sub={`短名单上限 ${fmt(smartFlow.symbol_limit, 0)} 个；${fmt(smartFlow.adjusted, 0)} 个获得软评分修正`} tone={smartFlow.enabled ? "positive" : ""} />
        <MetricCard title="旧证据阻断" value={`${fmt(v4.local_circuit_blocked, 0)} 个`} sub={`旧版本仅供复盘，不参与 ${strategyLabel} 准入、仓位或模型判断`} tone="positive" />
        <MetricCard title="连续准入" value={performanceGuard?.allowed ? "可开仓" : "安全暂停"} sub="普通亏损只递进降仓；S0 普通日亏损停牌关闭，5U 硬停止和运行安全异常仍会暂停" tone={performanceGuard?.allowed ? "positive" : "negative"} />
        <MetricCard title="全仓短打" value={`${fmt(v4.admitted, 0)} 个`} sub="约 90% 可用保证金，单币单向、一次全平" tone={Number(v4.admitted || 0) > 0 ? "positive" : ""} />
        <MetricCard title={`${strategyLabel} 准入底线`} value={v5Active ? "0.02% / -0.08%" : "按当前版本配置"} sub="扣费后期望 / 保守净期望；还需同时通过确认项、排名和流动性硬门" />
        <MetricCard title="快速兑现" value={v5Active ? "3 根K线内" : "按当前版本配置"} sub="独立 5 秒保护监督；达到保本触发后收紧风险，超时退出" />
        <MetricCard title="计划风险" value={v5Active ? "12% - 30%" : "按当前版本配置"} sub={v5Active ? "普通机会 12%，强机会逐级提高；三连止损冷却后回到 12% 基线" : "由当前版本的风险配置决定"} />
        <MetricCard title="动态杠杆" value="3 - 10 倍" sub="按止损距离和成本选择，不追加、不分批止盈" />
      </div>
      <p>{topBlockers.length ? `本轮主要等待原因：${topBlockers.map(([reason, count]: any) => `${reason}（${count}）`).join("；")}` : "本轮没有候选被阻断。"}</p>
    </div>
  );
}

function smartFlowDisplay(row: any) {
  const smartFlow = row.smart_flow || {};
  if (smartFlow.available) {
    const bias = smartFlow.bias === "LONG" ? "偏多" : smartFlow.bias === "SHORT" ? "偏空" : "中性";
    const applied = smartFlow.status === "applied";
    return {
      value: `${bias} ${Number(row.smart_flow_score_delta || 0) >= 0 ? "+" : ""}${fmt(row.smart_flow_score_delta, 1)}`,
      sub: applied ? `置信 ${fmt(Number(smartFlow.confidence || 0) * 100, 0)}% · 已计入最终排名` : `置信 ${fmt(Number(smartFlow.confidence || 0) * 100, 0)}% · 未达到计分置信度`,
    };
  }
  if (smartFlow.status === "no_v4_trigger") return { value: "不适用", sub: "尚未形成 V4 可执行触发" };
  if (smartFlow.status === "outside_v4_shortlist") return { value: "未深查", sub: "未进入本轮 V4 决策短名单，按中性 0 分" };
  if (smartFlow.status === "awaiting_background_refresh") return { value: "等待缓存", sub: "快车道按中性 0 分，不额外请求 REST" };
  return { value: "数据不可用", sub: "本轮按中性 0 分，不阻断准入" };
}

function CandidateTable({ rows, compact = false }: { rows: any[]; compact?: boolean }) {
  const calibrationDisplay = (item: any) => {
    const adaptive = item?.adaptive_calibration || {};
    if (!adaptive.enabled) return { value: "未启用", sub: "当前版本没有启用动态校准" };
    if (["adaptive_v49_global", "adaptive_v410_global", "adaptive_v411_global"].includes(adaptive.schema)) {
      const stats = adaptive.stats_24h || {};
      const sampleText = `${fmt(stats.shadow_trades, 0)} 影子 / ${fmt(stats.live_trades, 0)} 实盘`;
      const state = adaptive.ready ? (adaptive.positive ? "正向" : adaptive.negative ? "收紧" : "观察") : "积累样本";
      return {
        value: `全局 ${state}`,
        sub: `24h ${sampleText} · PF ${fmt(stats.profit_factor, 2)} · 不按币种调参`,
      };
    }
    const relation = adaptive.relation === "aligned" ? "顺势" : adaptive.relation === "countertrend" ? "逆势" : "中性";
    return {
      value: `${relation} ${fmt(adaptive.risk_multiplier, 2)}x`,
      sub: `旧版局部证据 ${fmt(adaptive.stats_12h?.current_trades, 0)}/${fmt(adaptive.stats_24h?.current_trades, 0)} 笔`,
    };
  };
  return (
    <div className="table-wrap">
      {!compact && <div className="table-hint"><strong>读表提示：</strong>“币种”列来自该币自己的实时信号；“全局校准”是所有候选共享的 24 小时策略状态，不会单独调整某个币。</div>}
      <table>
        <thead>
          <tr>
            <th>状态</th>
            <th>币种</th>
            <th>方向</th>
            <th>信号类型</th>
            <th>币种排名</th>
            <th>币种净期望</th>
            <th>币种同类证据</th>
            <th>币种资金流</th>
            <th>MoE影子建议</th>
            <th>全局校准（共享）</th>
            <th>币种趋势衰竭</th>
            <th>币种再入场</th>
            {!compact && <th>不开仓原因</th>}
            {!compact && <th>距离触发</th>}
            {!compact && <th>市场状态</th>}
            <th>币种成本比</th>
            <th>计划风险%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const smartFlow = smartFlowDisplay(row);
            const preRank = row.opportunity_v4_pre_rank;
            const finalRank = row.opportunity_v4;
            const moe = finalRank?.moe || {};
            const calibration = calibrationDisplay(finalRank);
            return (
            <tr key={`${row.symbol}-${row.direction}-${index}`}>
              <td><span className={row.passed ? "pill ok" : "pill"}>{row.passed ? (row.opportunity_v4?.validated ? "核心已验证" : row.opportunity_v4?.provisional ? "核心受限" : row.opportunity_v4?.bootstrap_admitted ? "核心试运行" : row.opportunity_v4?.exploration_admitted ? "受限探索" : "通过") : "等待"}</span></td>
              <td className="symbol">{row.symbol}</td>
              <td>{signalLabel(row.direction || row.signal?.signal)}</td>
              <td>{entryTypeLabel(row, "-")}</td>
              <td>{finalRank?.enabled ? `前 ${fmt((1 - Number(finalRank.rank_percentile || 0)) * 100, 0)}%` : "未形成触发"}<small>{preRank && finalRank?.enabled ? `预排前 ${fmt((1 - Number(preRank.rank_percentile || 0)) * 100, 0)}% → 最终排名` : ""}</small></td>
              <td className={Number(finalRank?.lower_expected_net_pct || 0) >= 0 ? "positive-text" : "negative-text"}>{finalRank?.enabled ? `${fmt(finalRank.lower_expected_net_pct, 3)}%` : "未形成触发"}</td>
              <td>{finalRank?.enabled ? `${fmt(finalRank.evidence?.selected?.trades, 0)} 笔` : "未形成触发"}<small>{finalRank?.enabled ? pfLabel(finalRank.evidence?.selected?.profit_factor, finalRank.evidence?.selected?.trades) : ""}</small></td>
              <td>{smartFlow.value}<small>{smartFlow.sub}</small></td>
              <td>{!moe.enabled ? "未评估" : !moe.active_gate ? "场景未验证" : moe.passed ? "建议记录" : "优势不足"}<small>{moe.active_gate ? `${moe.expert || "-"} · ${fmt(moe.model_edge, 3)} / ${fmt(moe.edge_floor, 3)} · 不影响实盘` : "独立影子模型"}</small></td>
              <td>{calibration.value}<small>{calibration.sub}</small></td>
              <td>{finalRank?.exhaustion?.enabled ? `${fmt(Number(finalRank.exhaustion.score || 0) * 100, 0)}%` : "-"}<small>{finalRank?.exhaustion?.blocked ? "禁止追价" : finalRank?.exhaustion?.caution ? "降仓" : finalRank?.exhaustion?.enabled ? "健康" : ""}</small></td>
              <td>{finalRank?.reentry_policy?.state === "blocked" ? "等待新事件" : finalRank?.reentry_policy?.state === "reset" ? "新结构已确认" : finalRank?.reentry_policy?.state === "caution" ? "谨慎" : "正常"}<small>{finalRank?.reentry_policy?.duplicate_event ? `${fmt(finalRank.reentry_policy.dedupe_minutes, 0)} 分钟内同一事件` : finalRank?.reentry_policy?.event_limit_reached ? `6 小时已完成 ${fmt(finalRank.reentry_policy.recent_event_count, 0)} 次` : finalRank?.reentry_policy?.live_loss_streak ? `同结构连亏 ${fmt(finalRank.reentry_policy.live_loss_streak, 0)}` : ""}</small></td>
              {!compact && <td className="reason-cell">{row.decision_reason || row.reason}</td>}
              {!compact && <td>{fmt(row.signal?.distance_to_trigger_pct, 3)}%</td>}
              {!compact && <td>{row.market_structure?.market_regime_label || row.market_state?.label || "-"}</td>}
              <td>{fmt(row.cost_ratio, 2)}</td>
              <td>{fmt(row.risk_pct, 2)}%</td>
            </tr>
          )})}
        </tbody>
      </table>
    </div>
  );
}

function LiveLearningPanel({ data, onSync }: { data: LiveLearningData; onSync: () => Promise<void> }) {
  const rows = data.scores || [];
  const scalpRows = data.scalp_scores || [];
  const extremeRows = data.extreme_scores || [];
  const v4Rows = data.v4_scores || [];
  const v4Net = v4Rows.reduce((sum, row) => sum + Number(row.net_pnl || 0), 0);
  const extremeNet = extremeRows.reduce((sum, row) => sum + Number(row.net_pnl || 0), 0);
  const scalpNet = scalpRows.reduce((sum, row) => sum + Number(row.net_pnl || 0), 0);
  const activePower = v4Rows.filter((row) => Number(row.risk_multiplier || 0) >= 1).length;
  const activeFuse = v4Rows.filter((row) => Number(row.risk_multiplier || 0) < 1).length;
  const renderRows = (items: any[], empty: string) => (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>币种</th>
            <th>方向</th>
            <th>策略族</th>
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
          {items.length === 0 ? (
            <tr><td colSpan={15}>{empty}</td></tr>
          ) : items.map((row) => (
            <tr key={`${row.strategy_family || "legacy"}-${row.symbol}-${row.direction}`}>
              <td className="symbol">{row.symbol}</td>
              <td>{signalLabel(row.direction)}</td>
              <td>{strategyFamilyLabel(row.strategy_family)}</td>
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
  );
  return (
    <section className="stack">
      <div className="metrics">
        <MetricCard title="V4 学习方向" value={String(v4Rows.length)} sub="只统计 V4 自己的真实成交" />
        <MetricCard title="正常以上倍率" value={String(activePower)} sub="V4 独立信用倍率 >= 1" />
        <MetricCard title="受限方向" value={String(activeFuse)} sub="V4 近期亏损方向" tone={activeFuse ? "negative" : ""} />
        <MetricCard title="V4 实盘净盈亏" value={`${fmt(v4Net, 4)} U`} tone={v4Net >= 0 ? "positive" : "negative"} />
      </div>
      <div className="panel">
        <div className="panel-head"><div><h2>V4 证据在哪里看</h2><p>“策略实验室”展示 V4 决策、探索和同机会对照影子；本页只展示当前 V4 实盘成交形成的币种方向信用。</p></div></div>
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>机会引擎 V4 专用信用分</h2>
            <p>V4 只读取自己的成交结果轻量调整仓位；新版本从中性信用启动，旧 V3 输赢不会混入。</p>
          </div>
          <button className="secondary" onClick={onSync}>同步历史信用分</button>
        </div>
        {renderRows(v4Rows, "V4 还没有产生已平仓实盘样本；当前以 50 分中性信用启动。")}
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>Extreme V2 历史信用分</h2>
            <p>仅用于复盘旧滚仓策略，不参与 V4 候选排序和仓位。历史累计净盈亏 {fmt(extremeNet, 4)} U。</p>
          </div>
        </div>
        {renderRows(extremeRows, "没有 Extreme V2 历史成交。")}
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>盘口剥头皮专用信用分</h2>
            <p>S3 以后才读取这张表；它只由盘口剥头皮成交结果加减，不继承滚仓阶段的输赢。</p>
          </div>
          <span className={scalpNet >= 0 ? "positive-text" : "negative-text"}>累计净盈亏 {fmt(scalpNet, 4)} U</span>
        </div>
        {renderRows(scalpRows, "还没有归因到盘口剥头皮的实盘成交。")}
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>旧策略混合信用分</h2>
            <p>这里保留旧策略历史，仅用于复盘；极限盘口剥头皮模式不会读取这张表来决定仓位。</p>
          </div>
        </div>
        {renderRows(rows, "暂无旧策略信用记录。")}
      </div>
    </section>
  );
}

function ReviewPanel({ chartData, snapshots, learning, shadow, reaction, onSync }: { chartData: any[]; snapshots: any[]; learning: LiveLearningData; shadow: ShadowData; reaction: LiveReactionData | null; onSync: () => Promise<void> }) {
  const [tab, setTab] = useState("equity");
  const tabs = [["equity", "收益曲线"], ["trades", "实盘证据"], ["lab", "策略实验室"], ["shadow", "影子明细"], ["reaction", "实时风控"]];
  const active = shadow.active_release || {};
  const challenger = shadow.challenger_release || {};
  const activeRecent = active.recent || {};
  const challengerAll = challenger.all || {};
  const hasChallenger = Boolean(challenger.strategy_version);
  return (
    <section className="stack">
      <div className="metrics">
        <MetricCard title="最新权益" value={`${fmt(snapshots.at(-1)?.equity, 4)} U`} />
        <MetricCard title={`${active.strategy_version || "V4"} 当前影子`} value={`${fmt(activeRecent.closed, 0)} 笔 · ${pfLabel(activeRecent.profit_factor, activeRecent.closed)}`} sub={`近期净收益 ${fmt(activeRecent.net_pnl, 4)} U，只对应正在执行的版本`} tone={Number(activeRecent.net_pnl || 0) >= 0 ? "positive" : "negative"} />
        <MetricCard title={hasChallenger ? `${challenger.strategy_version} 候选影子` : "策略状态"} value={hasChallenger ? `${fmt(challengerAll.closed, 0)} 笔 · ${pfLabel(challengerAll.profit_factor, challengerAll.closed)}` : "V4 已接管实盘"} sub={hasChallenger ? "候选版本不会自动接管实盘" : "旧 V3 证据已隔离，仅保留复盘"} tone={hasChallenger ? (Number(challengerAll.net_pnl || 0) >= 0 ? "positive" : "negative") : "positive"} />
      </div>
      <div className="panel">
        <div className="review-tabs">
          {tabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : "secondary"} onClick={() => setTab(value)}>{label}</button>)}
        </div>
      </div>
      {tab === "equity" && <div className="panel chart-panel"><h2>权益与盈亏曲线</h2><ResponsiveContainer width="100%" height={360}><ReLineChart data={chartData}><CartesianGrid strokeDasharray="3 3" stroke="#14345a" /><XAxis dataKey="time" stroke="#91a7c4" minTickGap={32} /><YAxis stroke="#91a7c4" /><Tooltip contentStyle={{ background: "#07111f", border: "1px solid #22d3ee", color: "#e5f6ff" }} /><Legend /><Line type="monotone" dataKey="equity" name="账户权益" stroke="#38bdf8" strokeWidth={2} dot={false} /><Line type="monotone" dataKey="available" name="可用余额" stroke="#22c55e" strokeWidth={2} dot={false} /><Line type="monotone" dataKey="unrealized" name="未实现盈亏" stroke="#f97316" strokeWidth={2} dot={false} /></ReLineChart></ResponsiveContainer></div>}
      {tab === "trades" && <LiveLearningPanel data={learning} onSync={onSync} />}
      {tab === "lab" && <StrategyLabPanel data={shadow} />}
      {tab === "shadow" && <ShadowTradingPanel data={shadow} />}
      {tab === "reaction" && <LiveReactionPanel data={reaction} />}
    </section>
  );
}

function StageRoutePanel({ route, profiles, stream, userStream, rate, equity, storage }: { route: any; profiles: any[]; stream: any; userStream: any; rate: any; equity: any; storage: any }) {
  const routeReason: Record<string, string> = {
    equity_range: "账户权益位于当前区间",
    hysteresis_hold: "处于阶段切换缓冲区，暂不来回跳档",
    awaiting_confirmation: "正在等待连续确认",
    waiting_for_flat_position: "已有持仓，平仓后再切换策略",
    manual_override: "人工临时指定",
    stage_routing_disabled: "自动阶段路线已关闭",
  };
  const used = Number(rate.used_current_minute || 0);
  const advertised = Number(rate.budgets?.advertised || rate.request_weight_limit || 0);
  return (
    <section className="stack">
      {route.pending && (
        <div className="alert"><AlertTriangle size={18} />当前持仓仍在保护中，系统将在空仓后切换到 {route.pending_stage}（{modeLabel[route.pending_mode] || route.pending_mode}）。</div>
      )}
      <div className="metrics">
        <MetricCard title="当前阶段" value={`${route.stage || "-"} ${route.label || ""}`} sub={routeReason[route.reason] || route.reason || (route.stage ? "按账户权益默认计算" : "等待机器人同步")} tone="positive" />
        <MetricCard title="实际执行策略" value={modeLabel[route.mode] || route.mode || "-"} sub={`策略信用：${strategyFamilyLabel(route.strategy_family)}`} />
        <MetricCard title="账户权益" value={`${fmt(equity, 4)} U`} sub={`阶段确认 ${fmt(route.confirmation_count, 0)} / ${fmt(route.confirmation_required, 0)}`} />
        <MetricCard title="单笔风险上限" value={`${fmt(route.risk_pct, 2)}%`} sub={`保证金上限 ${fmt(route.margin_pct, 1)}%`} tone={Number(route.risk_pct) >= 7 ? "negative" : ""} />
        <MetricCard
          title="仓位约束"
          value={`${fmt(route.max_open_positions, 0)} 仓 / ${fmt(route.leverage, 1)}x`}
          sub={route.stage === "S0" && !route.daily_loss_stop_enabled
            ? `普通日亏损停牌关闭 · 当日净利润 ${fmt(route.daily_profit_target_pct, 0)}% 后锁定`
            : `当日亏损上限 ${fmt(route.daily_loss_limit_pct, 1)}%`}
        />
        <MetricCard title="路由来源" value={route.source === "manual" ? "人工临时模式" : route.source === "disabled" ? "固定模式" : "权益自动路由"} sub={route.manual_until ? `到期：${new Date(route.manual_until).toLocaleString("zh-CN")}` : "使用 5% 升档、10% 降档缓冲"} />
      </div>

      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>自动阶段路线</h2>
            <p>达到边界后需经过缓冲和连续确认；跨策略切换时有持仓就先保持原策略，避免半途改变保护逻辑。</p>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>阶段</th><th>权益区间</th><th>策略</th><th>策略信用</th><th>单笔风险</th><th>保证金上限</th><th>杠杆</th><th>最大持仓</th><th>日内规则</th></tr></thead>
            <tbody>
              {profiles.map((profile) => (
                <tr key={profile.stage} className={profile.stage === route.stage ? "active-row" : ""}>
                  <td><span className={profile.stage === route.stage ? "pill ok" : "pill"}>{profile.stage}</span> {profile.label}</td>
                  <td>{fmt(profile.min_equity, 0)} - {profile.max_equity === null ? "无上限" : fmt(profile.max_equity, 0)} U</td>
                  <td>{profile.overlay_mode ? `${modeLabel[profile.recommended_mode] || profile.recommended_mode} + ${modeLabel[profile.overlay_mode] || profile.overlay_mode}` : modeLabel[profile.recommended_mode] || profile.recommended_mode}</td>
                  <td>{profile.overlay_mode ? "网格 / 剥头皮独立" : strategyFamilyLabel(profile.strategy_family)}</td>
                  <td>{fmt(profile.risk_pct, 2)}%</td>
                  <td>{fmt(profile.margin_pct, 1)}%</td>
                  <td>{fmt(profile.leverage, 1)}x</td>
                  <td>{fmt(profile.max_open_positions, 0)}</td>
                  <td>{profile.stage === "S0" && !profile.daily_loss_stop_enabled ? `盈利 ${fmt(profile.daily_profit_target_pct, 0)}% 后锁定` : `亏损 ${fmt(profile.daily_loss_limit_pct, 1)}% 停牌`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><div><h2>行情与账户数据链路</h2><p>WebSocket 承担实时数据，REST 只用于快照、交易和断线兜底，并为保护单预留请求额度。</p></div></div>
        <div className="metrics">
          <MetricCard title="公共行情流" value={stream.connected ? "已连接" : "已断开"} sub={`${fmt(stream.symbols?.length, 0)} 币 · ${fmt(stream.connection_count, 0)} 条连接 · ${fmt(stream.stream_count, 0)} 个订阅 · 年龄 ${fmt(stream.age_seconds, 0)} 秒`} tone={stream.connected ? "positive" : "negative"} />
          <MetricCard title="增量订单簿" value={`${fmt(stream.full_orderbook_count, 0)} 个`} sub={route.mode === "yolo_scalp" ? "剥头皮阶段按精选候选启用" : "当前阶段保持轻量 depth5"} />
          <MetricCard title="私有账户流" value={userStream.connected ? "已连接" : "REST 兜底"} sub={userStream.last_error || `账户年龄 ${fmt(userStream.account_age_seconds, 0)} 秒`} tone={userStream.connected ? "positive" : ""} />
          <MetricCard title="REST 本分钟" value={`${fmt(used, 0)} / ${fmt(advertised, 0)}`} sub={`交易所额度使用 ${fmt(rate.exchange_limit_used_pct, 1)}%`} tone={rate.cooldown_active ? "negative" : "positive"} />
          <MetricCard title="普通任务预算" value={`${fmt(rate.budgets?.normal, 0)}`} sub={`已使用普通预算 ${fmt(rate.normal_budget_used_pct, 1)}%`} />
          <MetricCard title="限流状态" value={rate.cooldown_active ? "等待恢复" : "正常"} sub={rate.cooldown_active ? `${fmt(rate.cooldown_remaining_seconds, 0)} 秒后重试` : "关键保护和下单保留独立额度"} tone={rate.cooldown_active ? "negative" : "positive"} />
          <MetricCard title="运行数据库" value={`${fmt(storage.database_mb, 1)} MB`} sub={`WAL ${fmt(storage.wal_mb, 1)} MB · 可回收 ${fmt(storage.reclaimable_mb, 1)} MB`} tone={Number(storage.database_mb || 0) > 2048 ? "negative" : ""} />
          <MetricCard title="策略决策序号" value={`${fmt(storage.rows?.strategy_runs, 0)}`} sub="高频决策只保留 2 天；实盘成交与影子样本独立保留" />
        </div>
      </div>
    </section>
  );
}

function strategyFamilyLabel(value?: string) {
  const labels: Record<string, string> = {
    extreme_v3_roll: "机会引擎 V3 滚仓",
    extreme_v4_roll: "机会引擎 V4 滚仓",
    extreme_v4_control: "V4 简单突破对照",
    extreme_v31_challenger: "V3.1 历史归档",
    extreme_v2_roll: "Extreme V2 滚仓",
    orderbook_scalp: "盘口剥头皮",
    grid_stable: "稳定网格",
    legacy_mixed: "旧混合策略",
  };
  return labels[value || ""] || value || "旧混合策略";
}

function pfLabel(value: any, trades: any) {
  return Number(value) >= 999 ? `暂无亏损样本（${fmt(trades, 0)}笔）` : `PF ${fmt(value, 2)}`;
}

function strategyRoleLabel(value?: string) {
  const labels: Record<string, string> = {
    active: "当前实盘",
    challenger: "候选影子",
    archived: "历史归档",
    legacy: "旧数据",
  };
  return labels[value || ""] || value || "旧数据";
}

function StrategyLabPanel({ data }: { data: ShadowData }) {
  const active = data.active_release || {};
  const challenger = data.challenger_release || {};
  const activeRecent = active.recent || {};
  const recovery = active.recovery || {};
  const candidate = challenger.all || {};
  const validation = challenger.validation || {};
  const hasChallenger = Boolean(challenger.strategy_version);
  const activeStrategyLabel = String(active.strategy_version || "v5.1").toUpperCase();
  const evidenceTypes = data.by_evidence_type || [];
  const admissionLanes = data.by_admission_lane || [];
  const checks = [
    ["样本数量", candidate.closed, validation.min_trades, validation.trades_ready],
    ["观察时长", candidate.span_hours, validation.min_hours, validation.hours_ready],
    ["市场状态", candidate.regimes, validation.min_regimes, validation.regimes_ready],
  ];
  return (
    <section className="stack">
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>策略版本一眼看懂</h2>
            <p>{active.strategy_version || "当前版本"} 是当前实盘版本。每笔机会都会记录全仓短打或仅影子通道，版本间证据严格隔离。</p>
          </div>
          <span className="pill ok">当前实盘</span>
        </div>
        <div className="metrics">
          <MetricCard title={`${active.strategy_version || "V4"} 近期基线`} value={`${fmt(activeRecent.closed, 0)} 笔`} sub={`${pfLabel(activeRecent.profit_factor, activeRecent.closed)} · 净收益 ${fmt(activeRecent.net_pnl, 4)} U`} tone={Number(activeRecent.net_pnl || 0) >= 0 ? "positive" : "negative"} />
          <MetricCard title="当前安全观察窗" value={`${fmt(recovery.closed, 0)} / 20 笔`} sub={`${pfLabel(recovery.profit_factor, recovery.closed)} · 只对应当前 V4`} tone={Number(recovery.net_pnl || 0) >= 0 ? "positive" : "negative"} />
          <MetricCard title={`${activeStrategyLabel} 运行方式`} value="全仓短打" sub="当前版本独立排序；合格后单仓全进全出；快速保护独立 5 秒监督" />
        </div>
      </div>
      {hasChallenger && <div className="panel table-wrap">
        <h2>{activeStrategyLabel} 候选晋级清单</h2>
        <table>
          <thead><tr><th>检查项</th><th>当前</th><th>最低要求</th><th>状态</th></tr></thead>
          <tbody>
            {checks.map(([label, current, required, ready]: any[]) => <tr key={label}><td>{label}</td><td>{fmt(current, label === "观察时长" ? 1 : 0)}</td><td>{fmt(required, label === "观察时长" ? 1 : 0)}</td><td><span className={ready ? "pill ok" : "pill"}>{ready ? "达标" : "未达标"}</span></td></tr>)}
            <tr><td>扣费后收益</td><td>PF {fmt(candidate.profit_factor, 2)} · {fmt(candidate.net_pnl, 4)} U</td><td>PF ≥ {fmt(validation.min_profit_factor, 2)} 且净收益为正</td><td><span className={validation.profit_factor_ready ? "pill ok" : "pill"}>{validation.profit_factor_ready ? "达标" : "未达标"}</span></td></tr>
          </tbody>
        </table>
      </div>}
      <div className="panel table-wrap">
        <h2>{activeStrategyLabel} 三类证据</h2>
        <table><thead><tr><th>类型</th><th>含义</th><th>已结束</th><th>胜率</th><th>PF</th><th>净收益</th><th>成本</th></tr></thead><tbody>
          {evidenceTypes.map((row: any) => <tr key={row.evidence_type}><td>{row.evidence_type === "decision" ? "决策样本" : row.evidence_type === "exploration" ? "探索样本" : "同机会对照"}</td><td>{row.evidence_type === "decision" ? "当前版本排名靠前、确认充分且按事件去重的机会；准入口径只取单账户可执行的非重叠路径" : row.evidence_type === "exploration" ? "从仅影子机会中抽样，用于检查是否漏判" : "同一时刻用简单规则做基线，不参与准入"}</td><td>{fmt(row.opportunities || row.closed, 0)}</td><td>{fmt(row.win_rate, 1)}%</td><td>{pfLabel(row.profit_factor, row.closed)}</td><td>{fmt(row.net_pnl, 4)} U</td><td>{fmt(row.cost, 4)} U</td></tr>)}
          {!evidenceTypes.length && <tr><td colSpan={7}>{activeStrategyLabel} 刚启用，等待第一批决策、探索和对照样本结束。</td></tr>}
        </tbody></table>
      </div>
      <div className="panel table-wrap">
        <h2>{activeStrategyLabel} 通道表现</h2>
        <table><thead><tr><th>通道</th><th>独立机会</th><th>已结束</th><th>胜率</th><th>PF</th><th>净收益</th><th>成本</th></tr></thead><tbody>
          {admissionLanes.map((row: any) => <tr key={row.admission_lane}><td>{row.admission_lane === "full_bet" ? "全仓短打" : row.admission_lane === "shadow_only" ? "仅影子" : row.admission_lane === "validated" ? "旧核心已验证" : row.admission_lane === "core_provisional" ? "旧核心受限" : row.admission_lane === "core_canary" ? "旧核心试运行" : row.admission_lane === "limited_exploration" ? "旧受限探索" : "旧数据未分类"}</td><td>{fmt(row.opportunities, 0)}</td><td>{fmt(row.closed, 0)}</td><td>{fmt(row.win_rate, 1)}%</td><td>{pfLabel(row.profit_factor, row.closed)}</td><td>{fmt(row.net_pnl, 4)} U</td><td>{fmt(row.cost, 4)} U</td></tr>)}
          {!admissionLanes.length && <tr><td colSpan={7}>当前版本刚启用，等待第一批按通道分类的事件级影子结果。</td></tr>}
        </tbody></table>
      </div>
      <div className="notice">“探索样本”说白了就是：系统会从以前直接淘汰的机会里挑少量代表，假装交易并扣掉成本。只有这样才能知道旧筛选是真的避开亏损，还是把赢家也一起挡掉。</div>
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

function ShadowTradingPanel({ data }: { data: ShadowData }) {
  const stats = data?.stats || {};
  const releases = data?.by_release || [];
  const rows = data?.trades || [];
  const outcomeLabel: Record<string, string> = {
    STOP: "模拟止损",
    TAKE_PROFIT: "模拟止盈",
    TIME_EXIT: "到时退出",
  };
  return (
    <section className="stack">
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>影子交易是什么</h2>
            <p>机器人只是假装按当时价格开仓，继续跟踪真实行情，再扣掉模拟手续费和滑点。它不会向 Binance 下单，也不会占用你的资金。</p>
          </div>
        </div>
        <div className="metrics">
          <MetricCard title="记录机会" value={fmt(stats.total, 0)} sub="去掉重复信号后的数量" />
          <MetricCard title="正在观察" value={fmt(stats.active, 0)} sub="还没有碰到模拟止盈或止损" />
          <MetricCard title="已经结束" value={fmt(stats.closed, 0)} />
          <MetricCard title="模拟胜率" value={`${fmt(stats.win_rate, 1)}%`} sub="样本少时只供观察" />
          <MetricCard title="全部历史净收益" value={`${fmt(stats.net_pnl, 4)} U`} sub={`包含归档版本；已扣模拟成本 ${fmt(stats.cost, 4)} U`} tone={Number(stats.net_pnl || 0) >= 0 ? "positive" : "negative"} />
        </div>
      </div>
      <div className="panel table-wrap">
        <h2>按策略版本对比</h2>
        <table>
          <thead><tr><th>策略</th><th>版本</th><th>用途</th><th>已结束</th><th>胜率</th><th>PF</th><th>净收益</th><th>成本</th></tr></thead>
          <tbody>{releases.map((item: any) => <tr key={item.release_id}><td>{strategyFamilyLabel(item.strategy_family)}</td><td>{item.strategy_version}</td><td><span className={item.strategy_role === "active" ? "pill ok" : "pill"}>{strategyRoleLabel(item.strategy_role)}</span></td><td>{fmt(item.closed, 0)}</td><td>{fmt(item.win_rate, 1)}%</td><td>{pfLabel(item.profit_factor, item.closed)}</td><td className={Number(item.net_pnl || 0) >= 0 ? "positive-text" : "negative-text"}>{fmt(item.net_pnl, 4)} U</td><td>{fmt(item.cost, 4)} U</td></tr>)}</tbody>
        </table>
      </div>
      <div className="panel table-wrap">
        <h2>最近影子交易</h2>
        <table>
          <thead><tr><th>状态</th><th>版本</th><th>证据类型</th><th>币种</th><th>方向</th><th>信号</th><th>说明</th><th>假设开仓</th><th>模拟止损</th><th>模拟止盈</th><th>结果</th><th>净盈亏</th></tr></thead>
          <tbody>
            {rows.map((item: any) => (
              <tr key={item.id}>
                <td>{item.status === "OPEN" ? "观察中" : "已结束"}</td>
                <td>{item.strategy_version || "legacy"}</td>
                <td>{item.evidence_type === "decision" ? "决策" : item.evidence_type === "exploration" ? "探索" : item.evidence_type === "paired_control" ? "对照" : strategyRoleLabel(item.strategy_role)}</td>
                <td className="symbol">{item.symbol}</td>
                <td>{item.direction === "SHORT" ? "做空" : "做多"}</td>
                <td>{item.signal_type || "-"}</td>
                <td>{item.blocked_reason || "未达到实盘条件"}</td>
                <td>{fmt(item.entry, 8)}</td>
                <td>{fmt(item.stop, 8)}</td>
                <td>{fmt(item.take_profit, 8)}</td>
                <td>{outcomeLabel[item.outcome] || (item.status === "OPEN" ? "等待结果" : "-")}</td>
                <td className={Number(item.net_pnl || 0) >= 0 ? "positive-text" : "negative-text"}>{fmt(item.net_pnl, 4)} U</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={12}>等待系统记录第一批符合观察条件的机会。</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}


function LiveReactionPanel({ data }: { data: LiveReactionData | null }) {
  const reactions = data?.reactions || [];
  const trades = data?.recent_trades || [];
  const banned = reactions.filter((row) => row.status === "banned").length;
  const reduced = reactions.filter((row) => ["cooldown", "tail_guard"].includes(row.status)).length;
  const recentNet = trades.reduce((sum, row) => sum + Number(row.net_pnl || 0), 0);
  return (
    <section className="stack">
      <div className="metrics">
        <MetricCard title="实时监控方向" value={String(reactions.length)} sub="按币种 + 做多/做空拆开统计" />
        <MetricCard title="暂停同向" value={String(banned)} sub="连亏或短时间净亏触发" tone={banned ? "negative" : ""} />
        <MetricCard title="降仓观察" value={String(reduced)} sub="一亏降仓、连盈防追尾" />
        <MetricCard title="近期归因净盈亏" value={`${fmt(recentNet, 4)} U`} tone={recentNet >= 0 ? "positive" : "negative"} />
      </div>
      <div className="panel">
        <div className="panel-head">
          <div>
            <h2>实时反应风控</h2>
            <p>这层专门处理分钟级反馈：刚亏过会小仓观察，连续亏损会暂停同币种同方向，连续盈利后会防止继续追尾。</p>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>币种</th>
                <th>方向</th>
                <th>状态</th>
                <th>仓位倍率</th>
                <th>连续盈亏</th>
                <th>近窗净盈亏</th>
                <th>当日净盈亏</th>
                <th>最大单笔伤害</th>
                <th>盈利回吐</th>
                <th>解除时间</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {reactions.map((row) => (
                <tr key={`${row.symbol}-${row.direction}`}>
                  <td className="symbol">{row.symbol}</td>
                  <td>{signalLabel(row.direction)}</td>
                  <td><span className={row.status === "banned" ? "pill bad" : row.status === "normal" ? "pill ok" : "pill"}>{row.status_label}</span></td>
                  <td>{fmt(row.risk_multiplier, 2)}x</td>
                  <td>{Number(row.consecutive_wins || 0) > 0 ? `连赢 ${row.consecutive_wins}` : Number(row.consecutive_losses || 0) > 0 ? `连亏 ${row.consecutive_losses}` : "-"}</td>
                  <td className={Number(row.recent_net_pnl) >= 0 ? "positive-text" : "negative-text"}>{fmt(row.recent_net_pnl, 4)} U</td>
                  <td className={Number(row.day_net_pnl) >= 0 ? "positive-text" : "negative-text"}>{fmt(row.day_net_pnl, 4)} U</td>
                  <td>{fmt(row.payload?.largest_single_loss_pct, 2)}%</td>
                  <td>{fmt(row.payload?.day_profit_giveback_pct, 2)}%</td>
                  <td>{row.ban_until || row.cooldown_until ? new Date(row.ban_until || row.cooldown_until).toLocaleString("zh-CN") : "-"}</td>
                  <td className="reason-cell">{row.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="panel">
        <h2>最近实盘成交归因</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>平仓时间</th><th>币种</th><th>方向</th><th>净盈亏</th><th>手续费</th><th>持仓秒数</th><th>成交笔数</th></tr>
            </thead>
            <tbody>
              {trades.map((row) => (
                <tr key={`${row.symbol}-${row.direction}-${row.open_time}-${row.close_time}`}>
                  <td>{row.close_time_iso ? new Date(row.close_time_iso).toLocaleString("zh-CN") : "-"}</td>
                  <td className="symbol">{row.symbol}</td>
                  <td>{signalLabel(row.direction)}</td>
                  <td className={Number(row.net_pnl) >= 0 ? "positive-text" : "negative-text"}>{fmt(row.net_pnl, 4)} U</td>
                  <td>{fmt(row.commission, 4)} U</td>
                  <td>{fmt(row.hold_seconds, 1)}</td>
                  <td>{row.trade_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
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
        <MetricCard title="风险预警线" value={`${fmt(config.risk_warning_equity, 2)} U`} sub="低于后只提醒，机器人继续运行" tone={Number(account.equity || 0) < Number(config.risk_warning_equity || 0) ? "negative" : ""} />
        <MetricCard title="硬停止线" value={`${fmt(config.hard_stop_equity, 2)} U`} sub="触发后退出持仓并禁止新仓" tone={state.hard_stop_triggered ? "negative" : ""} />
        <MetricCard title="影子交易" value={config.shadow_trading_enabled ? "已开启" : "已关闭"} sub="只做纸面记录，不会向 Binance 下单" />
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
  const [expert, setExpert] = useState(false);
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
  const datetime = (key: string, label: string, hint?: string) => {
    const parsed = form[key] ? new Date(form[key]) : null;
    const value = parsed && !Number.isNaN(parsed.getTime())
      ? new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
      : "";
    return (
      <label>{label}<input type="datetime-local" value={value} onChange={(event) => update(key, event.target.value ? new Date(event.target.value).toISOString() : "")} />{hint && <small>{hint}</small>}</label>
    );
  };
  const toggle = (key: string, label: string, hint?: string) => (
    <label className="switch-row">
      <span><strong>{label}</strong>{hint && <small>{hint}</small>}</span>
      <input type="checkbox" checked={Boolean(form[key])} onChange={(event) => update(key, event.target.checked)} />
    </label>
  );

  if (!expert) {
    return (
      <section className="stack">
        <div className="panel">
          <div className="panel-head"><div><h2>当前生效设置</h2><p>这里只显示当前阶段真正参与执行的参数。旧策略和未来阶段参数已收进专家设置。</p></div><button className="secondary" onClick={() => setExpert(true)}>进入专家设置</button></div>
          <div className="form-grid">
            {toggle("stage_routing_enabled", "按权益自动选择阶段", "推荐开启；当前 S0 使用 V5.1 单仓全进全出短打，后续阶段仍按权益自动切换")}
            {select("stage_manual_mode", "阶段控制", stageManualOptions.slice(0, 4), "自动模式会按权益切换策略")}
            {toggle("dry_run", "模拟交易", "开启后绝不会真实下单")}
            {toggle("live_trading_enabled", "允许实盘交易", "还需要正确的实盘确认短语")}
            {toggle("allow_short", "允许系统做空", "V5 会按方向分别计算排名与当前版本独立证据")}
            {number("hard_stop_equity", "权益硬停止线 U", "当前建议保持 5U")}
            {number("stage_s0_risk_pct", "S0 压力风险硬上限%", "默认 30%；这是最强机会的压力上限，普通机会仍从 12% 基线起步")}
            {number("stage_s0_max_leverage", "S0 最大杠杆", "默认 10 倍；系统会按止损距离在 3-10 倍间动态选择")}
            {toggle("opportunity_v4_live_enabled", "V5.1 当前实盘排序", "当前版本证据独立，旧版本只供复盘，不参与准入和仓位")}
            {toggle("opportunity_v44_full_bet_enabled", "启用 S0 单仓全进全出", "V5.1 最多使用约 90% 新鲜可用保证金，只持有一个币种和一个方向")}
            {toggle("stage_s0_daily_loss_stop_enabled", "S0 普通日亏损停牌", "默认关闭；5U 权益硬停止、每仓止盈止损和运行安全保护仍始终生效")}
            {toggle("stage_s0_daily_profit_lock_enabled", "S0 当日净利润锁", "默认开启；达到目标后不强平受保护持仓，空仓后停止当天新开仓")}
            {number("stage_s0_daily_profit_target_pct", "S0 当日净利润目标%", "默认 40%；按 UTC 当日初始权益计算，次日自动恢复")}
            {number("runtime_protection_supervisor_seconds", "持仓快速保护检查秒数", "默认 5 秒；独立于完整扫描周期，WebSocket 最新价优先")}
            {toggle("s0_moe_shadow_enabled", "启用 MoE 研究影子", "本地训练模型在线只做推理并记录结果，不参与实盘准入、仓位或止盈止损")}
            {number("s0_moe_online_window_hours", "MoE 线上统计窗口（小时）", "默认 24 小时；同时保留当前策略版本的全量统计")}
            {number("s0_moe_retrain_min_selected_closes", "MoE 候选重训练最少闭合样本", "默认 200 笔通过模型门控且已结束的独立影子机会")}
            {number("s0_moe_retrain_min_regimes", "MoE 候选重训练最少市场状态", "默认至少覆盖 2 种市场状态，避免只学到单边行情")}
            {number("opportunity_v50_margin_pct", "计划使用保证金%", "默认 90%；保留约 10% 处理费用和价格波动")}
            {number("opportunity_v50_min_risk_pct", "普通机会风险基线%", "默认 12%；亏损后降仓和三连止损冷却恢复都回到该基线")}
            {number("opportunity_v50_max_risk_pct", "最强机会风险上限%", "默认 30%；只在排名、确认、净期望和成本全部较强时接近上限")}
            {number("opportunity_v50_stressed_risk_cap_pct", "压力风险硬上限%", "默认 30%；仓位计算包含放大的手续费和滑点后仍不得越过")}
            {number("opportunity_v50_min_rank_percentile", "最低相对排名分位", "默认 0.85，即只考虑本轮前 15%")}
            {number("opportunity_v50_min_quality_score", "最低模型质量分", "默认 52 分；当前版本独立计算")}
            {number("opportunity_v50_min_expected_net_pct", "最低扣费后期望%", "默认 0.02%；手续费和滑点已计入")}
            {number("opportunity_v50_min_lower_expectancy_pct", "最低保守净期望%", "默认 -0.08%；用于允许小样本但高排名机会受控试仓")}
            {number("opportunity_v50_min_cost_ratio", "最低收益成本比", "默认 2.5 倍")}
            {number("opportunity_v50_min_confirmations", "最低确认项", "默认至少 2 项：量能、方向资金流、横截面排名、中周期路径和防追高")}
            {number("opportunity_v50_smart_flow_max_points", "聪明钱最大软修正分", "默认正负 10 分；只能修正排名，不能独立触发开仓")}
            {number("opportunity_v50_min_leverage", "最低动态杠杆", "默认 3 倍")}
            {number("opportunity_v50_max_leverage", "最高动态杠杆", "默认 10 倍")}
            {number("opportunity_v50_stop_atr", "初始止损 ATR", "默认 0.90 ATR")}
            {number("opportunity_v50_take_profit_r", "初始止盈 R", "默认 1.05R；0.45R 开始保护到保本附近")}
            {number("opportunity_v50_break_even_trigger_r", "保本触发 R", "默认 0.45R")}
            {number("opportunity_v50_max_hold_bars", "最长持仓 K 线数", "默认 3 根 5 分钟 K 线；短打不恋战")}
            {toggle("opportunity_v44_runtime_exit_enabled", "自动执行快速失效与时间退出", "推荐开启；退出失败时保留交易所止盈止损，不先撤保护单")}
            {number("opportunity_v50_loss_1_multiplier", "一次亏损后仓位倍率", "默认 0.80x；盈利后快速恢复")}
            {number("opportunity_v50_loss_2_multiplier", "两次连续亏损后仓位倍率", "默认 0.60x")}
            {number("opportunity_v50_loss_3_cooldown_minutes", "三连止损冷却分钟", "默认 20 分钟；到时自动恢复 12% 风险基线，不会无限停牌")}
            {toggle("s0_continuous_permit_enabled", "启用 S0 连续准入", "普通亏损只降仓；三连止损短暂冷却；5U 硬停止或运行安全故障仍会暂停新仓")}
            {number("execution_max_spread_pct", "盘口最大点差%", "流动性硬门，超过后不实盘")}
            {number("execution_min_depth_notional_usdt", "最低盘口深度 U", "流动性硬门，低于后不实盘")}
            {toggle("runtime_stop_management_enabled", "自动保本与移动止盈", "对新开的 V5 仓位生效")}
          </div>
        </div>
        <div className="panel danger-zone">
          <h2>Binance API 与实盘授权</h2>
          <div className="form-grid">
            <div className="field-wide credential-field">{text("api_key", "Binance API Key", "已保存时保持原样即可")}</div>
            <div className="field-wide credential-field">{password("api_secret", "Binance API Secret", "已保存过时留空表示不修改")}</div>
            {text("live_trading_confirmation", "实盘确认短语", "必须填写 ENABLE_LIVE_TRADING")}
          </div>
        </div>
        <div className="button-row"><button className="primary" onClick={() => onSave(form)}>保存设置</button><button className="secondary" onClick={onTestApi}>测试 Binance API</button></div>
      </section>
    );
  }

  return (
    <section className="stack">
      <div className="panel panel-head"><div><h2>专家设置</h2><p>包含兼容旧策略、未来阶段和基础设施参数。修改前应先完成回测。</p></div><button className="secondary" onClick={() => setExpert(false)}>返回简洁设置</button></div>
      <div className="panel">
        <h2>基础配置</h2>
        <div className="form-grid">
          {toggle("stage_routing_enabled", "按权益自动选择阶段", "推荐开启：S0-S2 运行机会引擎 V4，S3 运行盘口剥头皮，S4 进入网格阶段")}
          {select("stage_manual_mode", "阶段控制", stageManualOptions, "选择自动时忽略人工到期时间；手动模式只建议用于临时诊断")}
          {form.stage_manual_mode && form.stage_manual_mode !== "auto" && datetime("stage_manual_until", "手动模式到期时间", "到期后自动恢复权益路由；留空表示持续手动，风险较高")}
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
          {number("market_stream_max_symbols", "实时盯盘币数上限", "4G VPS 建议 120-150；仅扩大 WebSocket 低成本覆盖，不等于全部拉取 K 线回测")}
          {number("market_stream_min_24h_volume_usdt", "实时池最低 24h 成交额", "4G / 150 币建议 5000000；只影响 WebSocket 召回，不降低实盘流动性硬门")}
          {number("market_stream_symbols_per_connection", "每条行情连接币数", "建议 75；150 币分成两组 K 线流和两组轻盘口流，降低单连接故障影响")}
          {number("market_stream_rebuild_seconds", "实时盯盘重建间隔", "默认 300 秒；候选变化不足时不重连，持仓币仍会立即加入")}
          {number("stream_hot_symbols_limit", "热点进入盯盘数量", "默认 40；来自漏斗粗排和候选")}
          {toggle("fast_lane_enabled", "WebSocket 实时快车道", "异动事件独立于全量扫描，优先在数秒内完成决策")}
          {number("fast_lane_poll_seconds", "快车道轮询秒数", "默认 2 秒；只读取本地事件队列，不持续消耗 Binance REST")}
          {number("background_scan_min_interval_seconds", "后台全量扫描最短间隔", "默认 30 秒；实时机会仍由快车道立即处理，避免重复拉取 K 线耗尽 REST 预算")}
          {number("background_scan_timeout_seconds", "全量扫描超时线", "默认 90 秒；超过后看门狗自动重启交易进程并恢复扫描")}
          {number("account_supervisor_poll_seconds", "账户实时刷新秒数", "默认 2 秒；优先读私有 WebSocket，断线才使用 REST 兜底")}
          {number("account_supervisor_position_audit_seconds", "持仓保护复核秒数", "默认 10 秒；持仓变化时会立即检查止盈止损")}
          {toggle("smart_flow_enabled", "启用聪明钱复合信号", "组合顶级交易者持仓、账户多空比、主动买卖量和未平仓量，不是单独开仓条件")}
          {toggle("smart_flow_live_soft_score_enabled", "聪明钱参与当前版本最终重排", "先做 V4 预排，再以最多正负 5 分修正最终排名；数据不完整时保持中性")}
          {number("smart_flow_symbol_limit", "聪明钱决策短名单币数", "默认每轮只检查 V4 预排最高的 12 个去重币种，5 分钟缓存以控制 Binance API 开销")}
          {number("smart_flow_max_soft_points", "聪明钱最大修正分", "默认正负 5 分；只在置信度达到门槛时生效")}
          {number("smart_flow_min_confidence", "聪明钱最低置信度", "默认 0.45；不足时只展示，不参与排序")}
          {number("fast_lane_symbol_cooldown_seconds", "同币快车道冷却秒数", "默认 10 秒；合并连续推送，避免重复计算和追单")}
          {number("fast_lane_event_max_age_seconds", "快车道事件有效期", "默认 45 秒；过期异动不再追单")}
          {number("fast_lane_max_symbols", "快车道单次币数", "默认 3；优先最高分异动，避免挤占交易API预算")}
          {number("fast_lane_budget_seconds", "快车道计算预算", "默认 5 秒；超过预算只完成最高优先级币")}
          {number("telemetry_retention_days", "系统明细保留天数", "默认 30 天；成交与实盘学习记录不受影响")}
          {number("strategy_run_retention_days", "扫描决策明细保留天数", "默认 7 天；更早数据只保留聚合与真实成交，控制数据库体积")}
          {number("shadow_trade_retention_days", "影子交易明细保留天数", "默认 14 天；过期已平仓明细自动清理")}
          {toggle("performance_guard_enabled", "全局负期望保护", "推荐开启：实盘出现严重连续亏损或权益高点回撤时立即停止新仓；已有持仓保护仍继续运行")}
          {number("performance_guard_soft_consecutive_losses", "软保护连续亏损笔数", "默认 4 笔；只进入短时观察，不冻结所有局部好机会")}
          {number("performance_guard_soft_window_loss_equity_pct", "软保护短窗亏损权益%", "默认 8%")}
          {number("performance_guard_soft_peak_drawdown_pct", "软保护权益高点回撤%", "默认 12%")}
          {number("performance_guard_soft_observation_minutes", "软保护观察分钟", "默认 20 分钟；之后只允许局部证据合格的低倍率候选")}
          {number("performance_guard_hard_consecutive_losses", "硬保护连续亏损笔数", "默认 5 笔")}
          {number("performance_guard_hard_window_loss_equity_pct", "硬保护短窗亏损权益%", "默认 12%")}
          {number("performance_guard_hard_peak_drawdown_pct", "硬保护权益高点回撤%", "默认 15%")}
          {number("performance_guard_hard_pause_minutes", "硬保护全局冷却分钟", "默认 60 分钟；5U 硬停止仍独立生效")}
          {number("performance_guard_recovery_risk_multiplier", "恢复探路倍率", "仅在恢复证据达标后使用；严重负期望期间不会放行探路单")}
          {toggle("performance_recovery_permit_enabled", "持久化恢复许可证", "推荐开启：影子证据达标后保留限时资格，等待合格币种出现时再消费")}
          {number("performance_recovery_entry_profit_factor", "恢复影子 PF 门槛", "默认 0.90，并且最近20笔影子净收益必须为正")}
          {number("performance_recovery_confirm_closes", "恢复连续确认次数", "默认 3 次影子平仓，避免 PF 短暂越线马上实盘")}
          {number("performance_recovery_confirm_minutes", "恢复稳定确认分钟", "默认 5 分钟；满足次数或时间任一条件即可签发资格")}
          {number("performance_recovery_permit_minutes", "恢复资格有效分钟", "默认 180 分钟；没有合格币种不会提前消费")}
          {number("performance_recovery_revoke_profit_factor", "恢复资格撤销 PF", "默认 0.50；签发后至少新增3笔影子仍严重恶化才撤销")}
          {number("performance_recovery_current_live_warmup_trades", "当前版本实盘热身样本", "默认 8 笔；不足时不会因为一笔盈利直接恢复满仓")}
          {number("performance_recovery_normal_live_profit_factor", "恢复正常实盘 PF", "默认 1.05；当前版本实盘达到热身样本且净收益为正后才解除历史兜底")}
          {toggle("equity_guard_release_baseline_enabled", "按策略版本计算回撤仓位", "推荐开启：旧策略回撤继续保留安全告警，但不永久压低新版本恢复试单")}
          {toggle("strategy_evidence_enabled", "影子/实盘双确认", "推荐开启：只有两边都盈利并满足样本量，信用分才允许提高仓位")}
          {number("strategy_evidence_signal_negative_trades", "信号负向判定样本", "默认 10 笔；只统计当前实盘版本，不混入旧策略")}
          {toggle("strategy_evidence_recovery_block_negative", "恢复期跳过负向候选", "推荐开启：币种方向或信号类型已有明确负证据时，保留许可证等待下一个机会")}
          {number("strategy_evidence_loss_reentry_minutes", "亏损后同向等待分钟", "默认 30 分钟；避免同一币种同方向连续追假突破")}
          {toggle("adaptive_thresholds_enabled", "市场自适应阈值", "复用现有行情数据，按市场冷热和币种自身成交量动态调整放量与异动门槛")}
          {number("adaptive_volume_spike_floor", "自适应放量下限", "默认 1.2 倍，系统不能无限降低门槛")}
          {number("adaptive_volume_spike_ceiling", "自适应放量上限", "默认 2.4 倍，活跃市场会提高要求")}
          {number("adaptive_firecracker_move_floor_pct", "火药桶异动下限%", "默认 4%，安静市场也不会低于此值")}
          {number("adaptive_firecracker_move_ceiling_pct", "火药桶异动上限%", "默认 14%，高波动市场提高要求")}
          {toggle("shadow_trading_enabled", "影子交易", "只是假装开仓并跟踪结果，不会调用 Binance 下单接口")}
          {number("shadow_min_candidate_score", "影子交易最低候选分", "默认 70；只记录值得研究的机会")}
          {number("shadow_dedupe_minutes", "影子信号去重分钟", "同币、同方向、同信号在窗口内只算一笔")}
          {number("shadow_max_hold_minutes", "影子交易最长观察分钟", "到时仍未止盈止损则按当时价格模拟退出")}
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
        <summary>自动阶段参数</summary>
        <p>以下是已接入实际下单风控的默认值。新手建议保持默认，只在完整回测后调整。</p>
        <div className="form-grid">
          {number("stage_switch_up_buffer_pct", "升档缓冲%", "默认 5%；越过阶段线后还要多 5% 权益才升档")}
          {number("stage_switch_down_buffer_pct", "降档缓冲%", "默认 10%；避免权益在边界附近反复切换")}
          {number("stage_switch_confirmations", "切换连续确认次数", "默认 3 次；跨策略且有持仓时还会等到空仓")}
          {toggle("strategy_family_credit_enabled", "按策略隔离实盘信用", "Extreme V2、盘口剥头皮和网格分别学习，不互相继承历史奖惩")}
          {number("stage_s0_risk_pct", "S0 压力风险硬上限%", "0-300U，默认 15%；当前版本实际计划风险为 8%-15%")}
          {number("stage_s0_margin_pct", "S0 计划保证金%", "默认 90%，保留约 10% 费用与波动缓冲")}
          {number("stage_s0_max_leverage", "S0 最大杠杆", "默认 10 倍；按止损距离动态使用 3-10 倍")}
          {number("stage_s0_max_open_positions", "S0 最大持仓", "默认 1；当前版本只允许单币单向")}
          {toggle("stage_s0_daily_loss_stop_enabled", "S0 普通日亏损停牌", "默认关闭；开启后才使用下面的 S0 当日亏损上限")}
          {number("stage_s0_daily_loss_limit_pct", "S0 当日亏损上限%", "仅在 S0 普通日亏损停牌开启时生效")}
          {toggle("stage_s0_daily_profit_lock_enabled", "S0 当日净利润锁", "达到目标且空仓后锁定当天新开仓，UTC 次日重置")}
          {number("stage_s0_daily_profit_target_pct", "S0 当日净利润目标%", "默认 40%")}
          {number("stage_s1_risk_pct", "S1 单笔风险%", "300-10000U，默认 7%")}
          {number("stage_s1_margin_pct", "S1 保证金上限%", "默认 85%")}
          {number("stage_s1_max_leverage", "S1 最大杠杆", "默认 5x")}
          {number("stage_s1_max_open_positions", "S1 最大持仓", "默认 1")}
          {number("stage_s1_daily_loss_limit_pct", "S1 当日亏损上限%", "默认 25%")}
          {number("stage_s2_risk_pct", "S2 单笔风险%", "10000-100000U，默认 3%")}
          {number("stage_s2_margin_pct", "S2 保证金上限%", "默认 60%")}
          {number("stage_s2_max_leverage", "S2 最大杠杆", "默认 4x")}
          {number("stage_s2_max_open_positions", "S2 最大持仓", "默认 2")}
          {number("stage_s2_daily_loss_limit_pct", "S2 当日亏损上限%", "默认 12%")}
          {number("stage_s3_risk_pct", "S3 单笔风险%", "100000-1000000U，盘口剥头皮默认 0.35%")}
          {number("stage_s3_margin_pct", "S3 保证金上限%", "默认 25%")}
          {number("stage_s3_max_leverage", "S3 最大杠杆", "默认 3x")}
          {number("stage_s3_max_open_positions", "S3 最大持仓", "默认 4")}
          {number("stage_s3_daily_loss_limit_pct", "S3 当日亏损上限%", "默认 3%")}
          {number("stage_s4_risk_pct", "S4 单笔风险%", "1000000U 以上，默认 0.2%")}
          {number("stage_s4_margin_pct", "S4 保证金上限%", "默认 15%")}
          {number("stage_s4_max_leverage", "S4 最大杠杆", "默认 2x")}
          {number("stage_s4_max_open_positions", "S4 最大持仓", "默认 8")}
          {number("stage_s4_daily_loss_limit_pct", "S4 当日亏损上限%", "默认 1.5%")}
          {toggle("stage_s4_scalp_overlay_enabled", "S4 叠加盘口剥头皮", "网格之外用独立低风险额度捕捉短时机会，并自动避开网格币种")}
          {number("stage_s4_scalp_risk_pct", "S4 剥头皮单笔风险%", "默认 0.1%")}
          {number("stage_s4_scalp_margin_pct", "S4 剥头皮保证金上限%", "默认 5%")}
          {number("stage_s4_scalp_daily_loss_limit_pct", "S4 剥头皮当日亏损上限%", "默认 1%")}
        </div>
      </details>
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
          {toggle("yolo_scalp_orderbook_engine_enabled", "盘口剥头皮引擎", "极限梭哈优先使用 WebSocket 盘口、1m 异动和扣费后空间来触发快进快出")}
          {toggle("yolo_scalp_orderbook_only_enabled", "极限只跑盘口剥头皮", "开启后极限模式不会执行旧火药桶、抢跑或弱质量试探；旧信号只保留观察")}
          {toggle("orderbook_full_stream_enabled", "剥头皮增量订单簿", "只在 S3 盘口剥头皮阶段启用 100ms 深度更新，S0-S2 不承担这部分开销")}
          {number("orderbook_full_symbols_limit", "增量订单簿币数", "默认 20；只给精选候选、持仓和热点使用")}
          {number("orderbook_snapshot_limit", "订单簿快照档位", "默认 100 档；断档时用 REST 快照重建")}
          {toggle("yolo_scalp_trade_flow_enabled", "主动成交流确认", "使用逐笔聚合成交判断主动买卖方向，只在盘口剥头皮阶段订阅")}
          {number("yolo_scalp_trade_flow_window_seconds", "主动成交统计窗口秒", "默认 5 秒")}
          {number("yolo_scalp_trade_flow_weight", "主动成交方向权重", "默认 0.35；与本地 L2 盘口失衡共同评分")}
          {number("yolo_scalp_trade_flow_trigger_notional_usdt", "主动成交异动门槛U", "默认 100000U；达到后进入实时机会队列")}
          {number("yolo_scalp_orderbook_max_spread_pct", "剥头皮最大点差%", "默认 0.08%；点差太大时手续费和滑点会吃掉毛利")}
          {number("yolo_scalp_orderbook_min_depth_notional_usdt", "剥头皮最低盘口深度U", "默认 1000U；depth5 太薄不做")}
          {number("yolo_scalp_orderbook_probe_min_depth_notional_usdt", "失衡试探最低盘口深度U", "默认 300U；只用于小仓试探，盘口冲击和放量剥头皮仍用更高深度")}
          {number("yolo_scalp_orderbook_min_imbalance", "剥头皮最小盘口失衡", "默认 0.05；买卖盘优势不明显就等待")}
          {toggle("yolo_scalp_orderbook_allow_strong_imbalance_direction_probe", "强盘口允许小仓试探", "实时方向不一致但盘口失衡很强时，只允许按失衡试探小仓进入")}
          {toggle("yolo_scalp_strategy_credit_enabled", "剥头皮独立信用分", "开启后只用盘口剥头皮自己的实盘结果调整仓位；旧策略信用只展示不参与执行")}
          {number("scalp_credit_quick_stop_seconds", "剥头皮快速止损秒数", "默认 35 秒；短打策略更快识别失败样本")}
          {number("scalp_credit_win_reward", "剥头皮盈利奖励", "默认 +5 分；只影响盘口剥头皮专用信用")}
          {number("scalp_credit_loss_penalty", "剥头皮亏损惩罚", "默认 -7.5 分；只影响盘口剥头皮专用信用")}
          {number("scalp_credit_penalty_cooldown_hours", "剥头皮冷却小时", "默认 1 小时；比旧滚仓策略恢复更快")}
          {number("yolo_scalp_orderbook_min_1m_move_pct", "剥头皮1m最小异动%", "默认 0.08%；没有短时波动就不硬开")}
          {number("yolo_scalp_orderbook_min_profit_cost_ratio", "剥头皮最低收益/成本比", "默认 1.15；必须覆盖手续费和滑点后仍有空间")}
          {number("yolo_scalp_orderbook_target_net_profit_pct", "剥头皮目标净利%", "默认 0.06%；舔一口就走的最小目标")}
          {number("yolo_scalp_orderbook_stop_pct", "剥头皮硬止损%", "默认 0.20%；盘口失败时快速认错")}
          {number("yolo_scalp_orderbook_max_hold_seconds", "剥头皮最长持仓秒数", "默认 120 秒；超过仍没利润就撤")}
          {toggle("yolo_scalp_orderbook_runtime_exit_enabled", "盘口失效自动退出", "剥头皮持仓中如果点差扩大或盘口反向，运行时保护会提前平仓")}
          {number("yolo_scalp_orderbook_exit_spread_multiplier", "盘口退出点差倍数", "默认 2；超过入场点差上限的 2 倍视为盘口恶化")}
          {number("yolo_scalp_orderbook_exit_reverse_imbalance", "盘口反向退出阈值", "默认 0.04；同向盘口优势翻转后提前撤")}
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
          {number("yolo_scalp_standard_min_risk_pct", "标准剥头皮最低风险%", "默认 35%；标准信号不再被小仓抢跑倍率压得太低")}
          {number("yolo_scalp_standard_max_risk_pct", "标准剥头皮最高风险%", "默认 75%；仍受账户权益、杠杆和最大名义价值约束")}
          {number("yolo_scalp_preemptive_min_risk_pct", "抢跑剥头皮最低风险%", "默认 28%；强抢跑信号可以用有效仓位试错")}
          {number("yolo_scalp_preemptive_max_risk_pct", "抢跑剥头皮最高风险%", "默认 65%；防止普通抢跑直接满仓")}
          {number("yolo_scalp_firecracker_min_risk_pct", "火药桶最低风险%", "默认 30%；放量异动满足时不再只下极小仓")}
          {number("yolo_scalp_firecracker_max_risk_pct", "火药桶最高风险%", "默认 70%；亏损后会自动降到亏损保护上限")}
          {number("yolo_scalp_weak_probe_min_risk_pct", "小单探路最低风险%", "默认 8%；质量较弱时只收集样本")}
          {number("yolo_scalp_weak_probe_max_risk_pct", "小单探路最高风险%", "默认 25%")}
          {number("yolo_scalp_loss_probe_max_risk_pct", "亏损后探路风险上限%", "默认 12%；刚亏过的币种方向只允许小仓恢复")}
          {number("yolo_scalp_loss_probe_risk_multiplier", "亏损后探路倍率", "默认 0.75；用于连续亏损或冷却状态")}
          {toggle("yolo_scalp_min_order_lift_enabled", "梭哈最小下单智能补齐", "强信号低于币安最小下单量时，先评估止损风险和扣费利润，再决定是否补齐")}
          {number("yolo_scalp_effective_min_order_notional_usdt", "梭哈有效最小订单U", "默认 5U；和币安最小下单量取更高值")}
          {number("yolo_scalp_min_order_lift_max_loss_pct", "补齐订单最大止损风险%", "默认 8%；补齐后如果止损风险过大，仍然跳过")}
          {number("yolo_scalp_min_order_lift_min_cost_ratio", "补齐订单最低成本比", "默认 3；预期波动至少覆盖手续费和滑点")}
          {number("yolo_scalp_min_order_lift_min_net_profit_usdt", "补齐订单最低净利润U", "默认 0.03U；太小的毛利不强行成交")}
          {toggle("opportunity_v4_enabled", "启用 V4 机会引擎", "推荐开启：计算扣费后净期望并记录公平影子证据，不增加 Binance 下单请求")}
          {toggle("opportunity_v4_live_enabled", "V5.1 作为当前实盘排序器", "默认开启；旧策略实验已退出实盘和日常界面")}
          {text("opportunity_v4_strategy_version", "当前策略版本号", "默认 v5.1；不同版本实盘和影子证据严格隔离")}
          {toggle("opportunity_v44_full_bet_enabled", "V5.1 S0 单仓全进全出", "只持有一个币种和一个方向；一次建仓、一次全平，禁止盈利追加和分批止盈")}
          {toggle("opportunity_v49_global_adaptive_enabled", "V5.0-S30 全局动态校准", "只读取当前版本本地实盘与影子数据库，不增加 Binance API 请求，也不按单币种调参")}
          {number("opportunity_v49_global_window_hours", "全局统计窗口小时", "默认 24 小时")}
          {number("opportunity_v49_global_update_hours", "全局调整间隔小时", "默认 2 小时；一次只改一个主要变量")}
          {number("opportunity_v49_global_min_shadow_trades", "调整前最少影子样本", "默认 20 笔独立结束机会")}
          {number("opportunity_v49_global_min_live_trades", "调整前最少实盘样本", "默认 8 笔独立结束机会")}
          {number("opportunity_v49_global_target_live_trades", "24小时目标实盘样本", "默认 12 笔；正期望但频率不足时优先小幅放宽排名")}
          {toggle("opportunity_v410_global_regime_enabled", "V5.0-S30 启用全局市场状态", "根据当前版本结果识别趋势或震荡；只影响全局方向倍率，不为单币种建永久参数")}
          {toggle("opportunity_v410_global_smart_flow_enabled", "V5.0-S30 启用全局聪明钱代理", "把已有聪明钱复合信号汇总为全局背景，不新增 Binance 请求，也不单独触发开仓")}
          {number("opportunity_v410_direction_aligned_multiplier", "全局顺势倍率", "默认 1.05x；只在当前全局方向有证据时小幅增加")}
          {number("opportunity_v410_direction_countertrend_multiplier", "全局逆势倍率", "默认 0.90x；只是降倍率，不直接禁止机会")}
          {number("opportunity_v47_aligned_multiplier", "顺势基础倍率", "默认 1.00x；多空使用完全对称的行情关系")}
          {number("opportunity_v47_neutral_multiplier", "中性基础倍率", "默认 0.85x")}
          {number("opportunity_v47_countertrend_multiplier", "逆势基础倍率", "默认 0.65x；并要求更强确认")}
          {number("opportunity_v47_min_current_samples", "仓位校准最少当前样本", "默认 6 个独立结束机会")}
          {number("opportunity_v47_threshold_min_current_samples", "门槛校准最少当前样本", "默认 12 个；不足时只显示证据，不放宽门槛")}
          {number("opportunity_v47_seed_weight", "兼容：旧版启动先验权重", "保留给旧记录回放；不参与当前版本准入")}
          {number("opportunity_v47_seed_effective_sample_cap", "兼容：旧版本先验有效样本上限", "仅用于历史回放，防止旧记录压过当前版本证据")}
          {number("opportunity_v47_max_step", "单次校准最大变化", "默认 0.10；防止短窗口追涨杀跌")}
          {number("opportunity_v48_min_quality_score", "基础质量底线（当前版本使用）", "默认 56；即使本轮排名第一，低于底线也不开仓")}
          {number("opportunity_v48_min_expected_net_pct", "基础最低扣费后期望%", "默认 0.03%；全局校准只会在证据足够时小幅调整")}
          {number("opportunity_v48_min_lower_expectancy_pct", "基础保守期望下限%", "默认 -0.03%；当前版本按全局规则调整")}
          {number("opportunity_v48_min_cost_ratio", "基础最低毛利成本比", "默认 1.70 倍；必须先覆盖手续费和滑点")}
          {number("opportunity_v48_smart_flow_max_points", "聪明钱最大排名修正分", "默认正负 2 分；只在路径和防追价确认后使用")}
          {toggle("opportunity_v48_exhaustion_enabled", "启用趋势衰竭检测", "扩张过远、量能衰退、上影或路径失效时降仓或禁止追价")}
          {number("opportunity_v48_exhaustion_caution_score", "衰竭谨慎线", "默认 0.42；达到后降低仓位")}
          {number("opportunity_v48_exhaustion_block_score", "衰竭禁止线", "默认 0.62；达到后不在趋势末端追价")}
          {toggle("opportunity_v48_reentry_enabled", "启用结构化再入场", "同币同方向亏损后，必须出现回踩、路径、量能与扩张重置才重新放行")}
          {number("opportunity_v48_reentry_hard_losses", "同结构连续亏损硬门", "默认 2 次；新结构确认后自动解除")}
          {number("opportunity_v472_episode_dedupe_minutes", "同一行情事件去重（分钟）", "默认 45 分钟；同币、同方向、同形态和同市场状态只算一个事件")}
          {number("opportunity_v472_symbol_max_events_per_window", "单币 6 小时最多事件数", "默认 3 次；限制反复追逐同一币，但新结构仍可重新评估")}
          {number("opportunity_v472_pullback_min_cost_ratio", "历史回踩最低成本比", "仅用于旧版回放；V5.0-S30 使用独立全局成本门槛")}
          {number("opportunity_v472_breakout_min_cost_ratio", "突破最低成本比", "默认 2.3；突破需要更厚的扣费后空间")}
          {number("opportunity_v472_pullback_max_hold_bars", "回踩最长持仓 K 线", "默认 4 根；其他形态保持 2 根快速退出")}
          {number("opportunity_v473_min_expected_net_pct", "历史版本最低扣费后期望%", "仅用于旧版回放；V5.0-S30 使用独立全局期望门槛")}
          {number("opportunity_v473_min_lower_expectancy_pct", "历史版本最低保守净期望%", "仅用于旧版回放；不参与 V5.0-S30 准入")}
          {number("opportunity_v473_stagnation_seconds", "未兑现检查秒数", "默认 180 秒；到时仍未覆盖成本空间则主动退出")}
          {number("opportunity_v473_stagnation_min_profit_pct", "未兑现最低利润%", "默认 0.12%；约等于保守往返成本底线")}
          {number("opportunity_v473_max_hold_seconds", "最长持仓秒数", "默认 480 秒；只有已经进入盈利保护的仓位才允许持有到此")}
          {number("runtime_protection_supervisor_seconds", "持仓保护监督周期秒数", "默认 5 秒；不再等待完整的机会扫描")}
          {number("runtime_protection_stream_price_max_age_seconds", "WebSocket 价格最大年龄秒数", "默认 10 秒；过期时才使用 REST 持仓价格兜底")}
          {number("opportunity_v44_min_rank_percentile", "基础最低相对排名分位", "默认 0.80，即只考虑本轮前 20%；由全局校准决定是否小幅变化")}
          {number("opportunity_v44_min_confirmations", "基础最低确认项", "默认五项中至少 3 项")}
          {number("opportunity_v44_margin_pct", "当前版本使用可用保证金%", "默认 90%；剩余约 10% 作为费用和波动缓冲")}
          {number("opportunity_v44_min_risk_pct", "当前版本最低计划风险%", "默认 8%")}
          {number("opportunity_v44_max_risk_pct", "当前版本最高计划风险%", "默认 15%；仅最强机会接近上限")}
          {number("opportunity_v44_stressed_risk_cap_pct", "当前版本压力风险硬上限%", "默认 15%，包含放大的手续费和滑点")}
          {number("opportunity_v44_min_leverage", "当前版本最低动态杠杆", "默认 3 倍")}
          {number("opportunity_v44_max_leverage", "当前版本最高动态杠杆", "默认 10 倍")}
          {number("opportunity_v44_cost_stress_multiplier", "当前版本成本压力倍数", "默认 1.5 倍")}
          {number("opportunity_v44_stop_atr", "当前版本初始止损 ATR", "默认 0.85 ATR")}
          {number("opportunity_v44_take_profit_r", "当前版本初始止盈 R", "默认 1.05R")}
          {number("opportunity_v44_break_even_trigger_r", "当前版本保本触发 R", "默认 0.45R")}
          {number("opportunity_v44_max_hold_bars", "V4 低收益最长持仓 K 线", "默认 2 根；约 10 分钟仍未覆盖成本则退出")}
          {toggle("opportunity_v44_runtime_exit_enabled", "当前版本自动快速退出", "快速失效或持仓超时可实盘全平；平仓成功后才清理旧保护单")}
          {number("opportunity_v44_rotation_min_edge_r", "当前版本换仓最低净优势 R", "默认 0.35R，必须覆盖额外平仓与开仓成本")}
          {number("opportunity_v44_rotation_min_cost_ratio", "当前版本换仓最低成本比", "默认 1.25 倍")}
          {toggle("opportunity_v44_global_pf_soft_context", "全局 PF 仅作背景", "推荐开启；旧版或全局 PF 不会一票否决当前版本合格候选")}
          {number("execution_max_spread_pct", "V4 流动性硬门：最大点差%", "默认 0.10%；超过后不实盘")}
          {number("execution_min_depth_notional_usdt", "V4 流动性硬门：最低深度U", "默认 5000U；只检查进入竞价层的精选币")}
          {number("opportunity_v4_decision_min_rank_percentile", "V4 决策排名分位", "默认 0.75；只把本轮前 25% 作为决策样本")}
          {toggle("opportunity_v4_bootstrap_enabled", "允许 V4 受限实盘探索", "顶排候选在独立证据尚少时可用折扣仓位实盘，不继承旧 V3 拦截")}
          {number("opportunity_v4_bootstrap_min_rank_percentile", "V4 探索最低排名分位", "默认 0.85，即本轮前 15%")}
          {number("opportunity_v4_bootstrap_min_quality_score", "V4 探索最低模型分", "默认 58")}
          {number("opportunity_v4_bootstrap_min_model_expectancy_pct", "归档：旧核心最低融合净期望%", "仅供旧版本回放，不参与当前版本实盘")}
          {number("opportunity_v4_bootstrap_risk_multiplier", "归档：旧核心基础通道倍率", "仅供旧版本回放，不参与当前版本实盘")}
          {toggle("opportunity_v43_exploration_enabled", "归档：V4.3 顺势受限探索", "仅供旧版本回放，不参与当前版本实盘")}
          {number("opportunity_v43_exploration_min_rank_percentile", "探索最低排名分位", "默认 0.75，本轮前 25%")}
          {number("opportunity_v43_exploration_min_quality_score", "探索最低模型分", "默认 55")}
          {number("opportunity_v43_exploration_min_expected_net_pct", "探索最低扣费后期望%", "默认 0.04%")}
          {number("opportunity_v43_exploration_min_lower_expectancy_pct", "探索保守期望下限%", "默认 -0.03%，只允许很小的不确定区间")}
          {number("opportunity_v43_exploration_min_cost_ratio", "探索最低毛利成本比", "默认 1.60 倍")}
          {number("opportunity_v43_exploration_min_confirmations", "探索最少动量确认", "默认 2 项：量能、主动流、横截面强度、中周期路径")}
          {number("opportunity_v43_exploration_risk_multiplier", "探索仓位倍率", "默认 0.4；不与本版本杠杆同时放大")}
          {number("opportunity_v43_hierarchy_prior_trades", "局部证据收缩强度", "默认 30；小样本会向同方向背景收缩，但不会被背景直接否决")}
          {number("opportunity_v43_local_negative_min_trades", "同形态负证据最少样本", "默认 30 个事件级样本，达到后才允许硬阻断")}
          {number("opportunity_v43_local_negative_profit_factor", "同形态负证据 PF 线", "默认 0.50；大方向较差只降低仓位")}
          {toggle("opportunity_v43_dynamic_liquidity_enabled", "按实际订单动态检查盘口", "推荐开启；小账户不再固定要求 5000U 深度")}
          {number("opportunity_v43_min_depth_floor_usdt", "盘口深度最低底线 U", "默认 750U，仍同时受点差与订单占盘口比例约束")}
          {number("opportunity_v43_max_order_book_share_pct", "订单最多占可见盘口%", "默认 8%，防止浅盘口滑点吞掉利润")}
          {number("opportunity_v43_episode_dedupe_minutes", "同一行情事件去重分钟", "默认 30 分钟；结构或价格完成风险距离重置后可重新计样本")}
          {toggle("opportunity_v431_local_circuit_enabled", "启用局部组合熔断", "只冻结同市场状态、方向、形态和入场阶段，不做全币池一刀切")}
          {number("opportunity_v431_local_live_loss_streak", "局部组合连续实盘亏损", "默认 2 笔后转为影子观察")}
          {number("opportunity_v431_local_block_min_trades", "局部影子熔断最少样本", "默认 8 个独立机会")}
          {number("opportunity_v431_local_block_profit_factor", "局部影子熔断 PF", "默认低于 0.80 且净收益为负")}
          {number("opportunity_v431_local_restore_min_trades", "局部恢复新影子样本", "默认 8 笔，必须是熔断后新产生的证据")}
          {number("opportunity_v431_local_restore_min_symbols", "局部恢复最少币种", "默认 3 个币，避免单币偶然性")}
          {number("opportunity_v431_local_restore_profit_factor", "局部恢复影子 PF", "默认 1.05 且净收益为正")}
          {toggle("opportunity_v432_continuous_sizing_enabled", "归档：V4.3.2 连续质量仓位", "仅供旧版本回放，不参与当前版本实盘")}
          {number("opportunity_v432_initial_min_risk_pct", "归档：旧核心初始风险下限%", "仅供旧版本回放")}
          {number("opportunity_v432_initial_max_risk_pct", "归档：旧核心初始风险上限%", "仅供旧版本回放")}
          {toggle("opportunity_v432_add_on_enabled", "归档：旧版盈利追加", "当前版本强制关闭；仅供旧版本回放")}
          {number("opportunity_v432_add_on_min_confidence", "归档：旧版追加最低置信度", "仅供旧版本回放")}
          {number("opportunity_v432_add_on_min_cost_ratio", "归档：旧版追加最低成本比", "仅供旧版本回放")}
          {number("opportunity_v432_add_on_total_risk_cap_pct", "归档：旧版总风险上限%", "仅供旧版本回放")}
          {number("opportunity_v432_release_fallback_drawdown_pct", "归档：旧版本回退回撤%", "当前版本不使用旧版本回撤许可证")}
          {number("opportunity_v4_validated_risk_multiplier", "V4 已验证仓位倍率", "默认 1.0，仍受权益、信用和硬风控约束")}
          {number("opportunity_v4_decision_shadow_limit", "每轮决策样本上限", "默认 3；控制数据库与行情跟踪开销")}
          {number("opportunity_v4_exploration_shadow_limit", "每轮探索样本上限", "默认 6；从被拒机会中取代表样本，检查旧模型漏判")}
          {number("opportunity_v4_control_shadow_limit", "每轮对照样本上限", "默认 3；同机会运行简单突破基线")}
          {number("opportunity_v4_admission_min_trades", "归档：旧核心受限准入样本", "仅供旧版本回放；当前版本不依赖旧样本准入")}
          {number("opportunity_v4_admission_min_profit_factor", "V4 同类最低 PF", "默认 1.10，且必须是扣费后结果")}
          {number("opportunity_v4_admission_min_lower_expectancy_pct", "归档：旧核心保守净期望下限%", "仅供旧版本回放")}
          {number("opportunity_v41_validation_min_trades", "V4 核心标准实盘样本", "默认 500 个独立决策机会，并且需要跨时间块与币种")}
          {number("opportunity_v41_validation_min_profit_factor", "V4 核心标准实盘最低 PF", "默认 1.15，必须为扣费后 PF")}
          {number("opportunity_v41_provisional_risk_multiplier", "V4 核心受限准入倍率", "默认 0.7；达到 200 个正向同类样本后使用")}
          {number("opportunity_v41_min_expected_net_pct", "V4 核心最低扣费后模型期望%", "默认 0.10%")}
          {number("opportunity_v41_min_cost_ratio", "V4 核心最低毛利成本比", "默认 2.0 倍")}
          {toggle("opportunity_v41_medium_alignment_required", "要求中周期方向一致", "推荐开启；历史中方向一致样本显著更稳")}
          {toggle("s0_continuous_permit_enabled", "当前版本 S0 连续准入", "普通亏损不撤销许可证；候选仍必须通过触发、成本、流动性和交易所硬门")}
          {number("s0_continuous_initial_multiplier", "新版本初始仓位倍率", "默认 0.75x；第一笔扣费后有效盈利可快速提升")}
          {number("s0_continuous_loss_1_multiplier", "一次连亏仓位倍率", "默认 0.75x，不暂停其他币种机会")}
          {number("s0_continuous_loss_2_multiplier", "两次连亏仓位倍率", "默认 0.50x")}
          {number("s0_continuous_loss_3_multiplier", "三次及以上连亏仓位倍率", "默认 0.25x，仍保留合格机会")}
          {number("s0_continuous_daily_tier_1_pct", "当日回撤一档%", "默认 10%，全局仓位上限 0.75x")}
          {number("s0_continuous_daily_tier_2_pct", "当日回撤二档%", "默认 20%，全局仓位上限 0.50x")}
          {number("s0_continuous_daily_pause_pct", "当日回撤暂停线%", "默认 30%，达到后暂停到下一个 UTC 交易日")}
          {number("s0_continuous_profit_recovery_cost_multiple", "有效盈利成本倍数", "默认净盈利至少覆盖显式成本 2 倍，仓位恢复一级")}
          {number("s0_continuous_full_recovery_wins", "恢复满倍率连续盈利数", "默认连续 2 次扣费后有效盈利恢复 1.00x")}
          {toggle("strategy_canary_startup_cap_enabled", "归档：旧版首日许可证", "当前版本已由连续准入替代，不参与当前开仓")}
          {text("strategy_canary_release_id", "当前版本许可证", "按策略版本独立签发；旧版本状态不会参与当前准入")}
          {number("strategy_canary_permit_hours", "许可证有效小时", "默认 24 小时")}
          {number("strategy_canary_level_1_max_opportunities", "一级最多机会", "默认 6 次，倍率 1.00x；风险仍受 15% 压力硬上限约束")}
          {number("strategy_canary_level_2_min_trades", "升二级所需实盘", "默认 3 笔且净收益为正、PF≥1.05")}
          {number("strategy_canary_level_3_min_trades", "升标准所需实盘", "默认 8 笔且净收益为正、PF≥1.15")}
          {number("strategy_canary_max_losses", "试运行最大亏损笔数", "默认 3 笔；达到后进入 60 分钟观察，不是永久停用")}
          {number("strategy_canary_reissue_observation_minutes", "亏损后再签发观察分钟", "默认 60 分钟")}
          {number("strategy_canary_reissue_min_shadow_trades", "再签发新影子样本", "默认 8 笔且净收益为正")}
          {number("strategy_canary_reissue_min_symbols", "再签发最少币种", "默认 3 个")}
          {number("strategy_canary_reissue_min_profit_factor", "再签发影子 PF", "默认 1.15")}
          {number("strategy_canary_reissue_multiplier", "归档：再签发试单倍率", "仅供 V4.4 及更早版本回放")}
          {number("strategy_canary_reissue_max_opportunities", "归档：再签发最多机会", "仅供 V4.4 及更早版本回放")}
          {toggle("extreme_sprint_enabled", "开启 S0-S2 机会滚仓", "必须配合确认短语 ENABLE_EXTREME_SPRINT 才会生效；当前由 V4 排序")}
          {text("extreme_sprint_confirmation", "机会滚仓确认短语", "填写 ENABLE_EXTREME_SPRINT 后，增长模式才会启用")}
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
                {toggle("live_reaction_enabled", "启用实时反应风控", "平仓后快速学习：一亏降仓、两连亏暂停同向、连续盈利后防追尾")}
                {number("live_reaction_check_seconds", "实时风控同步秒数", "默认 20 秒；快车道也会按该频率吸收最新平仓")}
                {number("live_reaction_background_check_seconds", "后台风控同步秒数", "默认 180 秒；降低后台扫描对 REST 预算的占用")}
                {number("live_reaction_sync_max_symbols", "实时风控同步币种数", "默认 8；只同步近期相关币种，避免私有 API 压力过高")}
                {number("live_reaction_one_loss_cooldown_minutes", "一亏降仓分钟", "默认 5 分钟")}
                {number("live_reaction_one_loss_multiplier", "一亏仓位倍率", "默认 0.4x")}
                {number("live_reaction_two_loss_ban_minutes", "两连亏暂停分钟", "默认 30 分钟；同币种同方向暂停")}
                {number("live_reaction_three_loss_ban_hours", "三连亏暂停小时", "默认 4 小时")}
                {number("live_reaction_profit_tail_count", "盈利防追尾笔数", "默认 2 笔")}
                {number("live_reaction_tail_multiplier", "防追尾仓位倍率", "默认 0.5x")}
                {number("live_reaction_recent_loss_equity_pct", "短窗净亏权益%", "默认 12%；超过后暂停同方向")}
                {number("live_reaction_symbol_direction_daily_loss_pct", "当日同向净亏权益%", "默认 15%；超过后暂停到次日")}
                {number("live_reaction_single_loss_cooldown_equity_pct", "单笔伤害降仓阈值%", "默认 6%；单笔亏损过大会延长降仓观察")}
                {number("live_reaction_single_loss_ban_equity_pct", "单笔伤害暂停阈值%", "默认 10%；单笔重伤会暂停同向")}
                {number("live_reaction_single_loss_cooldown_minutes", "单笔伤害降仓分钟", "默认 60 分钟")}
                {number("live_reaction_single_loss_ban_minutes", "单笔伤害暂停分钟", "默认 180 分钟")}
                {number("live_reaction_giveback_min_profit_usdt", "回吐保护最低盈利U", "默认 1U；盈利达到后才计算回吐比例")}
                {number("live_reaction_giveback_cooldown_pct", "盈利回吐降仓%", "默认 50%；回吐一半后降仓")}
                {number("live_reaction_giveback_ban_pct", "盈利回吐暂停%", "默认 80%；大幅吐回后暂停同向")}
                {number("live_reaction_giveback_cooldown_minutes", "回吐降仓分钟", "默认 60 分钟")}
                {number("live_reaction_giveback_ban_minutes", "回吐暂停分钟", "默认 180 分钟")}
                {number("symbol_cooldown_minutes", "同币开仓冷却分钟", "默认 15")}
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
          {number("risk_warning_equity", "权益风险预警线U", "默认 30U；低于后只显示提醒，不阻止交易")}
          {number("hard_stop_equity", "权益硬停止线U", "默认 5U；触发后退出持仓、取消挂单并停止机器人")}
          {number("hard_stop_recovery_equity", "硬停止恢复权益U", "默认 5.5U；补充资金后仍需手动启动")}
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

function TrainingDataQualityPanel({ quality }: { quality?: TrainingQualityData | null }) {
  const lineage = quality?.lineage || {};
  const live = quality?.live || {};
  const shadow = quality?.shadow || {};
  const ready = Boolean(quality?.high_weight_training_ready);
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>模型训练数据质量</h2>
          <p>从机会出现、下单成交到最终平仓使用同一个编号。这里只统计可追溯数据，不会自动改变实盘策略。</p>
        </div>
        <span className={ready ? "pill ok" : "pill"}>{ready ? "达到首轮训练门槛" : "继续积累样本"}</span>
      </div>
      <div className="metrics">
        <MetricCard title="机会快照" value={`${fmt(lineage.total, 0)} 条`} sub={`含分钟特征 ${fmt(lineage.feature_rows, 0)} 条 · ${quality?.schema_version || "-"}`} />
        <MetricCard title="实盘精确回连" value={`${fmt(live.exact_matches, 0)} / ${fmt(live.total, 0)}`} sub={`按 Binance 订单号匹配 · 覆盖率 ${fmt(quality?.exact_live_link_rate_pct, 1)}%`} tone={ready ? "positive" : ""} />
        <MetricCard title="实盘未匹配" value={`${fmt(live.unmatched, 0)} 条`} sub="旧交易可能缺少机会编号，只作低权重历史参考" tone={Number(live.unmatched || 0) > 0 ? "negative" : "positive"} />
        <MetricCard title="真实滑点样本" value={`${fmt(live.slippage_rows, 0)} 条`} sub="多空按各自方向计算，不再使用统一估算值" />
        <MetricCard title="影子样本" value={`${fmt(shadow.closed, 0)} / ${fmt(shadow.total, 0)}`} sub={`已带机会编号 ${fmt(shadow.linked, 0)} 条`} />
        <MetricCard title="同事件实盘 / 影子" value={`${fmt(quality?.paired_events, 0)} 个`} sub={`实盘独立事件 ${fmt(live.events, 0)} · 影子独立事件 ${fmt(shadow.events, 0)}`} tone={Number(quality?.paired_events || 0) > 0 ? "positive" : ""} />
      </div>
      <div className="notice">{quality?.message || "等待后端完成第一批结构化样本。"}</div>
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
