#!/usr/bin/env node
/**
 * `src/lib/leaderboard.ts` 的断言检查。
 *
 * 这个模块编码的是"排行榜的数字该怎么读"，不是排版：
 * - 报不出成本 ≠ 最便宜（成本为 null 的行不许进散点图，否则画在 x=0 上）；
 * - 没数据 ≠ 全挂（分面矩阵缺格留 null，不许填 0）；
 * - 单轮没有"轮间"（run_count < 2 不谈抖动）；
 * - 平台故障率的分母是"已认定题数"（C-22 要求与解决率同行展示）。
 * 判错了页面照常渲染，只是给出一个看着合理但错的结论 —— 只能靠断言钉死。
 *
 * 零新依赖：tsc 编译 + 动态 import，同 check-display / check-task-detail。
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
  // JSON.stringify(NaN) === "null"：不先拦一下，NaN 会被当成 null 蒙混过关
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

/** 造一行榜单数据：只写关心的字段，其余给能用的默认值。 */
function row(overrides = {}) {
  return {
    rank: 1,
    agent_config_id: 1,
    label: "aider",
    agent_name: "aider",
    agent_display_name: "Aider",
    agent_version: "1.0",
    model_name: "deepseek/deepseek-chat",
    protocol_version: "v1.2",
    run_count: 1,
    run_ids: [1],
    tasks_per_run: 22,
    resolve_rate_mean: "0.5",
    resolve_rate_min: "0.5",
    resolve_rate_max: "0.5",
    resolve_rate_spread: "0",
    effective_resolve_rate_mean: "0.5",
    resolved_mean: "11",
    cost_usd_total: "0.30",
    cost_per_task: "0.0136",
    cost_lower_bound: false,
    cost_reported_attempts: 1,
    cost_estimated_attempts: 0,
    cost_unavailable_attempts: 0,
    total_tokens: 1000,
    tokens_per_task: 45,
    makespan_ms_mean: 60000,
    infra_failure_total: 0,
    retry_total: 0,
    facets: [],
    ...overrides,
  };
}

function cell(value, resolved, total) {
  return {
    value,
    resolved,
    total,
    resolve_rate: total === 0 ? null : String(resolved / total),
  };
}

