/**
 * 失败分析页（E7-T6）的口径。
 *
 * 数据来自 `GET /api/analysis` —— 它返回的 `failures` 和报告 JSON 的 `failures` 段是
 * 后端同一个函数算的（`app/report/aggregate.py` 的 `failure_summary()`），这里**不重算
 * 任何计数**，只做三件事：把分布摊成堆叠柱要的行、把热力图摊成矩阵、给案例定色调。
 *
 * 两条口径要钉住（`scripts/check-analysis.mjs`）：
 * - "规则分不出、还没结论"的失败（unattributed）**不进任何类别**，单独一个数；
 * - 堆叠柱每根的高度必须等于 `category_counts` 里那个数 —— 热力图和分布是同一批行，
 *   对不上说明有 Agent 被折进"其他"时算漏了。
 */

import type { components } from "./api-types";
import {
  FAILURE_CATEGORIES,
  failureCategoryLabel,
  failureCategoryTone,
  type FailureCategory,
  type Tone,
} from "./display";

type Schemas = components["schemas"];
export type AnalysisResponse = Schemas["AnalysisResponse"];
export type FailureSummary = Schemas["FailureSummary"];
export type FailureCase = Schemas["FailureCase"];
export type FailureCell = Schemas["FailureCell"];

// ── 类别 ────────────────────────────────────────────────────

/** 类别的短名："F6_REGRESSION" → "F6"。图的横轴放不下全名。 */
export function categoryShort(category: string): string {
  const underscore = category.indexOf("_");
  return underscore === -1 ? category : category.slice(0, underscore);
}

/** 类别全名（中文）；后端给了枚举外的值时原样显示，不吞。 */
export function categoryLabel(category: string): string {
  return (FAILURE_CATEGORIES as string[]).includes(category)
    ? failureCategoryLabel(category as FailureCategory)
    : category;
}

/**
 * 出现过的类别，按协议编号顺序（F1…F8、N1、N2），枚举外的排最后。
 * 只列 `category_counts` 里有的 —— 十个类别全画会有一半是空柱。
 */
export function presentCategories(summary: Pick<FailureSummary, "category_counts">): string[] {
  const known = (FAILURE_CATEGORIES as string[]).filter(
    (category) => (summary.category_counts[category] ?? 0) > 0,
  );
  const unknown = Object.keys(summary.category_counts)
    .filter((category) => !(FAILURE_CATEGORIES as string[]).includes(category))
    .sort();
  return [...known, ...unknown];
}

// ── 参赛者与颜色 ────────────────────────────────────────────

/**
 * 堆叠柱的分段色，一个参赛者一个，按首次出现固定分配、不按名次。
 * 五个槽位跑过 dataviz 的校验（相邻色对在色觉异常下 ΔE ≥ 9）；第六个参赛者起折进"其他"，
 * 不再生成新颜色 —— 九种颜色没人分得清。
 */
export const AGENT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"] as const;
export const OTHER_AGENTS = "其他";

/** 参赛者顺序：按标签字母序，和颜色一起固定，换个数据集同一个参赛者还是同一种颜色。 */
export function agentOrder(heatmap: Pick<FailureCell, "agent_label">[]): string[] {
  return [...new Set(heatmap.map((cell) => cell.agent_label))].sort();
}

/** 堆叠柱上实际画的序列：前五个参赛者各一段，其余合成"其他"。 */
export function stackSeries(agents: string[]): { key: string; label: string; color: string }[] {
  const series: { key: string; label: string; color: string }[] = agents
    .slice(0, AGENT_COLORS.length)
    .map((label, index) => ({ key: label, label, color: AGENT_COLORS[index] }));
  if (agents.length > AGENT_COLORS.length) {
    series.push({ key: OTHER_AGENTS, label: OTHER_AGENTS, color: "#8a8a86" });
  }
  return series;
}

// ── 堆叠柱 ──────────────────────────────────────────────────

export interface StackedRow {
  category: string;
  short: string;
  label: string;
  /** 这一类的总数（= category_counts）。 */
  total: number;
  /** 每个序列（参赛者 / 其他）在这一类里的条数。 */
  counts: Record<string, number>;
}

/**
 * 归因分布：一根柱 = 一个类别，分段 = 参赛者。
 * 只用 heatmap 摊，再拿 category_counts 核对 —— 两者是后端同一批行数出来的，
 * 对不上就是这里摊错了（比如折"其他"时漏加）。
 */
