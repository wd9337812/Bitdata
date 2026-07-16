export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    let message = text || `请求失败：${response.status}`;
    try {
      const data = JSON.parse(text);
      message = data.detail || data.message || data.error || message;
    } catch {
      // Keep the raw response text when the server did not return JSON.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const fmt = (value: unknown, digits = 2) => {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return number.toLocaleString("zh-CN", { maximumFractionDigits: digits });
};

export const modeLabel: Record<string, string> = {
  conservative: "稳健",
  balanced: "均衡",
  attack: "进攻",
  tournament: "锦标赛",
  tournament_sprint: "锦标赛冲刺",
  extreme_sprint: "机会引擎 V4 滚仓",
  yolo_scalp: "盘口剥头皮",
  grid: "稳定网格",
};

export const statusLabel: Record<string, string> = {
  running: "运行中",
  rate_limited: "限流等待中",
  paused: "已暂停",
  hard_stopped: "权益硬停止",
};

export const stageLabel: Record<string, string> = {
  growth: "滚仓增长",
  grid: "合约网格",
};
