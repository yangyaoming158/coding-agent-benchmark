"use client";

/**
 * 数据集版本列表。
 *
 * 只回答"有哪几版、各多少题" —— 语言分布与来源构成在详情的 composition 里
 * （§14.5 第一节定的：列表给版本与题量，详情给构成与门禁证据）。
 * 列表接口也只给得到这些字段。
 *
 * 不轮询：数据集版本的发布是低频动作（stage → gate → publish 三步走一次），
 * 跑动中的门禁实验也不影响这一页的内容，刷新页面就够。
 */

import Link from "next/link";
import { ToneBadge } from "@/components/run-status";
import { errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/display";
import { useBenchmarkSets } from "@/lib/queries";
import { benchmarkSetStatusText } from "@/lib/tasks";

export default function BenchmarksPage() {
  const { data, error, isLoading } = useBenchmarkSets();

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="border-b border-neutral-200 pb-6">
        <h1 className="text-2xl font-semibold tracking-tight">数据集</h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600">
          每一次发布都是一份冻结的题目快照 —— 同一版里的结果之间才能比较。
        </p>
      </header>

      <section className="mt-6">
        {isLoading && <p className="text-sm text-neutral-500">正在加载……</p>}

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">取不到数据集列表。</p>
            <p className="mt-1 text-xs leading-relaxed text-red-600">
              {errorMessage(error)}
            </p>
          </div>
        )}

        {data && data.items.length === 0 && (
          <div className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-center">
            <p className="text-sm text-neutral-600">还没有发布过任何数据集版本。</p>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              发布走三步：先{" "}
              <code className="font-mono">make dataset-stage SLUG=&lt;slug&gt;</code>{" "}
              冻快照，再 <code className="font-mono">make dataset-gate</code> 跑
              Oracle / Noop 门禁，过了才{" "}
              <code className="font-mono">make dataset-publish</code>。
            </p>
          </div>
        )}

        {data && data.items.length > 0 && (
          <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
            <table className="w-full">
              <thead>
                <tr className="whitespace-nowrap border-b border-neutral-200 bg-neutral-50 text-left">
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    数据集
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    状态
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    题量
                  </th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-neutral-600">
                    发布时间
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    描述
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((set) => {
                  const status = benchmarkSetStatusText(set.status);
                  return (
                    <tr
                      key={set.id}
                      className="border-b border-neutral-200 last:border-b-0 hover:bg-neutral-50"
                    >
                      <td className="px-4 py-3">
                        <Link
                          href={`/benchmarks/${encodeURIComponent(set.slug)}?version=${encodeURIComponent(set.version)}`}
                          className="text-sm font-medium text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                        >
                          {set.title}
                        </Link>
                        <p className="mt-0.5 font-mono text-xs text-neutral-400">
                          {set.slug}@{set.version}
                        </p>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <ToneBadge tone={status.tone}>{status.label}</ToneBadge>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-600">
                        {set.task_count}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs text-neutral-500">
                        {formatTime(set.published_at)}
                      </td>
                      <td className="px-4 py-3">
                        <p
                          className="max-w-md truncate text-xs text-neutral-600"
                          title={set.description ?? undefined}
                        >
                          {set.description ?? "—"}
                        </p>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
