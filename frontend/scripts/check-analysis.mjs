#!/usr/bin/env node
/**
 * `src/lib/analysis.ts` 的断言检查。
 *
 * 失败分析页不重算计数（后端 `failure_summary()` 算），但把分布摊成堆叠柱、把热力图
 * 摊成矩阵这一步仍是口径：
 * - "还没结论"的失败不进任何类别，单独一个数；
 * - 每根柱的分段之和 = category_counts 那个数（折"其他"时不能漏）；
 * - 热力图没数据的格是 null 不是 0；深浅按占该 Agent 失败的比例分，不按绝对数；
 * - 参赛者颜色按字母序固定，不按名次；第六个起折进"其他"，不生成新颜色；
 * - NEEDS_HUMAN 的案例是黄不是红。
 *
 * 零新依赖：tsc 编译 + 动态 import，同 check-dashboard。
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
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}`);
  if (!ok) {
    console.log(`        期望 ${JSON.stringify(expected)}`);
    console.log(`        实际 ${JSON.stringify(actual)}`);
  }
}

const cell = (agent_label, category, count) => ({ agent_label, category, count });

const outDir = mkdtempSync(join(tmpdir(), "bench-analysis-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "analysis.ts"),
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
    AGENT_COLORS,
    OTHER_AGENTS,
    agentOrder,
    caseTone,
    categoryShort,
    cellShade,
    coverage,
    heatmapMatrix,
    presentCategories,
    stackMismatches,
    stackSeries,
    stackedRows,
  } = await import(pathToFileURL(join(outDir, "analysis.js")).href);

  // 照开发库 2026-09-22 的样子造：中文集两轮，三个参赛者
  const summary = {
    total_failures: 10,
    attributed_failures: 8,
    unattributed_failures: 2,
    llm_attributed_failures: 3,
    needs_human_failures: 1,
    category_counts: { F4_INCORRECT_LOGIC: 3, F6_REGRESSION: 1, F7_EMPTY_OR_INVALID_PATCH: 4 },
    heatmap: [
      cell("miniagent@m", "F4_INCORRECT_LOGIC", 1),
      cell("aider@m", "F4_INCORRECT_LOGIC", 2),
      cell("claude-code@m", "F6_REGRESSION", 1),
      cell("aider@m", "F7_EMPTY_OR_INVALID_PATCH", 3),
      cell("miniagent@m", "F7_EMPTY_OR_INVALID_PATCH", 1),
    ],
  };

  // ── 覆盖：未归因单独一个数，不进类别 ──
  check(
    "覆盖数：总 / 已归因 / 未归因 / 规则 / LLM / 要人看",
    coverage(summary),
    { total: 10, attributed: 8, unattributed: 2, rule: 5, llm: 3, needsHuman: 1 },
  );
  check(
    "旧后端没有 llm / needs_human 字段时按 0 算，不是 NaN",
    coverage({ total_failures: 3, attributed_failures: 1, unattributed_failures: 2 }),
    { total: 3, attributed: 1, unattributed: 2, rule: 1, llm: 0, needsHuman: 0 },
  );
  check(
    "类别列表里没有未归因这一项",
    presentCategories(summary),
    ["F4_INCORRECT_LOGIC", "F6_REGRESSION", "F7_EMPTY_OR_INVALID_PATCH"],
  );
  check(
    "类别按协议编号排，枚举外的排最后",
    presentCategories({ category_counts: { ZZ_NEW: 1, F1_REQUIREMENT_MISUNDERSTANDING: 1, N1_INFRASTRUCTURE_FAILURE: 2 } }),
    ["F1_REQUIREMENT_MISUNDERSTANDING", "N1_INFRASTRUCTURE_FAILURE", "ZZ_NEW"],
  );
  check("计数为 0 的类别不画空柱", presentCategories({ category_counts: { F6_REGRESSION: 0 } }), []);
  check("短名", categoryShort("F7_EMPTY_OR_INVALID_PATCH"), "F7");
  check("短名：没有下划线原样", categoryShort("weird"), "weird");

  // ── 参赛者顺序与颜色：按字母序固定，不按名次 ──
  check("参赛者按字母序", agentOrder(summary.heatmap), ["aider@m", "claude-code@m", "miniagent@m"]);
  check(
    "三个参赛者三种颜色，按顺序分配",
    stackSeries(["aider@m", "claude-code@m", "miniagent@m"]).map((s) => s.color),
    [AGENT_COLORS[0], AGENT_COLORS[1], AGENT_COLORS[2]],
  );
  const six = ["a", "b", "c", "d", "e", "f"];
  check(
    "第六个参赛者折进「其他」，不生成新颜色",
    stackSeries(six).map((s) => s.key),
    ["a", "b", "c", "d", "e", OTHER_AGENTS],
  );

  // ── 堆叠柱：分段之和 = category_counts ──
  const rows = stackedRows(summary);
  check(
    "堆叠柱：F4 分段",
    rows[0].counts,
    { "aider@m": 2, "claude-code@m": 0, "miniagent@m": 1 },
  );
  check("堆叠柱：每根柱的总数 = category_counts", rows.map((r) => r.total), [3, 1, 4]);
  check("堆叠柱和 category_counts 一致", stackMismatches(rows), []);
  const folded = stackedRows({
    category_counts: { F6_REGRESSION: 6 },
    heatmap: six.map((agent) => cell(agent, "F6_REGRESSION", 1)),
  });
  check("折「其他」之后分段之和仍 = 总数", stackMismatches(folded), []);
  check("折「其他」：第六个的 1 条进了其他", folded[0].counts[OTHER_AGENTS], 1);
  check(
    "热力图少算了会被抓出来",
    stackMismatches([{ category: "F6_REGRESSION", short: "F6", label: "x", total: 5, counts: { a: 4 } }]),
    ["F6_REGRESSION"],
  );

  // ── 热力图矩阵 ──
  const matrix = heatmapMatrix(summary);
  check("热力图行（参赛者）", matrix.agents, ["aider@m", "claude-code@m", "miniagent@m"]);
  check(
    "热力图格子：没数据是 null 不是 0",
    matrix.cells,
    [
      [2, null, 3],
      [null, 1, null],
      [1, null, 1],
    ],
  );
  check("行合计", matrix.rowTotals, [5, 1, 2]);
  check("列合计 = category_counts", matrix.columnTotals, [3, 1, 4]);
  check("最大格", matrix.max, 3);

  // ── 深浅按占该 Agent 失败的比例 ──
  check("null → 0 档", cellShade(null, 10), 0);
  check("0 条 → 0 档", cellShade(0, 10), 0);
  check("1/10 → 1 档", cellShade(1, 10), 1);
  check("2/10 → 2 档", cellShade(2, 10), 2);
  check("3/10 → 3 档", cellShade(3, 10), 3);
  check("5/10 → 4 档", cellShade(5, 10), 4);
  check("1/1 → 4 档：少失败的 Agent 集中在一类也要看得出", cellShade(1, 1), 4);

  // ── 案例色调 ──
  check("没结论 → 中性", caseTone({ category: null, attribution_status: null }), { tone: "neutral", label: "还没结论" });
  check(
    "NEEDS_HUMAN → 黄，不是红",
    caseTone({ category: "F4_INCORRECT_LOGIC", attribution_status: "NEEDS_HUMAN" }),
    { tone: "warn", label: "F4 · 实现逻辑错误" },
  );
  check(
    "F 类 OK → 红",
    caseTone({ category: "F6_REGRESSION", attribution_status: "OK" }),
    { tone: "bad", label: "F6 · 引入回归" },
  );
  check(
    "N 类 → 黄",
    caseTone({ category: "N1_INFRASTRUCTURE_FAILURE", attribution_status: "OK" }),
    { tone: "warn", label: "N1 · 平台故障" },
  );
  check(
    "枚举外的类别 → 中性、原样显示",
    caseTone({ category: "ZZ_NEW", attribution_status: "OK" }),
    { tone: "neutral", label: "ZZ_NEW" },
  );
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
