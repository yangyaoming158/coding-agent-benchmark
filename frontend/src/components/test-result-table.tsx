"use client";

import { useState } from "react";
import { errorMessage } from "@/lib/api";
import {
  formatMillis,
  testStatusText,
  type TestRole,
  type TestStatus,
} from "@/lib/display";
import { useTaskRunTests, type TestResultRow } from "@/lib/queries";
import { ToneBadge } from "./run-status";

/**
 * 逐条用例的结果表 —— 判定的证据本身。
 *
 * 一屏要放下一次执行的判定依据，而规模可能很大：click 那组题 P2P 合计
 * 29796 条，单题能到 1300+。所以筛选和分页不是装饰：
 *
 * - 排序是后端定的 `(role, test_id, id)`，F2P 排在前面 —— 第一页天然就是
 *   "判定最核心的那批用例"，不用先筛才能看到重点；
 * - 想知道"F2P 全过没有"，看上面的统计卡片（f2p_passed/f2p_total），
 *   不用在一千多条里数；
 * - 想看"哪些 P2P 被改坏了"，状态筛一下。
 */

const PAGE_SIZE = 200; // 后端 MAX_LIMIT 也是 200，别指望要更多

const ROLE_FILTERS: { value: TestRole | ""; label: string }[] = [
  { value: "", label: "全部角色" },
  { value: "F2P", label: "F2P" },
  { value: "P2P", label: "P2P" },
  { value: "OTHER", label: "OTHER" },
];

/** 顺序按"排查时最常看"排：失败和错误在最前面。 */
const STATUS_FILTERS: (TestStatus | "")[] = [
  "",
  "FAILED",
  "ERROR",
  "PASSED",
  "SKIPPED",
  "XFAIL",
  "XPASS",
  "MISSING",
];

const ROLE_TONE: Record<TestRole, "active" | "neutral"> = {
  F2P: "active",
  P2P: "neutral",
  OTHER: "neutral",
};

function ResultRow({ row }: { row: TestResultRow }) {
  const status = testStatusText(row.status);
  return (
    <tr className="border-b border-neutral-100 last:border-b-0 hover:bg-neutral-50">
      <td className="px-3 py-2 font-mono text-xs text-neutral-800" title={row.test_id}>
        <span className="block max-w-md truncate">{row.test_id}</span>
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        <ToneBadge tone={ROLE_TONE[row.role]}>{row.role}</ToneBadge>
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        <ToneBadge tone={status.tone}>{status.label}</ToneBadge>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right font-mono text-xs text-neutral-500">
        {formatMillis(row.duration_ms)}
      </td>
      <td className="px-3 py-2 text-xs leading-relaxed text-neutral-600">
        {row.message_excerpt !== null && (
          <span className="line-clamp-2" title={row.message_excerpt}>
            {row.message_excerpt}
          </span>
        )}
      </td>
    </tr>
  );
}

export function TestResultTable({
  taskRunId,
  live = false,
}: {
  taskRunId: number;
  /** 执行还没到终态时跟着轮询 —— 测试跑完一批就会多出一批结果。 */
  live?: boolean;
}) {
  const [role, setRole] = useState<TestRole | "">("");
  const [status, setStatus] = useState<TestStatus | "">("");
  const [offset, setOffset] = useState(0);

  const { data, error, isLoading, isFetching } = useTaskRunTests(
    taskRunId,
    {
      role: role === "" ? undefined : role,
      status: status === "" ? undefined : status,
      limit: PAGE_SIZE,
      offset,
    },
    { refetchInterval: live ? 3000 : false },
  );

  const total = data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;
  const filtered = role !== "" || status !== "";

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-1">
          {ROLE_FILTERS.map((r) => (
            <button
              key={r.value}
              type="button"
              onClick={() => {
                setRole(r.value);
                setOffset(0);
              }}
              className={`rounded-md border px-2.5 py-1 text-xs transition-colors ${
                role === r.value
                  ? "border-neutral-900 bg-neutral-900 text-white"
                  : "border-neutral-300 text-neutral-700 hover:bg-neutral-50"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>

        <label className="text-xs text-neutral-600">
          状态
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value as TestStatus | "");
              setOffset(0);
            }}
            className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {s === "" ? "全部状态" : testStatusText(s).label}
              </option>
            ))}
          </select>
        </label>

        {data && (
          <span className="text-xs text-neutral-500">
            共 {total} 条{isFetching && " · 刷新中"}
          </span>
        )}
      </div>

      <div className="mt-3">
        {isLoading && <p className="text-sm text-neutral-500">正在加载用例……</p>}

        {error && (
          <p className="text-sm text-red-600">{errorMessage(error)}</p>
        )}

        {data && data.items.length === 0 && (
          <p className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
            {filtered
              ? "这个筛选条件下没有用例。"
              : "这次执行还没有用例结果 —— 判定还没走到测试那一步，或者执行提前终止了。"}
          </p>
        )}

        {data && data.items.length > 0 && (
          <div className="max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white">
            <table className="w-full">
              <thead className="sticky top-0 bg-neutral-50">
                <tr className="border-b border-neutral-200 text-left">
                  <th className="px-3 py-2 text-xs font-medium text-neutral-600">用例</th>
                  <th className="px-3 py-2 text-xs font-medium text-neutral-600">角色</th>
                  <th className="px-3 py-2 text-xs font-medium text-neutral-600">状态</th>
                  <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                    耗时
                  </th>
                  <th className="px-3 py-2 text-xs font-medium text-neutral-600">失败信息</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <ResultRow key={row.id} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {(hasPrev || hasNext) && (
          <div className="mt-3 flex items-center justify-between">
            <button
              type="button"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={!hasPrev}
              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:border-neutral-200 disabled:text-neutral-400"
            >
              上一页
            </button>
            <span className="font-mono text-xs text-neutral-500">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} / {total}
            </span>
            <button
              type="button"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!hasNext}
              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:border-neutral-200 disabled:text-neutral-400"
            >
              下一页
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
