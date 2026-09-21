/**
 * 数据集与题目的展示语义。
 *
 * 三张页面共用（数据集列表 / 数据集详情 / 单题详情）。这里编码的是"这些数字
 * 该怎么读"，不是排版 —— 判错了页面照常渲染，只是安静地给个错结论：
 * - 构成的三组计数要能跟题量对上（对不上要摆出来，不许悄悄照画条形图）；
 * - `publish_evidence` 的字段类型是 unknown（后端 JSONB 原样透出），
 *   缺字段/类型不对时退回"没有证据"，不许当 0 画；
 * - 哨兵的"过没过"是决议过的语义：Oracle 要全过、Noop 要零解决（§27.2），
 *   页面上要说出来，不能只把两个解决率摆上去让人自己悟；
 * - 历史表现只认 canonical 那次尝试（协议 C-24），题号精确匹配。
 *
 * 只允许 type import：断言脚本不带 tsconfig、直接用 tsc 编译这个文件 —— `@/` 别名解析不到。
 */

import type { components } from "./api-types";
import type { Tone } from "./display";

type Schemas = components["schemas"];

export type IssueLanguage = Schemas["IssueLanguage"];
export type TaskDifficulty = Schemas["TaskDifficulty"];
export type TaskValidationState = Schemas["TaskValidationState"];
export type BenchmarkSetStatus = Schemas["BenchmarkSetStatus"];
export type CompositionCell = Schemas["CompositionCell"];
export type RunSummary = Schemas["RunSummary"];
export type TaskRunSummary = Schemas["TaskRunSummary"];

// ── 枚举的中文标签 ──────────────────────────────────────────
//
// 三种命名风格并存，别照印象写：issue_language 与 difficulty 是小写
// （zh/easy），validation_state 与 benchmark_set 状态是大写下划线。

const ISSUE_LANGUAGE_TEXT: Record<IssueLanguage, string> = {
  zh: "中文",
  en: "英文",
  mixed: "中英混合",
};

const DIFFICULTY_TEXT: Record<TaskDifficulty, string> = {
  easy: "简单",
  medium: "中等",
  hard: "困难",
};

/**
 * 验证状态。tone 的含义与 display.ts 同档。
 *
 * QUARANTINED 标 bad 不是 neutral：隔离是"题目复验也失败"的结论（C-20 第 6 步），
 * 一个中性灰会让人错过；REVIEW_REQUIRED 标 warn —— 它等的是人，不是机器。
 */
const VALIDATION_STATE_TEXT: Record<
  TaskValidationState,
  { label: string; tone: Tone }
> = {
  DISCOVERED: { label: "已发现", tone: "neutral" },
  CANDIDATE: { label: "候选", tone: "neutral" },
  VALIDATING: { label: "验证中", tone: "active" },
  VALID: { label: "已验证", tone: "ok" },
  INVALID: { label: "验证未通过", tone: "bad" },
  REVIEW_REQUIRED: { label: "待人工复核", tone: "warn" },
  QUARANTINED: { label: "已隔离", tone: "bad" },
};

const BENCHMARK_SET_STATUS_TEXT: Record<
  BenchmarkSetStatus,
  { label: string; tone: Tone }
> = {
  DRAFT: { label: "草稿", tone: "neutral" },
  PUBLISHED: { label: "已发布", tone: "ok" },
  ARCHIVED: { label: "已归档", tone: "neutral" },
};

export function issueLanguageLabel(value: IssueLanguage): string {
  return ISSUE_LANGUAGE_TEXT[value] ?? value;
}

export function difficultyLabel(value: TaskDifficulty): string {
  return DIFFICULTY_TEXT[value] ?? value;
}

export function validationStateText(value: TaskValidationState): {
  label: string;
  tone: Tone;
} {
  return VALIDATION_STATE_TEXT[value] ?? { label: value, tone: "neutral" };
}

export function benchmarkSetStatusText(value: BenchmarkSetStatus): {
  label: string;
  tone: Tone;
} {
  return BENCHMARK_SET_STATUS_TEXT[value] ?? { label: value, tone: "neutral" };
}

// ── 构成（composition）──────────────────────────────────────
//
// 后端固定三组：language / difficulty / repository（§14.5 第一节），
// 数的是冻进快照的题，不是整张 benchmark_tasks。

export const COMPOSITION_GROUPS = ["language", "difficulty", "repository"] as const;
export type CompositionGroupKey = (typeof COMPOSITION_GROUPS)[number];

const COMPOSITION_GROUP_TEXT: Record<CompositionGroupKey, string> = {
  language: "语言",
  difficulty: "难度",
  repository: "仓库",
};

