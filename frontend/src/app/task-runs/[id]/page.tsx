"use client";

import { use } from "react";
import Link from "next/link";
import { AttributionPanel } from "@/components/attribution-panel";
import { IssueTitle } from "@/components/issue-title";
import { DiffViewer } from "@/components/diff-viewer";
import { LogViewer } from "@/components/log-viewer";
import { ToneBadge } from "@/components/run-status";
import { Stat } from "@/components/stat";
import { TestResultTable } from "@/components/test-result-table";
import { TrajectoryTimeline } from "@/components/trajectory-timeline";
import { API_BASE, errorMessage } from "@/lib/api";
import {
  agentOutcomeLabel,
  cellVerdict,
  costSourceLabel,
  formatBytes,
  formatCost,
  formatDuration,
  formatTime,
  formatTokens,
  infraLabel,
  isLiveTaskRun,
  lifecycleLabel,
  passRatioTone,
} from "@/lib/display";
import { useRun, useTaskRun } from "@/lib/queries";

/** 和 Run Detail 同一个档位：还在动就 3 秒看一眼。 */
const POLL_MS = 3000;

/**
 * 单题执行详情 —— 整个平台证据链的最后一环。
 *
 * §16.3 的硬要求：从任意页面出发，3 次点击内必须能到达「某个 Agent 在某道题上
 * 为什么失败」的完整证据。这一页就是那个终点，五个区块各管一段证据：
 *
 * 1. **判定**（页头 + 统计条）：谁的问题、修好没有（三个字段原样透出，C-04/05/06）
 * 2. **补丁**：它到底改了什么（两份补丁可对比，C-08b）
 * 3. **用例**：判定的原始依据，逐条 F2P / P2P
 * 4. **日志**：过程
 * 5. **轨迹**：它是怎么想的（工具调用序列）
 * 6. **归因**：没修好是哪一类、凭什么（E6，随详情一起返回；盲检期间后端藏起来）
 */
