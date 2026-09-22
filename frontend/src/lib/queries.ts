"use client";

/**
 * 服务端状态的查询入口。
 *
 * 每个资源一个 hook，key 用数组分层（"runs" / "run" / id），失效时按前缀批量干掉即可。
 * 类型全部来自 api-types.ts（OpenAPI 生成），不手写。
 *
 * 实时性走轮询不走 WebSocket（§16.1）：Run Detail 3s、Dashboard 10s ——
 * 需要的地方透传 `refetchInterval` 选项即可。
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { apiGet, apiPost, apiText } from "./api";
import type { components } from "./api-types";

type Schemas = components["schemas"];

// —— 类型再导出：页面只 import 这一个文件就够，不用记 api-types 里的编码命名 ——
export type AnalysisResponse = Schemas["AnalysisResponse"];
export type RunSummary = Schemas["RunSummary"];
export type RunDetail = Schemas["RunDetail"];
export type TaskRunSummary = Schemas["TaskRunSummary"];
export type TaskRunDetail = Schemas["TaskRunDetail"];
export type TestResultRow = Schemas["TestResultRow"];
export type PatchSummary = Schemas["PatchSummary"];
export type ArtifactSummary = Schemas["ArtifactSummary"];
export type AgentSummary = Schemas["AgentSummary"];
export type AgentConfigSummary = Schemas["AgentConfigSummary"];
export type BenchmarkSetSummary = Schemas["BenchmarkSetSummary"];
export type BenchmarkSetDetail = Schemas["BenchmarkSetDetail"];
export type CompositionCell = Schemas["CompositionCell"];
export type TaskSummary = Schemas["TaskSummary"];
export type TaskDetail = Schemas["TaskDetail"];
export type LeaderboardResponse = Schemas["LeaderboardResponse"];
export type CreateRunRequest = Schemas["CreateRunRequest"];
export type CreateRunResponse = Schemas["CreateRunResponse"];
export type CancelResponse = Schemas["CancelResponse"];
export type RetryResponse = Schemas["RetryResponse"];
export type RunPage = Schemas["Page_RunSummary_"];
export type TaskRunPage = Schemas["Page_TaskRunSummary_"];
export type TestResultPage = Schemas["Page_TestResultRow_"];
export type AgentPage = Schemas["Page_AgentSummary_"];
export type AgentConfigPage = Schemas["Page_AgentConfigSummary_"];
export type BenchmarkSetPage = Schemas["Page_BenchmarkSetSummary_"];
export type TaskPage = Schemas["Page_TaskSummary_"];
export type ReportBatch = Schemas["ReportBatchResponse"];
export type ReportPage = Schemas["Page_ReportBatchResponse_"];

// —— 各端点的查询参数（照抄后端 OpenAPI 里的定义，别自己发明字段）——

export interface RunsParams {
  /** 只看这一版数据集的（数据集 id） */
  set?: number;
  agent_config?: number;
  status?: Schemas["EvaluationRunStatus"];
  limit?: number;
  offset?: number;
}

export interface TaskRunsParams {
  status?: Schemas["LifecycleStatus"];
  /** 只看认定结果那一次 attempt（协议 C-24） */
  canonical_only?: boolean;
  limit?: number;
  offset?: number;
}

export interface TaskRunTestsParams {
  /** 只看 F2P 或 P2P（协议 C-10 的两个名单）。 */
  role?: Schemas["TestRole"];
  status?: Schemas["TestStatus"];
  limit?: number;
  offset?: number;
}

/**
 * 制品正文的种类。
 *
 * 两个来源：日志/轨迹这类是 `ArtifactKind`；补丁正文是另一个窄枚举
 * `AgentPatchKind`（后端 `task_runs.py` 里定义的，只有 Agent 自己的两份，
 * 官方补丁 GOLD/TEST 被挡在枚举之外 —— 前端类型里根本没有那两个选项）。
 */
export type ArtifactTextKind = Schemas["ArtifactKind"] | Schemas["AgentPatchKind"];

export interface AnalysisParams {
  /** 数据集 slug：这一版上所有排行榜准入的实验 */
  set?: string;
  version?: string;
  /** 实验号，和 set 二选一 */
  run?: number[];
  top_n?: number;
}

export interface LeaderboardParams {
  /** 数据集 slug。不给就取最新已发布的那一版 */
  set?: string;
  /** 数据集版本，配合 set 用 */
  version?: string;
  metric?: Schemas["LeaderboardMetric"];
  facet?: Schemas["LeaderboardFacet"];
  limit?: number;
  offset?: number;
}

export interface BenchmarkSetsParams {
  slug?: string;
  status?: Schemas["BenchmarkSetStatus"];
  limit?: number;
  offset?: number;
}

