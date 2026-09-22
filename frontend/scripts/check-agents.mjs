#!/usr/bin/env node
/**
 * `src/lib/agents.ts` 的断言检查（E7 走查 #19）。
 *
 * `/agents` 页分两组（参赛者 / 哨兵与诊断），同名的新旧配置（aider 的
 * deepseek-chat 旧配置 / deepseek-flash 新配置）要能分出哪条是"当前在用"的——
 * 判错了会把已经不用的旧配置画成正常参赛者，或者把当前配置错灰成"过时"。
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

function config(id, agent_kind, agent_display_name, enabled = true) {
  return { id, agent_kind, agent_display_name, enabled, label: `${agent_display_name}@${id}` };
}

function run(agent_config_id, created_at) {
  return { agent_config_id, created_at };
}

const outDir = mkdtempSync(join(tmpdir(), "bench-agents-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "agents.ts"),
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

  const { agentGroup, pilotHistoryConfigIds } = await import(
    pathToFileURL(join(outDir, "agents.js")).href
  );

  // ── 分组：复用 isContestant 的口径 ──
  check("CLI 且启用 → 参赛者", agentGroup(config(1, "CLI", "aider")), "CONTESTANT");
  check("ORACLE 哨兵 → 哨兵与诊断", agentGroup(config(2, "ORACLE", "Oracle 哨兵")), "SENTINEL_OR_DIAGNOSTIC");
  check("NOOP 哨兵 → 哨兵与诊断", agentGroup(config(3, "NOOP", "Noop 哨兵")), "SENTINEL_OR_DIAGNOSTIC");
  check("MOCK → 哨兵与诊断", agentGroup(config(4, "MOCK", "Mock")), "SENTINEL_OR_DIAGNOSTIC");
  check(
    "停用的诊断配置（aider+autotest）→ 哨兵与诊断",
    agentGroup(config(5, "CLI", "Aider（开 --auto-test）", false)),
    "SENTINEL_OR_DIAGNOSTIC",
  );

  // ── pilot 历史：开发库 2026-09-20 前后的真实形状 ──
  // 166/167 = aider/claude-code 的 deepseek-chat 旧配置（2026-09-12 跑过）
  // 170/171 = 换成 deepseek-flash 之后的新配置（2026-09-20/21 跑最终实验）
  const configs = [
    config(166, "CLI", "Aider"),
    config(170, "CLI", "Aider"),
    config(167, "CLI", "Claude Code"),
    config(171, "CLI", "Claude Code"),
    config(169, "CUSTOM", "MiniAgent（自研）"),
  ];
  const runs = [
    run(166, "2026-09-12T07:00:35Z"),
    run(166, "2026-09-12T09:10:17Z"),
    run(167, "2026-09-12T07:00:44Z"),
    run(170, "2026-09-20T14:02:26Z"),
    run(170, "2026-09-21T04:42:04Z"),
    run(171, "2026-09-20T14:02:26Z"),
    run(171, "2026-09-21T04:42:02Z"),
  ];

  const pilots = pilotHistoryConfigIds(configs, runs);
  check("旧配置（166 aider@deepseek-chat）标 pilot 历史", pilots.has(166), true);
  check("新配置（170 aider@deepseek-flash）不标", pilots.has(170), false);
  check(
    "只跑过一次的旧配置（167 claude-code@deepseek-chat）也标 pilot 历史",
    pilots.has(167),
    true,
  );
  check("新配置（171 claude-code@deepseek-flash）不标", pilots.has(171), false);
  check("只有一条配置的 Agent（169 MiniAgent）不标——没跑过和被取代是两回事", pilots.has(169), false);

  check(
    "哨兵和停用的诊断配置不参与 pilot 判断（不会混进同名分组）",
    pilotHistoryConfigIds(
      [config(1, "ORACLE", "Oracle 哨兵"), config(2, "ORACLE", "Oracle 哨兵")],
      [],
    ).size,
    0,
  );

  check("没有同名的第二条配置就不标（哪怕从没跑过）", pilotHistoryConfigIds([config(9, "CLI", "独苗")], []).size, 0);

  if (failed > 0) {
    console.error(`\n${failed} 条断言失败`);
    process.exit(1);
  }
  console.log("\n全部通过");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
