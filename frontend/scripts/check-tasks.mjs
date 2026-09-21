#!/usr/bin/env node
/**
 * `src/lib/tasks.ts` 的断言检查。
 *
 * 这个模块编码的是"数据集与题目的数字该怎么读"：
 * - 构成的合计要能跟题量对上（对不上要说出来，不许照画）；
 * - 门禁证据缺字段 = 判不了，不是 0（Oracle 要全过、Noop 要零解决，§27.2）；
 * - 历史表现只认 canonical 那次尝试（C-24），题号精确匹配（a-1 ≠ a-11）。
 * 判错了页面照常渲染，只是给出一个看着合理但错的结论 —— 只能靠断言钉死。
 *
 * 零新依赖：tsc 编译 + 动态 import，同 check-display / check-task-detail / check-leaderboard。
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

/** 造一行实验：只写关心的字段，其余给能用的默认值。 */
function run(overrides = {}) {
  return {
    id: 1,
    name: "演示实验",
    agent_config_label: "aider",
    ...overrides,
  };
}

/** 造一条逐题记录。 */
function taskRun(overrides = {}) {
  return {
    id: 100,
    evaluation_run_id: 1,
    task_id: "click-1",
    is_canonical: true,
    lifecycle_status: "COMPLETED",
    infra_outcome: "SUCCESS",
    agent_outcome: "RESOLVED",
    error_code: null,
    f2p_passed: 2,
    f2p_total: 2,
    p2p_passed: 8,
    p2p_total: 8,
    cost_usd: "0.0136",
    total_duration_ms: 61000,
    ...overrides,
  };
}

