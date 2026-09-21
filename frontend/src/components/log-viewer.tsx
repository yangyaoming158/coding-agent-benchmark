"use client";

import { useMemo, useState } from "react";
import { errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/display";
import {
  useArtifactText,
  type ArtifactSummary,
  type ArtifactTextKind,
} from "@/lib/queries";
import { findMatchingLines, splitByQuery } from "@/lib/search";

/**
 * 日志查看器 —— §16.3 说的"日志能搜"。
 *
 * 一次评测的 Agent stdout 能到几 MB（§17.2 的实测），**不可能整份渲染**：
 * 几万行 DOM 会让页面直接卡死。所以两种模式：
 *
 * - 不搜索时渲染前一段（看启动参数、看开头报错）；
 * - 搜索时**只渲染命中的行**（带原行号），命中太多就截断并说明截了多少。
 *
 * 搜索是在已经拉下来的文本上做的（内存里的字符串 indexOf），不是服务端搜索 ——
 * P0 够用，几百 KB 的日志本就在毫秒级。真到几十 MB 的那天再谈流式搜索。
 */

/** 搜索时最多显示多少条命中。再多也不是在"看"，是在"翻"。 */
const MAX_MATCHES = 500;

/** 不搜索时渲染前多少行。选 1500 是因为崩溃栈和启动参数都在这附近。 */
const PREVIEW_LINES = 1500;

/** 清单里出现过的、能当文本日志看的那几种制品。 */
const LOG_KINDS: ArtifactTextKind[] = [
  "AGENT_STDOUT",
  "AGENT_STDERR",
  "TEST_STDOUT",
];

const LOG_LABEL: Partial<Record<ArtifactTextKind, string>> = {
  AGENT_STDOUT: "Agent 输出",
  AGENT_STDERR: "Agent 错误",
  TEST_STDOUT: "测试输出",
};

type LogKind = (typeof LOG_KINDS)[number];

function LogLine({
  no,
  text,
  query,
}: {
  no: number;
  text: string;
  query: string;
}) {
  const parts = useMemo(() => splitByQuery(text, query), [text, query]);
  return (
    <div className="flex">
      <span className="w-14 shrink-0 select-none pr-3 text-right text-neutral-400">
        {no}
      </span>
      <span className="whitespace-pre pr-4">
        {parts.map((part, i) =>
          i % 2 === 1 ? (
            <mark key={i} className="rounded bg-amber-200 text-neutral-900">
              {part}
            </mark>
          ) : (
            part
          ),
        )}
      </span>
    </div>
  );
}

export function LogViewer({
  taskRunId,
  artifacts,
}: {
  taskRunId: number;
  artifacts: ArtifactSummary[];
}) {
  const available = useMemo(
    () => LOG_KINDS.filter((k) => artifacts.some((a) => a.kind === k)),
    [artifacts],
  );

  const [picked, setPicked] = useState<LogKind | null>(null);
  const active =
    picked !== null && available.includes(picked) ? picked : (available[0] ?? null);

  const [query, setQuery] = useState("");

  const text = useArtifactText(taskRunId, active ?? "AGENT_STDOUT", {
    enabled: active !== null,
  });

  const lines = useMemo(
    () => (text.data !== undefined ? text.data.split("\n") : []),
    [text.data],
  );

  const trimmedQuery = query.trim();
  const matches = useMemo(
    () => findMatchingLines(lines, trimmedQuery, MAX_MATCHES),
    [lines, trimmedQuery],
  );

  if (available.length === 0) {
    return (
      <p className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
        这次执行没有留下日志。不是每个阶段都会产出日志制品 ——
        比如 Agent 容器没起来的题，只有平台侧的记录。
      </p>
    );
  }

  const artifact = artifacts.find((a) => a.kind === active);
  const searching = trimmedQuery !== "";
  const shown = searching ? matches : lines.slice(0, PREVIEW_LINES).map((_, i) => i);
  const truncated = !searching && lines.length > PREVIEW_LINES;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          {available.map((kind) => (
            <button
              key={kind}
              type="button"
              onClick={() => setPicked(kind)}
              className={`rounded-md border px-3 py-1 text-xs transition-colors ${
                kind === active
                  ? "border-neutral-900 bg-neutral-900 text-white"
                  : "border-neutral-300 text-neutral-700 hover:bg-neutral-50"
              }`}
            >
              {LOG_LABEL[kind] ?? kind}
            </button>
          ))}
        </div>

        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索日志……"
          className="ml-auto w-48 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900 placeholder:text-neutral-400"
        />

        {artifact !== undefined && (
          <span className="font-mono text-xs text-neutral-400">
            {formatBytes(artifact.size_bytes)}
          </span>
        )}
      </div>

      {searching && (
        <p className="mt-2 text-xs text-neutral-500">
          {matches.length === 0
            ? "没有匹配的行。"
            : `命中 ${matches.length} 行${matches.length >= MAX_MATCHES ? `（只显示前 ${MAX_MATCHES} 行）` : ""}。`}
        </p>
      )}

      {text.isLoading && (
        <p className="mt-3 text-sm text-neutral-500">正在读取日志……</p>
      )}
      {text.error && (
        <p className="mt-3 text-sm text-red-600">{errorMessage(text.error)}</p>
      )}

      {/* max-h：前 1500 行平铺会把页面撑到三万像素，后面的轨迹和归因没人翻得到 */}
      {text.data !== undefined && (
        <div className="mt-3 max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white py-2 font-mono text-xs leading-relaxed">
          {shown.map((lineNo) => (
            <LogLine
              key={lineNo}
              no={lineNo + 1}
              text={lines[lineNo] ?? ""}
              query={searching ? trimmedQuery : ""}
            />
          ))}
          {truncated && (
            <p className="mt-2 px-4 text-neutral-400">
              ……日志共 {lines.length} 行，这里只显示了前 {PREVIEW_LINES} 行。用搜索定位后面的内容。
            </p>
          )}
        </div>
      )}
    </div>
  );
}
