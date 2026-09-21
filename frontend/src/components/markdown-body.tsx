"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 把 issue 正文按 Markdown 渲染。
 *
 * issue 来自 GitHub，本来就是 Markdown：代码块、列表、加粗都是作者的意思，
 * 当纯文本显示会把 ``` 和 ** 原样露出来 —— 这是答辩时第一眼看到的内容。
 *
 * 安全边界：react-markdown 默认**不渲染原始 HTML**（`<script>` 会当文字显示），
 * 这里也不加 rehype-raw。issue 正文是从公网抓来的不可信文本，这条不能松。
 * 链接加 `rel="noopener noreferrer"`、另开窗口，避免从评测平台跳走。
 *
 * 样式：Tailwind v4 的 preflight 把标题、列表的默认样式全抹了，所以这里用
 * 选择器把最常用的几种元素补回来，没引 typography 插件 —— 为一个正文块
 * 不值得多一个依赖。
 */
export function MarkdownBody({ text }: { text: string }) {
  return (
    <div
      className="text-sm leading-relaxed text-neutral-800 break-words
        [&_a]:text-neutral-900 [&_a]:underline [&_a]:decoration-neutral-300 [&_a]:underline-offset-2
        [&_p]:my-2 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0
        [&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:text-base [&_h1]:font-semibold
        [&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:text-base [&_h2]:font-semibold
        [&_h3]:mt-3 [&_h3]:mb-1 [&_h3]:text-sm [&_h3]:font-semibold
        [&_h4]:mt-3 [&_h4]:mb-1 [&_h4]:text-sm [&_h4]:font-semibold
        [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5
        [&_li]:my-0.5
        [&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-neutral-300 [&_blockquote]:pl-3 [&_blockquote]:text-neutral-600
        [&_code]:rounded [&_code]:bg-neutral-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.85em]
        [&_pre]:my-2 [&_pre]:max-h-96 [&_pre]:overflow-auto [&_pre]:rounded-md [&_pre]:bg-neutral-100 [&_pre]:p-3
        [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-xs
        [&_table]:my-2 [&_table]:text-xs [&_th]:border [&_th]:border-neutral-200 [&_th]:bg-neutral-50 [&_th]:px-2 [&_th]:py-1 [&_td]:border [&_td]:border-neutral-200 [&_td]:px-2 [&_td]:py-1
        [&_hr]:my-3 [&_hr]:border-neutral-200
        [&_img]:max-w-full"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