export interface TasksParams {
  /** 只看这一版数据集快照里冻住的题（数据集 id，不是 slug） */
  set?: number;
  state?: Schemas["TaskValidationState"];
  /** 仓库全名，如 pallets/click */
  repo?: string;
  difficulty?: Schemas["TaskDifficulty"];
  language?: Schemas["IssueLanguage"];
  /** 搜题号与 issue 标题，不区分大小写 */
  q?: string;
  limit?: number;
  offset?: number;
}

export interface ReportsParams {
  limit?: number;
  offset?: number;
}

/** 查询 key 工厂。层级：资源 → 参数，便于按前缀失效。 */
export const queryKeys = {
  health: ["health"] as const,
  runs: (params: RunsParams = {}) => ["runs", params] as const,
  run: (id: number) => ["run", id] as const,
  runTaskRuns: (runId: number, params: TaskRunsParams = {}) =>
    ["run", runId, "task-runs", params] as const,
  taskRun: (id: number) => ["task-run", id] as const,
  taskRunTests: (id: number, params: TaskRunTestsParams = {}) =>
    ["task-run", id, "tests", params] as const,
  artifactText: (taskRunId: number, kind: ArtifactTextKind) =>
    ["task-run", taskRunId, "artifact", kind] as const,
  leaderboard: (params: LeaderboardParams = {}) =>
    ["leaderboard", params] as const,
  analysis: (params: AnalysisParams = {}) => ["analysis", params] as const,
  agents: ["agents"] as const,
  agentConfigs: ["agent-configs"] as const,
  benchmarkSets: (params: BenchmarkSetsParams = {}) =>
    ["benchmark-sets", params] as const,
  benchmarkSet: (slug: string, version?: string) =>
    ["benchmark-set", slug, version ?? null] as const,
  tasks: (params: TasksParams = {}) => ["tasks", params] as const,
  task: (taskId: string) => ["task", taskId] as const,
  reports: (params: ReportsParams = {}) => ["reports", params] as const,
};

/** 拼查询串；空值（undefined / null / ""）直接丢掉，不产出 `?set=` 这种空参数。数组展开成重复参数（`run=1&run=2`）。 */
function withQuery<T extends object>(path: string, params: T): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
    } else if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

/** 查询 hook 的公共选项：透传 TanStack Query 原生选项（refetchInterval、enabled 等）。 */
type QueryOpts<T> = Omit<UseQueryOptions<T>, "queryKey" | "queryFn">;

export function useHealth(options?: QueryOpts<Schemas["HealthResponse"]>) {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => apiGet<Schemas["HealthResponse"]>("/api/health"),
    ...options,
  });
}

export function useRuns(params: RunsParams = {}, options?: QueryOpts<RunPage>) {
  return useQuery({
    queryKey: queryKeys.runs(params),
    queryFn: () => apiGet<RunPage>(withQuery("/api/runs", params)),
    ...options,
  });
}

/**
 * 把全部实验拉回来（翻页到凑齐 `total`）。
 *
 * `/agents` 页要按"最近一次实验是哪条配置跑的"区分同名的新旧配置（E7 走查 #19），
 * 这个判断得看到全部实验、不能只看第一页——旧配置的实验可能已经翻到后面去了。
 */
export async function fetchAllRuns(
  params: Omit<RunsParams, "limit" | "offset"> = {},
): Promise<RunSummary[]> {
  const first = await apiGet<RunPage>(
    withQuery("/api/runs", { ...params, limit: MAX_PAGE_LIMIT, offset: 0 }),
  );
  const items = [...first.items];
  while (items.length < first.total) {
    const page = await apiGet<RunPage>(
      withQuery("/api/runs", { ...params, limit: MAX_PAGE_LIMIT, offset: items.length }),
    );
    if (page.items.length === 0) break;
    items.push(...page.items);
  }
  return items;
}

export function useAllRuns(
  params: Omit<RunsParams, "limit" | "offset"> = {},
  options?: QueryOpts<RunSummary[]>,
) {
  return useQuery({
    queryKey: ["runs", "all", params],
    queryFn: () => fetchAllRuns(params),
    ...options,
  });
}

export function useRun(id: number, options?: QueryOpts<RunDetail>) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => apiGet<RunDetail>(`/api/runs/${id}`),
    ...options,
  });
}

/**
 * 逐题记录的非 hook 取数版本。
 *
 * TaskHistory 要在**一个 useQueries 里批量拉几十次实验**（hook 不能循环调用），
 * 所以把"怎么取"从 hook 里拆出来；`useRunTaskRuns` 与它共用同一条路径，
 * 两处拼 URL 迟早会漂移。
 */
