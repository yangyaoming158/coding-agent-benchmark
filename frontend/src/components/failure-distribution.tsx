"use client";

/**
 * 归因分布堆叠柱：一根柱 = 一个失败类别（F1…N2），分段 = 参赛者。
 *
 * 只画已归因的失败。"规则分不出、还没结论"的那些不在这张图里 ——
 * 它们不属于任何类别，页面上单独一格数字。
 */

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import {
  agentOrder,
  stackSeries,
  stackedRows,
  type FailureSummary,
} from "@/lib/analysis";

interface BarDatum {
  short: string;
  label: string;
  total: number;
  [series: string]: string | number;
}

export function FailureDistribution({
  summary,
}: {
  summary: Pick<FailureSummary, "category_counts" | "heatmap">;
}) {
  // 引用稳定：recharts 的记忆化依赖 data / series 在渲染间不变
  const { data, series } = useMemo(() => {
    const seriesList = stackSeries(agentOrder(summary.heatmap));
    const rows = stackedRows(summary).map<BarDatum>((row) => ({
      short: row.short,
      label: row.label,
      total: row.total,
      ...row.counts,
    }));
    return { data: rows, series: seriesList };
  }, [summary]);

  if (data.length === 0) {
    return (
      <p className="text-sm text-neutral-500">这批实验里没有已归因的失败。</p>
    );
  }

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
          barCategoryGap="30%"
        >
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="#e5e5e5"
            vertical={false}
          />
          <XAxis
            dataKey="short"
            tick={{ fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: "#d4d4d4" }}
          />
          <YAxis
            allowDecimals={false}
            tick={{ fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={32}
          />
          <Tooltip content={BarTooltip} cursor={{ fill: "rgba(0,0,0,0.04)" }} />
          <Legend
            wrapperStyle={{ fontSize: 12 }}
            iconType="square"
            iconSize={10}
            // 图例文字用文字色，颜色只在色块上：文字染成序列色会让浅色的那几个读不清
            formatter={legendText}
          />
          {series.map((item, index) => (
            <Bar
              key={item.key}
              dataKey={item.key}
              name={item.label}
              stackId="failures"
              fill={item.color}
              // 分段之间留 2px 白边，最上一段圆角
              stroke="#ffffff"
              strokeWidth={1}
              radius={index === series.length - 1 ? [3, 3, 0, 0] : 0}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

const legendText = (value: string) => (
  <span style={{ color: "#525252" }}>{value}</span>
);

function BarTooltip({ active, payload }: TooltipContentProps) {
  if (active !== true || payload === undefined || payload.length === 0)
    return null;
  const datum: BarDatum = payload[0].payload;
  return (
    <div className="rounded-md border border-neutral-200 bg-white px-3 py-2 text-xs shadow-sm">
      <p className="font-medium text-neutral-900">{datum.label}</p>
      <p className="font-mono text-neutral-500">共 {datum.total} 条</p>
      <ul className="mt-1 space-y-0.5">
        {payload.map((entry) => (
          <li
            key={String(entry.dataKey)}
            className="flex items-center gap-1.5 font-mono text-neutral-700"
          >
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ background: entry.color }}
            />
            {entry.name} {entry.value}
          </li>
        ))}
      </ul>
    </div>
  );
}
