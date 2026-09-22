"use client";

/**
 * 失败分析（E7-T6）：归因分布堆叠柱、Agent × 类别热力图、Top 失败案例。
 *
 * 数据来自 `GET /api/analysis`，口径和报告 JSON 的 `failures` 段是后端同一个函数；
 * 前端不重算计数（`lib/analysis.ts` 只摊形状，`scripts/check-analysis.mjs` 钉住）。
 *
 * 范围两种，走 URL：
 * - `?set=<slug>&version=<v>`：这一版数据集上所有**排行榜准入**的实验（默认，下拉框切换）
 * - `?run=158&run=167`：指定实验号，看被排除的、哨兵的、跨数据集的
 *
 * "规则分不出、还没结论"的失败**不进任何类别**，单独一格；LLM 层落库后自动进来；
 * 自动归因自己标了 NEEDS_HUMAN 的案例黄色标出，它们已计入分布但还不算数。
 */

import { Suspense, useMemo } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData } from "@tanstack/react-query";
import { FailureDistribution } from "@/components/failure-distribution";
import { FailureHeatmap } from "@/components/failure-heatmap";
import { IssueTitle } from "@/components/issue-title";
import { ToneBadge } from "@/components/run-status";
import { Stat } from "@/components/stat";
import { ApiError, errorMessage } from "@/lib/api";
import {
  caseTone,
  coverage,
  stackMismatches,
  stackedRows,
  type AnalysisResponse,
  type FailureCase,
} from "@/lib/analysis";
import { agentOutcomeLabel, formatRate, infraLabel } from "@/lib/display";
import { useAnalysis, useBenchmarkSets } from "@/lib/queries";
import type { components } from "@/lib/api-types";

const TOP_N = 50;

/** useSearchParams 必须包在 Suspense 里（同 /leaderboard）。 */
export default function Page() {
  return (
    <Suspense fallback={<p className="text-sm text-neutral-500">正在加载……</p>}>
      <AnalysisPage />
    </Suspense>
  );
}

function AnalysisPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const set = searchParams.get("set") ?? undefined;
  const version = searchParams.get("version") ?? undefined;
  const runs = searchParams
    .getAll("run")
    .map(Number)
    .filter((id) => Number.isInteger(id) && id > 0);
  const byRuns = runs.length > 0;

  const sets = useBenchmarkSets({ limit: 50 });
  // 不给 set 也不给 run 时默认最新已发布版：和 /leaderboard 的默认口径一致
  const defaultSet = sets.data?.items.find(
    (item) => item.status === "PUBLISHED",
  );
  const effectiveSet = byRuns ? undefined : (set ?? defaultSet?.slug);
  const effectiveVersion = byRuns
    ? undefined
    : set
      ? version
      : defaultSet?.version;

  const analysis = useAnalysis(
    byRuns
      ? { run: runs, top_n: TOP_N }
      : { set: effectiveSet, version: effectiveVersion, top_n: TOP_N },
    {
      enabled: byRuns || effectiveSet !== undefined,
      placeholderData: keepPreviousData,
    },
  );

  function pickSet(value: string) {
    const at = value.lastIndexOf("@");
    if (at === -1) return;
    const params = new URLSearchParams();
    params.set("set", value.slice(0, at));
    params.set("version", value.slice(at + 1));
    router.replace(`${pathname}?${params.toString()}`);
  }

  const selected = analysis.data?.benchmark_set ?? "";
  const notFound =
    analysis.error !== null &&
    analysis.error instanceof ApiError &&
    analysis.error.status === 404;

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="border-b border-neutral-200 pb-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">失败分析</h1>
            <p className="mt-2 text-sm leading-relaxed text-neutral-600">
              没修好的题都是哪一类原因。归因只解释原因，不改判定（协议 C-40）；
              和报告里的「失败分类」是同一份数据。
            </p>
          </div>
          {byRuns ? (
            <p className="text-xs text-neutral-600">
              按实验号：{runs.map((id) => `#${id}`).join("、")}{" "}
              <Link
                href="/analysis"
                className="underline decoration-neutral-300 underline-offset-2"
              >
                切回按数据集
              </Link>
            </p>
          ) : (
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
              </select>
            </label>
          )}
        </div>
      </header>

      {analysis.isPending && (
        <p className="mt-8 text-sm text-neutral-500">正在加载……</p>
      )}

      {analysis.error && !analysis.isPending && (
        <div
          className={
            notFound
              ? "mt-8 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
              : "mt-8 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          }
        >
          取不到失败分析：{errorMessage(analysis.error)}
        </div>
      )}

      {analysis.data && (
        <AnalysisBody data={analysis.data} refreshing={analysis.isFetching} />
      )}
    </div>
  );
}

