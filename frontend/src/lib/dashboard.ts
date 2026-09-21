/**
 * 首页（Dashboard，E7-T8）的几个汇总数怎么算。
 *
 * 全是纯函数，输入是现有接口的原样响应（`/api/benchmark-sets`、`/api/agent-configs`、
 * `/api/runs`），不新开后端端点。放这里而不是写在页面里，是因为"谁算参赛者"
 * "几版数据集"这两条是口径，不是排版 —— 算错了首页的数字就和排行榜对不上，
 * 而且没有任何症状。`scripts/check-dashboard.mjs` 钉住。
 */

import type { components } from "./api-types";

type Schemas = components["schemas"];
export type AgentConfigSummary = Schemas["AgentConfigSummary"];
export type BenchmarkSetSummary = Schemas["BenchmarkSetSummary"];
export type RunSummary = Schemas["RunSummary"];
type AgentKind = Schemas["AgentKind"];

// ── 参赛者 ──────────────────────────────────────────────────

/**
 * 哨兵不是参赛者。ORACLE 交官方补丁（解决率必须 100%）、NOOP 交空补丁（必须 0%）、
 * MOCK 按剧本演 —— 它们用来验平台，排行榜的准入六条里明确不收（§14.5 第三条）。
 */
const SENTINEL_KINDS: readonly AgentKind[] = ["ORACLE", "NOOP", "MOCK"];

export function isContestant(
  config: Pick<AgentConfigSummary, "agent_kind" | "enabled">,
): boolean {
  return config.enabled && !SENTINEL_KINDS.includes(config.agent_kind);
}

export interface ParticipantSummary {
  /** 有几个不同的 Agent 在参赛（aider、claude-code、MiniAgent 算 3 个）。 */
  agents: number;
  /** 启用中的参赛配置数。同一个 Agent 换模型是另一条配置，排行榜上各占一行。 */
  configs: number;
  /** 参赛 Agent 的显示名，按首次出现的顺序。 */
  names: string[];
}

export function participantSummary(configs: AgentConfigSummary[]): ParticipantSummary {
  const names: string[] = [];
  let count = 0;
  for (const config of configs) {
    if (!isContestant(config)) continue;
    count += 1;
    if (!names.includes(config.agent_display_name)) names.push(config.agent_display_name);
  }
  return { agents: names.length, configs: count, names };
}

// ── 数据集 ──────────────────────────────────────────────────

export interface DatasetSummary {
  /** 已发布的版本数（benchmark-cn-v1 的 v1、v2 算 2 版）。 */
  versions: number;
  /** 有几个不同的数据集（按 slug）。 */
  datasets: number;
  /** 每个数据集**最新已发布版**的题数之和。旧版本的题不重复计。 */
  latestTasks: number;
  /** 每个数据集最新的那一版，按 slug 排序，首页列出来点进去用。 */
  latest: BenchmarkSetSummary[];
}

/**
 * 只算 `PUBLISHED`。DRAFT 是还没过门禁的，ARCHIVED 是下架的，两者都不该出现在
 * "平台有几版数据集"这个数里 —— 排行榜也只认已发布的版本。
 *
 * "最新"按 `published_at` 取，没有发布时间的（理论上 PUBLISHED 都有）按 id 兜底。
 */
export function datasetSummary(sets: BenchmarkSetSummary[]): DatasetSummary {
  const published = sets.filter((set) => set.status === "PUBLISHED");
  const latestBySlug = new Map<string, BenchmarkSetSummary>();
  for (const set of published) {
    const current = latestBySlug.get(set.slug);
    if (current === undefined || isNewer(set, current)) latestBySlug.set(set.slug, set);
  }
  const latest = [...latestBySlug.values()].sort((a, b) => a.slug.localeCompare(b.slug));
  return {
    versions: published.length,
    datasets: latest.length,
    latestTasks: latest.reduce((sum, set) => sum + set.task_count, 0),
    latest,
  };
}

function isNewer(a: BenchmarkSetSummary, b: BenchmarkSetSummary): boolean {
  if (a.published_at !== null && b.published_at !== null && a.published_at !== b.published_at) {
    return a.published_at > b.published_at;
  }
  return a.id > b.id;
}

// ── 正在跑的实验 ────────────────────────────────────────────

export interface LiveRuns {
  running: RunSummary[];
  queued: RunSummary[];
}

/**
 * 只认 RUNNING 和 QUEUED。DRAFT 是建了还没投进队列的，不算"正在跑"；
 * `isLiveRun` 把它算作要轮询的状态，那是另一个问题（页面要不要刷）。
 */
export function liveRuns(runs: RunSummary[]): LiveRuns {
  return {
    running: runs.filter((run) => run.status === "RUNNING"),
    queued: runs.filter((run) => run.status === "QUEUED"),
  };
}

/** 已定出结论的题占比，进度条右边那个百分数。total 为 0 时是 0，不是 NaN。 */
export function progressPercent(run: Pick<RunSummary, "completed_tasks" | "total_tasks">): number {
  if (run.total_tasks <= 0) return 0;
  return Math.min(100, Math.round((run.completed_tasks / run.total_tasks) * 100));
}
