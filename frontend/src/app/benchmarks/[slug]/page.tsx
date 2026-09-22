"use client";

/**
 * 数据集详情：这一版由什么构成、门禁自检过没过、逐题表格。
 *
 * 四个筛选（仓库/难度/语言/状态）加一个搜索框，**五个条件全部走后端参数**
 * （`/api/tasks`，§14.5）—— 前端过滤只能滤当页，一个会说谎的控件不值得做。
 * 筛选选项从 `composition` 与枚举生成，不手写取值：写死的话后端多一个语言，
 * 这里就少一个选项，而页面上看不出来。
 *
 * 任务表格用 `keepPreviousData` 保活：切筛选时表格不闪空（E7-T4 的经验）。
 */

import { IssueTitle } from "@/components/issue-title";
import { Suspense, use, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { keepPreviousData } from "@tanstack/react-query";
import { CompositionBars } from "@/components/composition-bars";
import { PublishGate } from "@/components/publish-gate";
import { ToneBadge } from "@/components/run-status";
import { ApiError, errorMessage } from "@/lib/api";
import { formatTime } from "@/lib/display";
import { useBenchmarkSetDetail, useTasks } from "@/lib/queries";
import {
  benchmarkSetStatusText,
  compositionRows,
  difficultyLabel,
  issueLanguageLabel,
  publishGate,
  shortHash,
  validationStateText,
  type IssueLanguage,
  type TaskDifficulty,
  type TaskValidationState,
} from "@/lib/tasks";

const PAGE_SIZE = 20;

/** 状态筛选的取值：全枚举，按流程先后排。 */
const VALIDATION_STATES: TaskValidationState[] = [
  "DISCOVERED",
  "CANDIDATE",
  "VALIDATING",
  "VALID",
  "INVALID",
  "REVIEW_REQUIRED",
  "QUARANTINED",
];

/**
 * 默认导出只做一件事：把读 `?version=` 的组件包进 Suspense
 * （`useSearchParams` 没有 Suspense 边界时 `next build` 的预渲染会直接失败）。
 */
export default function Page(props: PageProps<"/benchmarks/[slug]">) {
  return (
    <Suspense fallback={<p className="text-sm text-neutral-500">正在加载……</p>}>
      <BenchmarkDetailPage {...props} />
    </Suspense>
  );
}

function BenchmarkDetailPage(props: PageProps<"/benchmarks/[slug]">) {
  const { slug } = use(props.params);
  // 同一个 slug 有多版，列表链接带着 ?version=；没带就看最新已发布那版
  const version = useSearchParams().get("version") ?? undefined;

  const [repo, setRepo] = useState("");
  const [difficulty, setDifficulty] = useState<TaskDifficulty | "">("");
  const [language, setLanguage] = useState<IssueLanguage | "">("");
  const [state, setState] = useState<TaskValidationState | "">("");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);

  const set = useBenchmarkSetDetail(slug, version);

  const tasks = useTasks(
    {
      set: set.data?.id,
      repo: repo === "" ? undefined : repo,
      difficulty: difficulty === "" ? undefined : difficulty,
      language: language === "" ? undefined : language,
      state: state === "" ? undefined : state,
      q: q === "" ? undefined : q,
      limit: PAGE_SIZE,
      offset,
    },
    {
      enabled: set.data !== undefined,
      placeholderData: keepPreviousData,
    },
  );

  if (set.isLoading) {
    return <p className="text-sm text-neutral-500">正在加载……</p>;
  }

  if (set.error) {
    const notFound =
      set.error instanceof ApiError && set.error.status === 404;
    return (
      <div className="mx-auto w-full max-w-5xl">
        <div
          className={
            notFound
              ? "rounded-md border border-amber-200 bg-amber-50 px-4 py-3"
              : "rounded-md border border-red-200 bg-red-50 px-4 py-3"
          }
        >
          <p className={`text-sm ${notFound ? "text-amber-800" : "text-red-700"}`}>
            取不到这个数据集版本。
          </p>
          <p
            className={`mt-1 text-xs leading-relaxed ${
              notFound ? "text-amber-700" : "text-red-600"
            }`}
          >
            {errorMessage(set.error)}
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

  if (set.data === undefined) return null;

  const detail = set.data;
  const status = benchmarkSetStatusText(detail.status);
  const groups = compositionRows(detail.composition, detail.task_count);
  const languageOptions = groups.find((g) => g.key === "language")?.cells ?? [];
  const difficultyOptions = groups.find((g) => g.key === "difficulty")?.cells ?? [];
  const repoOptions = groups.find((g) => g.key === "repository")?.cells ?? [];

  const total = tasks.data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;
  const filtered =
    repo !== "" || difficulty !== "" || language !== "" || state !== "" || q !== "";

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="border-b border-neutral-200 pb-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">{detail.title}</h1>
              <ToneBadge tone={status.tone}>{status.label}</ToneBadge>
            </div>
            <p className="mt-2 font-mono text-xs text-neutral-500">
              {detail.slug}@{detail.version} · {detail.task_count} 题 ·{" "}
              {formatTime(detail.published_at)} 发布
            </p>
            <p className="mt-1 font-mono text-xs text-neutral-400">
              快照指纹 {shortHash(detail.snapshot_digest)}
            </p>
          </div>
          <Link
            href={`/leaderboard?set=${encodeURIComponent(detail.slug)}&version=${encodeURIComponent(detail.version)}`}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-xs text-neutral-700 transition-colors hover:bg-neutral-50"
          >
            看这一版的排行榜 →
          </Link>
        </div>
        {detail.description !== null && (
          <p className="mt-3 text-sm leading-relaxed text-neutral-600">
            {detail.description}
          </p>
        )}
      </header>

      <section className="mt-6">
        <h2 className="mb-3 text-sm font-medium text-neutral-900">构成</h2>
        <CompositionBars groups={groups} taskCount={detail.task_count} />
      </section>

      <section className="mt-6">
        <PublishGate gate={publishGate(detail.publish_evidence)} />
      </section>

      <section className="mt-8">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-neutral-600">
            仓库
            <select
              value={repo}
              onChange={(e) => {
                setRepo(e.target.value);
                setOffset(0);
              }}
              className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
            >
              <option value="">全部</option>
              {repoOptions.map((cell) => (
                <option key={cell.value} value={cell.value}>
                  {cell.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-neutral-600">
            难度
            <select
              value={difficulty}
              onChange={(e) => {
                setDifficulty(e.target.value as TaskDifficulty | "");
                setOffset(0);
              }}
              className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
            >
              <option value="">全部</option>
              {difficultyOptions.map((cell) => (
                <option key={cell.value} value={cell.value}>
                  {cell.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-neutral-600">
            语言
            <select
              value={language}
              onChange={(e) => {
                setLanguage(e.target.value as IssueLanguage | "");
                setOffset(0);
              }}
              className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
            >
              <option value="">全部</option>
              {languageOptions.map((cell) => (
                <option key={cell.value} value={cell.value}>
                  {cell.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-neutral-600">
            状态
            <select
              value={state}
              onChange={(e) => {
                setState(e.target.value as TaskValidationState | "");
                setOffset(0);
              }}
              className="ml-2 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
            >
              <option value="">全部</option>
              {VALIDATION_STATES.map((value) => (
                <option key={value} value={value}>
                  {validationStateText(value).label}
                </option>
              ))}
            </select>
          </label>
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              setQ(qInput.trim());
              setOffset(0);
            }}
          >
            <label className="text-xs text-neutral-600">
              搜索
              <input
                type="text"
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                placeholder="题号或标题"
                className="ml-2 w-44 rounded-md border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900"
              />
            </label>
            <button
              type="submit"
              className="rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50"
            >
              搜索
            </button>
            {(qInput !== "" || q !== "") && (
              <button
                type="button"
                onClick={() => {
                  setQInput("");
                  setQ("");
                  setOffset(0);
                }}
                className="text-xs text-neutral-500 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
              >
                清空
              </button>
            )}
          </form>
          {tasks.data && (
            <span className="text-xs text-neutral-500">
              共 {total} 道{tasks.isFetching && " · 刷新中"}
            </span>
          )}
        </div>

        <div className="mt-4">
          {tasks.error && (
            <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
              <p className="text-sm text-red-700">取不到题目列表。</p>
              <p className="mt-1 text-xs leading-relaxed text-red-600">
                {errorMessage(tasks.error)}
              </p>
            </div>
          )}

          {tasks.data && tasks.data.items.length === 0 && (
            <div className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-center">
              <p className="text-sm text-neutral-600">
                {filtered ? "没有符合条件的题。" : "这一版里还没有题目。"}
              </p>
              {filtered && (
                <p className="mt-1 text-xs text-neutral-500">
                  清空筛选看看 —— 或者换个条件。
                </p>
              )}
            </div>
          )}

          {tasks.data && tasks.data.items.length > 0 && (
            <>
              <div className="overflow-x-auto rounded-md border border-neutral-200 bg-white">
                <table className="w-full">
                  <thead>
                    <tr className="whitespace-nowrap border-b border-neutral-200 bg-neutral-50 text-left">
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        题号
                      </th>
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        标题
                      </th>
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        仓库
                      </th>
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        语言
                      </th>
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        难度
                      </th>
                      <th className="px-3 py-2 text-xs font-medium text-neutral-600">
                        验证状态
                      </th>
                      <th className="px-3 py-2 text-right text-xs font-medium text-neutral-600">
                        F2P / P2P
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.data.items.map((task) => {
                      const validation = validationStateText(task.validation_state);
                      return (
                        <tr
                          key={task.id}
                          className="border-b border-neutral-200 last:border-b-0 hover:bg-neutral-50"
                        >
                          <td className="whitespace-nowrap px-3 py-3 font-mono text-xs">
                            <Link
                              href={`/tasks/${encodeURIComponent(task.task_id)}`}
                              className="text-neutral-700 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                            >
                              {task.task_id}
                            </Link>
                          </td>
                          {/* 标题一行截断、悬停看全：中文题面折成三行会把 41 行的表撑到两屏 */}
                          <td className="px-3 py-3">
                            <Link
                              href={`/tasks/${encodeURIComponent(task.task_id)}`}
                              title={task.issue_title}
                              className="block max-w-xs truncate text-sm text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                            >
                              <IssueTitle title={task.issue_title} />
                            </Link>
                          </td>
                          <td className="whitespace-nowrap px-3 py-3 font-mono text-xs text-neutral-600">
                            {task.repository}
                          </td>
                          <td className="whitespace-nowrap px-3 py-3 text-xs text-neutral-600">
                            {issueLanguageLabel(task.issue_language)}
                          </td>
                          <td className="whitespace-nowrap px-3 py-3 text-xs text-neutral-600">
                            {difficultyLabel(task.difficulty)}
                          </td>
                          <td className="whitespace-nowrap px-3 py-3">
                            <ToneBadge tone={validation.tone}>
                              {validation.label}
                            </ToneBadge>
                          </td>
                          <td className="whitespace-nowrap px-3 py-3 text-right font-mono text-xs text-neutral-600">
                            {task.fail_to_pass_count} / {task.pass_to_pass_count} 条
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

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
            </>
          )}
        </div>
      </section>
    </div>
  );
}
