> **这个项目的开发规则在仓库根目录的 [`AGENTS.md`](../AGENTS.md)**，先读那个。
> 下面是 create-next-app 生成的 Next.js 专用提示，保留是因为 Next 16 相对多数模型的
> 训练数据有破坏性改动，写前端代码前值得看一眼。

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