const outDir = mkdtempSync(join(tmpdir(), "bench-tasks-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "tasks.ts"),
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
    issueLanguageLabel,
    difficultyLabel,
    validationStateText,
    benchmarkSetStatusText,
    COMPOSITION_GROUPS,
    compositionRows,
    publishGate,
    ratePercent,
    quarantineNote,
    HISTORY_RUN_LIMIT,
    historyRows,
    shortHash,
  } = await import(pathToFileURL(join(outDir, "tasks.js")).href);

  // ── 枚举标签 ──
  check(
    "三种语言都有中文标签",
    ["zh", "en", "mixed"].map(issueLanguageLabel),
    ["中文", "英文", "中英混合"],
  );
  check(
    "三种难度都有中文标签",
    ["easy", "medium", "hard"].map(difficultyLabel),
    ["简单", "中等", "困难"],
  );
  check(
    "七种验证状态各有中文标签",
    ["DISCOVERED", "CANDIDATE", "VALIDATING", "VALID", "INVALID", "REVIEW_REQUIRED", "QUARANTINED"].map(
      (s) => validationStateText(s).label,
    ),
    ["已发现", "候选", "验证中", "已验证", "验证未通过", "待人工复核", "已隔离"],
  );
  check(
    "验证状态的色档：VALID ok / INVALID bad / QUARANTINED bad / REVIEW_REQUIRED warn",
    ["VALID", "INVALID", "QUARANTINED", "REVIEW_REQUIRED"].map(
      (s) => validationStateText(s).tone,
    ),
    ["ok", "bad", "bad", "warn"],
  );
  check(
    "三种数据集状态各有中文标签",
    ["DRAFT", "PUBLISHED", "ARCHIVED"].map((s) => benchmarkSetStatusText(s).label),
    ["草稿", "已发布", "已归档"],
  );
  check("认不出的验证状态原样返回", validationStateText("FUTURE_STATE"), {
    label: "FUTURE_STATE",
    tone: "neutral",
  });

  // ── compositionRows：固定三组、排序、百分比 ──
  check("固定三组且顺序固定", COMPOSITION_GROUPS, ["language", "difficulty", "repository"]);
  check(
    "后端多给第四组时只回三组",
    compositionRows(
      { language: [{ value: "zh", count: 1 }], tags: [{ value: "bug", count: 1 }] },
      1,
    ).map((g) => g.key),
    ["language", "difficulty", "repository"],
  );

  const full = compositionRows(
    {
      language: [
        { value: "zh", count: 12 },
        { value: "en", count: 8 },
        { value: "mixed", count: 2 },
      ],
      difficulty: [
        { value: "easy", count: 10 },
        { value: "hard", count: 10 },
        { value: "medium", count: 2 },
      ],
      repository: [{ value: "pallets/click", count: 22 }],
    },
    22,
  );
  check("语言组按数量降序", full[0].cells.map((c) => c.value), ["zh", "en", "mixed"]);
  check(
    "数量并列时按取值升序（不是字典序倒着来）",
    full[1].cells.map((c) => c.value),
    ["easy", "hard", "medium"],
  );
  check(
    "百分比按组合计一位小数（排完序 hard/medium/easy → 40/40/20）",
    compositionRows(
      {
        difficulty: [
          { value: "easy", count: 10 },
          { value: "hard", count: 20 },
          { value: "medium", count: 20 },
        ],
      },
      50,
    )[1].cells.map((c) => c.percent),
    [40, 40, 20],
  );
  check(
    "百分比一位小数（7 题里 3 道 → 42.9）",
    compositionRows({ difficulty: [{ value: "easy", count: 3 }, { value: "hard", count: 4 }] }, 7)[1]
      .cells.map((c) => c.percent),
    [57.1, 42.9],
  );
  check(
    "语言格子用中文标签、仓库格子原样透出",
    [full[0].cells[0].label, full[2].cells[0].label],
    ["中文", "pallets/click"],
  );
  check(
    "枚举外的语言取值退回原值，不 undefined",
    compositionRows({ language: [{ value: "ruby", count: 1 }] }, 1)[0].cells[0].label,
    "ruby",
  );
  check(
    "组缺失 → 空格子、合计 0、对不上题量",
    [full[0].cells.length > 0 && compositionRows({}, 22)[0].cells, compositionRows({}, 22)[0].total, compositionRows({}, 22)[0].matchesTaskCount],
    [[], 0, false],
  );
  check(
    "合计与题量一致时 matchesTaskCount 为 true",
    compositionRows({ repository: [{ value: "pallets/click", count: 22 }] }, 22)[2]
      .matchesTaskCount,
    true,
  );
  check(
    "合计差 1 也要报对不上（21 != 22）",
    compositionRows({ repository: [{ value: "pallets/click", count: 21 }] }, 22)[2]
      .matchesTaskCount,
    false,
  );
  check(
    "空构成 + 题量 0 → 合计对得上",
    compositionRows({}, 0)[0].matchesTaskCount,
    true,
  );
  check(
    "合计为 0 时百分比是 0 不是 NaN",
    compositionRows({ difficulty: [{ value: "easy", count: 0 }] }, 0)[1].cells[0].percent,
    0,
  );

  // ── publishGate：缺字段就是判不了 ──
  check("evidence 为 null → null", publishGate(null), null);
  check("evidence 不是对象 → null", publishGate(42), null);

  const complete = publishGate({
    checked_at: "2026-09-21T10:00:00Z",
    snapshot_digest: "sha256:abcdef0123456789",
    protocol_clause: "C-50",
    oracle: { evaluation_run_id: 7, total_tasks: 22, resolved_count: 22, resolve_rate: 1 },
    noop: { evaluation_run_id: 8, total_tasks: 22, resolved_count: 0, resolve_rate: 0 },
  });
  check(
    "完整证据各字段到位",
    [
      complete.checkedAt,
      complete.snapshotDigest,
      complete.protocolClause,
      complete.oracle.runId,
      complete.oracle.totalTasks,
      complete.oracle.resolvedCount,
      complete.oracle.resolveRate,
      complete.noop.resolveRate,
    ],
    ["2026-09-21T10:00:00Z", "sha256:abcdef0123456789", "C-50", 7, 22, 22, 1, 0],
  );
  check(
    "Oracle 全过 → ok true；Noop 零解决 → ok true",
    [complete.oracle.ok, complete.noop.ok],
    [true, true],
  );
  check(
    "Oracle 少解一道 → ok false",
    publishGate({
      oracle: { total_tasks: 22, resolved_count: 21, resolve_rate: 0.9545 },
    }).oracle.ok,
    false,
  );
  check(
    "Noop 解了一道 → ok false",
    publishGate({ noop: { total_tasks: 22, resolved_count: 1, resolve_rate: 0.0455 } })
      .noop.ok,
    false,
  );
  check(
    "缺 total_tasks → ok 为 null（判不了就是判不了）",
    publishGate({ oracle: { resolved_count: 22, resolve_rate: 1 } }).oracle.ok,
    null,
  );
  check(
    "resolve_rate 越界（1.5）→ 按缺处理",
    publishGate({ oracle: { total_tasks: 1, resolved_count: 1, resolve_rate: 1.5 } })
      .oracle.resolveRate,
    null,
  );
  check(
    "total_tasks 是字符串 \"5\" → 不做类型转换，按缺处理",
    publishGate({ oracle: { total_tasks: "5", resolved_count: 5, resolve_rate: 1 } })
      .oracle.totalTasks,
    null,
  );
  check("缺 oracle 子对象 → oracle 为 null", publishGate({ protocol_clause: "C-50" }).oracle, null);
  check(
    "只有 checked_at → Oracle/Noop 都没有记录",
    [publishGate({ checked_at: "2026-09-21T10:00:00Z" }).oracle, publishGate({ checked_at: "2026-09-21T10:00:00Z" }).noop],
    [null, null],
  );
  check(
    "ratePercent：null / 0.8636 / 0 / 1",
    [ratePercent(null), ratePercent(0.8636), ratePercent(0), ratePercent(1)],
    ["—", "86.4%", "0.0%", "100.0%"],
  );

  // ── quarantineNote ──
  check("隔离记录 null → null", quarantineNote(null), null);
  check(
    "隔离记录三字段透出",
    quarantineNote({
      at: "2026-09-20T08:00:00Z",
      from_state: "VALID",
      reason: "复验时 F2P 在 base 上通过",
    }),
    { at: "2026-09-20T08:00:00Z", fromState: "VALID", reason: "复验时 F2P 在 base 上通过" },
  );
  check(
    "字段类型不对（数字）→ 该字段按缺处理",
    quarantineNote({ at: 123, reason: "x" }),
    { at: null, fromState: null, reason: "x" },
  );

  // ── historyRows ──
  check("回溯上限是 20", HISTORY_RUN_LIMIT, 20);
  check(
    "只认 canonical：同实验两条记录，非 canonical 被丢",
    historyRows(
      [run({ id: 3 })],
      new Map([
        [
          3,
          [
            taskRun({ id: 301, task_id: "click-1", is_canonical: false, agent_outcome: "UNRESOLVED" }),
            taskRun({ id: 302, task_id: "click-1", is_canonical: true, agent_outcome: "RESOLVED" }),
          ],
        ],
      ]),
      "click-1",
    ).map((r) => r.taskRunId),
    [302],
  );
  check(
    "题号精确匹配：a-1 不匹配 a-11",
    historyRows(
      [run({ id: 3 })],
      new Map([[3, [taskRun({ id: 303, task_id: "a-11" })]]]),
      "a-1",
    ),
    [],
  );
  check(
    "runs 乱序传入 → 按实验号倒序输出",
    historyRows(
      [run({ id: 2 }), run({ id: 9 }), run({ id: 5 })],
      new Map([
        [2, [taskRun({ id: 201, task_id: "click-1" })]],
        [9, [taskRun({ id: 901, task_id: "click-1" })]],
        [5, [taskRun({ id: 501, task_id: "click-1" })]],
      ]),
      "click-1",
    ).map((r) => r.runId),
    [9, 5, 2],
  );
  check(
    "25 次实验都跑到过 → 只回最近 20 行",
    historyRows(
      Array.from({ length: 25 }, (_, i) => run({ id: i + 1 })),
      new Map(
        Array.from({ length: 25 }, (_, i) => [
          i + 1,
          [taskRun({ id: 1000 + i, task_id: "click-1" })],
        ]),
      ),
      "click-1",
    ).length,
    20,
  );
  check(
    "同实验两条 canonical（数据异常）→ 只出一行",
    historyRows(
      [run({ id: 3 })],
      new Map([
        [
          3,
          [
            taskRun({ id: 301, task_id: "click-1", is_canonical: true }),
            taskRun({ id: 302, task_id: "click-1", is_canonical: true }),
          ],
        ],
      ]),
      "click-1",
    ).map((r) => r.taskRunId),
    [301],
  );
  check(
    "没跑到过这道题（只有别的题）→ 不出现",
    historyRows(
      [run({ id: 3 })],
      new Map([[3, [taskRun({ id: 303, task_id: "other-9" })]]]),
      "click-1",
    ),
    [],
  );
  check(
    "没取到逐题记录的实验也不出现",
    historyRows([run({ id: 3 })], new Map(), "click-1"),
    [],
  );
  check(
    "字段透传：成本/耗时/结局原样带上",
    historyRows(
      [run({ id: 3, name: "第三轮", agent_config_label: "claude-code" })],
      new Map([
        [3, [taskRun({ id: 303, task_id: "click-1", cost_usd: "0.5", total_duration_ms: 120000, agent_outcome: "UNRESOLVED" })]],
      ]),
      "click-1",
    ).map((r) => [r.runName, r.agentLabel, r.costUsd, r.totalDurationMs, r.agentOutcome]),
    [["第三轮", "claude-code", "0.5", 120000, "UNRESOLVED"]],
  );
  check(
    "传 limit=3 只回 3 行",
    historyRows(
      [run({ id: 1 }), run({ id: 2 }), run({ id: 3 }), run({ id: 4 })],
      new Map([
        [1, [taskRun({ id: 101, task_id: "click-1" })]],
        [2, [taskRun({ id: 201, task_id: "click-1" })]],
        [3, [taskRun({ id: 301, task_id: "click-1" })]],
        [4, [taskRun({ id: 401, task_id: "click-1" })]],
      ]),
      "click-1",
      3,
    ).length,
    3,
  );

  // ── shortHash ──
  check(
    "短显：null / 空串 / 短值 / 超长 / 恰好等长",
    [
      shortHash(null),
      shortHash(""),
      shortHash("abc123"),
      shortHash("0123456789abcdef"),
      shortHash("0123456789ab"),
    ],
    ["—", "—", "abc123", "0123456789ab…", "0123456789ab"],
  );
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

if (failed > 0) {
  console.log(`\n${failed} 条不通过`);
  process.exit(1);
}
console.log("\n全部通过");
