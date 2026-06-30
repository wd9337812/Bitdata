export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(await response.text());
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
};

export const statusLabel: Record<string, string> = {
  running: "运行中",
  paused: "已暂停",
};

export const stageLabel: Record<string, string> = {
  growth: "滚仓增长",
  grid: "合约网格",
};
