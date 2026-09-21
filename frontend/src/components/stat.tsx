"use client";

/**
 * 详情页的统计小卡片：一个标签、一个值、一句补充。
 *
 * 原本写在 `/runs/[id]` 页面里，E7-T3 的 Task Run Detail 也要用它 ——
 * 两页十几个格子，抽出来比复制两遍好。
 */
export function Stat({
  label,
  value,
  hint,
  hintTone,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  /** 补充说明要不要标成警告色（比如"这个数是缺的，不是 0"）。 */
  hintTone?: "warn";
}) {
  return (
    <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
      <p className="text-xs text-neutral-500">{label}</p>
      <p className="mt-1 font-mono text-sm text-neutral-900">{value}</p>
      {hint !== undefined && (
        <p
          className={`mt-0.5 text-xs ${
            hintTone === "warn" ? "text-amber-600" : "text-neutral-400"
          }`}
        >
          {hint}
        </p>
      )}
    </div>
  );
}
