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
    <main className="mx-auto w-full max-w-4xl px-6 py-12">
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
        <h2 className="text-sm font-semibold text-neutral-900">还没做的页面</h2>
        <ul className="mt-3 space-y-1 text-sm text-neutral-600">
          {[
            "数据集与题目详情",
            "实验列表与实时进度",
            "单次评测详情：补丁 diff、逐条用例结果、失败归因",
            "排行榜",
          ].map((item) => (
            <li key={item} className="flex gap-2">
              <span className="text-neutral-400">·</span>
              {item}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-neutral-500">
          这些属于 E7，等后端接口（E5）出来之后再做。
        </p>
      </section>
    </main>
  );
}
