#!/usr/bin/env node
/**
 * `src/lib/review-metrics.ts` 的断言检查（E6-T4）。
 *
 * 混淆矩阵只列出真实出现过的类别（不是全部十个类别都摆一遍），行列口径是
 * "自动判的类别 × 人工判的类别"，算错了会把 F1/F4 这种常见的混淆对调。
 *
 * 零新依赖：tsc 编译 + 动态 import，同 check-dashboard / check-analysis。
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

function cell(automatic_category, human_category, count) {
  return { automatic_category, human_category, count };
}

const outDir = mkdtempSync(join(tmpdir(), "bench-review-metrics-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "review-metrics.ts"),
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

  const { buildConfusionMatrix, formatRatePercent } = await import(
    pathToFileURL(join(outDir, "review-metrics.js")).href
  );

  check("没有标注时不列出任何类别", buildConfusionMatrix([]).automaticCategories, []);

  const matrix = buildConfusionMatrix([
    cell("F1_REQUIREMENT_MISUNDERSTANDING", "F1_REQUIREMENT_MISUNDERSTANDING", 2),
    cell("F1_REQUIREMENT_MISUNDERSTANDING", "F4_INCORRECT_LOGIC", 1),
    cell("F3_INCOMPLETE_FIX", "F3_INCOMPLETE_FIX", 5),
  ]);
  check("只列出出现过的自动类别（不是十个类别摆全）", matrix.automaticCategories, [
    "F1_REQUIREMENT_MISUNDERSTANDING",
    "F3_INCOMPLETE_FIX",
  ]);
  check("只列出出现过的人工类别", matrix.humanCategories, [
    "F1_REQUIREMENT_MISUNDERSTANDING",
    "F3_INCOMPLETE_FIX",
    "F4_INCORRECT_LOGIC",
  ]);
  check(
    "对角线格子（F1 判 F1）",
    matrix.countAt("F1_REQUIREMENT_MISUNDERSTANDING", "F1_REQUIREMENT_MISUNDERSTANDING"),
    2,
  );
  check(
    "非对角格子（F1 自动判成 F4 人工）",
    matrix.countAt("F1_REQUIREMENT_MISUNDERSTANDING", "F4_INCORRECT_LOGIC"),
    1,
  );
  check("没出现过的组合是 0，不是 undefined", matrix.countAt("F3_INCOMPLETE_FIX", "F1_REQUIREMENT_MISUNDERSTANDING"), 0);
  check("行合计：F1 这一行 2+1=3", matrix.rowTotal("F1_REQUIREMENT_MISUNDERSTANDING"), 3);
  check("没出现过的自动类别行合计是 0", matrix.rowTotal("F8_AGENT_TOOL_OR_BUDGET_FAILURE"), 0);

  check("null 显示成短横线，不是 0%", formatRatePercent(null), "—");
  check("0.667 显示成 66.7%", formatRatePercent(2 / 3), "66.7%");
  check("1.0 显示成 100.0%", formatRatePercent(1), "100.0%");

  if (failed > 0) {
    console.error(`\n${failed} 条断言失败`);
    process.exit(1);
  }
  console.log("\n全部通过");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
