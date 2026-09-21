"use client";

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "@/lib/api";
import { countChanges, parseUnifiedDiff, type DiffLine } from "@/lib/diff";
import { formatBytes } from "@/lib/display";
import {
  useArtifactText,
  type ArtifactTextKind,
  type PatchSummary,
} from "@/lib/queries";

/**
 * Patch Viewer —— §16.3 说的"diff 能看清"。
 *
 * 为什么有两份补丁（协议 C-08a/C-08b）：
 *
 * - `AGENT_NORMALIZED`：过滤掉受保护路径（测试文件、conftest 之类，C-41）
 *   **之后**的那份，实际参与判定的就是它；
 * - `AGENT_RAW`：AI 交出来的原样。
 *
 * 两份不一致的时候，**差异本身就是证据** —— 比如归一化后变成空补丁，
 * 但原文里改了一堆测试文件，那是"试图改测试蒙混过关"的直接记录（C-13d），
 * 要触发人工复核。所以这个视图允许来回切，而不是只显示能看的那一份。
 *
 * `PatchKind` 里另外两个值（`GOLD` 官方补丁、`TEST` 官方测试补丁）绝不会
 * 出现在这里 —— 后端取补丁的端点用的是窄枚举 `AgentPatchKind`，
 * 官方补丁被挡在外面（C-44/C-76）。类型上也就没得选。
 */

type PatchKind = PatchSummary["kind"];
type AgentPatchKind = Extract<ArtifactTextKind, "AGENT_RAW" | "AGENT_NORMALIZED">;

/** 归一化那份在前：它是判定依据，先看它。 */
const PATCH_ORDER: AgentPatchKind[] = ["AGENT_NORMALIZED", "AGENT_RAW"];

const PATCH_LABEL: Record<AgentPatchKind, string> = {
  AGENT_NORMALIZED: "标准化后",
  AGENT_RAW: "AI 原样",
};

function isAgentPatchKind(kind: PatchKind): kind is AgentPatchKind {
  return kind === "AGENT_NORMALIZED" || kind === "AGENT_RAW";
}

/** 行的底色。上下文行不设底色，让增删行自己跳出来。 */
const ROW_STYLE: Record<DiffLine["kind"], string> = {
  add: "bg-emerald-50",
  del: "bg-red-50",
  context: "",
  nonewline: "text-neutral-400",
};

const PREFIX: Record<DiffLine["kind"], string> = {
  add: "+",
  del: "-",
  context: " ",
  nonewline: "",
};

function DiffRow({ line }: { line: DiffLine }) {
  return (
    <div className={`flex ${ROW_STYLE[line.kind]}`}>
      <span className="w-12 shrink-0 select-none pr-2 text-right text-neutral-400">
        {line.oldLine ?? ""}
      </span>
      <span className="w-12 shrink-0 select-none pr-2 text-right text-neutral-400">
        {line.newLine ?? ""}
      </span>
      <span className="w-4 shrink-0 select-none text-neutral-400">
        {PREFIX[line.kind]}
      </span>
      <span className="whitespace-pre pr-4 text-neutral-900">{line.text}</span>
    </div>
  );
}

