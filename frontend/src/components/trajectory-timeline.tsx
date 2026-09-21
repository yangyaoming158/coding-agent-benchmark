"use client";

import { useMemo } from "react";
import { errorMessage } from "@/lib/api";
import type { Tone } from "@/lib/display";
import { useArtifactText, type ArtifactSummary } from "@/lib/queries";
import {
  countEvents,
  eventMillis,
  parseTrajectory,
  type TrajectoryEvent,
} from "@/lib/trajectory";
import { ToneBadge } from "./run-status";

/**
 * Agent 轨迹时间线（§9.5 的 JSONL）。
 *
 * 这条时间线是失败归因的证据来源（E6），也是答辩时讲"Agent 内部怎么工作"
 * 的直观材料。事件只显示**确定能对上**的三类；不认识的原样列出，
 * 不猜含义 —— 猜出来的证据比没有证据更糟（§9.5 实测回填）。
 */

/** 最多渲染多少个事件。轨迹是 JSONL，一次执行的工具调用通常几十到几百个。 */
const MAX_EVENTS = 500;

const EVENT_TEXT: Record<TrajectoryEvent["kind"], { label: string; tone: Tone }> = {
  tool_call: { label: "工具", tone: "active" },
  llm_usage: { label: "用量", tone: "neutral" },
  message: { label: "消息", tone: "ok" },
  unknown: { label: "未知", tone: "warn" },
};

function formatEventTime(ts: number | null): string {
  const ms = eventMillis(ts);
  if (ms === null) return "—";
  return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
}

function EventBody({ event }: { event: TrajectoryEvent }) {
  switch (event.kind) {
    case "tool_call":
      return (
        <span className="text-xs leading-relaxed text-neutral-700">
          <span className="font-mono text-neutral-900">{event.name}</span>
          {event.summary !== "" && (
            <span className="ml-2 text-neutral-600">{event.summary}</span>
          )}
          {event.digest !== null && (
            <span
              className="ml-2 font-mono text-xs text-neutral-400"
              title={`参数哈希 ${event.digest}`}
            >
              {event.digest.slice(0, 14)}…
            </span>
          )}
        </span>
      );
    case "llm_usage":
      return (
        <span className="font-mono text-xs text-neutral-600">
          输入 {event.input ?? "—"} · 输出 {event.output ?? "—"} tokens
        </span>
      );
    case "message":
      return (
        <span className="text-xs leading-relaxed text-neutral-700">
          {event.role !== null && (
            <span className="mr-2 text-neutral-400">{event.role}</span>
          )}
          <span className="line-clamp-4 whitespace-pre-wrap">{event.text}</span>
        </span>
      );
    case "unknown":
      return (
        <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-neutral-500">
          {event.raw}
        </pre>
      );
  }
}

export function TrajectoryTimeline({
  taskRunId,
  artifacts,
}: {
  taskRunId: number;
  artifacts: ArtifactSummary[];
}) {
  const hasTrajectory = artifacts.some((a) => a.kind === "TRAJECTORY");

  const text = useArtifactText(taskRunId, "TRAJECTORY", {
    enabled: hasTrajectory,
  });

  const parsed = useMemo(
    () => (text.data !== undefined ? parseTrajectory(text.data) : null),
    [text.data],
  );

  if (!hasTrajectory) {
    return (
      <p className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
        这次执行没有留下轨迹。适配器只从确定能对上的输出里提取事件
        （§9.5），提取不到时不产出这份制品 —— 比如 Agent 根本没启动。
      </p>
    );
  }

  if (text.isLoading) {
    return <p className="text-sm text-neutral-500">正在读取轨迹……</p>;
  }
  if (text.error) {
    return <p className="text-sm text-red-600">{errorMessage(text.error)}</p>;
  }
  if (parsed === null) return null;

  const counts = countEvents(parsed.events);
  const shown = parsed.events.slice(0, MAX_EVENTS);

  return (
    <div>
      <p className="font-mono text-xs text-neutral-500">
        {parsed.events.length} 个事件 · 工具 {counts.tool_call} · 用量{" "}
        {counts.llm_usage} · 消息 {counts.message}
        {parsed.skipped > 0 && (
          <span className="ml-2 text-amber-600">
            另有 {parsed.skipped} 行解析不了（已跳过）
          </span>
        )}
      </p>

      {parsed.events.length === 0 ? (
        <p className="mt-3 rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
          轨迹文件是空的。
        </p>
      ) : (
        <ol className="mt-3 max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white">
          {shown.map((event, index) => {
            const meta = EVENT_TEXT[event.kind];
            return (
              <li
                key={index}
                className="flex items-start gap-3 border-b border-neutral-100 px-4 py-2 last:border-b-0"
              >
                <span className="w-20 shrink-0 pt-0.5 font-mono text-xs text-neutral-400">
                  {formatEventTime(event.ts)}
                </span>
                <span className="shrink-0 pt-0.5">
                  <ToneBadge tone={meta.tone}>{meta.label}</ToneBadge>
                </span>
                <div className="min-w-0 flex-1">
                  <EventBody event={event} />
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {parsed.events.length > MAX_EVENTS && (
        <p className="mt-2 text-xs text-neutral-400">
          ……共 {parsed.events.length} 个事件，这里只显示了前 {MAX_EVENTS} 个。
        </p>
      )}
    </div>
  );
}
