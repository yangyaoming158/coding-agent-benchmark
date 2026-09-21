"use client";

import type { CompositionGroupRow } from "@/lib/tasks";

/**
 * 数据集构成：语言 / 难度 / 仓库三组计数。
 *
 * 只负责画 —— 顺序、百分比、合计对不对得上都在 `lib/tasks.ts` 里判过（有断言）。
 * 合计与题量对不上时组内摆一行提醒：`compositionRows` 刻意**不修正**这种数据，
 * 页面就得把它显出来，不然条形图会安静地把一个错的构成画得很好看。
 */
export function CompositionBars({
  groups,
  taskCount,
}: {
  groups: CompositionGroupRow[];
  taskCount: number;
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-3">
      {groups.map((group) => (
        <div
          key={group.key}
          className="rounded-md border border-neutral-200 bg-white px-4 py-3"
        >
          <h3 className="text-xs font-medium text-neutral-600">{group.label}</h3>
          {group.cells.length === 0 ? (
            <p className="mt-2 text-xs text-neutral-400">没有数据</p>
          ) : (
            <ul className="mt-2 space-y-2">
              {group.cells.map((cell) => (
                <li key={cell.value}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-xs text-neutral-700" title={cell.value}>
                      {cell.label}
                    </span>
                    <span className="shrink-0 font-mono text-xs text-neutral-500">
                      {cell.count} · {cell.percent}%
                    </span>
                  </div>
                  <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-neutral-200">
                    <div
                      className="h-full rounded-full bg-neutral-500"
                      style={{ width: `${cell.percent}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
          {group.matchesTaskCount === false && (
            <p className="mt-2 text-xs leading-relaxed text-amber-600">
              这一组的合计（{group.total}）与题量（{taskCount}）对不上
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