export default function TaskRunDetailPage(props: PageProps<"/task-runs/[id]">) {
  const { id } = use(props.params);
  const taskRunId = Number(id);

  const taskRun = useTaskRun(taskRunId, {
    refetchInterval: (query) =>
      query.state.data && isLiveTaskRun(query.state.data.lifecycle_status)
        ? POLL_MS
        : false,
  });

  // 所属实验：拿实验名和参赛者配置标签放进副标题。单题接口本身不带这两样，
  // 而"这是哪个 Agent 哪份配置跑的"是看单题证据时第一个要知道的事。
  const run = useRun(taskRun.data?.evaluation_run_id ?? 0, {
    enabled: taskRun.data !== undefined,
  });

  if (taskRun.isLoading) {
    return <p className="text-sm text-neutral-500">正在加载……</p>;
  }

  if (taskRun.error) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm text-red-700">{errorMessage(taskRun.error)}</p>
          <Link
            href="/runs"
            className="mt-2 inline-block text-xs text-red-700 underline underline-offset-2"
          >
            回到实验列表
          </Link>
        </div>
      </div>
    );
  }

  if (!taskRun.data) return null;
  const detail = taskRun.data;
  const live = isLiveTaskRun(detail.lifecycle_status);
  const verdict = cellVerdict(detail);

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="border-b border-neutral-200 pb-6">
        <Link
          href={`/runs/${detail.evaluation_run_id}`}
          className="text-xs text-neutral-500 transition-colors hover:text-neutral-900"
        >
          ← 实验运行 #{detail.evaluation_run_id}
          {run.data !== undefined && ` · ${run.data.name}`}
        </Link>

        <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight">
                <IssueTitle title={detail.issue_title} />
              </h1>
              <ToneBadge tone={verdict.tone}>{verdict.label}</ToneBadge>
            </div>
            <p className="mt-1 font-mono text-xs text-neutral-400">
              #{detail.id} · {detail.task_id}
              {run.data !== undefined && ` · ${run.data.agent_config_label}`}
              {" "}· 第 {detail.attempt_no} 次尝试
              {detail.is_canonical && " · 统计依据（canonical）"}
              {detail.retry_of_id !== null && ` · 重试自 #${detail.retry_of_id}`}
            </p>
            {/* 三个互相独立的字段原样透出（C-04/05/06）：上面的徽章是它们合起来的结论，
                这一行是原始值 —— "平台有没有做完"和"AI 有没有修好"分开记是协议最核心的一条 */}
            <p className="mt-1 text-xs text-neutral-500">
              <span title="lifecycle_status：这次执行走到哪一步了">流程 {lifecycleLabel(detail.lifecycle_status)}</span>
              {" · "}
              <span title="infra_outcome：平台有没有正确完成这次评测">
                平台 {detail.infra_outcome === null ? "—" : infraLabel(detail.infra_outcome)}
              </span>
              {" · "}
              <span title="agent_outcome：被测 AI 有没有把 bug 修好">
                AI {detail.agent_outcome === null ? "—" : agentOutcomeLabel(detail.agent_outcome)}
              </span>
            </p>
          </div>
        </div>
      </header>

      {/* ── 诊断：有话说才显示 ── */}
      <div className="mt-4 space-y-2">
        {detail.protected_path_edit_attempted === true && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-xs leading-relaxed text-red-800">
              这次改动碰了<strong>受保护路径</strong>（测试文件、配置等），那部分改动已在标准化时丢弃。按协议
              C-13d 这种情况要人工复核。
            </p>
          </div>
        )}

        {detail.agent_outcome === "EMPTY_PATCH" && (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3">
            <p className="text-xs leading-relaxed text-amber-800">
              {detail.raw_patch_empty === true ? (
                <>
                  交上来的补丁<strong>原样就是空的</strong> ——
                  没有产生任何文件改动。
                </>
              ) : (
                <>
                  交上来的补丁非空，但<strong>标准化之后变空了</strong> ——
                  改动集中在受保护路径上，全被丢掉了（协议 C-08b）。
                  这两者的区别是证据，不是同一种“没干活”。
                </>
              )}
            </p>
          </div>
        )}

        {detail.error_code !== null && (
          <div className="rounded-md border border-neutral-200 bg-neutral-50 px-4 py-3">
            <p className="font-mono text-xs text-neutral-700">
              {detail.error_code}
            </p>
            {detail.error_message_excerpt !== null && (
              <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-xs text-neutral-500">
                {detail.error_message_excerpt}
              </pre>
            )}
          </div>
        )}
      </div>

      {/* ── 判定与规模 ── */}
      <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {/* 全过绿、有挂红：判定来自后端，颜色只是让"修好一个、弄坏四个"一眼看出来 */}
        <Stat
          label="F2P 通过"
          value={
            <PassRatio passed={detail.f2p_passed} total={detail.f2p_total} />
          }
          hint="修复前失败的用例，修好后必须全过"
        />
        <Stat
          label="P2P 通过"
          value={
            <PassRatio passed={detail.p2p_passed} total={detail.p2p_total} />
          }
          hint="改动不许弄坏的用例"
        />
        <Stat
          label="成本"
          value={formatCost(detail.cost_usd)}
          hint={
            detail.cost_source !== null
              ? costSourceLabel(detail.cost_source)
              : undefined
          }
          hintTone={
            detail.cost_source === "unavailable" ? "warn" : undefined
          }
        />
        <Stat
          label="Token"
          value={formatTokens(detail.tokens_total)}
          hint={
            detail.tokens_total !== null
              ? `输入 ${formatTokens(detail.tokens_input)} · 缓存 ${formatTokens(
                  detail.tokens_cache_read,
                )} · 输出 ${formatTokens(detail.tokens_output)}`
              : undefined
          }
        />
        <Stat label="轮数" value={detail.turns ?? "—"} />
        <Stat
          label="Agent 耗时"
          value={formatDuration(detail.agent_duration_ms)}
          hint={
            detail.test_duration_ms !== null
              ? `测试 ${formatDuration(detail.test_duration_ms)}`
              : undefined
          }
        />
        <Stat label="总耗时" value={formatDuration(detail.total_duration_ms)} />
        <Stat
          label="补丁规模"
          value={
            detail.files_changed !== null
              ? `${detail.files_changed} 个文件`
              : "—"
          }
          hint={
            detail.lines_added !== null
              ? `+${detail.lines_added} −${detail.lines_deleted ?? 0}`
              : undefined
          }
        />
      </section>

      {/* ── 过程时间轴 ── */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">过程时间轴</h2>
        <p className="mt-1 text-xs text-neutral-500">
          「未走到」说明执行停在了更早的阶段 —— 比如 Agent 启动那一格为空，
          就是协议 C-77 说的“没给 AI 机会”，和“给了机会但没拿到结论”是两回事。
        </p>
        <ol className="mt-3 grid gap-2 sm:grid-cols-4">
          {STAGES.map((stage) => {
            const value = detail[stage.key];
            return (
              <li
                key={stage.key}
                className="rounded-md border border-neutral-200 bg-white px-3 py-2"
              >
                <p className="text-xs text-neutral-500">{stage.label}</p>
                <p
                  className={`mt-0.5 font-mono text-xs ${
                    value === null ? "text-neutral-300" : "text-neutral-700"
                  }`}
                >
                  {value === null ? "未走到" : formatTime(value)}
                </p>
              </li>
            );
          })}
        </ol>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">补丁</h2>
        <div className="mt-3">
          <DiffViewer taskRunId={taskRunId} patches={detail.patches} />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">逐条用例</h2>
        <div className="mt-3">
          <TestResultTable
            taskRunId={taskRunId}
            live={live}
            // 有用例挂了就默认只列失败的：那几条是判定的依据，不该埋在一千多条通过里
            defaultStatus={
              passRatioTone(detail.f2p_passed, detail.f2p_total) === "bad" ||
              passRatioTone(detail.p2p_passed, detail.p2p_total) === "bad"
                ? "FAILED"
                : ""
            }
          />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">日志</h2>
        <div className="mt-3">
          <LogViewer taskRunId={taskRunId} artifacts={detail.artifacts} />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">Agent 轨迹</h2>
        <div className="mt-3">
          <TrajectoryTimeline taskRunId={taskRunId} artifacts={detail.artifacts} />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">失败归因</h2>
        <p className="mt-1 text-xs text-neutral-500">
          「为什么没修好」的分类。它只解释原因，不改上面的判定（协议 C-40）。
        </p>
        <div className="mt-3">
          <AttributionPanel detail={detail} />
        </div>
      </section>

      {/* ── 制品清单：审计用 ── */}
      <section className="mt-8">
        <details className="rounded-md border border-neutral-200 bg-white">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-neutral-700">
            全部制品（{detail.artifacts.length}）
          </summary>
          <div className="border-t border-neutral-200">
            {detail.worker_id !== null && (
              <p className="px-4 py-2 font-mono text-xs text-neutral-400">
                worker {detail.worker_id}
              </p>
            )}
            {detail.artifacts.length === 0 ? (
              <p className="px-4 py-3 text-xs text-neutral-500">
                这次执行没有产出任何制品。
              </p>
            ) : (
              <ul className="divide-y divide-neutral-100">
                {detail.artifacts.map((artifact) => (
                  <li
                    key={artifact.kind}
                    className="flex flex-wrap items-center gap-3 px-4 py-2 text-xs"
                  >
                    <a
                      href={`${API_BASE}/api/task-runs/${taskRunId}/artifacts/${artifact.kind}`}
                      className="font-mono text-neutral-800 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-800"
                    >
                      {artifact.kind}
                    </a>
                    <span className="font-mono text-neutral-400">
                      {formatBytes(artifact.size_bytes)}
                    </span>
                    <span className="font-mono text-neutral-400">
                      {artifact.content_type}
                    </span>
                    <span
                      className="ml-auto font-mono text-neutral-400"
                      title={artifact.sha256}
                    >
                      {artifact.sha256.slice(0, 12)}…
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </details>
      </section>
    </div>
  );
}

/** "5 / 5"、"1311 / 1315" 这种通过比，全过绿、有挂红（`passRatioTone`）。 */
function PassRatio({ passed, total }: { passed: number | null; total: number | null }) {
  if (total === null) return <>—</>;
  const tone = passRatioTone(passed, total);
  const color =
    tone === "ok" ? "text-emerald-700" : tone === "bad" ? "text-red-700" : "text-neutral-900";
  return (
    <span className={color}>
      {passed ?? 0} / {total}
    </span>
  );
}

/**
 * 时间轴的八个阶段。顺序就是协议里一次执行的时间顺序（C-24）。
 * `agent_started_at` 那一格在 C-77 里是关键判据，见上面的说明。
 */
const STAGES = [
  { key: "queued_at", label: "排队" },
  { key: "prepare_started_at", label: "准备" },
  { key: "agent_started_at", label: "Agent 启动" },
  { key: "agent_finished_at", label: "Agent 结束" },
  { key: "test_started_at", label: "测试开始" },
  { key: "test_finished_at", label: "测试结束" },
  { key: "judged_at", label: "判定" },
  { key: "completed_at", label: "结束" },
] as const;
