import Link from "next/link";
import { Dashboard } from "@/components/dashboard";
import { PlatformStatus } from "@/components/platform-status";

/**
 * 首页 = Dashboard（E7-T8，§16.2 的第一行）。
 *
 * 上面是总览：几版数据集、几个参赛者、跑了多少次实验、正在跑的进度、最近 5 次。
 * 下面留着平台自检 —— 它是 E7-T1 时首页唯一的内容，把「前端 → 后端 → 数据库」
 * 这条链真调通一次的证据；演示时后端没起来，先看这一栏。
 */
export default function Home() {
  return (
    <div className="mx-auto w-full max-w-5xl">
      <header className="border-b border-neutral-200 pb-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          AI Coding Agent 评测基准平台
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-600">
          把开源项目里已经修好的真 bug 回退到修复前，把当初那份 issue
          原样交给被测 AI，再用项目自己的测试验证它交出来的补丁。
        </p>
      </header>

      <Dashboard />

      <section className="mt-10">
        <h2 className="text-sm font-semibold text-neutral-900">从哪看起</h2>
        <p className="mt-1 text-xs text-neutral-500">
          三次点击到达「某个 AI 在某道题上为什么失败」的完整证据：
        </p>
        <ol className="mt-3 space-y-1 text-sm text-neutral-600">
          {[
            ["/leaderboard", "排行榜", "同一版数据集上各 Agent 的解决率与成本，点参赛者进实验"],
            ["/runs", "实验运行", "点一次实验，看逐题网格；点一格，看补丁、用例、日志、轨迹和归因"],
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
      </section>

      <PlatformStatus />
    </div>
  );
}