export function fetchRunTaskRuns(
  runId: number,
  params: TaskRunsParams = {},
): Promise<TaskRunPage> {
  return apiGet<TaskRunPage>(withQuery(`/api/runs/${runId}/task-runs`, params));
}

export function useRunTaskRuns(
  runId: number,
  params: TaskRunsParams = {},
  options?: QueryOpts<TaskRunPage>,
) {
  return useQuery({
    queryKey: queryKeys.runTaskRuns(runId, params),
    queryFn: () => fetchRunTaskRuns(runId, params),
    ...options,
  });
}

/** 后端单页上限（`app/api/deps.py` 的 MAX_LIMIT）。要更多只能翻页。 */
const MAX_PAGE_LIMIT = 200;

/**
 * 把一次实验的逐题记录**全部**拉回来：按后端单页上限翻页，直到凑齐 `total`。
 *
 * 逐题网格要的是"这一轮的每道题"，少一页就少画几个格子，而且页面上看不出来
 * （2026-09-21 复核时库里最多 78 条，还没超过一页，但 100 题 × 重试就会超）。
 * 翻页中途后端数据变了（Worker 又建了几条）也不会死循环：拉到空页就停。
 */
export async function fetchAllRunTaskRuns(
  runId: number,
  params: Omit<TaskRunsParams, "limit" | "offset"> = {},
): Promise<TaskRunPage> {
  const first = await fetchRunTaskRuns(runId, { ...params, limit: MAX_PAGE_LIMIT, offset: 0 });
  const items = [...first.items];
  while (items.length < first.total) {
    const page = await fetchRunTaskRuns(runId, {
      ...params,
      limit: MAX_PAGE_LIMIT,
      offset: items.length,
    });
    if (page.items.length === 0) break;
    items.push(...page.items);
  }
  return { items, total: first.total, limit: items.length, offset: 0 };
}

export function useAllRunTaskRuns(
  runId: number,
  params: Omit<TaskRunsParams, "limit" | "offset"> = {},
  options?: QueryOpts<TaskRunPage>,
) {
  return useQuery({
    queryKey: [...queryKeys.runTaskRuns(runId, params), "all"] as const,
    queryFn: () => fetchAllRunTaskRuns(runId, params),
    ...options,
  });
}

export function useTaskRun(id: number, options?: QueryOpts<TaskRunDetail>) {
  return useQuery({
    queryKey: queryKeys.taskRun(id),
    queryFn: () => apiGet<TaskRunDetail>(`/api/task-runs/${id}`),
    ...options,
  });
}

export function useTaskRunTests(
  id: number,
  params: TaskRunTestsParams = {},
  options?: QueryOpts<TestResultPage>,
) {
  return useQuery({
    queryKey: queryKeys.taskRunTests(id, params),
    queryFn: () =>
      apiGet<TestResultPage>(withQuery(`/api/task-runs/${id}/tests`, params)),
    ...options,
  });
}

/**
 * 拉一份文本制品（补丁正文 / 日志 / 轨迹）。
 *
 * 不走轮询、不重试：制品一旦生成就不再改动（sha256 是它的身份证），
 * 而「没有这份制品」（404）是正常情况 —— 一道没跑起来的题就没有轨迹。
 * 想让查询别发出去（比如那份制品不在清单里），传 `enabled: false`。
 */
export function useArtifactText(
  taskRunId: number,
  kind: ArtifactTextKind,
  options?: QueryOpts<string>,
) {
  return useQuery({
    queryKey: queryKeys.artifactText(taskRunId, kind),
    queryFn: () => apiText(`/api/task-runs/${taskRunId}/artifacts/${kind}`),
    staleTime: Infinity,
    retry: false,
    ...options,
  });
}

export function useLeaderboard(
  params: LeaderboardParams = {},
  options?: QueryOpts<LeaderboardResponse>,
) {
  return useQuery({
    queryKey: queryKeys.leaderboard(params),
    queryFn: () =>
      apiGet<LeaderboardResponse>(withQuery("/api/leaderboard", params)),
    ...options,
  });
}

export function useAnalysis(
  params: AnalysisParams = {},
  options?: QueryOpts<AnalysisResponse>,
) {
  return useQuery({
    queryKey: queryKeys.analysis(params),
    queryFn: () => apiGet<AnalysisResponse>(withQuery("/api/analysis", params)),
    ...options,
  });
}

export function useAgentConfigs(options?: QueryOpts<AgentConfigPage>) {
  return useQuery({
    queryKey: queryKeys.agentConfigs,
    queryFn: () => apiGet<AgentConfigPage>("/api/agent-configs"),
    ...options,
  });
}

export function useAgents(options?: QueryOpts<AgentPage>) {
  return useQuery({
    queryKey: queryKeys.agents,
    queryFn: () => apiGet<AgentPage>("/api/agents"),
    ...options,
  });
}

