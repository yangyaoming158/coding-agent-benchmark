#!/usr/bin/env node
/**
 * E7-T3 纯函数层的断言检查：`diff.ts`、`trajectory.ts`、`search.ts`，
 * 加上 `display.ts` 本期新增的几个判定函数。
 *
 * 和 `check-display.mjs` 同一条路线、同一个理由：这部分逻辑的错法全都
 * 很安静，界面照常渲染，只是证据悄悄错了 ——
 *
 * - diff 解析差一行，之后所有行号错位；
 * - 轨迹解析吞掉一行，恰好丢掉诊断时最需要的那段；
 * - 日志搜索的边界写错，表现是"搜不到"，用户会以为日志里没有（而日志
 *   恰恰是"为什么没修好"的最后证据）；
 * - `isLiveTaskRun` 判反，跑完的页面永远轮询，或者跑着的页面不刷新。
 *
 * 这些都没法靠肉眼在几千行里发现。纯函数用断言钉死；组件层（DOM 行为）
 * 留给以后前端引入测试框架时补，不在这里硬造一套。
 *
 * 零新依赖：用仓库自带的 tsc 把源文件编成 JS，import 进来断言。
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

// ── diff 样例 ──────────────────────────────────────────────

const NO_NEWLINE = "\\ No newline at end of file";

/** 普通改动 + 二进制新文件。`---`/`+++` 在 hunk 外，不是增删行。 */
const SAMPLE_BASIC = [
  "diff --git a/foo.py b/foo.py",
  "index 1111111..2222222 100644",
  "--- a/foo.py",
  "+++ b/foo.py",
  "@@ -1,3 +1,4 @@",
  " def foo():",
  "-    return 1",
  "+    return 2",
  "+    # note",
  " bar = 1",
  "diff --git a/data.bin b/data.bin",
  "new file mode 100644",
  "index 0000000..3333333",
  "Binary files /dev/null and b/data.bin differ",
].join("\n");

/** 删文件：`+++` 是 /dev/null，但路径不能跟着变成 /dev/null。顺带钉 `\ No newline`。 */
const SAMPLE_DELETED = [
  "diff --git a/gone.txt b/gone.txt",
  "deleted file mode 100644",
  "index 4444444..0000000",
  "--- a/gone.txt",
  "+++ /dev/null",
  "@@ -1,3 +0,0 @@",
  "-line1",
  "-",
  "-line3",
  NO_NEWLINE,
].join("\n");

/**
 * 两个 hunk + 空上下文行。
 *
 * 空行在 diff 里本来是「一个空格」，复制/转发时常被吃掉行尾空格变成真空行。
 * 按「空格开头才是上下文」判，会把它漏掉，那一行之后的行号全部错位。
 */
const SAMPLE_CONTEXT = [
  "diff --git a/x.txt b/x.txt",
  "--- a/x.txt",
  "+++ b/x.txt",
  "@@ -1,4 +1,4 @@",
  " a",
  "",
  "-old",
  "+new",
  "",
  "@@ -10,3 +10,3 @@",
  " x",
  "-y",
  "+z",
  " w",
].join("\n");

/** 裸 diff（没有 diff --git 头）+ 省略行数的紧凑 hunk 头 `@@ -1 +1 @@`。 */
const SAMPLE_BARE = [
  "--- a/only.txt",
  "+++ b/only.txt",
  "@@ -1 +1 @@",
  "-old line",
  "+new line",
].join("\n");

/** diff 行的行号、正文、种类 —— 断言里逐行比这个。 */
const rows = (hunk) =>
  hunk.lines.map((l) => ({ kind: l.kind, text: l.text, old: l.oldLine, new: l.newLine }));

