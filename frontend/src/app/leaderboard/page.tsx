"use client";

/**
 * 排行榜。
 *
 * 一个数据集版本一张榜（不同版本的解决率之间没有可比性）；行按
 * (参赛者, 协议版本) 分组（C-59）。排序一律用后端的 rank —— 前端重排会把
 * "成本报不出的垫底"这类规则丢掉，那是数字变错而不是变丑。
 *
 * 页面不轮询：准入门槛要求实验 COMPLETED，跑动中的实验本来就不上榜，
 * 数据只在"某次实验跑完"时变化，刷新页面即可。
 */

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData } from "@tanstack/react-query";
import { EligibilityNote } from "@/components/eligibility-note";
import { FacetMatrix } from "@/components/facet-matrix";
import { ScatterPlot } from "@/components/scatter-plot";
import { ApiError, errorMessage } from "@/lib/api";
import { formatCost, formatDuration, formatRate, formatTokens } from "@/lib/display";
import {
  FACET_LABELS,
  METRIC_LABELS,
  costNote,
  infraFailureRate,
  scatterPoints,
  spreadRange,
  type LeaderboardFacet,
  type LeaderboardMetric,
  type LeaderboardRow,
} from "@/lib/leaderboard";
import { useBenchmarkSets, useLeaderboard } from "@/lib/queries";

const METRICS: LeaderboardMetric[] = ["resolve_rate", "cost", "duration", "tokens"];
const FACETS: LeaderboardFacet[] = ["difficulty", "language", "repository"];
const PAGE_LIMIT = 50;

/**
 * 默认导出只做一件事：把读 `?set=&version=` 的组件包进 Suspense。
 *
 * `?set=<slug>&version=<v>` 既是 Benchmarks 页拉过来的深链，也是页面顶部
 * 数据集下拉框写回去的地址 —— 切换走 URL，刷新、分享链接都保得住选择。
 * useSearchParams 必须包在 Suspense 里：next build 的预渲染遇到没有边界的
 * useSearchParams 会直接失败（Next 16 的报错原文是 "useSearchParams() should
 * be wrapped in a suspense boundary"）。
 */
export default function Page() {
  return (
    <Suspense fallback={<p className="text-sm text-neutral-500">正在加载……</p>}>
      <LeaderboardPage />
    </Suspense>
  );
}

function LeaderboardPage() {
  const [metric, setMetric] = useState<LeaderboardMetric>("resolve_rate");
  const [facet, setFacet] = useState<LeaderboardFacet>("difficulty");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // 不给 ?set= 就交给后端取默认那版（"有实验跑过的最新 slug"）
  const set = searchParams.get("set") ?? undefined;
  const version = searchParams.get("version") ?? undefined;
  // placeholderData：切换指标/分面时先留住上一批数据（整块正文连同切换按钮不卸载，
  // 键盘焦点不丢、加载期间还能点第二个按钮），新数据到了再换。
  const { data, isPending, error, isFetching } = useLeaderboard(
    { set, version, metric, facet, limit: PAGE_LIMIT },
    { placeholderData: keepPreviousData },
  );
  // 数据集下拉框的选项。选中项以响应里的 benchmark_set（"slug@version"）为准，
  // 而不是 URL：URL 没给时后端选了哪版，下拉框就显示哪版。
  const sets = useBenchmarkSets({ limit: 50 });
  const selected = data?.benchmark_set ?? "";

  function pickSet(value: string) {
    const at = value.lastIndexOf("@");
    if (at === -1) return;
    const params = new URLSearchParams();
    params.set("set", value.slice(0, at));
    params.set("version", value.slice(at + 1));
    router.replace(`${pathname}?${params.toString()}`);
  }
  // 404 = 这版数据集上还没有跑完的实验（空库的正常情况）；其他错误才是真故障。
  const notFound = error !== null && error instanceof ApiError && error.status === 404;

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="border-b border-neutral-200 pb-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">排行榜</h1>
            <p className="mt-2 text-sm leading-relaxed text-neutral-600">
              只有同一个数据集版本的结果之间可以比较，不同协议版本的行也分开排（C-59）。
            </p>
          </div>
          <label className="text-xs text-neutral-600">
            数据集
            <select
              value={selected}
              onChange={(e) => pickSet(e.target.value)}
              disabled={sets.data === undefined}
              className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 font-mono text-sm text-neutral-900 disabled:text-neutral-400"
            >
              {selected === "" && <option value="">—</option>}
              {sets.data?.items.map((item) => {
                const value = `${item.slug}@${item.version}`;
                return (
                  <option key={item.id} value={value}>
                    {value} · {item.task_count} 题
                    {item.status !== "PUBLISHED" ? ` · ${item.status}` : ""}
                  </option>
                );
              })}
              {/* 后端选出的那版不在前 50 条里时也要能显示出来 */}
              {selected !== "" &&
                sets.data !== undefined &&
                !sets.data.items.some((item) => `${item.slug}@${item.version}` === selected) && (
                  <option value={selected}>{selected}</option>
                )}
            </select>
          </label>
        </div>
      </header>

      {isPending && <p className="mt-8 text-sm text-neutral-500">正在加载……</p>}

      {error && !isPending && (
        <div
          className={
            notFound
              ? "mt-8 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
              : "mt-8 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          }
        >
          <p>排行榜暂时取不到数据：{errorMessage(error)}</p>
          {notFound && (
            <p className="mt-1 text-xs">
              如果库里还没有跑完的实验，这是正常的 —— 先到{" "}
              <Link href="/runs" className="underline">
                实验运行
              </Link>{" "}
              建一轮。
            </p>
          )}
        </div>
      )}

      {data && (
        <LeaderboardBody
          rows={data.rows.items}
          total={data.rows.total}
          metric={data.metric}
          facet={data.facet ?? facet}
          refreshing={isFetching}
          onMetric={setMetric}
          onFacet={setFacet}
          eligibility={data.eligibility}
          excluded={data.excluded_runs}
        />
      )}
    </div>
  );
}

