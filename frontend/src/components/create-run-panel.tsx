"use client";

import { useState } from "react";
import Link from "next/link";
import { AdminTokenField } from "@/components/admin-token-field";
import { useAdminToken } from "@/lib/admin-token";
import { errorMessage } from "@/lib/api";
import { useAgentConfigs, useBenchmarkSets, useCreateRun } from "@/lib/queries";

/**
 * 新建实验。
 *
 * 只放三个必填项和一个可选项。并发（`agent_concurrency` / `sandbox_concurrency`）
 * **刻意不在界面上**：它们的默认值来自 `.env`，是按本机实测调出来的
 * （`18-perf-report.md` 那套容量模型），随手在界面上改一个数字就会让
 * 跑出来的性能数字没法和其他实验比。要改并发应该改配置，不是点一下。
 *
 * 令牌从 `lib/admin-token.ts` 取（浏览器里输一次、本标签页内通用），提交时带上。
 * 没设令牌照样让它提交，后端回 401，原话显示出来。
 *
 * 提交失败时**原样显示后端那句话**。最典型的是协议 C-27：工作区有未提交改动，
 * 后端返回 409 `WORKSPACE_DIRTY`。前端不该替用户判断"这个应该是能跑的" ——
 * 后端才是权威，它说不行就是不行，把理由端上来即可。
 */
export function CreateRunPanel({ onClose }: { onClose: () => void }) {
  const [setId, setSetId] = useState("");
  const [configId, setConfigId] = useState("");
  const [rounds, setRounds] = useState("1");
  const [name, setName] = useState("");

  const sets = useBenchmarkSets({ limit: 50 });
  const configs = useAgentConfigs();
  const create = useCreateRun();
  const [token] = useAdminToken();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    create.mutate({
      body: {
        benchmark_set_id: Number(setId),
        agent_config_id: Number(configId),
        rounds: Number(rounds),
        name: name.trim() === "" ? null : name.trim(),
      },
      token,
    });
  }

  const ready = setId !== "" && configId !== "" && !create.isPending;

  return (
    <section className="mt-6 rounded-md border border-neutral-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <h2 className="text-sm font-semibold text-neutral-900">新建实验</h2>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-neutral-500 transition-colors hover:text-neutral-900"
        >
          收起
        </button>
      </div>

      <form onSubmit={submit} className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-neutral-600">
          数据集
          <select
            value={setId}
            onChange={(e) => setSetId(e.target.value)}
            className="mt-1 w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm text-neutral-900"
          >
            <option value="">选择一版数据集……</option>
            {sets.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slug}@{s.version} · {s.task_count} 题 · {s.status}
              </option>
            ))}
          </select>
          {sets.data && sets.data.items.length === 0 && (
            <span className="mt-1 block text-neutral-500">
              库里还没有数据集版本。先跑 <code className="font-mono">make seed</code>。
            </span>
          )}
        </label>

        <label className="text-xs text-neutral-600">
          参赛者
          <select
            value={configId}
            onChange={(e) => setConfigId(e.target.value)}
            className="mt-1 w-full rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm text-neutral-900"
          >
            <option value="">选择一条配置……</option>
            {configs.data?.items
              .filter((c) => c.enabled)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}（{c.agent_display_name}）
                </option>
              ))}
          </select>
        </label>

        <label className="text-xs text-neutral-600">
          轮数
          <input
            type="number"
            min={1}
            max={10}
            value={rounds}
            onChange={(e) => setRounds(e.target.value)}
            className="mt-1 w-full rounded-md border border-neutral-300 px-2 py-1.5 text-sm text-neutral-900"
          />
          <span className="mt-1 block text-neutral-500">
            同一份题跑几轮，用于看抖动。多轮会建出多个独立实验。
          </span>
        </label>

        <label className="text-xs text-neutral-600">
          名称（可留空）
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="留空则自动取「数据集 × 参赛者」"
            className="mt-1 w-full rounded-md border border-neutral-300 px-2 py-1.5 text-sm text-neutral-900 placeholder:text-neutral-400"
          />
        </label>

        <div className="sm:col-span-2">
          <AdminTokenField />
        </div>

        <div className="sm:col-span-2">
          <button
            type="submit"
            disabled={!ready}
            className="rounded-md bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:bg-neutral-400"
          >
            {create.isPending ? "正在建……" : "建实验并投队列"}
          </button>
          <span className="ml-3 text-xs text-neutral-500">
            只投作业，不会立刻开跑 —— 要有 Worker 在跑才会执行。
          </span>
        </div>
      </form>

      {create.isError && (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2">
          <p className="text-xs leading-relaxed text-red-700">
            {errorMessage(create.error)}
          </p>
        </div>
      )}

      {create.isSuccess && (
        <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2">
          <p className="text-xs text-emerald-800">
            已建 {create.data.runs.length} 个实验，共 {create.data.task_count}{" "}
            道题：
          </p>
          <p className="mt-1 flex flex-wrap gap-2">
            {create.data.runs.map((run) => (
              <Link
                key={run.id}
                href={`/runs/${run.id}`}
                className="font-mono text-xs text-emerald-800 underline decoration-emerald-400 underline-offset-2"
              >
                #{run.id}
              </Link>
            ))}
          </p>
        </div>
      )}
    </section>
  );
}
