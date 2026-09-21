"use client";

/**
 * 单题详情 —— §26.3 答辩主线第 1 条落点：展示一道中文 Issue 任务、
 * F2P/P2P 清单、验证证据。
 *
 * 阅读顺序即证据链：这道题是什么（页头 + issue 原文）→ 怎么判（F2P/P2P 名单）
 * → 验证过没有（验证证据 + 隔离记录）→ 各 Agent 在它上面的历史表现。
 *
 * Issue 正文**按纯文本渲染**（保留换行）：引 Markdown 库还得做消毒，
 * 超出本卡范围（卡片"不做"里写明了）。
 */

import { IssueTitle } from "@/components/issue-title";
import { use } from "react";
import Link from "next/link";
import { Stat } from "@/components/stat";
import { TaskHistory } from "@/components/task-history";
import { ToneBadge } from "@/components/run-status";
import { MarkdownBody } from "@/components/markdown-body";
import { ApiError, errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/display";
import { useTaskDetail } from "@/lib/queries";
import {
  difficultyLabel,
  issueLanguageLabel,
  quarantineNote,
  shortHash,
  validationStateText,
} from "@/lib/tasks";

export default function TaskDetailPage(props: PageProps<"/tasks/[taskId]">) {
  const { taskId } = use(props.params);
  const task = useTaskDetail(taskId);

  if (task.isLoading) {
    return <p className="text-sm text-neutral-500">正在加载……</p>;
  }

  if (task.error) {
    const notFound = task.error instanceof ApiError && task.error.status === 404;
    return (
      <div className="mx-auto w-full max-w-4xl">
        <div
          className={
            notFound
              ? "rounded-md border border-amber-200 bg-amber-50 px-4 py-3"
              : "rounded-md border border-red-200 bg-red-50 px-4 py-3"
          }
        >
          <p className={`text-sm ${notFound ? "text-amber-800" : "text-red-700"}`}>
            取不到这道题。
          </p>
          <p
            className={`mt-1 text-xs leading-relaxed ${
              notFound ? "text-amber-700" : "text-red-600"
            }`}
          >
            {errorMessage(task.error)}
          </p>
          <Link
            href="/benchmarks"
            className="mt-2 inline-block text-xs text-neutral-600 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
          >
            ← 回到数据集列表
          </Link>
        </div>
      </div>
    );
  }

  if (task.data === undefined) return null;

  const detail = task.data;
  const validation = validationStateText(detail.validation_state);
  const quarantine = quarantineNote(detail.quarantine);

  return (
    <div className="mx-auto w-full max-w-4xl">
      <Link
        href="/benchmarks"
        className="text-xs text-neutral-500 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
      >
        ← 数据集列表
      </Link>

      <header className="mt-3 border-b border-neutral-200 pb-6">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight"><IssueTitle title={detail.issue_title} /></h1>
          <ToneBadge tone={validation.tone}>{validation.label}</ToneBadge>
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-600">
            {difficultyLabel(detail.difficulty)}
          </span>
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-600">
            {issueLanguageLabel(detail.issue_language)}
          </span>
        </div>
        <p className="mt-2 font-mono text-xs text-neutral-500">
          {detail.task_id} · {detail.repository} · {detail.environment_id} ·{" "}
          {shortHash(detail.base_commit)} · {formatTime(detail.created_at)} 创建
        </p>
      </header>

      {(quarantine !== null || detail.invalid_reason_code !== null) && (
        <section className="mt-6 space-y-3">
          {quarantine !== null && (
            <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
              <p className="text-sm font-medium text-red-700">这道题已被隔离</p>
              <p className="mt-1 text-xs leading-relaxed text-red-600">
                {quarantine.at !== null && `${quarantine.at} · `}
                {quarantine.fromState !== null && `从 ${quarantine.fromState} 转入 · `}
                {quarantine.reason ?? "没有写理由"}
              </p>
              <p className="mt-1 text-xs text-red-500">
                隔离是「题目复验也失败」的结论（协议 C-20 第 6 步），这道题不该再进数据集。
              </p>
            </div>
          )}
          {detail.invalid_reason_code !== null && (
            <div className="rounded-md border border-neutral-200 bg-neutral-50 px-4 py-2">
              <p className="font-mono text-xs text-neutral-600">
                验证未通过原因：{detail.invalid_reason_code}
              </p>
            </div>
          )}
        </section>
      )}

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">Issue 原文</h2>
        <p className="mt-1 text-xs text-neutral-500">
          {issueLanguageLabel(detail.issue_language)}
          {" · "}
          {detail.source_issue_url !== null ? (
            <a
              href={detail.source_issue_url}
              className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
            >
              来源 issue
            </a>
          ) : (
            "无可考来源"
          )}
          {detail.source_pr_url !== null && (
            <>
              {" · "}
              <a
                href={detail.source_pr_url}
                className="underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
              >
                来源 PR
              </a>
            </>
          )}
        </p>
        <div className="mt-3 max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white px-4 py-3">
          {detail.issue_body.trim() === "" ? (
            <p className="text-sm text-neutral-400">这题没有 issue 正文。</p>
          ) : (
            <MarkdownBody text={detail.issue_body} />
          )}
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">用例清单</h2>
        <p className="mt-1 text-xs leading-relaxed text-neutral-500">
          F2P 是修复前必须失败、修复后必须通过的用例；P2P 是修复前后都必须通过的
          （协议 C-10 的两个名单）。
        </p>
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
            <h3 className="text-xs font-medium text-neutral-600">
              F2P · {detail.fail_to_pass.length} 条
            </h3>
            {detail.fail_to_pass.length === 0 ? (
              <p className="mt-2 text-xs text-neutral-400">这题没有 F2P 用例。</p>
            ) : (
              <ul className="mt-2 max-h-72 space-y-1 overflow-auto">
                {detail.fail_to_pass.map((testId) => (
                  <li key={testId} className="break-all font-mono text-xs text-neutral-700">
                    {testId}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
            <h3 className="text-xs font-medium text-neutral-600">
              P2P · {detail.pass_to_pass.length} 条
            </h3>
            {detail.pass_to_pass.length === 0 ? (
              <p className="mt-2 text-xs text-neutral-400">这题没有 P2P 用例。</p>
            ) : (
              // P2P 动辄五六百条（click 单题 599），平铺会把下面的验证证据顶出一万多像素
              <ul className="mt-2 max-h-72 space-y-1 overflow-auto">
                {detail.pass_to_pass.map((testId) => (
                  <li key={testId} className="break-all font-mono text-xs text-neutral-700">
                    {testId}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-900">验证证据</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <Stat
            label="验证状态"
            value={<ToneBadge tone={validation.tone}>{validation.label}</ToneBadge>}
          />
          <Stat label="验证时间" value={formatTime(detail.validated_at)} />
          <Stat label="内容哈希" value={shortHash(detail.content_hash)} />
        </div>
        <div className="mt-3 rounded-md border border-neutral-200 bg-white px-4 py-3">
          <p className="text-xs text-neutral-500">验证制品引用</p>
          {detail.validation_evidence_uri === null ? (
            <p className="mt-1 text-xs text-neutral-400">没留下验证制品引用</p>
          ) : (
            <p className="mt-1 break-all font-mono text-xs text-neutral-700">
              {detail.validation_evidence_uri}
            </p>
          )}
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-5">
          <Stat label="Agent 超时" value={`${detail.agent_timeout_s} 秒`} />
          <Stat label="测试超时" value={`${detail.test_timeout_s} 秒`} />
          <Stat label="CPU 限额" value={detail.sandbox_cpu} />
          <Stat label="内存限额" value={`${detail.sandbox_memory_mb} MB`} />
          <Stat label="PID 上限" value={String(detail.sandbox_pids_limit)} />
        </div>
      </section>

      <section className="mt-8">
        <TaskHistory taskId={detail.task_id} />
      </section>
    </div>
  );
}
