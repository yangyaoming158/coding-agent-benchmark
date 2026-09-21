import type { Metadata } from "next";
import "./globals.css";
import { AppNav } from "@/components/app-nav";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "AI Coding Agent 评测基准平台",
  description:
    "把开源项目里已经修好的真 bug 回退到修复前，把当初那份 issue 交给被测 AI，再用项目自己的测试验证它的补丁。",
};

/**
 * 根布局：左侧固定导航 + 右侧内容区，所有页面共用，不在每页重复造导航。
 *
 * 字体用系统栈，不引 next/font/google：构建时要联网拉字体文件，
 * 这台机器走代理，拉不到就整个构建失败。为了两个字形不值得。
 */
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full bg-neutral-50 text-neutral-900">
        <Providers>
          {/* 应用外壳：左侧导航固定宽度，右侧内容区自适应。
              宽度按"1200 宽窗口 + 浏览器 125% 缩放"（答辩投影）定：那时视口只有 960 CSS px，
              侧栏 224 + 两侧内边距 64 曾把内容区挤到 655 px，排行榜最右的实验链接被推出屏幕外。 */}
          <div className="flex min-h-screen">
            <aside className="w-48 shrink-0 border-r border-neutral-200 bg-white px-3 py-6">
              <div className="px-3">
                <p className="text-sm font-semibold tracking-tight">
                  AI Coding Agent
                </p>
                <p className="mt-0.5 text-xs text-neutral-500">评测基准平台</p>
              </div>
              <AppNav />
            </aside>
            <main className="min-w-0 flex-1 px-5 py-8">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
