"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQueries } from "@tanstack/react-query";
import { ToneBadge } from "@/components/run-status";
import { errorMessage } from "@/lib/api";
import { cellVerdict, formatCost, formatDuration } from "@/lib/display";
import {
  fetchAllRunTaskRuns,
  queryKeys,
  useRuns,
  type TaskRunsParams,
  type TaskRunSummary,
} from "@/lib/queries";
import { HISTORY_RUN_LIMIT, historyRows } from "@/lib/tasks";

/**
 * 各 Agent 在这道题上的历史表现。
 *
 * 后端没有"按题查跨实验"的端点（§14.4 的清单里没有），所以在前端聚合：
 * 拉最近 HISTORY_RUN_LIMIT 次实验 + 每次实验的 canonical 逐题记录，滤出这道题。
 *
 * **惰性**：折叠着的时候一个请求都不发。这道题页面的主角是 issue 和用例，
 * 历史是补充材料，不该为它付出十几个请求的首屏代价。
 */
const HISTORY_TASK_RUN_PARAMS: Omit<TaskRunsParams, "limit" | "offset"> = {
  canonical_only: true,
};

export function TaskHistory({ taskId }: { taskId: string }) {
  const [open, setOpen] = useState(false);
  const runs = useRuns({ limit: HISTORY_RUN_LIMIT }, { enabled: open });
  const runItems = useMemo(() => runs.data?.items ?? [], [runs.data]);

  // canonical 记录每次实验每题最多一条，按后端单页上限翻页拉全。
  // 窗口大小由 HISTORY_RUN_LIMIT 封顶，更大的回溯要靠后端补按题查的端点。
  const taskRuns = useQueries({
    queries: runItems.map((run) => ({
      queryKey: [...queryKeys.runTaskRuns(run.id, HISTORY_TASK_RUN_PARAMS), "all"] as const,
      queryFn: () => fetchAllRunTaskRuns(run.id, HISTORY_TASK_RUN_PARAMS),
      enabled: open,
    })),
  });

  const { rows, failedCount } = useMemo(() => {
    const byRun = new Map<number, TaskRunSummary[]>();
    let failed = 0;
    runItems.forEach((run, index) => {
      const result = taskRuns[index];
      if (result?.data) byRun.set(run.id, result.data.items);
      else if (result?.isError) failed += 1;
    });
    return { rows: historyRows(runItems, byRun, taskId), failedCount: failed };
  }, [runItems, taskRuns, taskId]);

  const loading =
    runs.isLoading || (open && taskRuns.some((result) => result.isLoading));
  const ready = open && runs.isSuccess && !loading;

  return (
    <details
      className="rounded-md border border-neutral-200 bg-white px-4 py-3"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm text-neutral-600">
        各 Agent 在这道题上的历史表现（最多回溯 {HISTORY_RUN_LIMIT} 次实验）
      </summary>

      <div className="mt-3">
        {loading && (
          <p className="text-sm text-neutral-500">
            {runs.isLoading
              ? "正在读取实验列表……"
              : `正在读取 ${runItems.length} 次实验的逐题记录……`}
          </p>
        )}

        {open && runs.error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">取不到实验列表。</p>
            <p className="mt-1 text-xs leading-relaxed text-red-600">
              {errorMessage(runs.error)}
            </p>
          </div>
        )}

        {ready && runItems.length === 0 && (
          <p className="text-sm text-neutral-600">库里还没有跑过任何实验。</p>
        )}

        {ready && runItems.length > 0 && rows.length === 0 && (
          <p className="text-sm text-neutral-600">没有实验跑到过这道题。</p>
        )}

        {ready && rows.length > 0 && (
          <>
            {failedCount > 0 && (
              <p className="mb-2 text-xs leading-relaxed text-amber-600">
                有 {failedCount} 次实验的逐题记录没取到，下面的行可能不全。
              </p>
            )}
            <div className="overflow-x-auto rounded-md border border-neutral-200">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-neutral-200 bg-neutral-50 text-left">
                    <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                      参赛者
                    </th>
                    <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                      实验
                    </th>
                    <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                      判定
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                      F2P
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                      P2P
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                      成本
                    </th>
                    <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                      耗时
                    </th>
                    <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                      证据
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const verdict = cellVerdict({
                      lifecycle_status: row.lifecycleStatus,
                      infra_outcome: row.infraOutcome,
                      agent_outcome: row.agentOutcome,
                      error_code: row.errorCode,
                    });
                    return (
                      <tr
                        key={row.taskRunId}
                        className="border-b border-neutral-100 last:border-b-0"
                      >
                        <td className="px-3 py-2 font-mono text-xs text-neutral-600">
                          {row.agentLabel}
                        </td>
                        <td className="px-3 py-2 text-xs">
                          <Link
                            href={`/runs/${row.runId}`}
                            className="text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                          >
                            {row.runName}
                          </Link>
                          <span className="ml-1 font-mono text-neutral-400">
                            #{row.runId}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          <ToneBadge tone={verdict.tone}>{verdict.label}</ToneBadge>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-neutral-600">
                          {row.f2pPassed === null || row.f2pTotal === null
                            ? "—"
                            : `${row.f2pPassed} / ${row.f2pTotal}`}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-neutral-600">
                          {row.p2pPassed === null || row.p2pTotal === null
                            ? "—"
                            : `${row.p2pPassed} / ${row.p2pTotal}`}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-neutral-600">
                          {formatCost(row.costUsd)}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-neutral-600">
                          {formatDuration(row.totalDurationMs)}
                        </td>
                        <td className="px-3 py-2 text-xs">
                          <Link
                            href={`/task-runs/${row.taskRunId}`}
                            className="font-mono text-neutral-500 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                          >
                            #{row.taskRunId}
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </details>
  );
}