export interface CompositionCellRow {
  /** 原始取值 —— 筛选下拉框的 value 用它，别用中文标签 */
  value: string;
  label: string;
  count: number;
  /** 占该组合计的百分比，0–100，一位小数 */
  percent: number;
}

export interface CompositionGroupRow {
  key: CompositionGroupKey;
  label: string;
  cells: CompositionCellRow[];
  total: number;
  /** 组合计与题量是否一致。不一致说明构成和快照对不上，页面要显式提醒 */
  matchesTaskCount: boolean;
}

/** 组内取值的标签：语言和难度有中文名，仓库用全名。 */
function compositionCellLabel(key: CompositionGroupKey, value: string): string {
  if (key === "language") return ISSUE_LANGUAGE_TEXT[value as IssueLanguage] ?? value;
  if (key === "difficulty") return DIFFICULTY_TEXT[value as TaskDifficulty] ?? value;
  return value;
}

/**
 * 把 `BenchmarkSetDetail.composition` 摊平成三组展示行。
 *
 * 三条规则：
 * - **固定三组、固定顺序**（语言 → 难度 → 仓库），后端多给的组忽略；
 * - 组内按数量降序、并列按取值升序 —— 别让字典序决定"哪个语言是主要语言"；
 * - 组缺失或计数之和与题量对不上时照原样返回（`matchesTaskCount=false`），
 *   **不修正** —— 修正过就看不出后端出问题了。
 */
export function compositionRows(
  composition: Record<string, CompositionCell[]>,
  taskCount: number,
): CompositionGroupRow[] {
  return COMPOSITION_GROUPS.map((key) => {
    const raw = composition[key] ?? [];
    const cells: CompositionCellRow[] = [...raw]
      .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
      .map((cell) => ({
        value: cell.value,
        label: compositionCellLabel(key, cell.value),
        count: cell.count,
        percent: 0,
      }));
    const total = cells.reduce((sum, cell) => sum + cell.count, 0);
    for (const cell of cells) {
      cell.percent =
        total === 0 ? 0 : Math.round((cell.count / total) * 1000) / 10;
    }
    return {
      key,
      label: COMPOSITION_GROUP_TEXT[key],
      cells,
      total,
      matchesTaskCount: total === taskCount,
    };
  });
}

// ── 发布门禁（publish_evidence）─────────────────────────────
//
// 后端的类型是 `{[key: string]: unknown} | null`：JSONB 原样透出，老版本可能缺
// 字段、类型可能不对。全部按"缺"处理，不做猜测性转换（"5" 不当 5）。

export interface SentinelEvidence {
  /** 门禁实验的 evaluation_run id，链到 /runs/[id] 用 */
  runId: number | null;
  totalTasks: number | null;
  resolvedCount: number | null;
  /** 0–1；越界（>1 或 <0）按缺处理 */
  resolveRate: number | null;
  /** Oracle 全过才算过、Noop 零解决才算过（§27.2）；数不够判就给 null */
  ok: boolean | null;
}

