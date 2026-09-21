#!/usr/bin/env node
/**
 * `src/lib/display.ts` 的断言检查。
 *
 * 为什么单独给这一个模块写检查：它编码的是**协议语义**，不是排版。
 * `cellVerdict()` 要判定"这道题算不算 AI 的锅" —— 判错了，平台最核心的那个
 * 数字（解决率的分母）就是错的，而且错得没有任何症状：界面照常渲染，
 * 只是把环境的问题记在了 AI 头上。这类错误靠肉眼看截图发现不了。
 *
 * 不引测试框架：仓库里还没有前端测试，装一套 vitest 得改 package.json、
 * 加配置、进 CI，而这里要验的是纯函数。用仓库自带的 tsc 把文件编成 JS 再断言，
 * 零新依赖，`npm run check:display` 就能跑。
 *
 * 什么时候该把它换掉：前端一旦有了正经的测试框架（比如 E7-T3 要给 diff
 * 渲染写组件测试），这个脚本应该并进去，而不是两套并存。
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

const outDir = mkdtempSync(join(tmpdir(), "bench-display-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "display.ts"),
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
    cellVerdict,
    evidenceView,
    failureCategoryLabel,
    failureCategoryTone,
    FAILURE_CATEGORIES,
    formatConfidence,
    formatCost,
    formatDuration,
    formatRate,
    formatTokens,
    isLiveRun,
  } = await import(pathToFileURL(join(outDir, "display.js")).href);

  const base = { infra_outcome: "SUCCESS", agent_outcome: null, error_code: null };

  // ── 判定：重点是"平台故障"的边界 ──
  check(
    "跑完且 AI 修好了 → 已解决",
    cellVerdict({ ...base, lifecycle_status: "COMPLETED", agent_outcome: "RESOLVED" }),
    { bucket: "resolved", tone: "ok", label: "已解决" },
  );

  // 这两条是整份检查里最要紧的：C-20 第 4 步说"只有打了 AI 补丁才超时"算 AI 的锅，
  // 此时 lifecycle 是 COMPLETED 而 infra_outcome 不是 SUCCESS。
  // 拿 infra_outcome 去判就会把 AI 的锅甩给环境，把解决率做好看。
  check(
    "COMPLETED + AGENT_TIMEOUT 仍是 AI 的锅 → 未解决",
    cellVerdict({
      ...base,
      lifecycle_status: "COMPLETED",
      infra_outcome: "AGENT_TIMEOUT",
      agent_outcome: "UNRESOLVED",
    }),
    { bucket: "unresolved", tone: "bad", label: "未解决" },
  );
  check(
    "COMPLETED + TEST_TIMEOUT + 空补丁 → 未解决",
    cellVerdict({
      ...base,
      lifecycle_status: "COMPLETED",
      infra_outcome: "TEST_TIMEOUT",
      agent_outcome: "EMPTY_PATCH",
    }),
    { bucket: "unresolved", tone: "bad", label: "空补丁" },
  );

  check(
    "FAILED + OOM → 平台故障，带细节",
    cellVerdict({
      ...base,
      lifecycle_status: "FAILED",
      infra_outcome: "OOM_KILLED",
      agent_outcome: "NOT_ATTEMPTED",
    }),
    { bucket: "infra", tone: "warn", label: "平台故障 · 内存超限被杀" },
  );
  check(
    "FAILED 且没给 infra 细节 → 平台故障，不编细节",
    cellVerdict({ ...base, lifecycle_status: "FAILED", infra_outcome: null, agent_outcome: null }),
    { bucket: "infra", tone: "warn", label: "平台故障 · 未给出细节" },
  );
  check(
    "非终态 → 进行中",
    cellVerdict({ ...base, lifecycle_status: "TESTING", infra_outcome: null, agent_outcome: null }),
    { bucket: "active", tone: "active", label: "测试中" },
  );
  check(
    "已取消 → cancelled（压过 infra 里的 CANCELLED）",
    cellVerdict({
      ...base,
      lifecycle_status: "CANCELLED",
      infra_outcome: "CANCELLED",
      agent_outcome: "NOT_ATTEMPTED",
    }),
    { bucket: "cancelled", tone: "neutral", label: "已取消" },
  );
  check(
    "COMPLETED 但没尝试 → 未解决，不算已解决",
    cellVerdict({ ...base, lifecycle_status: "COMPLETED", agent_outcome: "NOT_ATTEMPTED" }),
    { bucket: "unresolved", tone: "neutral", label: "未尝试" },
  );

  // ── 数值格式化 ──
  check("解决率 0.8640", formatRate("0.8640"), "86.4%");
  check("解决率 0.1360", formatRate("0.1360"), "13.6%");
  check("解决率 0", formatRate("0.0000"), "0.0%");
  check("解决率 null 不等于 0", formatRate(null), "—");

  check("单题成本 0.0175 不被舍成 $0.02", formatCost("0.0175"), "$0.0175");
  check("成本 0.042", formatCost("0.0420"), "$0.042");
  check("成本 0.5 不留尾零", formatCost("0.5000"), "$0.5");
  check("成本 0", formatCost("0"), "$0");
  check("成本 null", formatCost(null), "—");
  check("成本 1.2 用两位", formatCost("1.2000"), "$1.20");
  check("成本 12.5", formatCost("12.5000"), "$12.50");

  check("耗时 45 秒", formatDuration(45_000), "45 秒");
  check("耗时 1 分 30 秒", formatDuration(90_000), "1 分 30 秒");
  check("耗时 1 小时", formatDuration(3_600_000), "1 小时 0 分");
  check("耗时 null", formatDuration(null), "—");

  check("token 999", formatTokens(999), "999");
  check("token 12.4k", formatTokens(12_400), "12.4k");
  check("token 1.23M", formatTokens(1_234_567), "1.23M");

  // ── 失败归因（E6 的类别与证据怎么显示）──
  check("十个类别、协议编号顺序", FAILURE_CATEGORIES.length, 10);
  check("F7 的中文名", failureCategoryLabel("F7_EMPTY_OR_INVALID_PATCH"), "F7 · 空补丁或无效补丁");
  check("F 类是 AI 的锅（红）", failureCategoryTone("F4_INCORRECT_LOGIC"), "bad");
  check("N1 不是 AI 的锅（黄）", failureCategoryTone("N1_INFRASTRUCTURE_FAILURE"), "warn");
  check("N2 题目缺陷也不是 AI 的锅", failureCategoryTone("N2_TASK_DEFECT"), "warn");
  // 规则层没有置信度：null 显示成"—"而不是 0%（0% 会被读成"完全不可信"）
  check("置信度 null 不等于 0%", formatConfidence(null), "—");
  check("置信度 0.875 → 88%", formatConfidence("0.875"), "88%");
  check("置信度 1 → 100%", formatConfidence("1.000"), "100%");
  check(
    "规则层证据 → 规则名 + 判据表",
    evidenceView({ rule: "regression", facts: { f2p: "4/4", p2p: "1067/1274", agent_outcome: "UNRESOLVED" } }),
    {
      kind: "rule",
      rule: "regression",
      facts: [["f2p", "4/4"], ["p2p", "1067/1274"], ["agent_outcome", "UNRESOLVED"]],
    },
  );
  check(
    "规则层的布尔判据转成字符串，不丢",
    evidenceView({ rule: "empty_or_invalid_patch", facts: { raw_patch_empty: true } }),
    { kind: "rule", rule: "empty_or_invalid_patch", facts: [["raw_patch_empty", "true"]] },
  );
  check(
    "LLM 层证据 → 引文 + 投票",
    evidenceView({
      citations: [{ source: "patch", quote: "return x - 1" }],
      vote_categories: ["F4_INCORRECT_LOGIC", "F4_INCORRECT_LOGIC"],
    }),
    {
      kind: "citations",
      citations: [{ source: "patch", quote: "return x - 1" }],
      votes: ["F4_INCORRECT_LOGIC", "F4_INCORRECT_LOGIC"],
    },
  );
  check("空 evidence → none", evidenceView({}), { kind: "none" });
  check(
    "认不出的形状 → 原样 JSON，不丢信息",
    evidenceView({ note: "x" }),
    { kind: "raw", json: JSON.stringify({ note: "x" }, null, 2) },
  );

  // ── 轮询开关（决定一个跑完的页面还打不打后端）──
  check("QUEUED 要轮询", isLiveRun("QUEUED"), true);
  check("RUNNING 要轮询", isLiveRun("RUNNING"), true);
  check("COMPLETED 停轮询", isLiveRun("COMPLETED"), false);
  check("FAILED 停轮询", isLiveRun("FAILED"), false);
  check("CANCELLED 停轮询", isLiveRun("CANCELLED"), false);
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