/**
 * 把文本放进剪贴板。
 *
 * `navigator.clipboard` 只在安全上下文（HTTPS 或 localhost）里存在 —— 答辩时从
 * 另一台机器开 `http://192.168.x.x:3000` 就没有它，直接调用会抛
 * "Cannot read properties of undefined"，按钮看起来像坏了。所以退回老办法：
 * 临时 textarea + `document.execCommand("copy")`，两条路都不通再告诉用户手动选。
 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 权限被拒之类，落到下面的兜底
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

function shortDigest(digest: string): string {
  return digest.length > 12 ? `${digest.slice(0, 12)}…` : digest;
}

export function DiffViewer({
  taskRunId,
  patches,
}: {
  taskRunId: number;
  patches: PatchSummary[];
}) {
  const usable = useMemo(
    () =>
      patches
        .filter((p): p is PatchSummary & { kind: AgentPatchKind } =>
          isAgentPatchKind(p.kind),
        )
        .sort((a, b) => PATCH_ORDER.indexOf(a.kind) - PATCH_ORDER.indexOf(b.kind)),
    [patches],
  );

  const [picked, setPicked] = useState<AgentPatchKind | null>(null);
  const active = picked ?? usable[0]?.kind ?? null;
  const summary = usable.find((p) => p.kind === active) ?? null;

  const text = useArtifactText(taskRunId, active ?? "AGENT_NORMALIZED", {
    enabled: active !== null,
  });

  const files = useMemo(
    () => (text.data !== undefined ? parseUnifiedDiff(text.data) : []),
    [text.data],
  );

  // 复制结果只显示两秒：成功不用一直挂着，失败要让人看到再消失
  const [copied, setCopied] = useState<"ok" | "fail" | null>(null);
  useEffect(() => {
    if (copied === null) return;
    const timer = window.setTimeout(() => setCopied(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (usable.length === 0) {
    return (
      <p className="rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
        这次执行没有产生补丁。判定没走到“捕获补丁”那一步时就是这样
        （比如 Agent 还没启动、或者平台故障提前终止）。
      </p>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        {usable.map((p) => (
          <button
            key={p.kind}
            type="button"
            onClick={() => setPicked(p.kind)}
            className={`rounded-md border px-3 py-1 text-xs transition-colors ${
              p.kind === active
                ? "border-neutral-900 bg-neutral-900 text-white"
                : "border-neutral-300 text-neutral-700 hover:bg-neutral-50"
            }`}
          >
            {PATCH_LABEL[p.kind]}
            <span className="ml-2 font-mono">
              +{p.lines_added} −{p.lines_deleted}
            </span>
          </button>
        ))}
        <button
          type="button"
          onClick={() => {
            if (text.data === undefined) return;
            void copyText(text.data).then((ok) => setCopied(ok ? "ok" : "fail"));
          }}
          disabled={text.data === undefined}
          className="ml-auto rounded-md border border-neutral-300 px-3 py-1 text-xs text-neutral-700 transition-colors hover:bg-neutral-50 disabled:cursor-not-allowed disabled:text-neutral-400"
        >
          {copied === "ok" ? "已复制" : copied === "fail" ? "复制失败，请手动选择" : "复制补丁"}
        </button>
      </div>

      <p className="mt-2 text-xs leading-relaxed text-neutral-500">
        「标准化后」是过滤掉受保护路径（测试文件等，C-41）之后实际参与判定的那份；
        「AI 原样」是它交出来的原文。两者不一致时，差异本身就是证据（C-08b）。
      </p>

      {summary !== null && (
        <p className="mt-2 font-mono text-xs text-neutral-500">
          {summary.files_changed} 个文件 · {formatBytes(summary.size_bytes)} · sha256{" "}
          {shortDigest(summary.sha256)}
          {summary.applies_cleanly === false && (
            <span className="ml-2 text-amber-600">应用时有冲突</span>
          )}
          {summary.applies_cleanly === null && (
            <span className="ml-2 text-neutral-400">未做应用检查</span>
          )}
        </p>
      )}

      {text.isLoading && (
        <p className="mt-3 text-sm text-neutral-500">正在读取补丁……</p>
      )}
      {text.error && (
        <p className="mt-3 text-sm text-red-600">{errorMessage(text.error)}</p>
      )}

      {text.data !== undefined && files.length === 0 && (
        <p className="mt-3 rounded-md border border-neutral-200 bg-white px-4 py-6 text-sm text-neutral-500">
          {text.data.trim() === ""
            ? "这份补丁是空的 —— 没有对任何文件产生改动。"
            : "补丁里没有可解析的文件段。下面是原文。"}
        </p>
      )}
      {text.data !== undefined &&
        files.length === 0 &&
        text.data.trim() !== "" && (
          <pre className="mt-3 max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white p-4 font-mono text-xs text-neutral-700">
            {text.data}
          </pre>
        )}

      {files.length > 0 && (
        <div className="mt-3 max-h-[70vh] overflow-auto rounded-md border border-neutral-200 bg-white">
          {files.map((file, index) => {
            const changes = countChanges(file);
            return (
              <details key={index} open className="border-b border-neutral-200 last:border-b-0">
                <summary className="flex cursor-pointer flex-wrap items-center gap-2 bg-neutral-50 px-4 py-2 text-xs">
                  <span className="font-mono font-medium text-neutral-800">
                    {file.path || file.oldPath || "(未知路径)"}
                  </span>
                  {file.oldPath !== null && file.oldPath !== file.path && (
                    <span className="font-mono text-neutral-400">
                      ← {file.oldPath}
                    </span>
                  )}
                  <span className="font-mono text-emerald-700">+{changes.added}</span>
                  <span className="font-mono text-red-700">−{changes.deleted}</span>
                  {file.binary && (
                    <span className="text-neutral-500">二进制文件</span>
                  )}
                </summary>

                {file.binary ? (
                  <p className="px-4 py-3 text-xs text-neutral-500">
                    二进制文件没有逐行差异，只看得到「变了」这件事。
                  </p>
                ) : (
                  <div className="overflow-x-auto font-mono text-xs leading-relaxed">
                    {file.hunks.map((hunk, hunkIndex) => (
                      <div key={hunkIndex}>
                        <div className="bg-blue-50/60 px-4 py-1 text-blue-800">
                          {hunk.header}
                        </div>
                        {hunk.lines.map((line, lineIndex) => (
                          <DiffRow key={lineIndex} line={line} />
                        ))}
                      </div>
                    ))}
                  </div>
                )}
              </details>
            );
          })}
        </div>
      )}
    </div>
  );
}
