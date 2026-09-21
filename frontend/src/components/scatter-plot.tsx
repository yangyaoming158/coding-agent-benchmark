"use client";

/**
 * 成本-解决率散点图。
 *
 * x = 每题成本，y = 严格解决率，一个点 = 一个参赛者 × 一个协议版本。
 * 成本不可用的参赛者不在这里 —— 页面把它们列在图旁。
 * 把 null 当 0 画，它们会落在"最便宜"的位置上（§14.5 第五条修掉的那个坑）。
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import { formatCost, formatRate } from "@/lib/display";
import type { ScatterPoint } from "@/lib/leaderboard";

interface PointDatum {
  /** 图上标签：参赛者 + 协议版本，同名的两个版本点能分开。 */
  pointLabel: string;
  label: string;
  protocol: string;
  cost: number;
  rate: number;
  runs: number;
  /** 成本是下界（有 attempt 报不出成本）：画成空心点，数字前加 ≥。 */
  lowerBound: boolean;
}

// 刻度格式化器放到模块顶层：内联箭头每次渲染都是新引用，
// 会打破 recharts 的记忆化（ResponsiveContainer + 不稳定 props 有公开的
// Maximum update depth exceeded 案例）。
const formatCostTick = (value: number) => formatCost(String(value));
const formatRateTick = (value: number) => formatRate(String(value));
/** x 轴上限留 15% 余量：最贵的那个点正好压在右边界上时，它的标签会被裁掉。 */
const costDomainMax = (dataMax: number) => (dataMax > 0 ? dataMax * 1.15 : 1);

export function ScatterPlot({ points }: { points: ScatterPoint[] }) {
  // 行身份是 (参赛者, 协议版本)（C-59），pointLabel 把两者都带上，
  // 否则 aider@v1.1 和 aider@v1.2 会画成两个同名点。
  const data: PointDatum[] = useMemo(
    () =>
      points.map((point) => ({
        pointLabel: `${point.row.label} ${point.row.protocol_version}`,
        label: point.row.label,
        protocol: point.row.protocol_version,
        cost: point.cost,
        rate: point.rate,
        runs: point.row.run_count,
        lowerBound: point.lowerBound,
      })),
    [points],
  );

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 20, right: 24, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
          <XAxis
            type="number"
            dataKey="cost"
            name="每题成本"
            domain={[0, costDomainMax]}
            tickFormatter={formatCostTick}
            tick={{ fontSize: 12 }}
          />
          <YAxis
            type="number"
            dataKey="rate"
            name="解决率"
            domain={[0, 1]}
            tickFormatter={formatRateTick}
            tick={{ fontSize: 12 }}
          />
          <Tooltip content={PointTooltip} />
          <Scatter data={data} fill="#171717" shape={PointShape}>
            <LabelList dataKey="pointLabel" position="top" style={{ fontSize: 12 }} />
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * 点的形状：成本完整的实心，成本是下界的空心。
 * recharts 把点的坐标和原始数据一起传进来；`cx`/`cy` 在数据没画上时可能缺，直接不画。
 */
function PointShape(props: { cx?: number; cy?: number; payload?: PointDatum }) {
  const { cx, cy, payload } = props;
  if (cx === undefined || cy === undefined || payload === undefined) return null;
  return payload.lowerBound ? (
    <circle cx={cx} cy={cy} r={5} fill="#ffffff" stroke="#171717" strokeWidth={1.5} />
  ) : (
    <circle cx={cx} cy={cy} r={5} fill="#171717" />
  );
}

// 泛型用默认值（ValueType, NameType）：Tooltip 组件的 content 期望的正是
// 默认泛型实例，写死 <number, string> 会因函数参数逆变而不兼容。
function PointTooltip({ active, payload }: TooltipContentProps) {
  if (active !== true || payload === undefined || payload.length === 0) return null;
  const datum: PointDatum = payload[0].payload;
  return (
    <div className="rounded-md border border-neutral-200 bg-white px-3 py-2 text-xs shadow-sm">
      <p className="font-medium text-neutral-900">{datum.label}</p>
      <p className="font-mono text-neutral-600">协议 {datum.protocol}</p>
      <p className="mt-1 font-mono text-neutral-600">
        每题成本 {datum.lowerBound ? "≥ " : ""}
        {formatCost(String(datum.cost))}
      </p>
      {datum.lowerBound && (
        <p className="text-amber-600">有 attempt 报不出成本，这是下界</p>
      )}
      <p className="font-mono text-neutral-600">解决率 {formatRate(String(datum.rate))}</p>
      <p className="mt-1 text-neutral-400">{datum.runs} 轮</p>
    </div>
  );
}
