"use client";

import { use } from "react";
import Link from "next/link";
import { ConfirmAction } from "@/components/confirm-action";
import { ProgressBar, StatusBadge } from "@/components/run-status";
import { Stat } from "@/components/stat";
import { TaskGrid } from "@/components/task-grid";
import { errorMessage } from "@/lib/api";
import {
  formatCost,
  formatDuration,
  formatRate,
  formatTime,
  formatTokens,
  isLiveRun,
  runStatusTone,
} from "@/lib/display";
import { useAdminToken } from "@/lib/admin-token";
import { useAllRunTaskRuns, useCancelRun, useRetryFailed, useRun } from "@/lib/queries";
import { AdminTokenField } from "@/components/admin-token-field";

/** 详情页的轮询间隔。§16.1 给 Run Detail 定的就是 3s —— 进度要看着在动。 */
const POLL_MS = 3000;

/**
 * 单次实验运行详情。
 *
 * 这一页要回答的是"这一轮跑到哪了、跑成什么样"。**为什么失败**在下一跳 ——
 * 网格里每个格子都链到 `/task-runs/{id}`，那是 §16.3 要的"3 次点击到证据"的终点。
 *
 * Next 16 的 `params` 是 Promise。这里是客户端组件，用 React 的 `use()` 解包，
 * 而不是把页面拆成"服务端壳 + 客户端组件"两个文件 —— 数据全部来自 API，
 * 壳上没有任何服务端才拿得到的东西。
 */
