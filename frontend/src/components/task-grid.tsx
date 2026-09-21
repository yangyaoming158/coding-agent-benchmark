"use client";

import Link from "next/link";
import {
  BUCKET_ORDER,
  cellVerdict,
  type CellBucket,
  type Tone,
} from "@/lib/display";
import type { TaskRunSummary } from "@/lib/queries";
import { BucketHeading } from "./run-status";

const CHIP: Record<Tone, string> = {
  neutral: "border-neutral-200 bg-white text-neutral-600 hover:border-neutral-400",
  active: "border-blue-200 bg-blue-50 text-blue-800 hover:border-blue-400",
  ok: "border-emerald-200 bg-emerald-50 text-emerald-800 hover:border-emerald-400",
  warn: "border-amber-200 bg-amber-50 text-amber-800 hover:border-amber-400",
  bad: "border-red-200 bg-red-50 text-red-800 hover:border-red-400",
};

const CHIP_DOT: Record<Tone, string> = {
  neutral: "bg-neutral-400",
  active: "bg-blue-500",
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
};

/** `bench-golden__auth-2` → `auth-2`。格子里放全名会把网格撑成一堆省略号。 */
function shortTaskId(taskId: string): string {
  const tail = taskId.split("__").at(-1) ?? taskId;
  return tail.length > 18 ? `${tail.slice(0, 16)}…` : tail;
}

/**
 * 一道题只留一行。
 *
 * 重试会让一道题有多条 attempt。认定结果看的是 canonical 那条（协议 C-24），
 * 但**还在跑的题还没有 canonical** —— 只认 canonical 会让进行中的格子凭空消失，
 * 网格看着像掉了题。所以：有 canonical 用 canonical，没有就用最新那次 attempt。
 */
function oneRowPerTask(rows: TaskRunSummary[]): TaskRunSummary[] {
  const picked = new Map<number, TaskRunSummary>();
  for (const row of rows) {
    const current = picked.get(row.benchmark_task_id);
    if (current === undefined) {
      picked.set(row.benchmark_task_id, row);
      continue;
    }
    const better =
      row.is_canonical !== current.is_canonical
        ? row.is_canonical
        : row.attempt_no > current.attempt_no;
    if (better) picked.set(row.benchmark_task_id, row);
  }
  return [...picked.values()].sort((a, b) => a.benchmark_task_id - b.benchmark_task_id);
}

function tooltip(row: TaskRunSummary, verdictLabel: string): string {
  const lines = [
    row.task_id,
    row.issue_title,
    `第 ${row.attempt_no} 次尝试 · ${verdictLabel}`,
    `F2P ${row.f2p_passed ?? "—"}/${row.f2p_total ?? "—"} · P2P ${row.p2p_passed ?? "—"}/${row.p2p_total ?? "—"}`,
  ];
  if (row.error_code) lines.push(`错误码 ${row.error_code}`);
  return lines.join("\n");
}

/**
 * 逐题网格。
 *
 * 每格点进去就是 `/task-runs/{id}` —— §16.3 那条"3 次点击内到达某道题完整证据"
 * 的路径里，这是第 2 跳。
 *
 * **算不出状态的题不会画成空格子。** 逐题记录是 Worker 真开跑才建的
 * （`POST /api/runs` 只投作业），所以题还堆在队列里的时候，实验的
 * `total_tasks` 是 4 而这里一行都没有。那种情况如实说"还没产生记录"，
 * 而不是画 4 个灰格子假装知道它们在排队 —— 它们也可能压根没被领走。
 */
export function TaskGrid({
  taskRuns,
  totalTasks,
  runFinished,
}: {
  taskRuns: TaskRunSummary[];
  totalTasks: number;
  runFinished: boolean;
}) {
  const rows = oneRowPerTask(taskRuns);
  const missing = Math.max(0, totalTasks - rows.length);

  if (rows.length === 0) {
    return (
      <p className="text-sm text-neutral-500">
        {missing > 0
          ? `${missing} 道题都还没有执行记录 —— 作业在队列里排着，Worker 领到才会建记录。`
          : "这次实验没有逐题记录。"}
      </p>
    );
  }

  const grouped = new Map<CellBucket, { row: TaskRunSummary; label: string; tone: Tone }[]>();
  for (const row of rows) {
    const verdict = cellVerdict(row);
    const list = grouped.get(verdict.bucket) ?? [];
    list.push({ row, label: verdict.label, tone: verdict.tone });
    grouped.set(verdict.bucket, list);
  }

  return (
    <div className="space-y-4">
      {BUCKET_ORDER.map((bucket) => {
        const list = grouped.get(bucket);
        if (list === undefined || list.length === 0) return null;
        return (
          <section key={bucket}>
            <BucketHeading bucket={bucket} count={list.length} />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {list.map(({ row, label, tone }) => (
                <Link
                  key={row.id}
                  href={`/task-runs/${row.id}`}
                  title={tooltip(row, label)}
                  className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 font-mono text-xs transition-colors ${CHIP[tone]}`}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${CHIP_DOT[tone]}`} />
                  {shortTaskId(row.task_id)}
                </Link>
              ))}
            </div>
          </section>
        );
      })}

      {missing > 0 && (
        <p className="text-xs text-neutral-500">
          {runFinished
            ? `另有 ${missing} 道题没有执行记录。实验已经结束，这几道题应该是有记录的，值得查一下。`
            : `另有 ${missing} 道题还在队列里等着，还没有执行记录。`}
        </p>
      )}
    </div>
  );
}
