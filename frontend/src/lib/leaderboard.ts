/**
 * 排行榜页的展示语义。
 *
 * 这里编码的是"数字该怎么读"，不是排版：
 * - 报不出成本 ≠ 最便宜（成本为 null 的行不许进散点图）；
 * - 没数据 ≠ 全挂（分面矩阵缺格是 null，不是 0）；
 * - 单轮没有"轮间"（run_count < 2 不谈抖动，§18.6 实测两轮能差 9 个百分点）。
 * 这些判错了页面不会报错，只会安静地给出一个错的结论 —— 所以有一条专门的
 * 断言脚本（npm run check:leaderboard）钉住它们。
 *
 * 只允许 type import：断言脚本不带 tsconfig、直接用 tsc 编译这个文件 —— `@/` 别名解析不到；
 * 运行时 import 还会把依赖文件一起编进产物、输出路径错位。
 */

import type { components } from "./api-types";

type Schemas = components["schemas"];

export type LeaderboardMetric = Schemas["LeaderboardMetric"];
export type LeaderboardFacet = Schemas["LeaderboardFacet"];
export type LeaderboardRow = Schemas["LeaderboardRowOut"];
export type FacetCell = Schemas["FacetCellOut"];
export type ExcludedRun = Schemas["ExcludedRun"];

/** 排序口径的中文名。键就是后端 metric 参数的枚举原值。 */
export const METRIC_LABELS: Record<LeaderboardMetric, string> = {
  resolve_rate: "解决率",
  cost: "每题成本",
  duration: "耗时",
  tokens: "每题 token",
};

/** 分面维度的中文名。 */
export const FACET_LABELS: Record<LeaderboardFacet, string> = {
  difficulty: "难度",
  language: "语言",
  repository: "仓库",
};

const FACET_VALUE_LABELS: Record<LeaderboardFacet, Record<string, string>> = {
  difficulty: { easy: "简单", medium: "中等", hard: "困难" },
  language: { zh: "中文", en: "英文", mixed: "中英混合" },
  repository: {},
};

/** 分面取值的中文标签。认不出的原样返回（枚举原值透出，翻译只在展示层）。 */
export function facetValueLabel(facet: LeaderboardFacet, value: string): string {
  return FACET_VALUE_LABELS[facet][value] ?? value;
}

const FACET_VALUE_ORDER: Record<LeaderboardFacet, string[]> = {
  difficulty: ["easy", "medium", "hard"],
  language: ["zh", "en", "mixed"],
  repository: [],
};

/**
 * 矩阵行的排序：认识的取值按固定顺序（难度从易到难），不认识的排后面按字母。
 * 后端按字母序返回，难度会排成 easy/hard/medium —— 顺序在展示层定。
 */
export function facetValueOrder(facet: LeaderboardFacet, values: string[]): string[] {
  const known = FACET_VALUE_ORDER[facet];
  const weight = (value: string) => {
    const index = known.indexOf(value);
    return index === -1 ? known.length : index;
  };
  return [...values].sort((a, b) => weight(a) - weight(b) || a.localeCompare(b, "en"));
}

export interface FacetMatrix {
  /** 行标签（已排序的分面取值）。 */
  values: string[];
  /** `cells[参赛者下标][取值下标]`；null = 该参赛者在这个取值上没有数据。 */
  cells: (FacetCell | null)[][];
}

/**
 * 分面矩阵：行 = 分面取值，列 = 参赛者。
 * 缺格留 null —— 页面画成 "—"；填 0 会把"没数据"读成"全挂了"。
 * facet 必须是取回这批 rows 时用的那个分面（用响应里的 facet 字段）；传错行序和标签会和数据对不上。
 */
export function facetMatrix(rows: LeaderboardRow[], facet: LeaderboardFacet): FacetMatrix {
  const seen = new Set<string>();
  for (const row of rows) {
    for (const cell of row.facets) seen.add(cell.value);
  }
  const values = facetValueOrder(facet, [...seen]);
  const cells = rows.map((row) => {
    const byValue = new Map(row.facets.map((cell) => [cell.value, cell]));
    return values.map((value) => byValue.get(value) ?? null);
  });
  return { values, cells };
}

export interface ScatterPoint {
  row: LeaderboardRow;
  cost: number;
  rate: number;
  /**
   * true = 成本是下界：有 attempt 报不出成本，后端只把报得出的部分加了起来
   * （`cost_lower_bound`）。页面画成空心点并注明，不能和实心点一样读。
   */
  lowerBound: boolean;
}

/**
 * 散点取点：x = 每题成本，y = 严格解决率。
 *
 * 成本或解决率为 null 的行**不进图**，单独放 `skipped` 给页面列名说明 ——
 * 把 null 当 0 画，"报不出成本"的参赛者会落在最便宜的位置上（§14.5 第五条）。
 * 后端从 2026-09-21 起只在**一次都报不出**时给 null；部分报不出的行给下界，
 * 这里照画，但把 `lowerBound` 带出去让页面标出来。
 */
export function scatterPoints(rows: LeaderboardRow[]): {
  points: ScatterPoint[];
  skipped: LeaderboardRow[];
} {
  const points: ScatterPoint[] = [];
  const skipped: LeaderboardRow[] = [];
  for (const row of rows) {
    const cost = row.cost_per_task === null ? null : Number(row.cost_per_task);
    const rate = row.resolve_rate_mean === null ? null : Number(row.resolve_rate_mean);
    if (cost === null || rate === null || !Number.isFinite(cost) || !Number.isFinite(rate)) {
      skipped.push(row);
    } else {
      points.push({ row, cost, rate, lowerBound: row.cost_lower_bound });
    }
  }
  return { points, skipped };
}

export interface SpreadRange {
  /** 比例原值（0–1 的字符串），格式化和 display.formatRate 同一处。 */
  min: string;
  max: string;
}

/** 轮间范围。单轮没有"轮间"可言 → null，页面不显示这一行。 */
export function spreadRange(row: LeaderboardRow): SpreadRange | null {
  if (row.run_count < 2 || row.resolve_rate_min === null || row.resolve_rate_max === null) {
    return null;
  }
  return { min: row.resolve_rate_min, max: row.resolve_rate_max };
}

/**
 * 平台故障率（C-22 要求与解决率同行展示）。
 * 分母 = 每题 × 轮数 = 这批实验认定过的题数；没有题时给 null 而不是 0。
 */
export function infraFailureRate(row: LeaderboardRow): number | null {
  const total = row.tasks_per_run * row.run_count;
  return total > 0 ? row.infra_failure_total / total : null;
}

export interface CostNote {
  text: string;
  /** true = 金额不可全信（报不出成本的次数 > 0），页面标警示色。 */
  warn: boolean;
}

/**
 * 成本来源提示。§14.5 第五条：报不出成本 ≠ 最便宜 ——
 * 页面必须把"这个金额是缺的"说出来，而不是安静地显示一个数。
 * 有报不出的次数时 `cost_per_task` 是下界（后端 `cost_lower_bound`），提示要标警示色。
 */
export function costNote(row: LeaderboardRow): CostNote | null {
  const parts: string[] = [];
  if (row.cost_unavailable_attempts > 0) {
    parts.push(`${row.cost_unavailable_attempts} 次未报出成本`);
  }
  if (row.cost_estimated_attempts > 0) {
    parts.push(`${row.cost_estimated_attempts} 次为估算`);
  }
  if (parts.length === 0) return null;
  return { text: parts.join("，"), warn: row.cost_unavailable_attempts > 0 };
}