export function stackedRows(
  summary: Pick<FailureSummary, "category_counts" | "heatmap">,
): StackedRow[] {
  const agents = agentOrder(summary.heatmap);
  const series = stackSeries(agents);
  const keyOf = (agent: string) =>
    agents.indexOf(agent) < AGENT_COLORS.length ? agent : OTHER_AGENTS;
  return presentCategories(summary).map((category) => {
    const counts: Record<string, number> = Object.fromEntries(series.map((s) => [s.key, 0]));
    for (const cell of summary.heatmap) {
      if (cell.category !== category) continue;
      counts[keyOf(cell.agent_label)] += cell.count;
    }
    return {
      category,
      short: categoryShort(category),
      label: categoryLabel(category),
      total: summary.category_counts[category] ?? 0,
      counts,
    };
  });
}

/** 堆叠柱和 category_counts 对不上的类别；空数组才算口径一致。 */
export function stackMismatches(rows: StackedRow[]): string[] {
  return rows
    .filter((row) => Object.values(row.counts).reduce((a, b) => a + b, 0) !== row.total)
    .map((row) => row.category);
}

// ── 热力图 ──────────────────────────────────────────────────

export interface HeatmapMatrix {
  agents: string[];
  categories: string[];
  /** cells[agentIndex][categoryIndex]；没有这一格是 null，不是 0（没数据不等于零次）。 */
  cells: (number | null)[][];
  rowTotals: number[];
  columnTotals: number[];
  max: number;
}

export function heatmapMatrix(
  summary: Pick<FailureSummary, "category_counts" | "heatmap">,
): HeatmapMatrix {
  const agents = agentOrder(summary.heatmap);
  const categories = presentCategories(summary);
  const cells: (number | null)[][] = agents.map(() => categories.map(() => null));
  for (const cell of summary.heatmap) {
    const row = agents.indexOf(cell.agent_label);
    const column = categories.indexOf(cell.category);
    if (row === -1 || column === -1) continue;
    cells[row][column] = (cells[row][column] ?? 0) + cell.count;
  }
  const rowTotals = cells.map((row) => row.reduce<number>((a, b) => a + (b ?? 0), 0));
  const columnTotals = categories.map((_, column) =>
    cells.reduce<number>((a, row) => a + (row[column] ?? 0), 0),
  );
  const max = Math.max(0, ...cells.flat().map((value) => value ?? 0));
  return { agents, categories, cells, rowTotals, columnTotals, max };
}

/**
 * 热力图格子深浅：0～4 五档，按"占这个 Agent 失败总数的比例"分，不按绝对条数 ——
 * aider 失败 70 道、claude-code 失败 19 道，按绝对数 claude-code 整行都是浅色，看不出它
 * 的失败集中在哪一类。
 */
export function cellShade(count: number | null, rowTotal: number): 0 | 1 | 2 | 3 | 4 {
  if (count === null || count === 0 || rowTotal === 0) return 0;
  const share = count / rowTotal;
  if (share >= 0.5) return 4;
  if (share >= 0.3) return 3;
  if (share >= 0.15) return 2;
  return 1;
}

// ── 覆盖情况 ────────────────────────────────────────────────

export interface Coverage {
  total: number;
  attributed: number;
  /** 规则分不出、也还没跑 LLM 或人工的。**不在任何类别里**。 */
  unattributed: number;
  /** 由规则层给出结论的条数（attributed − llm）。 */
  rule: number;
  llm: number;
  /** 自动归因自己标了"要人看"的条数；它们已计入 attributed 和各类别。 */
  needsHuman: number;
}

export function coverage(
  summary: Pick<
    FailureSummary,
    | "total_failures"
    | "attributed_failures"
    | "unattributed_failures"
    | "llm_attributed_failures"
    | "needs_human_failures"
  >,
): Coverage {
  const llm = summary.llm_attributed_failures ?? 0;
  return {
    total: summary.total_failures,
    attributed: summary.attributed_failures,
    unattributed: summary.unattributed_failures,
    rule: Math.max(0, summary.attributed_failures - llm),
    llm,
    needsHuman: summary.needs_human_failures ?? 0,
  };
}

// ── 案例 ────────────────────────────────────────────────────

/**
 * 案例行的色调：没结论中性；NEEDS_HUMAN 黄（要人看，还不算数）；其余按类别（F 红 / N 黄）。
 */
export function caseTone(
  item: Pick<FailureCase, "category" | "attribution_status">,
): { tone: Tone; label: string } {
  if (item.category === null) return { tone: "neutral", label: "还没结论" };
  const label = categoryLabel(item.category);
  if (item.attribution_status === "NEEDS_HUMAN") return { tone: "warn", label };
  const tone = (FAILURE_CATEGORIES as string[]).includes(item.category)
    ? failureCategoryTone(item.category as FailureCategory)
    : "neutral";
  return { tone, label };
}