export function useBenchmarkSets(
  params: BenchmarkSetsParams = {},
  options?: QueryOpts<BenchmarkSetPage>,
) {
  return useQuery({
    queryKey: queryKeys.benchmarkSets(params),
    queryFn: () =>
      apiGet<BenchmarkSetPage>(withQuery("/api/benchmark-sets", params)),
    ...options,
  });
}

/**
 * 数据集详情（含 composition 与 publish_evidence）。slug 不存在时后端 404。
 *
 * **version 必须带上**：同一个 slug 会有好几版（benchmark-cn-v1 有 v1/v2，
 * swebench-verified-subset 有 v1/v2/v3），不带的话后端取最新已发布那版 ——
 * 列表里点 v1 打开的却是 v3（2026-09-21 复核时抓到的）。不给 version 只用于
 * "看最新"这种明确的场合。
 */
export function useBenchmarkSetDetail(
  slug: string,
  version?: string,
  options?: QueryOpts<BenchmarkSetDetail>,
) {
  return useQuery({
    queryKey: queryKeys.benchmarkSet(slug, version),
    queryFn: () =>
      apiGet<BenchmarkSetDetail>(
        withQuery(`/api/benchmark-sets/${encodeURIComponent(slug)}`, { version }),
      ),
    ...options,
  });
}

/** 题目列表。五个筛选条件（set/state/repo/difficulty/language/q）全部走后端参数。 */
export function useTasks(
  params: TasksParams = {},
  options?: QueryOpts<TaskPage>,
) {
  return useQuery({
    queryKey: queryKeys.tasks(params),
    queryFn: () => apiGet<TaskPage>(withQuery("/api/tasks", params)),
    ...options,
  });
}

/** 生成过的报告列表；不轮询——报告是命令行手动生成的，不会在页面开着的时候自己冒出来。 */
export function useReports(
  params: ReportsParams = {},
  options?: QueryOpts<ReportPage>,
) {
  return useQuery({
    queryKey: queryKeys.reports(params),
    queryFn: () => apiGet<ReportPage>(withQuery("/api/reports", params)),
    ...options,
  });
}

/** 单题详情。题号里有 `__` 与 `-`，都是 URL 安全字符，encodeURIComponent 是双保险。 */
export function useTaskDetail(
  taskId: string,
  options?: QueryOpts<TaskDetail>,
) {
  return useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () =>
      apiGet<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`),
    ...options,
  });
}

// —— 写操作 ——
//
// 管理员令牌由调用方在 `mutate()` 时传入（来自 `lib/admin-token.ts`），
// 不在这里读：hook 不知道令牌从哪来，也不该知道。
//
// 失效范围刻意比"改了什么"宽一档：取消和重试都会连着动 **实验详情**（状态、计数）
// 和 **它的逐题记录**，重试还会改**列表里的进度**。挨个算清楚哪些 key 受影响，
// 将来后端多改一个字段就会漏；按前缀失效多查一次接口，换来的是不会看到过期数字。
// `["run", id]` 是 `${id}` 与其下 `task-runs` 的公共前缀，一条就够。

function useInvalidateRun() {
  const queryClient = useQueryClient();
  return (runId: number) => {
    void queryClient.invalidateQueries({ queryKey: ["run", runId] });
    void queryClient.invalidateQueries({ queryKey: ["runs"] });
  };
}

/** 取消一次实验。**不可撤销**，界面那边要先过一道确认。`mutate(token)`。 */
export function useCancelRun(runId: number) {
  const invalidate = useInvalidateRun();
  return useMutation({
    mutationFn: (token: string) =>
      apiPost<CancelResponse>(`/api/runs/${runId}/cancel`, undefined, token),
    onSuccess: () => invalidate(runId),
  });
}

/** 把失败的题重新投进队列。没到重试上限的才会被重投。`mutate(token)`。 */
export function useRetryFailed(runId: number) {
  const invalidate = useInvalidateRun();
  return useMutation({
    mutationFn: (token: string) =>
      apiPost<RetryResponse>(`/api/runs/${runId}/retry-failed`, undefined, token),
    onSuccess: () => invalidate(runId),
  });
}

/**
 * 新建实验。
 *
 * 走 `POST /api/runs`，它内部是 `create_runs()` —— 协议 C-27（工作区不干净拒绝
 * 启动）的唯一强制点。工作区脏的时候后端返回 409 `WORKSPACE_DIRTY`，
 * 前端照原样显示那条消息：这不是前端该替用户判断的事。
 */
export function useCreateRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ body, token }: { body: CreateRunRequest; token: string }) =>
      apiPost<CreateRunResponse>("/api/runs", body, token),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runs"] }),
  });
}
