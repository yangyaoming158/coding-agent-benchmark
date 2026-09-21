"use client";

import { useState } from "react";

/**
 * 两步确认的按钮。
 *
 * 取消会掐掉一场可能已经跑了两个小时的实验，而且**不可撤销** ——
 * 不该一点就走。重试失败项没这么重，但同样会往队列里投一堆活，也走这道门。
 *
 * 不用 `window.confirm`：它挡在浏览器那一层，样式不可控，而且没法在确认框里
 * 说清"点了之后到底会发生什么"——那恰恰是用户按下按钮前要知道的。
 * 就地展开的成本一样低，但每一步都看得见。
 */
export function ConfirmAction({
  label,
  warning,
  confirmText,
  tone = "danger",
  onConfirm,
  isPending,
  error,
  disabled = false,
  disabledReason,
}: {
  label: string;
  /** 展开后显示的一句话：按下去会发生什么 */
  warning: string;
  confirmText: string;
  tone?: "danger" | "neutral";
  onConfirm: () => void;
  isPending: boolean;
  /** 失败时显示后端原样返回的那句话 */
  error: string | null;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const [armed, setArmed] = useState(false);

  const idleClass =
    tone === "danger"
      ? "border-red-300 text-red-700 hover:bg-red-50"
      : "border-neutral-300 text-neutral-700 hover:bg-neutral-50";

  if (!armed) {
    return (
      <div>
        <button
          type="button"
          onClick={() => setArmed(true)}
          disabled={disabled}
          title={disabled ? disabledReason : undefined}
          className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:border-neutral-200 disabled:text-neutral-400 disabled:hover:bg-transparent ${idleClass}`}
        >
          {label}
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-neutral-200 bg-neutral-50 p-3">
      <p className="text-xs leading-relaxed text-neutral-600">{warning}</p>
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={onConfirm}
          disabled={isPending}
          className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:bg-neutral-400"
        >
          {isPending ? "提交中……" : confirmText}
        </button>
        <button
          type="button"
          onClick={() => setArmed(false)}
          disabled={isPending}
          className="rounded-md px-3 py-1.5 text-sm text-neutral-600 transition-colors hover:bg-neutral-100 disabled:cursor-not-allowed disabled:text-neutral-400"
        >
          先不
        </button>
      </div>
      {error !== null && (
        <p className="mt-2 text-xs leading-relaxed text-red-600">{error}</p>
      )}
    </div>
  );
}
