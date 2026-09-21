"use client";

import { useState } from "react";
import Link from "next/link";
import { CreateRunPanel } from "@/components/create-run-panel";
import { ProgressBar, StatusBadge } from "@/components/run-status";
import { errorMessage } from "@/lib/api";
import {
  formatCost,
  formatDuration,
  formatRate,
  formatTime,
  isLiveRun,
  runStatusLabel,
  runStatusTone,
  type RunStatus,
} from "@/lib/display";
import { useRuns, type RunSummary } from "@/lib/queries";

const PAGE_SIZE = 20;

const STATUSES: RunStatus[] = [
  "DRAFT",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "PARTIAL",
  "FAILED",
  "CANCELLED",
];

function RunRow({ run }: { run: RunSummary }) {
  return (
    <tr className="border-b border-neutral-200 last:border-b-0 hover:bg-neutral-50">
      <td className="min-w-72 px-4 py-3">
        <Link
          href={`/runs/${run.id}`}
          className="text-sm font-medium text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
        >
          {run.name}
        </Link>
        <p className="mt-0.5 font-mono text-xs text-neutral-400">
          #{run.id} · {run.benchmark_set} · {run.agent_config_label}
          {run.dirty && (
            <span
              className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 font-sans text-amber-800"
              title="跑的时候工作区有未提交改动（协议 C-28），不得进排行榜"
            >
              dirty
            </span>
          )}
          {run.leaderboard_excluded_reason !== null && (
            <span
              className="ml-2 rounded bg-neutral-100 px-1.5 py-0.5 font-sans text-neutral-600"
              title={run.leaderboard_excluded_reason}
            >
              已排除
            </span>
          )}
        </p>
      </td>
      <td className="whitespace-nowrap px-4 py-3">
        <StatusBadge status={run.status} />
      </td>
      <td className="whitespace-nowrap px-4 py-3">
        <ProgressBar
          completed={run.completed_tasks}
          total={run.total_tasks}
          tone={runStatusTone(run.status)}
        />
      </td>
      <td
        className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-600"
        title="严格解决率：已解决的题数 / 题库里的全部题数（协议 C-21）"
      >
        {formatRate(run.strict_resolve_rate)}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-600">
        {formatCost(run.total_cost_usd)}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-600">
        {formatDuration(run.makespan_ms)}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-500">
        {formatTime(run.created_at)}
      </td>
    </tr>
  );
}

/**
 * 实验运行列表。
 *
 * 轮询只在**列表里还有活着的实验**时开（10s，§16.1 给 Dashboard 定的档，
 * 列表和它同级）。全部跑完就停 —— 一个五种状态都是"已完成"的页面，
 * 每 10 秒打一次后端，打回来的每个字节都和上次一样。
 */
export default function RunsPage() {
  const [status, setStatus] = useState<RunStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [showCreate, setShowCreate] = useState(false);

  const { data, error, isLoading, isFetching } = useRuns(
    { status: status === "" ? undefined : status, limit: PAGE_SIZE, offset },
    {
      refetchInterval: (query) =>
        query.state.data?.items.some((run) => isLiveRun(run.status))
          ? 10_000
          : false,
    },
  );

  const total = data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="flex items-start justify-between border-b border-neutral-200 pb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">实验运行</h1>
          <p className="mt-2 text-sm leading-relaxed text-neutral-600">
            把一批题交给某个 Agent 跑一轮，就是一次实验运行。
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((open) => !open)}
          className="shrink-0 rounded-md bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-neutral-700"
        >
          {showCreate ? "收起新建" : "新建实验"}
        </button>
      </header>

      {showCreate && <CreateRunPanel onClose={() => setShowCreate(false)} />}

      <div className="mt-6 flex items-center gap-3">
        <label className="text-xs text-neutral-600">
          状态
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as RunStatus | "");
              setOffset(0);
            }}
            className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
          >
            <option value="">全部</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {runStatusLabel(s)}
              </option>
            ))}
          </select>
        </label>
        {data && (
          <span className="text-xs text-neutral-500">
            共 {total} 次{isFetching && " · 刷新中"}
          </span>
        )}
      </div>

      <section className="mt-4">
        {isLoading && <p className="text-sm text-neutral-500">正在加载……</p>}

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">取不到实验列表。</p>
            <p className="mt-1 text-xs leading-relaxed text-red-600">
              {errorMessage(error)}
            </p>
          </div>
        )}

        {data && data.items.length === 0 && (
          <div className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-center">
            <p className="text-sm text-neutral-600">
              {status === "" ? "还没有跑过任何实验。" : "这个状态下没有实验。"}
            </p>
            {status === "" && (
              <p className="mt-1 text-xs text-neutral-500">
                点右上角「新建实验」，或者跑{" "}
                <code className="font-mono">make enqueue AGENT=oracle</code>。
              </p>
            )}
          </div>
        )}

        {/* 短列一律不折行，窄屏时表格横向滚动而不是把"已完成"挤成三行竖排 */}
        {data && data.items.length > 0 && (
          <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
            <table className="w-full">
              <thead>
                <tr className="whitespace-nowrap border-b border-neutral-200 bg-neutral-50 text-left">
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    实验 · 数据集 · 参赛者
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    状态
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    进度
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    解决率
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    成本
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    耗时
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    创建时间
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((run) => (
                  <RunRow key={run.id} run={run} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {(hasPrev || hasNext) && (
          <div className="mt-3 flex items-center justify-between">
            <button
              type="button"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={!hasPrev}
              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:border-neutral-200 disabled:text-neutral-400"
            >
              上一页
            </button>
            <span className="font-mono text-xs text-neutral-500">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} / {total}
            </span>
            <button
              type="button"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!hasNext}
              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:border-neutral-200 disabled:text-neutral-400"
            >
              下一页
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
