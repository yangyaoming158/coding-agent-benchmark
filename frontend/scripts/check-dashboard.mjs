#!/usr/bin/env node
/**
 * `src/lib/dashboard.ts` 的断言检查。
 *
 * 首页那几个数字是口径，不是排版：
 * - 哨兵（Oracle / Noop / Mock）不是参赛者，停用的配置也不算；
 * - "几版数据集"只数已发布的，题数按每个数据集最新那版算、旧版不重复计；
 * - "正在跑"只认 RUNNING / QUEUED，DRAFT 不算。
 * 算错了首页照常渲染，只是数字和排行榜、数据集页对不上。
 *
 * 零新依赖：tsc 编译 + 动态 import，同 check-display / check-tasks。
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = join(here, "..");

let failed = 0;
function check(name, actual, expected) {
  const ok =
    Number.isNaN(actual) === Number.isNaN(expected) &&
    JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}`);
  if (!ok) {
    console.log(`        期望 ${JSON.stringify(expected)}`);
    console.log(`        实际 ${JSON.stringify(actual)}`);
  }
}

/** 造一条 Agent 配置：只写关心的字段。 */
function config(agent_kind, agent_display_name, enabled = true) {
  return { agent_kind, agent_display_name, enabled, label: `${agent_display_name}@x` };
}

/** 造一版数据集。 */
function set(id, slug, version, status, task_count, published_at) {
  return { id, slug, version, status, task_count, published_at: published_at ?? null };
}

const outDir = mkdtempSync(join(tmpdir(), "bench-dashboard-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "dashboard.ts"),
      "--outDir",
      outDir,
      "--module",
      "nodenext",
      "--target",
      "es2022",
      "--moduleResolution",
      "nodenext",
      "--skipLibCheck",
    ],
    { stdio: "inherit" },
  );

  const { datasetSummary, isContestant, liveRuns, participantSummary, progressPercent } =
    await import(pathToFileURL(join(outDir, "dashboard.js")).href);

  // ── 参赛者：哨兵和停用的都不算 ──
  check("CLI 且启用 → 参赛者", isContestant(config("CLI", "aider")), true);
  check("CUSTOM（自研）且启用 → 参赛者", isContestant(config("CUSTOM", "MiniAgent")), true);
  check("ORACLE 哨兵不是参赛者", isContestant(config("ORACLE", "Oracle")), false);
  check("NOOP 哨兵不是参赛者", isContestant(config("NOOP", "Noop")), false);
  check("MOCK 不是参赛者", isContestant(config("MOCK", "Mock")), false);
  check("停用的 CLI 不算", isContestant(config("CLI", "aider", false)), false);

  // 开发库 2026-09-21 的真实形状：3 个哨兵 + aider / claude-code 各两条启用配置 + 一条停用 + MiniAgent
  const realish = [
    config("ORACLE", "Oracle 哨兵"),
    config("NOOP", "Noop 哨兵"),
    config("MOCK", "Mock（行为可编程）"),
    config("CLI", "aider"),
    config("CLI", "claude-code"),
    config("CLI", "aider", false),
    config("CUSTOM", "MiniAgent（自研）"),
    config("CLI", "aider"),
    config("CLI", "claude-code"),
  ];
  check("3 个 Agent、5 条启用配置", participantSummary(realish), {
    agents: 3,
    configs: 5,
    names: ["aider", "claude-code", "MiniAgent（自研）"],
  });
  check("一条配置都没有", participantSummary([]), { agents: 0, configs: 0, names: [] });

  // ── 数据集：只数已发布、题数按最新版 ──
  const sets = [
    set(122, "golden", "v1", "DRAFT", 4),
    set(123, "benchmark-dev", "v1", "PUBLISHED", 22, "2026-09-11T00:00:00Z"),
    set(126, "benchmark-cn-v1", "v1", "PUBLISHED", 41, "2026-09-17T00:00:00Z"),
    set(128, "benchmark-cn-v1", "v2", "PUBLISHED", 41, "2026-09-18T00:00:00Z"),
    set(124, "swebench-verified-subset", "v1", "PUBLISHED", 42, "2026-09-16T00:00:00Z"),
    set(127, "swebench-verified-subset", "v3", "PUBLISHED", 75, "2026-09-18T00:00:00Z"),
  ];
  const summary = datasetSummary(sets);
  check("已发布 5 版（DRAFT 的 golden 不算）", summary.versions, 5);
  check("3 个数据集", summary.datasets, 3);
  check("题数按最新版：22 + 41 + 75", summary.latestTasks, 138);
  check(
    "最新版按 slug 排序",
    summary.latest.map((s) => `${s.slug}@${s.version}`),
    ["benchmark-cn-v1@v2", "benchmark-dev@v1", "swebench-verified-subset@v3"],
  );
  check(
    "发布时间相同按 id 取新的",
    datasetSummary([
      set(1, "a", "v1", "PUBLISHED", 10, "2026-09-01T00:00:00Z"),
      set(2, "a", "v2", "PUBLISHED", 12, "2026-09-01T00:00:00Z"),
    ]).latest.map((s) => s.version),
    ["v2"],
  );
  check("没有数据集", datasetSummary([]), { versions: 0, datasets: 0, latestTasks: 0, latest: [] });

  // ── 正在跑：RUNNING / QUEUED，DRAFT 不算 ──
  const runs = [
    { id: 1, status: "RUNNING" },
    { id: 2, status: "QUEUED" },
    { id: 3, status: "DRAFT" },
    { id: 4, status: "COMPLETED" },
  ];
  const live = liveRuns(runs);
  check("RUNNING 一条", live.running.map((r) => r.id), [1]);
  check("QUEUED 一条，DRAFT 不算", live.queued.map((r) => r.id), [2]);

  check("进度 9/41 → 22%", progressPercent({ completed_tasks: 9, total_tasks: 41 }), 22);
  check("进度 41/41 → 100%", progressPercent({ completed_tasks: 41, total_tasks: 41 }), 100);
  check("总数 0 → 0 而不是 NaN", progressPercent({ completed_tasks: 0, total_tasks: 0 }), 0);
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
