/**
 * unified diff 的解析 —— 只做「文本 → 结构」，不碰 DOM、不带样式。
 *
 * 为什么自研（§16.1 写的是「diff2html 或自研轻量 diff 渲染」二选一）：
 * 这里要的东西很窄 —— 逐行着色加两边行号。unified diff 的语法就那么几条，
 * 几十行就能覆盖；拆成纯函数之后边界情况能用断言脚本钉住
 * （`npm run check:task-detail`），这是换一个黑盒依赖得不到的。
 *
 * 格式（git 默认输出）：
 *
 *     diff --git a/x.py b/x.py        ← 文件段开始
 *     index 1234567..89abcde 100644
 *     --- a/x.py
 *     +++ b/x.py
 *     @@ -1,5 +1,7 @@ def foo():     ← hunk 头，两边的起始行号在这里
 *      context                       ← 前缀是一个空格
 *     -removed                       ← 只有旧文件有这一行
 *     +added                         ← 只有新文件有这一行
 *     \ No newline at end of file    ← 附属于上一行的标记，不占行号
 *
 * 三处容易写错，每一处都有断言钉着：
 *
 * 1. **hunk 内外的 `-` / `+` 含义不同。** `--- a/x.py` 和 `+++ b/x.py` 是文件头，
 *    不是「删了一行又加了一行」。只有在 hunk 里面 `-`/`+` 才是增删行。
 * 2. **空行是上下文行。** diff 里的空上下文行本来是「一个空格」，但编辑器复制、
 *    聊天工具转发常把行尾空格吃掉，变成真空行。按「空格开头」判会把它当成
 *    hunk 之外的东西，那一行之后的行号全部错位。
 * 3. **`\ No newline at end of file` 不占行号。** 它标记的是上一行的状态。
 */

export type DiffLineKind = "add" | "del" | "context" | "nonewline";

export interface DiffLine {
  kind: DiffLineKind;
  /** 去掉行首标记（`+` / `-` / 空格）之后的正文。 */
  text: string;
  /** 这一行在旧文件里的行号；新增行没有。 */
  oldLine: number | null;
  /** 这一行在新文件里的行号；删除行没有。 */
  newLine: number | null;
}

export interface DiffHunk {
  /** 原样的 hunk 头，如 `@@ -1,5 +1,7 @@ def foo():`。 */
  header: string;
  oldStart: number;
  newStart: number;
  lines: DiffLine[];
}

export interface DiffFile {
  /**
   * 文件路径，**以 `diff --git` 头为准**。
   *
   * 不能以 `+++ b/...` 为准：删文件时那一行是 `+++ /dev/null`，
   * 而删文件在评测里很常见（重构、清理），拿它当路径会让界面显示一个
   * 谁都不是的文件名。`/dev/null` 只说明"这一侧没有文件"，不是路径。
   */
  path: string;
  oldPath: string | null;
  /** 二进制文件的 diff 没有 hunk，只有一行 `Binary files ... differ`。 */
  binary: boolean;
  /** `diff --git`、`index`、`new file mode` 这些原样保留，折叠区里显示。 */
  meta: string[];
  hunks: DiffHunk[];
}

/** `@@ -1,5 +1,7 @@`；逗号后的行数省略时表示 1 行（git 的紧凑写法）。 */
const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** `diff --git a/x.py b/x.py`。含空格的路径 git 会加引号，那种情况交给 `---`/`+++` 兜底。 */
const DIFF_GIT_HEADER = /^diff --git a\/(.*) b\/(.*)$/;

function stripPathPrefix(raw: string): string {
  return raw.replace(/^[ab]\//, "");
}

export function parseUnifiedDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;

  const rawLines = text.replace(/\r\n/g, "\n").split("\n");
  // 文本以换行结尾时 split 会产生一个空尾巴，它不是 diff 的内容
  if (rawLines.length > 0 && rawLines[rawLines.length - 1] === "") rawLines.pop();

  for (const line of rawLines) {
    if (line.startsWith("diff --git ")) {
      const m = DIFF_GIT_HEADER.exec(line);
      file = {
        path: m ? m[2] : "",
        oldPath: m ? m[1] : null,
        binary: false,
        meta: [line],
        hunks: [],
      };
      files.push(file);
      hunk = null;
      continue;
    }

    if (file === null) {
      // 裸 diff（没有 diff --git 头，比如手工拼的）也要能解析
      file = { path: "", oldPath: null, binary: false, meta: [], hunks: [] };
      files.push(file);
    }

    if (line.startsWith("@@")) {
      const m = HUNK_HEADER.exec(line);
      oldNo = m ? Number(m[1]) : 0;
      newNo = m ? Number(m[2]) : 0;
      hunk = { header: line, oldStart: oldNo, newStart: newNo, lines: [] };
      file.hunks.push(hunk);
      continue;
    }

    if (hunk !== null) {
      if (line.startsWith("\\")) {
        hunk.lines.push({ kind: "nonewline", text: line, oldLine: null, newLine: null });
      } else if (line.startsWith("+")) {
        hunk.lines.push({
          kind: "add",
          text: line.slice(1),
          oldLine: null,
          newLine: newNo++,
        });
      } else if (line.startsWith("-")) {
        hunk.lines.push({
          kind: "del",
          text: line.slice(1),
          oldLine: oldNo++,
          newLine: null,
        });
      } else {
        hunk.lines.push({
          kind: "context",
          text: line.startsWith(" ") ? line.slice(1) : line,
          oldLine: oldNo++,
          newLine: newNo++,
        });
      }
      continue;
    }

    // hunk 之外：文件级信息
    if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) {
      file.binary = true;
    }
    // 路径已在 diff --git 头里拿到，这里只给裸 diff 兜底；/dev/null 不是路径
    if (line.startsWith("--- ")) {
      const p = stripPathPrefix(line.slice(4));
      if (file.oldPath === null && p !== "/dev/null") file.oldPath = p;
    }
    if (line.startsWith("+++ ")) {
      const p = stripPathPrefix(line.slice(4));
      if (file.path === "" && p !== "/dev/null") file.path = p;
    }
    file.meta.push(line);
  }

  return files;
}

/** 单文件的增删行小结（文件头那行 "+3 -1"）。整体统计后端已给，这里只服务显示。 */
export function countChanges(file: DiffFile): { added: number; deleted: number } {
  let added = 0;
  let deleted = 0;
  for (const hunk of file.hunks) {
    for (const line of hunk.lines) {
      if (line.kind === "add") added++;
      if (line.kind === "del") deleted++;
    }
  }
  return { added, deleted };
}

/** 路径列表，给「改了哪几个文件」的摘要用。 */
export function changedPaths(files: DiffFile[]): string[] {
  return files.map((f) => f.path || f.oldPath || "(未知路径)");
}