function LeaderboardBody({
  rows,
  total,
  metric,
  facet,
  refreshing,
  onMetric,
  onFacet,
  eligibility,
  excluded,
}: {
  rows: LeaderboardRow[];
  total: number;
  /** 排序口径：来自响应回显（和这批 rows 是同一来源，避免"按钮说按成本排、表里还是按解决率"的瞬态）。 */
  metric: LeaderboardMetric;
  /** 分面：来自响应回显（同上）。 */
  facet: LeaderboardFacet;
  /** 正在后台刷新（keepPreviousData 展示旧数据时给个提示）。 */
  refreshing: boolean;
  onMetric: (metric: LeaderboardMetric) => void;
  onFacet: (facet: LeaderboardFacet) => void;
  eligibility: string[];
  excluded: { evaluation_run_id: number; reason: string }[];
}) {
  // useMemo 保引用稳定：recharts 的记忆化依赖它在渲染间不变
  const { points, skipped } = useMemo(() => scatterPoints(rows), [rows]);

  return (
    <>
      <section className="mt-6">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-neutral-500">排序口径</span>
          {refreshing && <span className="text-xs text-neutral-400">刷新中…</span>}
          {METRICS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onMetric(item)}
              aria-pressed={item === metric}
              className={
                item === metric
                  ? "rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white"
                  : "rounded-md border border-neutral-200 px-3 py-1 text-xs text-neutral-600 transition-colors hover:bg-neutral-100"
              }
            >
              {METRIC_LABELS[item]}
            </button>
          ))}
        </div>
      </section>

      <section className="mt-4">
        {rows.length === 0 ? (
          <p className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
            这份数据集上还没有合格的实验。准入规则见下方“榜单口径”。
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
                  {/* 列顺序：实验链接紧跟参赛者 —— 它是"榜 → 实验"这条演示主线的入口，
                      放在最右会在窄视口被横向滚动藏掉。协议版本并进参赛者那格的小字。 */}
                  <th className="whitespace-nowrap px-3 py-2 font-medium">名次</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">参赛者</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">实验</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">解决率</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium" title="平台故障率：平台自己没能完成的评测占比，不记在 AI 头上">故障率</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">每题成本</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">耗时</th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">每题 token</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <LeaderboardTableRow
                    key={`${row.agent_config_id}-${row.protocol_version}`}
                    row={row}
                    metric={metric}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {total > rows.length && (
          <p className="mt-2 text-xs text-neutral-400">
            共 {total} 行，当前显示前 {rows.length} 行。
          </p>
        )}
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">成本-解决率散点</h2>
        {points.length === 0 ? (
          <p className="mt-2 text-sm text-neutral-500">没有同时具备成本和解决率的参赛者可画。</p>
        ) : (
          <div className="mt-2 rounded-md border border-neutral-200 bg-white p-2">
            <ScatterPlot points={points} />
          </div>
        )}
        {points.some((point) => point.lowerBound) && (
          <p className="mt-2 text-xs text-neutral-500">
            空心点的成本是下界（有 attempt 报不出成本，只算了报得出的部分）。
          </p>
        )}
        {skipped.length > 0 && (
          <p className="mt-2 text-xs text-amber-600">
            不参与散点（每题成本或解决率缺失）：
            {skipped.map((row) => `${row.label} ${row.protocol_version}`).join("、")}
          </p>
        )}
      </section>

      <section className="mt-8">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-medium text-neutral-900">分面</h2>
          {FACETS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onFacet(item)}
              aria-pressed={item === facet}
              className={
                item === facet
                  ? "rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white"
                  : "rounded-md border border-neutral-200 px-3 py-1 text-xs text-neutral-600 transition-colors hover:bg-neutral-100"
              }
            >
              {FACET_LABELS[item]}
            </button>
          ))}
        </div>
        <div className="mt-3 rounded-md border border-neutral-200 bg-white">
          <FacetMatrix rows={rows} facet={facet} />
        </div>
      </section>

      <section className="mt-8">
        <EligibilityNote rules={eligibility} excluded={excluded} />
      </section>
    </>
  );
}

