import Link from "next/link";
import { PlatformStatus } from "@/components/platform-status";

/**
 * 首页。
 *
 * 目前只放一件事：平台自检。完整的 Dashboard 是 E7 的活。
 * 这一页存在的意义是把"前端 → 后端 → 数据库"这条链真的跑通一次 ——
 * 脚手架建起来但从没调通过后端，等于什么都没验证。
 */
export default function Home() {
  return (
    <div className="mx-auto w-full max-w-4xl">
      <header className="border-b border-neutral-200 pb-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          AI Coding Agent 评测基准平台
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600">
          把开源项目里已经修好的真 bug 回退到修复前，把当初那份 issue
          原样交给被测 AI，再用项目自己的测试验证它交出来的补丁。
        </p>
      </header>

      <PlatformStatus />

      <section className="mt-10">
        <h2 className="text-sm font-semibold text-neutral-900">从哪看起</h2>
        <p className="mt-1 text-xs text-neutral-500">
          三次点击到达「某个 AI 在某道题上为什么失败」的完整证据：
        </p>
        <ol className="mt-3 space-y-1 text-sm text-neutral-600">
          {[
            ["/leaderboard", "排行榜", "同一版数据集上各 Agent 的解决率与成本"],
            ["/runs", "实验运行", "点一次实验，看逐题网格"],
            ["/benchmarks", "数据集", "每一版的构成、门禁证据、逐题表"],
          ].map(([href, label, desc]) => (
            <li key={href} className="flex gap-2">
              <span className="text-neutral-400">·</span>
              <Link
                href={href}
                className="text-neutral-900 underline decoration-neutral-300 underline-offset-2 hover:decoration-neutral-900"
              >
                {label}
              </Link>
              <span className="text-neutral-500">{desc}</span>
            </li>
          ))}
        </ol>
        <p className="mt-3 text-xs text-neutral-500">
          还没做的：失败分析（E7-T6）、Dashboard（E7-T8）。
        </p>
      </section>
    </div>
  );
}
