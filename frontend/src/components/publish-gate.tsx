"use client";

import Link from "next/link";
import { ToneBadge } from "@/components/run-status";
import { formatTime } from "@/lib/display";
import { ratePercent, type PublishGate, type SentinelEvidence } from "@/lib/tasks";
import type { Tone } from "@/lib/display";

/**
 * 发布门禁自检区（协议 C-50）：Oracle 与 Noop 两次哨兵实验。
 *
 * 语义是定死的（§27.2）：Oracle 必须全过、Noop 必须零解决，两个都成立才算
 * 这一版通过了自检。所以每一行都要把"过没过"说出来 —— 只摆两个解决率，
 * 看的人得自己记着方向（哪个该是 100%、哪个该是 0%），记错方向就看反了。
 *
 * 三种"没得看"要分开说：整块证据缺失、证据里没有某些记录、记录里缺字段。
 * 合成一句"没有数据"会让人分不清是没跑过还是存坏了。
 */

const SENTINEL_TEXT = {
  oracle: { name: "Oracle", expect: "全过（100%）", wantResolved: "全部" },
  noop: { name: "Noop", expect: "零解决（0%）", wantResolved: "一道都不该解决" },
} as const;

function sentinelTone(ok: boolean | null): Tone {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "warn";
}

function sentinelLabel(ok: boolean | null): string {
  if (ok === true) return "通过";
  if (ok === false) return "未通过";
  return "数据不足";
}

function SentinelRow({
  kind,
  evidence,
}: {
  kind: keyof typeof SENTINEL_TEXT;
  evidence: SentinelEvidence;
}) {
  const meta = SENTINEL_TEXT[kind];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2">
      <div className="w-32">
        <span className="text-sm font-medium text-neutral-900">{meta.name}</span>
        <p className="text-xs text-neutral-500">{meta.expect}</p>
      </div>
      <div className="w-24 font-mono text-xs text-neutral-600">
        {evidence.runId === null ? (
          "—"
        ) : (
          <Link
            href={`/runs/${evidence.runId}`}
            className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
          >
            #{evidence.runId}
          </Link>
        )}
      </div>
      <div className="w-28 font-mono text-xs text-neutral-600">
        {evidence.resolvedCount === null || evidence.totalTasks === null
          ? "— / —"
          : `${evidence.resolvedCount} / ${evidence.totalTasks}`}
      </div>
      <div className="w-20 font-mono text-xs text-neutral-600">
        {ratePercent(evidence.resolveRate)}
      </div>
      <ToneBadge tone={sentinelTone(evidence.ok)}>
        {sentinelLabel(evidence.ok)}
      </ToneBadge>
      {evidence.ok === null && (
        <span className="text-xs text-amber-600">
          数不够判：{meta.wantResolved}，证据里没写全
        </span>
      )}
    </div>
  );
}

export function PublishGate({ gate }: { gate: PublishGate | null }) {
  if (gate === null) {
    return (
      <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
        <p className="text-sm text-neutral-600">
          这一版没有留下门禁证据 —— 发布时没跑过 Oracle / Noop 自检（协议 C-50）。
        </p>
      </div>
    );
  }

  const bothMissing = gate.oracle === null && gate.noop === null;
  return (
    <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-xs font-medium text-neutral-600">发布门禁自检</h3>
        <p className="font-mono text-xs text-neutral-400">
          {gate.protocolClause ?? "C-50"}
          {gate.checkedAt !== null && ` · 检查于 ${formatTime(gate.checkedAt)}`}
        </p>
      </div>
      {bothMissing ? (
        <p className="mt-2 text-sm text-neutral-600">
          证据里没有 Oracle / Noop 记录。
        </p>
      ) : (
        <div className="mt-1 divide-y divide-neutral-100">
          {gate.oracle !== null && (
            <SentinelRow kind="oracle" evidence={gate.oracle} />
          )}
          {gate.noop !== null && <SentinelRow kind="noop" evidence={gate.noop} />}
        </div>
      )}
    </div>
  );
}