const outDir = mkdtempSync(join(tmpdir(), "bench-leaderboard-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "leaderboard.ts"),
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

  const {
    METRIC_LABELS,
    FACET_LABELS,
    facetValueLabel,
    facetValueOrder,
    facetMatrix,
    scatterPoints,
    spreadRange,
    infraFailureRate,
    costNote,
  } = await import(pathToFileURL(join(outDir, "leaderboard.js")).href);

  // ── 标签 ──
  check("四个排序口径都有中文名", Object.keys(METRIC_LABELS), [
    "resolve_rate",
    "cost",
    "duration",
    "tokens",
  ]);
  check("三个分面维度都有中文名", Object.keys(FACET_LABELS), [
    "difficulty",
    "language",
    "repository",
  ]);

  // ── 分面取值标签：认识的原值给中文，不认识的原样返回 ──
  check("难度 easy → 简单", facetValueLabel("difficulty", "easy"), "简单");
  check("难度 hard → 困难", facetValueLabel("difficulty", "hard"), "困难");
  check("语言 mixed → 中英混合", facetValueLabel("language", "mixed"), "中英混合");
  check("仓库名原样透出", facetValueLabel("repository", "pallets/click"), "pallets/click");
  check("认不出的取值原样返回", facetValueLabel("difficulty", "future-level"), "future-level");

  // ── 矩阵行序：难度从易到难，不认识的排后面按字母 ──
  check(
    "难度排序不是字母序",
    facetValueOrder("difficulty", ["hard", "easy", "medium"]),
    ["easy", "medium", "hard"],
  );
  check(
    "不认识的取值排在认识的后",
    facetValueOrder("difficulty", ["zzz", "hard", "easy"]),
    ["easy", "hard", "zzz"],
  );
  check(
    "仓库按字母序",
    facetValueOrder("repository", ["b/b", "a/a"]),
    ["a/a", "b/b"],
  );

  // ── 分面矩阵：缺格留 null，不填 0 ──
  const r1 = row({
    agent_config_id: 1,
    label: "aider",
    facets: [cell("easy", 4, 5), cell("hard", 1, 5)],
  });
  const r2 = row({
    agent_config_id: 2,
    label: "claude-code",
    facets: [cell("easy", 1, 5), cell("medium", 0, 5)],
  });
  const matrix = facetMatrix([r1, r2], "difficulty");
  check("矩阵的取值行按难度序", matrix.values, ["easy", "medium", "hard"]);
  check("第 1 列（aider）缺 medium 是 null", matrix.cells[0][1], null);
  check("第 2 列（claude-code）缺 hard 是 null", matrix.cells[1][2], null);
  check("easy 格子的解决数", matrix.cells[0][0].resolved, 4);
  check("空榜单矩阵为空", facetMatrix([], "difficulty"), { values: [], cells: [] });
  check(
    "行非空但分面全空 → 取值行也为空",
    facetMatrix([row({ facets: [] })], "difficulty").values,
    [],
  );

  // ── 散点取点：成本为 null 的行不许进图 ──
  const priced = row({ cost_per_task: "0.0175", resolve_rate_mean: "0.1364" });
  const unpriced = row({ agent_config_id: 9, label: "claude-code", cost_per_task: null });
  const noRate = row({ agent_config_id: 8, label: "broken", resolve_rate_mean: null });
  const taken = scatterPoints([priced, unpriced, noRate]);
  check("只有成本与解决率都有的行进散点", taken.points.length, 1);
  check("进图的行取到数值", [taken.points[0].cost, taken.points[0].rate], [0.0175, 0.1364]);
  check("被跳过的行原样带出（给页面列名）", taken.skipped.map((r) => r.label), [
    "claude-code",
    "broken",
  ]);
  check("成本 0 是合法值、进图", scatterPoints([row({ cost_per_task: "0" })]).points.length, 1);

  // 部分 attempt 报不出成本 → 后端给下界 + cost_lower_bound=true：进图，但要带着标记出去
  const partial = scatterPoints([
    row({ cost_per_task: "0.0102", cost_lower_bound: true, cost_unavailable_attempts: 2 }),
  ]);
  check("下界成本进图", partial.points.length, 1);
  check("下界标记带出去", partial.points[0].lowerBound, true);
  check("完整成本不标下界", scatterPoints([row()]).points[0].lowerBound, false);
  check(
    "成本是垃圾字符串 → 进跳过名单",
    scatterPoints([row({ cost_per_task: "abc" })]).skipped.length,
    1,
  );

  // ── 轮间范围：单轮不谈抖动 ──
  check("单轮没有轮间范围", spreadRange(row({ run_count: 1 })), null);
  check(
    "两轮给出 min–max 原值",
    spreadRange(row({ run_count: 2, resolve_rate_min: "0.19", resolve_rate_max: "0.28" })),
    { min: "0.19", max: "0.28" },
  );
  check(
    "两轮但 min/max 缺失也是 null",
    spreadRange(row({ run_count: 2, resolve_rate_min: null, resolve_rate_max: null })),
    null,
  );

  // ── 平台故障率：分母 = 每题 × 轮数 ──
  check(
    "2 轮 22 题 2 个故障 → 2/44",
    infraFailureRate(row({ run_count: 2, tasks_per_run: 22, infra_failure_total: 2 })),
    2 / 44,
  );
  check("没有题时不给率", infraFailureRate(row({ tasks_per_run: 0, run_count: 0 })), null);

  // ── 成本来源提示 ──
  check("全部自报 → 没有提示", costNote(row()), null);
  const unavailable = costNote(row({ cost_unavailable_attempts: 3, cost_per_task: null }));
  check("有报不出成本 → 警示", unavailable.warn, true);
  check("提示里写清次数", unavailable.text.includes("3 次未报出成本"), true);
  const estimated = costNote(row({ cost_estimated_attempts: 2 }));
  check("只有估算 → 不警示但注明", [estimated.warn, estimated.text.includes("2 次为估算")], [
    false,
    true,
  ]);
  const both = costNote(row({ cost_unavailable_attempts: 1, cost_estimated_attempts: 4 }));
  check(
    "两者都有时以报不出为主、估算照写",
    [both.warn, both.text.includes("1 次未报出成本"), both.text.includes("4 次为估算")],
    [true, true, true],
  );
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

if (failed > 0) {
  console.log(`\n${failed} 条不通过`);
  process.exit(1);
}
console.log("\n全部通过");