function LeaderboardTableRow({ row, metric }: { row: LeaderboardRow; metric: LeaderboardMetric }) {
  const spread = spreadRange(row);
  const infraRate = infraFailureRate(row);
  const note = costNote(row);
  // 当前排序口径所在列加粗：后端排的，前端只负责让人一眼看到"按什么排的"
  const strong = (active: boolean) =>
    active ? "text-neutral-900 font-medium" : "text-neutral-600";

  return (
    <tr className="border-b border-neutral-100 last:border-b-0 hover:bg-neutral-50">
      <td className="px-3 py-3 font-mono text-xs text-neutral-500">{row.rank}</td>
      <td className="px-3 py-3">
        <p className="whitespace-nowrap font-medium text-neutral-900">{row.label}</p>
        <p className="mt-0.5 max-w-52 text-xs text-neutral-400">
          {row.agent_display_name} · {row.model_name} · 协议 {row.protocol_version}
        </p>
      </td>
      <td className="whitespace-nowrap px-3 py-3 text-xs">
        {row.run_ids.map((runId) => (
          <Link
            key={runId}
            href={`/runs/${runId}`}
            className="mr-1.5 font-mono text-neutral-600 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
          >
            #{runId}
          </Link>
        ))}
        <p className="mt-0.5 text-neutral-400">{row.run_count} 轮 × {row.tasks_per_run} 题</p>
      </td>
      <td className={`whitespace-nowrap px-3 py-3 font-mono text-xs ${strong(metric === "resolve_rate")}`}>
        {formatRate(row.resolve_rate_mean)}
        {spread !== null && (
          <p className="mt-0.5 font-sans text-xs font-normal text-neutral-400">
            轮间 {formatRate(spread.min)}–{formatRate(spread.max)}
          </p>
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-3 font-mono text-xs text-neutral-600">
        {formatRate(infraRate === null ? null : String(infraRate))}
      </td>
      {/* min-w：这格有三行小字，不给下限会被自动布局挤成一列竖字 */}
      <td className={`min-w-40 px-3 py-3 font-mono text-xs ${strong(metric === "cost")}`}>
        {row.cost_lower_bound && row.cost_per_task !== null && (
          <span title="有 attempt 报不出成本，这个数只算了报得出的部分，真实成本只会更高">≥ </span>
        )}
        <span className="whitespace-nowrap">{formatCost(row.cost_per_task)}</span>
        <p className="mt-0.5 whitespace-nowrap font-sans text-xs font-normal text-neutral-400">
          总 {formatCost(row.cost_usd_total)}
          {row.cost_reported_attempts > 0 && ` · 自报 ${row.cost_reported_attempts}`}
        </p>
        {note !== null && (
          <p
            className={`mt-0.5 max-w-40 font-sans text-xs font-normal ${
              note.warn ? "text-amber-600" : "text-neutral-400"
            }`}
          >
            {note.text}
          </p>
        )}
      </td>
      <td className={`whitespace-nowrap px-3 py-3 font-mono text-xs ${strong(metric === "duration")}`}>
        {formatDuration(row.makespan_ms_mean)}
      </td>
      <td className={`whitespace-nowrap px-3 py-3 font-mono text-xs ${strong(metric === "tokens")}`}>
        {formatTokens(row.tokens_per_task)}
      </td>
    </tr>
  );
}
