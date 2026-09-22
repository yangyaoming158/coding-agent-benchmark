"use client";

/**
 * 生成过的报告列表（E7-T9）。
 *
 * 一次 `cli.report generate` 产出三种格式（HTML/Markdown/JSON），后端已经把它们
 * 合并成一批（`app.report.listing.group_report_records`），这里按批展示，
 * 每批给三个下载链接。不轮询——报告是命令行手动生成的，页面开着不会自己冒出新的一批。
 *
 * 下载走浏览器原生跳转（`<a>` 直接指向后端），不用 fetch：后端要么 302 到签名 URL、
 * 要么流式转发字节，两种都不该经过前端的内存。
 */

import { useState } from "react";
import { API_BASE, errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/display";
import { useReports, type ReportBatch } from "@/lib/queries";

const PAGE_SIZE = 20;

const SCOPE_TEXT: Record<string, string> = {
  SINGLE_RUN: "单次实验",
  COMPARISON: "跨实验对比",
};

const FORMAT_TEXT: Record<string, string> = {
  HTML: "HTML",
  MARKDOWN: "Markdown",
  JSON: "JSON",
};

function downloadUrl(reportRecordId: number): string {
  return `${API_BASE}/api/reports/${reportRecordId}/download`;
}

function ReportRow({ batch }: { batch: ReportBatch }) {
  return (
    <tr className="border-b border-neutral-100 last:border-0">
      <td className="whitespace-nowrap px-4 py-2.5 text-sm text-neutral-600">
        {formatTime(batch.generated_at)}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-sm">
        {SCOPE_TEXT[batch.scope] ?? batch.scope}
      </td>
      <td className="px-4 py-2.5 text-sm text-neutral-700">
        <div className="flex flex-wrap gap-1.5">
          {batch.dataset_labels.length > 0 ? (
            batch.dataset_labels.map((label, index) => (
              <span
                key={`${batch.id}-${index}-${label}`}
                className="rounded-full border border-neutral-200 bg-neutral-50 px-2 py-0.5 text-xs"
              >
                {label}
              </span>
            ))
          ) : (
            <span className="text-xs text-neutral-400">
              涉及的实验（{batch.run_ids.join(", ")}）已不在库里
            </span>
          )}
        </div>
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 text-sm">
        <div className="flex gap-3">
          {batch.formats.map((item) => (
            <a
              key={item.format}
              href={downloadUrl(item.report_record_id)}
              target="_blank"
              rel="noreferrer"
              className="text-sm font-medium text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
            >
              {FORMAT_TEXT[item.format] ?? item.format}
            </a>
          ))}
        </div>
      </td>
    </tr>
  );
}

export default function ReportsPage() {
  const [offset, setOffset] = useState(0);
  const { data, error, isLoading } = useReports({ limit: PAGE_SIZE, offset });

  const total = data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="border-b border-neutral-200 pb-6">
        <h1 className="text-2xl font-semibold tracking-tight">报告</h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600">
          用 <code className="font-mono">cli.report generate</code>{" "}
          生成过的报告，按生成时间倒序。每一批有 HTML / Markdown / JSON 三种下载。
        </p>
      </header>

      <section className="mt-6">
        {isLoading && <p className="text-sm text-neutral-500">正在加载……</p>}

        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">取不到报告列表。</p>
            <p className="mt-1 text-xs leading-relaxed text-red-600">
              {errorMessage(error)}
            </p>
          </div>
        )}

        {data && data.items.length === 0 && (
          <div className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-center">
            <p className="text-sm text-neutral-600">还没有生成过任何报告。</p>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              后端跑{" "}
              <code className="font-mono">
                python -m cli.report generate --run &lt;实验号&gt;
              </code>{" "}
              即可生成。
            </p>
          </div>
        )}

        {data && data.items.length > 0 && (
          <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
            <table className="w-full">
              <thead>
                <tr className="whitespace-nowrap border-b border-neutral-200 bg-neutral-50 text-left">
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    生成时间
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    范围
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    涉及的实验
                  </th>
                  <th className="px-4 py-2 text-xs font-medium text-neutral-600">
                    下载
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((batch) => (
                  <ReportRow key={batch.id} batch={batch} />
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
      </section>
    </div>
  );
}
