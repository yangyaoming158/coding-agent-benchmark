"use client";

/**
 * Agent × 类别热力图：行 = 参赛者，列 = 失败类别，格子 = 条数。
 *
 * 深浅按"占这个 Agent 失败总数的比例"分档，不按绝对数（`cellShade` 的注释）。
 * 没数据的格画 "—" 不画 0。这张表同时是堆叠柱的表格视图。
 */

import {
  categoryLabel,
  categoryShort,
  cellShade,
  heatmapMatrix,
  type FailureSummary,
} from "@/lib/analysis";

/** 五档深浅。0 档是"没有"，用文字色区分而不是背景。 */
const SHADE = [
  "bg-white text-neutral-300",
  "bg-red-50 text-neutral-700",
  "bg-red-100 text-neutral-800",
  "bg-red-200 text-neutral-900",
  "bg-red-300 text-neutral-900 font-medium",
] as const;

export function FailureHeatmap({
  summary,
}: {
  summary: Pick<FailureSummary, "category_counts" | "heatmap">;
}) {
  const matrix = heatmapMatrix(summary);
  if (matrix.agents.length === 0) {
    return (
      <p className="text-sm text-neutral-500">这批实验里没有已归因的失败。</p>
    );
  }
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
              <th className="whitespace-nowrap px-3 py-2 font-medium">
                参赛者
              </th>
              {/* 表头只放短名（F1…N2），全名在表底一行和悬停里：十列全名一屏放不下 */}
              {matrix.categories.map((category) => (
                <th
                  key={category}
                  className="whitespace-nowrap px-3 py-2 text-center font-medium"
                  title={categoryLabel(category)}
                >
                  {categoryShort(category)}
                </th>
              ))}
              <th className="whitespace-nowrap px-3 py-2 text-right font-medium">
                合计
              </th>
            </tr>
          </thead>
          <tbody>
            {matrix.agents.map((agent, row) => (
              <tr
                key={agent}
                className="border-b border-neutral-100 last:border-b-0"
              >
                <td className="whitespace-nowrap px-3 py-2 font-medium text-neutral-900">
                  {agent}
                </td>
                {matrix.categories.map((category, column) => {
                  const count = matrix.cells[row][column];
                  const shade = cellShade(count, matrix.rowTotals[row]);
                  const share =
                    count === null || matrix.rowTotals[row] === 0
                      ? null
                      : Math.round((count / matrix.rowTotals[row]) * 100);
                  return (
                    <td
                      key={category}
                      className={`px-3 py-2 text-center font-mono text-xs ${SHADE[shade]}`}
                      title={
                        count === null
                          ? `${agent} 没有 ${categoryLabel(category)} 的失败`
                          : `${agent} · ${categoryLabel(category)}：${count} 条，占它失败总数的 ${share}%`
                      }
                    >
                      {count === null ? "—" : count}
                    </td>
                  );
                })}
                <td className="px-3 py-2 text-right font-mono text-xs text-neutral-600">
                  {matrix.rowTotals[row]}
                </td>
              </tr>
            ))}
            <tr className="border-t border-neutral-200 text-xs text-neutral-500">
              <td className="px-3 py-2">合计</td>
              {matrix.columnTotals.map((total, column) => (
                <td
                  key={matrix.categories[column]}
                  className="px-3 py-2 text-center font-mono"
                >
                  {total}
                </td>
              ))}
              <td className="px-3 py-2 text-right font-mono">
                {matrix.rowTotals.reduce((a, b) => a + b, 0)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="border-t border-neutral-100 px-3 py-2 text-xs text-neutral-500">
        {matrix.categories
          .map(
            (category) =>
              `${categoryShort(category)} ${categoryLabel(category).replace(/^\w+ · /, "")}`,
          )
          .join(" · ")}
      </p>
    </div>
  );
}