const outDir = mkdtempSync(join(tmpdir(), "bench-task-detail-"));
try {
  execFileSync(
    process.execPath,
    [
      join(frontend, "node_modules", "typescript", "bin", "tsc"),
      join(frontend, "src", "lib", "diff.ts"),
      join(frontend, "src", "lib", "trajectory.ts"),
      join(frontend, "src", "lib", "search.ts"),
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

  const [
    { parseUnifiedDiff, countChanges, changedPaths },
    { parseTrajectory, eventMillis, countEvents },
    { splitByQuery, findMatchingLines },
    { isLiveTaskRun, testStatusText, formatMillis, formatBytes, costSourceLabel },
  ] = await Promise.all([
    import(pathToFileURL(join(outDir, "diff.js")).href),
    import(pathToFileURL(join(outDir, "trajectory.js")).href),
    import(pathToFileURL(join(outDir, "search.js")).href),
    import(pathToFileURL(join(outDir, "display.js")).href),
  ]);

  // ── diff：文件切分与路径 ──
  const basic = parseUnifiedDiff(SAMPLE_BASIC);
  check("两个文件段", basic.length, 2);
  check("路径取 diff --git 头、去掉前缀", basic[0].path, "foo.py");
  check("旧路径", basic[0].oldPath, "foo.py");
  check(
    "--- / +++ 是文件头，不进 hunk",
    basic[0].hunks.length,
    1,
  );
  check("meta 保留原始四行", basic[0].meta.length, 4);
  check("二进制文件没有 hunk", { binary: basic[1].binary, hunks: basic[1].hunks.length }, {
    binary: true,
    hunks: 0,
  });
  check("二进制文件的路径照常拿到", basic[1].path, "data.bin");
  check("文件清单", changedPaths(basic), ["foo.py", "data.bin"]);

  // ── diff：hunk 内的行号与正文 ──
  check("hunk 头起始行号", { old: basic[0].hunks[0].oldStart, new: basic[0].hunks[0].newStart }, {
    old: 1,
    new: 1,
  });
  check("逐行：+/- 是增删、空格是上下文、行号各归各边", rows(basic[0].hunks[0]), [
    { kind: "context", text: "def foo():", old: 1, new: 1 },
    { kind: "del", text: "    return 1", old: 2, new: null },
    { kind: "add", text: "    return 2", old: null, new: 2 },
    { kind: "add", text: "    # note", old: null, new: 3 },
    { kind: "context", text: "bar = 1", old: 3, new: 4 },
  ]);
  check("增删计数", countChanges(basic[0]), { added: 2, deleted: 1 });

  // ── diff：删文件不显示 /dev/null ──
  const deleted = parseUnifiedDiff(SAMPLE_DELETED);
  check("删文件路径仍是文件名，不是 /dev/null", deleted[0].path, "gone.txt");
  check("删空行也是删除行", countChanges(deleted[0]), { added: 0, deleted: 3 });
  check("\\ No newline 不占行号，附在删除行之后", rows(deleted[0].hunks[0]), [
    { kind: "del", text: "line1", old: 1, new: null },
    { kind: "del", text: "", old: 2, new: null },
    { kind: "del", text: "line3", old: 3, new: null },
    { kind: "nonewline", text: NO_NEWLINE, old: null, new: null },
  ]);

  // ── diff：空行上下文 + 第二个 hunk 的行号重置 ──
  const context = parseUnifiedDiff(SAMPLE_CONTEXT);
  check("空行（行尾空格被吃掉）仍算上下文行，行号照推", rows(context[0].hunks[0]), [
    { kind: "context", text: "a", old: 1, new: 1 },
    { kind: "context", text: "", old: 2, new: 2 },
    { kind: "del", text: "old", old: 3, new: null },
    { kind: "add", text: "new", old: null, new: 3 },
    { kind: "context", text: "", old: 4, new: 4 },
  ]);
  check("第二个 hunk 从自己的头重置行号", rows(context[0].hunks[1]), [
    { kind: "context", text: "x", old: 10, new: 10 },
    { kind: "del", text: "y", old: 11, new: null },
    { kind: "add", text: "z", old: null, new: 11 },
    { kind: "context", text: "w", old: 12, new: 12 },
  ]);

  // ── diff：裸 diff 与 CRLF ──
  const bare = parseUnifiedDiff(SAMPLE_BARE);
  check("裸 diff 走 ---/+++ 兜底拿路径", { path: bare[0].path, oldPath: bare[0].oldPath }, {
    path: "only.txt",
    oldPath: "only.txt",
  });
  check("紧凑 hunk 头 @@ -1 +1 @@", rows(bare[0].hunks[0]), [
    { kind: "del", text: "old line", old: 1, new: null },
    { kind: "add", text: "new line", old: null, new: 1 },
  ]);
  check(
    "CRLF 换行与 LF 解析结果一致",
    parseUnifiedDiff(SAMPLE_BASIC.replace(/\n/g, "\r\n")),
    parseUnifiedDiff(SAMPLE_BASIC),
  );

  // ── 轨迹：三类事件、坏行、未知类型 ──
  const RAW_UNKNOWN = '{"ts":1767000000003,"type":"future_event","payload":1}';
  const trajectory = parseTrajectory(
    [
      '{"ts":1767000000000,"type":"tool_call","name":"edit_file","args_digest":"a1b2c3","summary":"x.py 改了 3 行"}',
      '{"ts":1767000000001,"type":"llm_usage","input":8123,"output":412}',
      '{"ts":1767000000002,"type":"message","role":"assistant","text_excerpt":"先看测试再动手"}',
      "这行不是 JSON",
      RAW_UNKNOWN,
      "",
      '{"ts":"不是数字","type":"message"}',
    ].join("\n"),
  );

  check("坏行计入 skipped", trajectory.skipped, 1);
  check("工具调用", trajectory.events[0], {
    kind: "tool_call",
    ts: 1767000000000,
    name: "edit_file",
    digest: "a1b2c3",
    summary: "x.py 改了 3 行",
  });
  check("用量", trajectory.events[1], {
    kind: "llm_usage",
    ts: 1767000000001,
    input: 8123,
    output: 412,
  });
  check("消息", trajectory.events[2], {
    kind: "message",
    ts: 1767000000002,
    role: "assistant",
    text: "先看测试再动手",
  });
  check("不认识的事件原样保留，不假装它不存在", trajectory.events[3], {
    kind: "unknown",
    ts: 1767000000003,
    type: "future_event",
    raw: RAW_UNKNOWN,
  });
  check("ts 不是数字时给 null，不猜", trajectory.events[4], {
    kind: "message",
    ts: null,
    role: null,
    text: "",
  });
  check("事件计数", countEvents(trajectory.events), {
    tool_call: 1,
    llm_usage: 1,
    message: 2,
    unknown: 1,
  });
  check("空轨迹不算坏行", parseTrajectory(""), { events: [], skipped: 0 });
  check(
    "工具名缺失给占位，不显示 undefined",
    parseTrajectory('{"type":"tool_call"}').events[0],
    { kind: "tool_call", ts: null, name: "(未命名工具)", digest: null, summary: "" },
  );
  check(
    "毫秒时间戳直通",
    eventMillis(1767000000000),
    1767000000000,
  );
  check("秒时间戳换算成毫秒（1e12 是防呆下界）", eventMillis(1767000000), 1767000000000);
  check("null 时间戳保持 null", eventMillis(null), null);

  // ── 日志搜索 ──
  check("折叠大小写但高亮保留原文", splitByQuery("ERROR: boom", "error"), [
    "",
    "ERROR",
    ": boom",
  ]);
  check("整行匹配 → 首尾各一个空片段", splitByQuery("abc", "abc"), ["", "abc", ""]);
  check("没有匹配 → 原样一行", splitByQuery("abc", "zzz"), ["abc"]);
  check("一次出现切成三段", splitByQuery("aXbXc", "x"), ["a", "X", "b", "X", "c"]);
  check("空关键词不切", splitByQuery("line", ""), ["line"]);
  check("关键词比行长", splitByQuery("abc", "abcd"), ["abc"]);
  check("maxParts 截断后片段数仍是奇数", splitByQuery("aaaa", "a", 2), [
    "",
    "a",
    "",
    "a",
    "aa",
  ]);
  check(
    "大小写不敏感 + 返回行号",
    findMatchingLines(["ERROR one", "info", "error two"], "error", 10),
    [0, 2],
  );
  check("空查询不把整个日志当命中", findMatchingLines(["a", "b"], "", 10), []);
  check("到上限就停", findMatchingLines(["x", "x", "x"], "x", 2), [0, 1]);

  // ── display：本期新增 ──
  check("PREPARING 还在动", isLiveTaskRun("PREPARING"), true);
  check("TESTING 还在动", isLiveTaskRun("TESTING"), true);
  check("ANALYZING 还在动", isLiveTaskRun("ANALYZING"), true);
  check("COMPLETED 停轮询", isLiveTaskRun("COMPLETED"), false);
  check("FAILED 停轮询", isLiveTaskRun("FAILED"), false);
  check("CANCELLED 停轮询", isLiveTaskRun("CANCELLED"), false);

  check("MISSING 标成警告色，不是中性跳过（C-12）", testStatusText("MISSING"), {
    label: "缺失",
    tone: "warn",
  });
  check("PASSED", testStatusText("PASSED"), { label: "通过", tone: "ok" });
  check("FAILED", testStatusText("FAILED"), { label: "失败", tone: "bad" });

  check("毫秒级耗时不被舍成 0 秒", formatMillis(120), "120 ms");
  check("满一秒走秒级格式", formatMillis(1000), "1 秒");
  check("null 耗时", formatMillis(null), "—");

  check("字节", formatBytes(512), "512 B");
  check("KB", formatBytes(2048), "2.0 KB");
  check("MB", formatBytes(5_242_880), "5.0 MB");

  check("成本来源：拿不到不是 $0", costSourceLabel("unavailable"), "拿不到");
  check("成本来源：自报", costSourceLabel("reported"), "Agent 自报");
  check("成本来源：估算", costSourceLabel("estimated"), "平台估算");
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failed === 0 ? "\n全部通过" : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