function AnalysisBody({
  data,
  refreshing,
}: {
  data: AnalysisResponse;
  refreshing: boolean;
}) {
  const { failures } = data;
  const cover = coverage(failures);
  // 堆叠柱和 category_counts 对不上是后端 / 摊形状的 bug，页面上直接说，不静默
  const mismatches = useMemo(
    () => stackMismatches(stackedRows(failures)),
    [failures],
  );

  return (
    <>
      <p className="mt-4 text-xs text-neutral-500">
        统计范围：
        {data.run_ids.length === 0 ? (
          "这一版上还没有排行榜准入的实验"
        ) : (
          <>
            {data.benchmark_set !== null &&
              `${data.benchmark_set} 上排行榜准入的 `}
            {data.run_ids.length} 次实验（
            {data.run_ids.map((id, index) => (
              <span key={id}>
                {index > 0 && "、"}
                <Link
                  href={`/runs/${id}`}
                  className="font-mono underline decoration-neutral-300 underline-offset-2"
                >
                  #{id}
                </Link>
              </span>
            ))}
            ）
          </>
        )}
        {refreshing && " · 刷新中…"}
      </p>

      {data.attribution_withheld && (
        <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
          盲检进行中（后端
          BENCH_BLIND_REVIEW）：分布和热力图照常，逐案例的类别和理由被后端藏起来了。
        </div>
      )}

      {/* ── 覆盖情况：未归因单独一格，不混进任何类别 ── */}
      <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="失败"
          value={cover.total}
          hint="canonical 结论不是「已解决」的执行"
        />
        <Stat
          label="已归因"
          value={cover.attributed}
          hint={`规则层 ${cover.rule} · LLM ${cover.llm}`}
        />
        <Stat
          label="还没结论"
          value={cover.unattributed}
          hint="规则分不出、也还没跑 LLM 或人工；不在任何类别里"
          hintTone={cover.unattributed > 0 ? "warn" : undefined}
        />
        <Stat
          label="要人看"
          value={cover.needsHuman}
          hint="自动归因标了 NEEDS_HUMAN；已计入分布，但还不算数"
          hintTone={cover.needsHuman > 0 ? "warn" : undefined}
        />
      </section>

      {mismatches.length > 0 && (
        <p className="mt-3 text-xs text-red-700">
          堆叠柱和类别计数对不上：{mismatches.join("、")} —— 这是
          bug，数字别信。
        </p>
      )}

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">归因分布</h2>
        <p className="mt-1 text-xs text-neutral-500">
          一根柱一个类别，分段是参赛者。F1～F8 记在 AI 头上，N1 是平台故障，N2
          是题目本身有问题。
        </p>
        <div className="mt-2 rounded-md border border-neutral-200 bg-white p-2">
          <FailureDistribution summary={failures} />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">Agent × 类别</h2>
        <p className="mt-1 text-xs text-neutral-500">
          深浅按占该参赛者失败总数的比例，不按绝对条数；悬停看百分比。
        </p>
        <div className="mt-2 rounded-md border border-neutral-200 bg-white">
          <FailureHeatmap summary={failures} />
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">失败案例</h2>
        <p className="mt-1 text-xs text-neutral-500">
          前 {failures.top_cases.length} 条（共 {failures.total_failures}{" "}
          条，按执行 id）。每行点进去是完整证据。
        </p>
        <div className="mt-2 overflow-x-auto rounded-md border border-neutral-200 bg-white">
          {failures.top_cases.length === 0 ? (
            <p className="px-4 py-6 text-sm text-neutral-500">没有失败案例。</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
                  {/* 归因紧跟执行号：它是这张表的主角，放最右会在窄视口被横向滚动藏掉 */}
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    执行
                  </th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    归因
                  </th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    题目
                  </th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    参赛者
                  </th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    判定
                  </th>
                  <th className="whitespace-nowrap px-3 py-2 font-medium">
                    理由
                  </th>
                </tr>
              </thead>
              <tbody>
                {failures.top_cases.map((item) => (
                  <CaseRow
                    key={item.task_run_id}
                    item={item}
                    withheld={data.attribution_withheld}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="mt-8">
        <ReviewMetrics failures={failures} />
      </section>
    </>
  );
}

function CaseRow({ item, withheld }: { item: FailureCase; withheld: boolean }) {
  const verdict = caseTone(item);
  return (
    <tr className="border-b border-neutral-100 last:border-b-0 hover:bg-neutral-50">
      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">
        <Link
          href={`/task-runs/${item.task_run_id}`}
          className="text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
        >
          #{item.task_run_id}
        </Link>
        <p className="mt-0.5 text-neutral-400" title={item.dataset_label}>
          实验 #{item.run_id}
        </p>
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        {withheld ? (
          <span className="text-xs text-neutral-400">盲检中</span>
        ) : (
          <>
            <ToneBadge tone={verdict.tone}>{verdict.label}</ToneBadge>
            {item.attribution_status === "NEEDS_HUMAN" && (
              <span className="ml-1.5 text-xs text-amber-700">要人看</span>
            )}
          </>
        )}
      </td>
      <td className="px-3 py-2">
        <Link
          href={`/task-runs/${item.task_run_id}`}
          title={item.issue_title}
          className="block max-w-56 truncate text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
        >
          <IssueTitle title={item.issue_title} />
        </Link>
        <p className="mt-0.5 font-mono text-xs text-neutral-400">
          {item.task_id}
        </p>
      </td>
      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-neutral-600">
        {item.agent_label}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-xs text-neutral-600">
        <Verdict infra={item.infra_outcome} agent={item.agent_outcome} />
      </td>
      <td className="px-3 py-2 text-xs leading-relaxed text-neutral-600">
        {item.reasoning_zh !== null && (
          <span className="line-clamp-2 max-w-md" title={item.reasoning_zh}>
            {item.reasoning_zh}
          </span>
        )}
      </td>
    </tr>
  );
}

type InfraOutcome = components["schemas"]["InfraOutcome"];
type AgentOutcome = components["schemas"]["AgentOutcome"];

/** 判定两字段：平台那格只在不是 SUCCESS 时显示，AI 那格翻中文。 */
function Verdict({
  infra,
  agent,
}: {
  infra: string | null;
  agent: string | null;
}) {
  const infraText =
    infra !== null && infra !== "SUCCESS"
      ? infraLabel(infra as InfraOutcome)
      : null;
  const agentText =
    agent !== null ? agentOutcomeLabel(agent as AgentOutcome) : "—";
  return (
    <>
      {infraText !== null && (
        <span className="text-amber-700">平台 {infraText} · </span>
      )}
      AI {agentText}
    </>
  );
}

/** 抽检准确率 / κ：E6-T4 的数据。没做出来就把后端给的原因显示出来，不显示 0。 */
function ReviewMetrics({
  failures,
}: {
  failures: AnalysisResponse["failures"];
}) {
  return (
    <details className="rounded-md border border-neutral-200 bg-white px-4 py-3">
      <summary className="cursor-pointer text-sm text-neutral-600">
        自动归因对人工抽检的准确率：
        {failures.review_accuracy.available &&
        failures.review_accuracy_value !== null
          ? ` ${formatRate(String(failures.review_accuracy_value))}`
          : " 暂无"}
        {" · κ："}
        {failures.kappa.available && failures.kappa_value !== null
          ? failures.kappa_value.toFixed(2)
          : "暂无"}
      </summary>
      <div className="mt-2 space-y-1 text-xs text-neutral-500">
        {!failures.review_accuracy.available && (
          <p>{failures.review_accuracy.reason}</p>
        )}
        {!failures.kappa.available && <p>{failures.kappa.reason}</p>}
        {!failures.llm_attribution.available && (
          <p>{failures.llm_attribution.reason}</p>
        )}
        <p>
          人工盲检在{" "}
          <Link
            href="/review"
            className="underline decoration-neutral-300 underline-offset-2"
          >
            /review
          </Link>
          ；标完之后这里自动有数。
        </p>
      </div>
    </details>
  );
}
