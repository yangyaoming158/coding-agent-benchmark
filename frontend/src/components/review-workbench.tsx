"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/lib/api-types";
import { MarkdownBody } from "@/components/markdown-body";
import { useAdminToken } from "@/lib/admin-token";
import { API_BASE, apiGet, apiPost, apiText } from "@/lib/api";

type ReviewQueue = components["schemas"]["ReviewQueueResponse"];
type ReviewCase = components["schemas"]["ReviewCaseResponse"];
type ReviewSubmit = components["schemas"]["ReviewSubmitResponse"];
type FailureCategory = components["schemas"]["FailureCategory"];

type ActiveSession = {
  reviewer: string;
  token: string;
  seed: number;
};

const CATEGORY_LABELS: Record<FailureCategory, string> = {
  F1_REQUIREMENT_MISUNDERSTANDING: "F1 · 误解需求",
  F2_WRONG_FILE_LOCALIZATION: "F2 · 找错修改位置",
  F3_INCOMPLETE_FIX: "F3 · 修复不完整",
  F4_INCORRECT_LOGIC: "F4 · 实现逻辑错误",
  F5_SYNTAX_OR_BUILD_ERROR: "F5 · 语法或构建错误",
  F6_REGRESSION: "F6 · 引入回归",
  F7_EMPTY_OR_INVALID_PATCH: "F7 · 空补丁或无效补丁",
  F8_AGENT_TOOL_OR_BUDGET_FAILURE: "F8 · Agent 工具或预算问题",
  N1_INFRASTRUCTURE_FAILURE: "N1 · 平台故障",
  N2_TASK_DEFECT: "N2 · 题目本身有问题",
};

const CATEGORIES = Object.keys(CATEGORY_LABELS) as FailureCategory[];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败";
}

function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  const colors =
    tone === "warn"
      ? "border-amber-200 bg-amber-50 text-amber-800"
      : tone === "ok"
        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
        : "border-neutral-200 bg-neutral-50 text-neutral-700";
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs ${colors}`}>
      {children}
    </span>
  );
}

function Panel({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="min-w-0 rounded-lg border border-neutral-200 bg-white">
      <h2 className="border-b border-neutral-200 px-4 py-3 text-sm font-semibold">{title}</h2>
      <div className="space-y-4 p-4">{children}</div>
    </section>
  );
}

function PatchViewer({ text }: { text: string }) {
  return (
    <pre className="max-h-[520px] overflow-auto rounded-md bg-neutral-950 p-3 text-xs leading-5 text-neutral-100">
      {text.split("\n").map((line, index) => {
        const color = line.startsWith("+")
          ? "text-emerald-300"
          : line.startsWith("-")
            ? "text-red-300"
            : line.startsWith("@@")
              ? "text-sky-300"
              : "text-neutral-200";
        return (
          <span key={`${index}-${line.slice(0, 12)}`} className={`block ${color}`}>
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function SetupForm({ onStart }: { onStart: (session: ActiveSession) => void }) {
  const [reviewer, setReviewer] = useState("");
  // 令牌和实验页共用同一处保管（sessionStorage）：在任一处输过一次，这里就带出来了
  const [token, setToken] = useAdminToken();
  const [seed, setSeed] = useState("20260920");

  return (
    <form
      className="grid gap-3 rounded-lg border border-neutral-200 bg-white p-4 md:grid-cols-[1fr_1.4fr_160px_auto]"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmedReviewer = reviewer.trim();
        const trimmedToken = token.trim();
        if (!trimmedReviewer || !trimmedToken) return;
        onStart({
          reviewer: trimmedReviewer,
          token: trimmedToken,
          seed: Number(seed),
        });
      }}
    >
      <label className="text-xs font-medium text-neutral-600">
        标注者
        <input
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm text-neutral-950"
          placeholder="例如 alice"
          required
        />
      </label>
      <label className="text-xs font-medium text-neutral-600">
        管理员 Token
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm text-neutral-950"
          placeholder="X-Bench-Token"
          required
        />
      </label>
      <label className="text-xs font-medium text-neutral-600">
        固定随机种子
        <input
          type="number"
          min={0}
          value={seed}
          onChange={(event) => setSeed(event.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm text-neutral-950"
          required
        />
      </label>
      <button
        type="submit"
        className="self-end rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-700"
      >
        进入盲检
      </button>
    </form>
  );
}

function QueuePanel({
  queue,
  selected,
  onSelect,
}: {
  queue: ReviewQueue;
  selected: number | null;
  onSelect: (taskRunId: number) => void;
}) {
  return (
    <aside className="rounded-lg border border-neutral-200 bg-white">
      <div className="border-b border-neutral-200 p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">待处理队列</h2>
          <Badge>{queue.pending_count} 条</Badge>
        </div>
        <p className="mt-2 break-all font-mono text-[11px] text-neutral-500">
          {queue.batch.batch_id}
        </p>
        {queue.batch.insufficient_pool && (
          <p className="mt-2 text-xs text-amber-700">
            当前只有 {queue.batch.selected_count} 条可归因失败，已全部纳入；真实 LLM
            回填后应新建正式批次。
          </p>
        )}
      </div>
      <div className="max-h-[720px] divide-y divide-neutral-100 overflow-auto">
        {queue.items.map((item) => (
          <button
            key={item.task_run_id}
            type="button"
            onClick={() => onSelect(item.task_run_id)}
            className={`block w-full px-4 py-3 text-left hover:bg-neutral-50 ${
              selected === item.task_run_id ? "bg-neutral-100" : ""
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-xs text-neutral-500">#{item.position}</span>
              {item.required_phase === "ARBITRATION" && <Badge tone="warn">待仲裁</Badge>}
            </div>
            <p className="mt-1 text-sm font-medium text-neutral-900">{item.task_id}</p>
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-neutral-600">
              {item.issue_title}
            </p>
          </button>
        ))}
        {queue.items.length === 0 && (
          <p className="p-5 text-sm text-neutral-500">这个批次暂时没有你需要处理的案例。</p>
        )}
      </div>
    </aside>
  );
}

