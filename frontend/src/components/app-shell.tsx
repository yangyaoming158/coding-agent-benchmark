"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const ACTIVE_LINKS = [
  { href: "/", label: "平台概览" },
  { href: "/review", label: "人工复核" },
];

const PLANNED_LINKS = ["数据集", "Agents", "实验运行", "排行榜", "失败分析", "报告"];

/** 项目后台的最小导航壳；后续业务页沿用，不在每页重复造导航。 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[220px_1fr]">
      <aside className="border-b border-neutral-200 bg-white lg:min-h-screen lg:border-b-0 lg:border-r">
        <div className="px-5 py-5">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-neutral-500">
            Coding Agent
          </p>
          <p className="mt-1 text-sm font-semibold text-neutral-950">评测基准平台</p>
        </div>
        <nav className="flex gap-2 overflow-x-auto px-3 pb-3 lg:block lg:space-y-1">
          {ACTIVE_LINKS.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`block whitespace-nowrap rounded-md px-3 py-2 text-sm ${
                  active
                    ? "bg-neutral-900 font-medium text-white"
                    : "text-neutral-600 hover:bg-neutral-100 hover:text-neutral-950"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
          {PLANNED_LINKS.map((label) => (
            <span
              key={label}
              className="hidden rounded-md px-3 py-2 text-sm text-neutral-300 lg:block"
              title="后续任务实现"
            >
              {label}
            </span>
          ))}
        </nav>
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
