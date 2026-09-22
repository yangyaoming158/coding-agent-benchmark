"use client";

import { ToneBadge } from "@/components/run-status";
import {
  attributionStageLabel,
  attributionStatusText,
  evidenceSourceLabel,
  evidenceView,
  failureCategoryLabel,
  failureCategoryTone,
  factLabel,
  factValueLabel,
  formatConfidence,
  formatTime,
  ruleNameLabel,
} from "@/lib/display";
import type { TaskRunDetail } from "@/lib/queries";

/**
 * 单题页的"失败归因"区块：这次没修好，机器认为是哪一类、凭什么。
 *
 * 数据是 `TaskRunDetail.failure_attribution`（E6 的 `failure_attributions` 一行）。
 * 它只解释原因，**不回写判定**（协议 C-40）—— 页头那个"未解决"来自测试结果，
 * 这里的"F4 逻辑错误"来自归因，两者独立，改了这里不会动上面。
 *
 * 四种状态要分开说，混成一个"无"会误导：
 *
 * 1. `attribution_withheld` — 后端开着盲检开关（`BENCH_BLIND_REVIEW`），结论**有但藏着**
 * 2. 修好了 — 没有失败，自然没有归因
 * 3. 失败了但 `null` — 规则层分不出（F1～F5 那类），LLM 归因没跑或没跑到它
 * 4. 有结论 — 类别、层级、置信度、证据、理由
 */
export function AttributionPanel({ detail }: { detail: TaskRunDetail }) {
  if (detail.attribution_withheld) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-xs leading-relaxed text-amber-800">
          <strong>盲检进行中，机器归因暂不公开。</strong>
          后端开着 <code className="font-mono">BENCH_BLIND_REVIEW</code>：
          人工抽检要求标注的人提交前看不到机器答案（E6-T3），这一页的开放接口也一并藏起来。
          抽检结束把开关关掉、重启 api 就能看到。
        </p>
      </div>
    );
  }

  const attribution = detail.failure_attribution;
  if (attribution === null) {
    if (detail.agent_outcome === "RESOLVED") {
      return (
        <p className="rounded-md border border-neutral-200 bg-white px-4 py-4 text-xs leading-relaxed text-neutral-500">
          这次修好了，没有失败要归因。
        </p>
      );
    }
    return (
      <p className="rounded-md border border-neutral-200 bg-white px-4 py-4 text-xs leading-relaxed text-neutral-500">
        还没有归因结论。规则层只判得了 F6 回归、F7 空补丁、F8 Agent 自身问题、N1
        平台故障这四类；「改了但没修对」（F1～F5）要跑大模型归因或人工抽检。
        判断用的原料就在上面：补丁、逐条用例、日志和轨迹。
      </p>
    );
  }

  const status = attributionStatusText(attribution.status);
  const evidence = evidenceView(attribution.evidence);

  return (
    <div className="rounded-md border border-neutral-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-4 py-3">
        <ToneBadge tone={failureCategoryTone(attribution.category)}>
          {failureCategoryLabel(attribution.category)}
        </ToneBadge>
        <ToneBadge tone="neutral">{attributionStageLabel(attribution.stage)}</ToneBadge>
        <ToneBadge tone={status.tone}>{status.label}</ToneBadge>
        {/* 规则层是确定性判定，没有置信度这个概念：不显示，免得"可信"旁边挂个"置信度 —" */}
        {attribution.confidence !== null && (
          <span className="font-mono text-xs text-neutral-500">
            置信度 {formatConfidence(attribution.confidence)}
          </span>
        )}
        {attribution.secondary_category !== null && (
          <span className="text-xs text-neutral-500">
            候选：{failureCategoryLabel(attribution.secondary_category)}
          </span>
        )}
      </div>

      <div className="space-y-4 px-4 py-4">
        {attribution.reasoning_zh !== null && (
          <p className="text-sm leading-relaxed text-neutral-800">{attribution.reasoning_zh}</p>
        )}

        {/* ── 证据：按 evidence 的形状渲染，见 lib/display.ts 的 evidenceView ── */}
        {evidence.kind === "rule" && (
          <div>
            <p className="text-xs font-medium text-neutral-600">
              命中规则：{ruleNameLabel(evidence.rule)}
              <span className="ml-1 font-mono text-neutral-400">({evidence.rule})</span>
            </p>
            <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
              {evidence.facts.map(([key, value]) => (
                <div key={key} className="flex gap-2">
                  <dt className="text-neutral-500" title={key}>
                    {factLabel(key)}
                  </dt>
                  <dd className="font-mono text-neutral-800" title={value}>
                    {factValueLabel(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        )}

        {evidence.kind === "citations" && (
          <div>
            <p className="text-xs font-medium text-neutral-600">
              引用的证据（每条都逐字来自模型看到的输入，对不上的回答不落库）
            </p>
            <ul className="mt-2 space-y-2">
              {evidence.citations.map((citation, index) => (
                <li
                  key={`${citation.source}-${index}`}
                  className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2"
                >
                  <p className="text-xs text-neutral-500">{evidenceSourceLabel(citation.source)}</p>
                  <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-xs text-neutral-800">
                    {citation.quote}
                  </pre>
                </li>
              ))}
            </ul>
            {evidence.votes.length > 1 && (
              <p className="mt-2 text-xs text-neutral-500">
                低置信，取了 {evidence.votes.length} 票：
                {evidence.votes.map((vote) => vote.split("_")[0]).join(" / ")}
              </p>
            )}
          </div>
        )}

        {evidence.kind === "raw" && (
          <pre className="overflow-x-auto rounded-md bg-neutral-50 px-3 py-2 font-mono text-xs text-neutral-700">
            {evidence.json}
          </pre>
        )}

        <p className="font-mono text-xs text-neutral-400">
          {formatTime(attribution.created_at)}
          {attribution.judge_model !== null && ` · ${attribution.judge_model}`}
          {attribution.prompt_hash !== null && (
            <span title={attribution.prompt_hash}> · prompt {attribution.prompt_hash.slice(0, 12)}…</span>
          )}
        </p>
      </div>
    </div>
  );
}
