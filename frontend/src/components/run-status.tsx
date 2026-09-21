"use client";

import {
  BUCKET_LABEL,
  runStatusLabel,
  runStatusTone,
  type CellBucket,
  type RunStatus,
  type Tone,
} from "@/lib/display";

/** 色调 → Tailwind class。display.ts 不带样式，颜色只在这里定义一处。 */
const BADGE: Record<Tone, string> = {
  neutral: "bg-neutral-100 text-neutral-600",
  active: "bg-blue-50 text-blue-700",
  ok: "bg-emerald-50 text-emerald-700",
  warn: "bg-amber-50 text-amber-700",
  bad: "bg-red-50 text-red-700",
};

const BAR: Record<Tone, string> = {
  neutral: "bg-neutral-400",
  active: "bg-blue-500",
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-neutral-300",
  active: "bg-blue-400",
  ok: "bg-emerald-500",
  warn: "bg-amber-400",
  bad: "bg-red-500",
};

/** 通用的色调徽章。`display.ts` 出 tone，这里出 class，其他组件拼内容。 */
export function ToneBadge({
  tone,
  children,
}: {
  tone: Tone;
  children: React.ReactNode;
}) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${BADGE[tone]}`}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return <ToneBadge tone={runStatusTone(status)}>{runStatusLabel(status)}</ToneBadge>;
}

/**
 * 进度条。
 *
 * `completed` 数的是**已经定出结论的题**（后端 `completed_tasks` =
 * 有 canonical 认定的题数），不是"已经开跑的题数"。还在排队的、以及跑了一半
 * 没留下结论的，都不在里面。所以进度条会停在 9/10 上不动，直到最后一道题
 * 也有了说法 —— 这比按"跑过就算完成"更接近真正想知道的那件事。
 */
export function ProgressBar({
  completed,
  total,
  tone = "active",
}: {
  completed: number;
  total: number;
  tone?: Tone;
}) {
  const percent = total > 0 ? Math.min(100, (completed / total) * 100) : 0;
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-24 overflow-hidden rounded-full bg-neutral-200"
        role="progressbar"
        aria-valuenow={completed}
        aria-valuemin={0}
        aria-valuemax={total}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${BAR[tone]}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="font-mono text-xs text-neutral-500">
        {completed}/{total}
      </span>
    </div>
  );
}

/** 网格分组的小标题，带一个色点和条数。 */
export function BucketHeading({
  bucket,
  count,
}: {
  bucket: CellBucket;
  count: number;
}) {
  const dot: Record<CellBucket, string> = {
    active: DOT.active,
    resolved: DOT.ok,
    unresolved: DOT.bad,
    infra: DOT.warn,
    cancelled: DOT.neutral,
  };
  return (
    <h3 className="flex items-center gap-1.5 text-xs font-medium text-neutral-600">
      <span className={`h-1.5 w-1.5 rounded-full ${dot[bucket]}`} />
      {BUCKET_LABEL[bucket]}
      <span className="font-mono text-neutral-400">{count}</span>
    </h3>
  );
}
