"use client";

/**
 * 榜单自证区：六条准入规则 + 被人工排除的实验。
 *
 * 后端注释原话："一个不说自己筛掉了什么的排行榜没法复核。"
 * 默认折叠、标题带条数 —— 不占版面，但一眼看得见它的存在。
 */

import Link from "next/link";
import type { ExcludedRun } from "@/lib/leaderboard";

export function EligibilityNote({
  rules,
  excluded,
}: {
  rules: string[];
  excluded: ExcludedRun[];
}) {
  const excludedText =
    excluded.length > 0 ? `排除 ${excluded.length} 次实验` : "没有实验被排除";
  return (
    <details className="rounded-md border border-neutral-200 bg-white px-4 py-3">
      <summary className="cursor-pointer text-sm text-neutral-600">
        榜单口径：{rules.length} 条准入规则 · {excludedText}
      </summary>
      <ol className="mt-3 list-decimal space-y-1 pl-5 text-xs leading-relaxed text-neutral-600">
        {rules.map((rule) => (
          <li key={rule}>{rule}</li>
        ))}
      </ol>
      {excluded.length > 0 && (
        <div className="mt-3 border-t border-neutral-100 pt-3">
          <p className="text-xs font-medium text-neutral-700">被人工排除的实验</p>
          <ul className="mt-1 space-y-1 text-xs text-neutral-600">
            {excluded.map((run) => (
              <li key={run.evaluation_run_id}>
                <Link
                  href={`/runs/${run.evaluation_run_id}`}
                  className="font-mono text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
                >
                  #{run.evaluation_run_id}
                </Link>
                ：{run.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </details>
  );
}
