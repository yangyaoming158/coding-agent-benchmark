"use client";

import Link from "next/link";
import { ProgressBar, StatusBadge } from "@/components/run-status";
import { Stat } from "@/components/stat";
import { errorMessage } from "@/lib/api";
import { datasetSummary, liveRuns, participantSummary, progressPercent } from "@/lib/dashboard";
import { formatRate, formatTime, isLiveRun, runStatusTone } from "@/lib/display";
import { useAgentConfigs, useBenchmarkSets, useRuns, type RunSummary } from "@/lib/queries";

/** §16.1 给 Dashboard 定的轮询档：10 秒，和 /runs 列表同级。 */
const POLL_MS = 10_000;
/** 首页只放最近这几次，看全部去 /runs。 */
const RECENT = 5;
/** 后端单页上限（`app/api/deps.py` 的 MAX_LIMIT）。数据集版本和参赛配置远不到这个数，一页就够。 */
const MAX_PAGE_LIMIT = 200;

/**
 * 首页的总览（E7-T8）。
 *
 * 四个数（几版数据集、几个参赛者、跑了多少次实验、几个在跑）、正在跑的进度、
 * 最近 5 次实验。**不新开后端接口**：数据集数从 `/api/benchmark-sets` 数，参赛者从
 * `/api/agent-configs` 数（哨兵不算，口径在 `lib/dashboard.ts`），实验次数就是
 * `/api/runs` 的 `total`。
 *
 * 轮询和 /runs 一个规矩：**只在有活着的实验时开**（10 秒）。全部跑完的首页每 10 秒
 * 打一次后端，打回来的每个字节都和上次一样。数据集和参赛者两个数不轮询 —— 它们
 * 不会在你看着首页的时候变。
 */
export function Dashboard() {
  const sets = useBenchmarkSets({ limit: MAX_PAGE_LIMIT });
  const configs = useAgentConfigs();
  const recent = useRuns(
    { limit: RECENT },
    {
      refetchInterval: (query) =>
        query.state.data?.items.some((run) => isLiveRun(run.status)) ? POLL_MS : false,
    },
  );
  // 正在跑的可能不在"最近 5 次"里（比如一口气建了 6 个），所以单独按状态取
  const running = useRuns(
    { status: "RUNNING", limit: MAX_PAGE_LIMIT },
    { refetchInterval: (query) => (query.state.data?.items.length ? POLL_MS : false) },
  );
  const queued = useRuns(
    { status: "QUEUED", limit: MAX_PAGE_LIMIT },
    { refetchInterval: (query) => (query.state.data?.items.length ? POLL_MS : false) },
  );

  const datasets = sets.data ? datasetSummary(sets.data.items) : null;
  const participants = configs.data ? participantSummary(configs.data.items) : null;
  const live = liveRuns([...(running.data?.items ?? []), ...(queued.data?.items ?? [])]);

  const firstError = [sets, configs, recent, running, queued].find((q) => q.error)?.error;

  return (
    <>
      {firstError !== undefined && (
        <div className="mt-6 rounded-md border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm text-red-700">取不到总览数据。</p>
          <p className="mt-1 text-xs leading-relaxed text-red-600">{errorMessage(firstError)}</p>
        </div>
      )}

      {/* ── 四个数 ── */}
      <section className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="数据集"
          value={
            datasets === null ? "…" : (
              <Link href="/benchmarks" className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900">
                {datasets.datasets} 个 · {datasets.versions} 版
              </Link>
            )
          }
          hint={
            datasets === null
              ? undefined
              : `已发布；最新版合计 ${datasets.latestTasks} 题`
          }
        />
        <Stat
          label="参赛 Agent"
          value={
            participants === null ? "…" : (
              <Link href="/agents" className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900">
                {participants.agents} 个
              </Link>
            )
          }
          hint={
            participants === null
              ? undefined
              : `${participants.configs} 条启用配置 · ${participants.names.join(" / ")}`
          }
        />
        <Stat
          label="实验运行"
          value={
            recent.data === undefined ? "…" : (
              <Link href="/runs" className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900">
                {recent.data.total} 次
              </Link>
            )
          }
          hint="含哨兵、门禁和被排除的实验；排行榜只收准入的那些"
        />
        <Stat
          label="正在跑"
          value={running.data === undefined ? "…" : `${live.running.length} 个`}
          hint={
            queued.data === undefined
              ? undefined
              : live.queued.length > 0
                ? `另有 ${live.queued.length} 个排队中`
                : "没有排队的"
          }
        />
      </section>

      {/* ── 正在跑：有才显示 ── */}
      {live.running.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-semibold text-neutral-900">正在跑</h2>
          <p className="mt-1 text-xs text-neutral-500">
            进度数的是已经定出结论的题（协议 C-24），不是开跑的题；每 10 秒刷一次。
          </p>
          <ul className="mt-3 space-y-2">
            {live.running.map((run) => (
              <li
                key={run.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-neutral-200 bg-white px-4 py-3"
              >
                <div className="min-w-64 flex-1">
                  <Link
                    href={`/runs/${run.id}`}
                    className="text-sm font-medium text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                  >
                    {run.name}
                  </Link>
                  <p className="mt-0.5 font-mono text-xs text-neutral-400">
                    #{run.id} · {run.benchmark_set} · {run.agent_config_label}
                  </p>
                </div>
                <StatusBadge status={run.status} />
                <ProgressBar
                  completed={run.completed_tasks}
                  total={run.total_tasks}
                  tone={runStatusTone(run.status)}
                />
                <span className="font-mono text-xs text-neutral-500">{progressPercent(run)}%</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ── 最近 5 次 ── */}
      <section className="mt-8">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-neutral-900">最近 {RECENT} 次实验</h2>
          <Link
            href="/runs"
            className="text-xs text-neutral-500 underline decoration-neutral-300 underline-offset-2 hover:text-neutral-900"
          >
            全部实验 →
          </Link>
        </div>
        <div className="mt-3">
          {recent.isLoading && <p className="text-sm text-neutral-500">正在加载……</p>}
          {recent.data && recent.data.items.length === 0 && (
            <div className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-center">
              <p className="text-sm text-neutral-600">还没有跑过任何实验。</p>
              <p className="mt-1 text-xs text-neutral-500">
                去 <Link href="/runs" className="underline">实验运行</Link> 页点「新建实验」，或者跑{" "}
                <code className="font-mono">make enqueue AGENT=oracle</code>。
              </p>
            </div>
          )}
          {recent.data && recent.data.items.length > 0 && (
            <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
              <table className="w-full">
                <thead>
                  <tr className="whitespace-nowrap border-b border-neutral-200 bg-neutral-50 text-left">
                    <th className="px-4 py-2 text-xs font-medium text-neutral-600">实验 · 数据集 · 参赛者</th>
                    <th className="px-4 py-2 text-xs font-medium text-neutral-600">状态</th>
                    <th className="px-4 py-2 text-xs font-medium text-neutral-600">进度</th>
                    <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">解决率</th>
                    <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data.items.map((run) => (
                    <RecentRow key={run.id} run={run} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </>
  );
}

function RecentRow({ run }: { run: RunSummary }) {
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
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-500">
        {formatTime(run.created_at)}
      </td>
    </tr>
  );
}
