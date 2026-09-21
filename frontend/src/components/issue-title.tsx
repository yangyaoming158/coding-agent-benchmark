/**
 * issue 标题：把 Markdown 里 `反引号` 包着的标识符渲染成 <code>，其余原样。
 *
 * 只处理反引号（`display.ts` 的 `titleSegments`），不走完整 Markdown ——
 * 标题里不该出现链接和图片，真出现了原样显示比渲染出来更安全。
 */
import { titleSegments } from "@/lib/display";

export function IssueTitle({ title }: { title: string }) {
  return (
    <>
      {titleSegments(title).map((segment, index) =>
        segment.code ? (
          <code
            key={index}
            className="rounded bg-neutral-100 px-1 font-mono text-[0.85em] font-normal text-neutral-800"
          >
            {segment.text}
          </code>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  );
}