export interface PublishGate {
  checkedAt: string | null;
  snapshotDigest: string | null;
  protocolClause: string | null;
  oracle: SentinelEvidence | null;
  noop: SentinelEvidence | null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function sentinelEvidence(
  raw: unknown,
  wantResolved: (resolved: number, total: number) => boolean,
): SentinelEvidence | null {
  if (typeof raw !== "object" || raw === null) return null;
  const record = raw as Record<string, unknown>;
  const totalTasks = asNumber(record.total_tasks);
  const resolvedCount = asNumber(record.resolved_count);
  const resolveRate = asNumber(record.resolve_rate);
  return {
    runId: asNumber(record.evaluation_run_id),
    totalTasks,
    resolvedCount,
    resolveRate:
      resolveRate !== null && resolveRate >= 0 && resolveRate <= 1
        ? resolveRate
        : null,
    ok:
      totalTasks !== null && resolvedCount !== null
        ? wantResolved(resolvedCount, totalTasks)
        : null,
  };
}

/**
 * 解析 `BenchmarkSetDetail.publish_evidence`。
 *
 * `null`（没跑过门禁）与非对象都返回 `null`，页面据此说"没留下门禁证据"。
 * 另外两种情况页面要分开说，所以对象照常返回、由页面看字段：
 * - 全字段都缺 → 页面说"证据里没有 Oracle/Noop 记录"；
 * - 缺个别字段 → 缺的格子显示"—"，`ok` 判不了就是"数据不足"。
 */
export function publishGate(
  evidence: Record<string, unknown> | null,
): PublishGate | null {
  if (typeof evidence !== "object" || evidence === null) return null;
  return {
    checkedAt: asString(evidence.checked_at),
    snapshotDigest: asString(evidence.snapshot_digest),
    protocolClause: asString(evidence.protocol_clause),
    oracle: sentinelEvidence(evidence.oracle, (resolved, total) => resolved === total),
    noop: sentinelEvidence(evidence.noop, (resolved) => resolved === 0),
  };
}

/** 门禁证据里的解决率是 0–1 的浮点数，不是 display.formatRate 收的那种字符串。 */
export function ratePercent(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

// ── 隔离记录（quarantine）───────────────────────────────────

export interface QuarantineNote {
  at: string | null;
  fromState: string | null;
  reason: string | null;
}

/** 隔离记录 `{at, from_state, reason}`。三个字段都可能缺（JSONB 原样透出）。 */
export function quarantineNote(
  quarantine: Record<string, unknown> | null,
): QuarantineNote | null {
  if (typeof quarantine !== "object" || quarantine === null) return null;
  return {
    at: asString(quarantine.at),
    fromState: asString(quarantine.from_state),
    reason: asString(quarantine.reason),
  };
}

// ── 单题的历史表现 ─────────────────────────────────────────
//
// §16.2 要"各 Agent 在该题的历史表现"，但后端没有按题查跨实验的端点
// （§14.4 那张清单里没有）—— 这是前端侧的就地聚合：拉最近若干次实验 +
// 每次实验的 canonical 逐题记录，滤出这道题。请求数被 HISTORY_RUN_LIMIT 封顶。

/**
 * 最多回溯多少次实验。
 *
 * 演示库里十几个实验，真实库里实验会一直涨 —— 不封顶就是一个越用越慢的页面。
 * 要更大的窗口应该由后端补一个按题查的端点，不是把这个数调大。
 */
export const HISTORY_RUN_LIMIT = 20;

export interface HistoryRow {
  runId: number;
  runName: string;
  agentLabel: string;
  taskRunId: number;
  lifecycleStatus: Schemas["LifecycleStatus"];
  infraOutcome: Schemas["InfraOutcome"] | null;
  agentOutcome: Schemas["AgentOutcome"] | null;
  errorCode: string | null;
  f2pPassed: number | null;
  f2pTotal: number | null;
  p2pPassed: number | null;
  p2pTotal: number | null;
  costUsd: string | null;
  totalDurationMs: number | null;
}

/**
 * 从"实验列表 + 各实验的逐题记录"里滤出这道题的历史。
 *
 * 四条规则，每条判错之后从页面上都看不出来：
 * - **只认 canonical**（协议 C-24）：一次实验里同一题可能有好几次尝试，
 *   统计口径只取认定那一次；非 canonical 的行即使被拉回来也要丢掉；
 * - **题号精确匹配**：`a-1` 不许匹配到 `a-11`（不做前缀、不做包含）；
 * - 同一次实验里出现多条 canonical（数据异常）时取第一条，不重复出行；
 * - 按实验号倒序（最新在前），截断到 `limit`。
 */
export function historyRows(
  runs: RunSummary[],
  taskRunsByRun: Map<number, TaskRunSummary[]>,
  taskId: string,
  limit: number = HISTORY_RUN_LIMIT,
): HistoryRow[] {
  const rows: HistoryRow[] = [];
  for (const run of runs) {
    const hit = (taskRunsByRun.get(run.id) ?? []).find(
      (taskRun) => taskRun.is_canonical && taskRun.task_id === taskId,
    );
    if (hit === undefined) continue;
    rows.push({
      runId: run.id,
      runName: run.name,
      agentLabel: run.agent_config_label,
      taskRunId: hit.id,
      lifecycleStatus: hit.lifecycle_status,
      infraOutcome: hit.infra_outcome,
      agentOutcome: hit.agent_outcome,
      errorCode: hit.error_code,
      f2pPassed: hit.f2p_passed,
      f2pTotal: hit.f2p_total,
      p2pPassed: hit.p2p_passed,
      p2pTotal: hit.p2p_total,
      costUsd: hit.cost_usd,
      totalDurationMs: hit.total_duration_ms,
    });
  }
  return rows.sort((a, b) => b.runId - a.runId).slice(0, limit);
}

// ── 杂项 ────────────────────────────────────────────────────

/** 哈希短显（快照指纹、content_hash）。空值显示 "—"，别显示 "null…"。 */
export function shortHash(value: string | null, length = 12): string {
  if (value === null || value === "") return "—";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}