function CaseEvidence({
  reviewCase,
  patchText,
}: {
  reviewCase: ReviewCase;
  patchText?: string;
}) {
  const artifactUrl = (kind: string) =>
    `${API_BASE}/api/task-runs/${reviewCase.task_run_id}/artifacts/${kind}`;
  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <Panel title="题目">
        <div>
          <p className="text-xs text-neutral-500">
            {reviewCase.repository} · {reviewCase.difficulty}
          </p>
          <h3 className="mt-1 font-semibold">{reviewCase.issue_title}</h3>
          <div className="mt-3 max-h-[60vh] overflow-auto">
            <MarkdownBody text={reviewCase.issue_body} />
          </div>
        </div>
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">
            官方补丁摘要
          </h3>
          {reviewCase.gold_patch.available ? (
            <>
              <p className="mt-2 text-xs text-neutral-600">
                +{reviewCase.gold_patch.lines_added} / -{reviewCase.gold_patch.lines_deleted}
              </p>
              <ul className="mt-2 space-y-1 font-mono text-xs">
                {reviewCase.gold_patch.files.map((file) => (
                  <li key={file}>{file}</li>
                ))}
              </ul>
            </>
          ) : (
            <p className="mt-2 text-xs text-neutral-500">官方补丁摘要不可用</p>
          )}
        </div>
      </Panel>

      <Panel title="Agent 补丁与轨迹">
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge>{reviewCase.agent_config_label}</Badge>
          <Badge>{reviewCase.agent_outcome ?? "无 Agent 结论"}</Badge>
          <Badge>{reviewCase.infra_outcome ?? "无平台结论"}</Badge>
        </div>
        {patchText ? (
          <PatchViewer text={patchText} />
        ) : (
          <p className="text-sm text-neutral-500">没有可显示的标准化补丁。</p>
        )}
        <div className="flex flex-wrap gap-2">
          {reviewCase.artifacts.map((kind) => (
            <a
              key={kind}
              href={artifactUrl(kind)}
              target="_blank"
              rel="noreferrer"
              className="rounded-md border border-neutral-300 px-2.5 py-1.5 text-xs hover:bg-neutral-50"
            >
              打开 {kind}
            </a>
          ))}
        </div>
      </Panel>

      <Panel title="测试证据">
        <div className="max-h-[520px] space-y-2 overflow-auto">
          {reviewCase.tests.map((test) => (
            <div key={`${test.role}-${test.test_id}`} className="rounded-md border p-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <Badge>{test.role}</Badge>
                <Badge tone={test.status === "PASSED" ? "ok" : "warn"}>{test.status}</Badge>
              </div>
              <p className="mt-2 break-all font-mono text-neutral-800">{test.test_id}</p>
              {test.message_excerpt && (
                <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded bg-neutral-50 p-2 text-neutral-600">
                  {test.message_excerpt}
                </pre>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function AttributionReveal({ reviewCase }: { reviewCase: ReviewCase }) {
  const automatic = reviewCase.automatic_attribution;
  if (!automatic) {
    return (
      <div className="rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900">
        自动归因仍处于盲态。提交你的类别之后，后端才会返回类别、理由、置信度和证据。
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-emerald-950">自动归因对照</span>
        <Badge tone="ok">{CATEGORY_LABELS[automatic.category]}</Badge>
        <Badge>{automatic.stage}</Badge>
        {automatic.confidence !== null && <Badge>置信度 {automatic.confidence}</Badge>}
      </div>
      <p className="mt-3 text-sm leading-6 text-emerald-950">
        {automatic.reasoning_zh ?? "没有中文理由"}
      </p>
      <details className="mt-3 text-xs text-emerald-900">
        <summary className="cursor-pointer font-medium">查看 evidence</summary>
        <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded bg-white/70 p-3">
          {JSON.stringify(automatic.evidence, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function ReviewForm({
  reviewCase,
  pending,
  onSubmit,
}: {
  reviewCase: ReviewCase;
  pending: boolean;
  onSubmit: (category: FailureCategory | null, comment: string) => void;
}) {
  const [category, setCategory] = useState<FailureCategory | "">("");
  const [comment, setComment] = useState("");
  const alreadySubmitted = reviewCase.progress.current_reviewer_submitted;
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">
          {reviewCase.progress.phase === "ARBITRATION" ? "第三人独立仲裁" : "提交独立判断"}
        </h2>
        <div className="flex gap-2">
          <Badge>{reviewCase.progress.label_count} 人已提交</Badge>
          {reviewCase.progress.phase === "COMPLETE" && <Badge tone="ok">复核完成</Badge>}
        </div>
      </div>
      {reviewCase.own_selected_category && (
        <p className="mt-3 text-sm text-neutral-700">
          你的判断：{CATEGORY_LABELS[reviewCase.own_selected_category]}
        </p>
      )}
      {!alreadySubmitted && reviewCase.progress.phase !== "COMPLETE" && (
        <div className="mt-4 grid gap-3 md:grid-cols-[1fr_2fr_auto_auto]">
          <label className="text-xs font-medium text-neutral-600">
            失败类别
            <select
              value={category}
              onChange={(event) => setCategory(event.target.value as FailureCategory | "")}
              className="mt-1 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm text-neutral-950"
            >
              <option value="">请选择</option>
              {CATEGORIES.map((item) => (
                <option key={item} value={item}>
                  {CATEGORY_LABELS[item]}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs font-medium text-neutral-600">
            备注{category === "N2_TASK_DEFECT" ? "（题目缺陷必须填写）" : "（可选）"}
            <input
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              className="mt-1 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm text-neutral-950"
              placeholder="写下判断依据"
            />
          </label>
          <button
            type="button"
            disabled={pending || !category || (category === "N2_TASK_DEFECT" && !comment.trim())}
            onClick={() => category && onSubmit(category, comment)}
            className="self-end rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            提交类别
          </button>
          <button
            type="button"
            disabled={pending || !comment.trim()}
            onClick={() => onSubmit(null, comment)}
            className="self-end rounded-md border border-neutral-300 px-4 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-40"
          >
            只记备注
          </button>
        </div>
      )}
    </section>
  );
}

/** E6-T3 盲检工作台。自动答案是否可见只由后端响应决定。 */
export function ReviewWorkbench() {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<ActiveSession | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const queueQuery = useQuery({
    queryKey: ["review-queue", session?.reviewer, session?.seed],
    queryFn: () => {
      if (!session) throw new Error("尚未进入盲检");
      const params = new URLSearchParams({
        reviewer: session.reviewer,
        seed: String(session.seed),
      });
      return apiGet<ReviewQueue>(`/api/review/queue?${params}`, session.token);
    },
    enabled: session !== null,
  });

  const activeBatchId = queueQuery.data?.batch.batch_id;
  const effectiveSelected = selected ?? queueQuery.data?.items[0]?.task_run_id ?? null;
  const caseQuery = useQuery({
    queryKey: ["review-case", activeBatchId, session?.reviewer, effectiveSelected],
    queryFn: () => {
      if (!session || !activeBatchId || effectiveSelected === null) {
        throw new Error("没有选中案例");
      }
      const params = new URLSearchParams({
        batch_id: activeBatchId,
        reviewer: session.reviewer,
      });
      return apiGet<ReviewCase>(
        `/api/review/${effectiveSelected}?${params}`,
        session.token,
      );
    },
    enabled: Boolean(session && activeBatchId && effectiveSelected !== null),
  });

  const normalizedPatch = useMemo(
    () => caseQuery.data?.patches.some((patch) => patch.kind === "AGENT_NORMALIZED") ?? false,
    [caseQuery.data?.patches],
  );
  const patchQuery = useQuery({
    queryKey: ["review-patch", effectiveSelected],
    queryFn: () =>
      apiText(`/api/task-runs/${effectiveSelected}/artifacts/AGENT_NORMALIZED`),
    enabled: effectiveSelected !== null && normalizedPatch,
  });

  const submitMutation = useMutation({
    mutationFn: ({ category, comment }: { category: FailureCategory | null; comment: string }) => {
      if (!session || !activeBatchId || effectiveSelected === null) {
        throw new Error("没有选中案例");
      }
      return apiPost<ReviewSubmit>(
        `/api/review/${effectiveSelected}`,
        {
          batch_id: activeBatchId,
          reviewer: session.reviewer,
          category,
          comment: comment.trim() || null,
        },
        session.token,
      );
    },
    onSuccess: async (result) => {
      setNotice(
        result.task_quarantined
          ? "复核完成：最终判定为题目缺陷，题目已进入 QUARANTINED。"
          : result.action === "COMMENT"
            ? "备注已保存；自动归因仍保持盲态。"
            : "判断已保存，自动归因对照现已解锁。",
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["review-queue"] }),
        queryClient.invalidateQueries({ queryKey: ["review-case"] }),
      ]);
    },
  });

  return (
    <div className="mx-auto w-full max-w-[1680px]">
      <header className="mb-6">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">人工盲检</h1>
          <Badge tone="warn">后端强制隐藏自动答案</Badge>
        </div>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-600">
          两名标注者先独立判断；意见不一致时由第三人仲裁。提交有效类别前，接口不会返回自动归因类别、理由、置信度或 evidence。
        </p>
      </header>

      <SetupForm
        onStart={(value) => {
          setSession(value);
          setSelected(null);
          setNotice(null);
        }}
      />

      {queueQuery.error && (
        <p className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {errorMessage(queueQuery.error)}
        </p>
      )}
      {notice && (
        <p className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
          {notice}
        </p>
      )}

      {queueQuery.data && (
        <div className="mt-5 grid gap-5 lg:grid-cols-[280px_minmax(0,1fr)]">
          <QueuePanel
            queue={queueQuery.data}
            selected={effectiveSelected}
            onSelect={setSelected}
          />
          <div className="min-w-0 space-y-4">
            {caseQuery.isLoading && (
              <p className="rounded-lg border bg-white p-5 text-sm text-neutral-500">
                正在读取案例证据……
              </p>
            )}
            {caseQuery.error && (
              <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                {errorMessage(caseQuery.error)}
              </p>
            )}
            {caseQuery.data && (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="font-mono text-xs text-neutral-500">
                      #{caseQuery.data.position} · task-run {caseQuery.data.task_run_id}
                    </p>
                    <p className="mt-1 text-sm font-medium">{caseQuery.data.task_id}</p>
                  </div>
                  {caseQuery.data.progress.phase === "ARBITRATION" && (
                    <Badge tone="warn">第三人仲裁</Badge>
                  )}
                </div>
                <CaseEvidence reviewCase={caseQuery.data} patchText={patchQuery.data} />
                <AttributionReveal reviewCase={caseQuery.data} />
                <ReviewForm
                  key={`${caseQuery.data.task_run_id}-${caseQuery.data.progress.label_count}`}
                  reviewCase={caseQuery.data}
                  pending={submitMutation.isPending}
                  onSubmit={(category, comment) => {
                    setNotice(null);
                    submitMutation.mutate({ category, comment });
                  }}
                />
                {submitMutation.error && (
                  <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                    {errorMessage(submitMutation.error)}
                  </p>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
