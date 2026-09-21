"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * 侧边导航。
 *
 * 只放已经可达的页面 —— 导航里挂一个 404 的链接，比不放更糟。
 * 新页面做出来之后往这里加一行即可（E7-T6 失败分析、E7-T8 Dashboard）。
 */
const NAV_ITEMS = [
  { href: "/", label: "平台自检" },
  { href: "/benchmarks", label: "数据集" },
  { href: "/runs", label: "实验运行" },
  { href: "/agents", label: "Agent 与配置" },
  { href: "/leaderboard", label: "排行榜" },
  { href: "/review", label: "人工复核" },
] as const;

/** 当前项高亮。`/` 要精确匹配，否则所有路径都算命中首页。 */
function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function AppNav() {
  const pathname = usePathname();

  return (
    <nav className="mt-6 flex flex-col gap-0.5">
      {NAV_ITEMS.map((item) => {
        const active = isActive(pathname, item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={
              active
                ? "rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white"
                : "rounded-md px-3 py-1.5 text-sm text-neutral-600 transition-colors hover:bg-neutral-100 hover:text-neutral-900"
            }
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
