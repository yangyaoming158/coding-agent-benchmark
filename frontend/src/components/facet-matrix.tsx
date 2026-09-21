"use client";

/**
 * 分面矩阵：行 = 分面取值（如 简单/中等/困难），列 = 参赛者。
 * 缺格画 "—" 不画 0 —— 没数据不等于全挂。
 */

import { formatRate } from "@/lib/display";
import {
  FACET_LABELS,
  facetMatrix,
  facetValueLabel,
  type LeaderboardFacet,
  type LeaderboardRow,
} from "@/lib/leaderboard";

export function FacetMatrix({
  rows,
  facet,
}: {
  rows: LeaderboardRow[];
  facet: LeaderboardFacet;
}) {
  const { values, cells } = facetMatrix(rows, facet);

  if (values.length === 0) {
    return <p className="text-sm text-neutral-500">这批实验里没有可用作分面的数据。</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
            <th className="px-3 py-2 font-medium">{FACET_LABELS[facet]}</th>
            {rows.map((row) => (
              <th
                key={`${row.agent_config_id}-${row.protocol_version}`}
                className="px-3 py-2 font-medium"
              >
                {row.label}
                <p className="mt-0.5 font-mono text-xs font-normal text-neutral-400">
                  {row.protocol_version}
                </p>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {values.map((value, valueIndex) => (
            <tr key={value} className="border-b border-neutral-100 last:border-b-0">
              <td className="px-3 py-2 text-neutral-600">{facetValueLabel(facet, value)}</td>
              {rows.map((row, rowIndex) => {
                const cell = cells[rowIndex][valueIndex];
                return (
                  <td
                    key={`${row.agent_config_id}-${row.protocol_version}`}
                    className="px-3 py-2 font-mono text-neutral-600"
                  >
                    {cell === null ? "—" : `${cell.resolved}/${cell.total} = ${formatRate(cell.resolve_rate)}`}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
