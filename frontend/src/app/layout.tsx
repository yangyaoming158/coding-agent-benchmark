import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "AI Coding Agent 评测基准平台",
  description:
    "把开源项目里已经修好的真 bug 回退到修复前，把当初那份 issue 交给被测 AI，再用项目自己的测试验证它的补丁。",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  // 字体用系统栈，不引 next/font/google：构建时要联网拉字体文件，
  // 这台机器走代理，拉不到就整个构建失败。为了两个字形不值得。
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-neutral-50 text-neutral-900">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