export default function RunDetailPage(props: PageProps<"/runs/[id]">) {
  const { id } = use(props.params);
  const runId = Number(id);

  const run = useRun(runId, {
    refetchInterval: (query) =>
      query.state.data && isLiveRun(query.state.data.status) ? POLL_MS : false,
  });

  // 实验状态一到终态就固定了，逐题记录也不会再变 —— 跟着一起停轮询。
  // 用 run 的状态决定，而不是各自判断：两个请求必须同进同退，
  // 否则会出现"头部说已完成、网格还在转"的自相矛盾画面。
  // 逐题记录翻页拉全（100 题 × 重试会超过后端单页上限 200），实验不存在时不发。
  const live = run.data !== undefined && isLiveRun(run.data.status);
  const taskRuns = useAllRunTaskRuns(
    runId,
    {},
    { refetchInterval: live ? POLL_MS : false, enabled: run.data !== undefined },
  );

  const cancel = useCancelRun(runId);
  const retry = useRetryFailed(runId);
  const [token] = useAdminToken();

  if (run.isLoading) {
    return <p className="text-sm text-neutral-500">正在加载……</p>;
  }

  if (run.error) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm text-red-700">{errorMessage(run.error)}</p>
          <Link
            href="/runs"
            className="mt-2 inline-block text-xs text-red-700 underline underline-offset-2"
          >
            回到实验列表
          </Link>
        </div>
      </div>
    );
  }

  if (!run.data) return null;
  const detail = run.data;

  const finished =
    detail.status === "COMPLETED" ||
    detail.status === "PARTIAL" ||
    detail.status === "FAILED" ||
    detail.status === "CANCELLED";

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="border-b border-neutral-200 pb-6">
        <Link
          href="/runs"
          className="text-xs text-neutral-500 transition-colors hover:text-neutral-900"
        >
          ← 实验运行
        </Link>
        <div className="mt-2 flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">
                {detail.name}
              </h1>
              <StatusBadge status={detail.status} />
            </div>
            <p className="mt-1 font-mono text-xs text-neutral-400">
              #{detail.id} · {detail.benchmark_set} ·{" "}
              {detail.agent_config_label} · 协议 {detail.protocol_version} ·{" "}
              {/* dirty=false 也要写出来：结果绑定一个干净的 git commit 是"可复现"的前提（C-27/C-28），
                  演示时要指得到；true 的情况下面另有黄框说明 */}
              <span title="跑的时候工作区有没有未提交改动（C-27）；有的话结果不得进排行榜（C-28）">
                {detail.dirty ? "dirty=true" : "工作区干净（dirty=false）"}
              </span>
            </p>
          </div>
          <div className="shrink-0 text-right">
            <ProgressBar
              completed={detail.completed_tasks}
              total={detail.total_tasks}
              tone={runStatusTone(detail.status)}
            />
            <p className="mt-1 text-xs text-neutral-400">
              Agent 并发 {detail.agent_concurrency} · 沙箱并发 {detail.sandbox_concurrency}
            </p>
          </div>
        </div>
      </header>

      {(detail.dirty || detail.leaderboard_excluded_reason !== null) && (
        <div className="mt-4 space-y-1 rounded-md border border-amber-200 bg-amber-50 px-4 py-3">
          {detail.dirty && (
            <p className="text-xs leading-relaxed text-amber-800">
              跑的时候工作区有未提交改动，结果标记为 dirty，
              <strong>不得进排行榜</strong>（协议 C-28）。
            </p>
          )}
          {detail.leaderboard_excluded_reason !== null && (
            <p className="text-xs leading-relaxed text-amber-800">
              已被排除出排行榜：{detail.leaderboard_excluded_reason}
            </p>
          )}
        </div>
      )}

      <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="已解决"
          value={`${detail.resolved_count} / ${detail.total_tasks}`}
          hint={`还有 ${detail.total_tasks - detail.completed_tasks} 道题没定出结论`}
        />
        <Stat
          label="严格解决率"
          value={formatRate(detail.strict_resolve_rate)}
          hint={`有效解决率 ${formatRate(detail.effective_resolve_rate)}（分母剔除平台故障）`}
        />
        <Stat label="成本" value={formatCost(detail.total_cost_usd)} />
        <Stat
          label="耗时"
          value={formatDuration(detail.makespan_ms)}
          hint={`${formatTokens(detail.total_tokens)} tokens`}
        />
        {/* value 是最终还挂着的平台故障数（重试救回的不算，C-25）；
            过程中发生过几次另写在 hint 里，不然"0 · 其中 2 次被救回"读起来自相矛盾 */}
        <Stat
          label="平台故障"
          value={detail.infra_failure_count}
          hint={
            detail.recovered_infra_failure_count > 0
              ? `最终 ${detail.infra_failure_count} · 过程中 ${detail.recovered_infra_failure_count} 次被重试救回（C-56）`
              : undefined
          }
        />
        <Stat label="重试次数" value={detail.retry_count} />
        <Stat label="创建时间" value={formatTime(detail.created_at)} />
        <Stat
          label="结束时间"
          value={formatTime(detail.finished_at)}
          hint={detail.created_by ? `发起人 ${detail.created_by}` : undefined}
        />
      </section>

      <section className="mt-6">
        <AdminTokenField hint="取消和重试都是写接口，要带令牌；只存在本标签页。" />
      </section>

      <section className="mt-4 flex flex-wrap items-start gap-4">
        <div>
          <ConfirmAction
            label="取消实验"
            warning="未领走的作业会被丢弃，已经在跑的题会被 Worker 停掉。这一步不可撤销。"
            confirmText="确认取消"
            tone="danger"
            disabled={!live}
            disabledReason="实验已经结束，没什么可取消的"
            isPending={cancel.isPending}
            error={cancel.error ? errorMessage(cancel.error) : null}
            onConfirm={() => cancel.mutate(token)}
          />
          {cancel.isSuccess && (
            <p className="mt-2 text-xs text-neutral-600">
              {cancel.data.already_cancelled
                ? "这次实验之前就已经取消了。"
                : `已取消：丢弃 ${cancel.data.dropped_jobs} 个没领走的作业，${cancel.data.in_flight_jobs} 个在跑。`}
            </p>
          )}
        </div>

        <div>
          <ConfirmAction
            label="重试失败项"
            warning="把失败的题重新投进队列，会消耗新的模型额度。已经定出结论的题不会被重投。"
            confirmText="确认重试"
            tone="neutral"
            disabled={!finished}
            disabledReason="等实验跑完再重试"
            isPending={retry.isPending}
            error={retry.error ? errorMessage(retry.error) : null}
            onConfirm={() => retry.mutate(token)}
          />
          {retry.isSuccess && (
            <p className="mt-2 text-xs leading-relaxed text-neutral-600">
              重投 {retry.data.requeued.length} 道；
              {retry.data.at_attempt_cap.length > 0 &&
                `${retry.data.at_attempt_cap.length} 道已到重试上限；`}
              {retry.data.already_decided > 0 &&
                `${retry.data.already_decided} 道已有结论；`}
              {retry.data.still_running > 0 &&
                `${retry.data.still_running} 道还在跑。`}
            </p>
          )}
        </div>
      </section>

      <section className="mt-8">
        <h2 className="text-sm font-semibold text-neutral-900">逐题结果</h2>
        <p className="mt-1 text-xs text-neutral-500">
          点任意一格看那道题的补丁、用例和日志。
        </p>
        <div className="mt-3">
          {taskRuns.isLoading && (
            <p className="text-sm text-neutral-500">正在加载……</p>
          )}
          {taskRuns.error && (
            <p className="text-sm text-red-600">
              {errorMessage(taskRuns.error)}
            </p>
          )}
          {taskRuns.data && (
            <TaskGrid
              taskRuns={taskRuns.data.items}
              totalTasks={detail.total_tasks}
              runFinished={finished}
            />
          )}
        </div>
      </section>

      <section className="mt-8">
        <details className="rounded-md border border-neutral-200 bg-white">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-neutral-700">
            可复现性清单
          </summary>
          <div className="border-t border-neutral-200 px-4 py-3">
            <p className="text-xs leading-relaxed text-neutral-500">
              建实验时冻结的代码版本、镜像、参数。协议 C-27 要求它能唯一代表这次跑的代码状态。
            </p>
            <pre className="mt-2 max-h-80 overflow-auto rounded bg-neutral-50 p-3 font-mono text-xs text-neutral-700">
              {JSON.stringify(detail.manifest, null, 2)}
            </pre>
          </div>
        </details>
      </section>
    </div>
  );
}
