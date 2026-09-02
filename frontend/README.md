# 前端

[AI Coding Agent 评测基准平台](../README.md) 的 Web 界面。

Next.js 16（App Router）· React 19 · TypeScript · Tailwind CSS · TanStack Query。

## 跑起来

```bash
make dev        # 在仓库根目录跑，同时起后端 :8000 和前端 :3000
make dev-web    # 只起前端（后端没起的话首页会显示"连不上后端"）
```

## API 类型不要手写

```bash
make gen-api    # 需要后端在跑
```

它从后端的 `/openapi.json` 生成 `src/lib/api-types.ts`。改完后端接口跑一次，
前端用错字段的地方会直接编译不过。手写的类型漂移了不会报错，只会在运行时拿到 `undefined`。

生成的文件**不要手改**，改了下次生成就没了。

## 目前有什么

只有一个首页，作用是把「前端 → 后端 → 数据库」这条链真的调通一次。
完整的页面（数据集、实验进度、补丁 diff、排行榜）是 E7 的活，
等后端接口（E5）出来之后再做。

页面清单和优先级见 [`docs/plan/07-platform-architecture.md`](../docs/plan/07-platform-architecture.md) §16.2。

## 界面纪律

见 §16.3。一句话版本：

- **不做**：登录美化、暗黑模式切换动效、复杂设计系统、页面转场动画
- **要做**：表格能筛能排、diff 能看清、日志能搜、长列表虚拟滚动、进度不刷屏

一条实用规则：**任何页面在 3 次点击内能到达「某个 AI 在某道题上为什么失败」的完整证据。**
这是这个平台的核心用户旅程，也是答辩演示的主线。

## 开发规则

见仓库根目录的 [`AGENTS.md`](../AGENTS.md) 和 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。

本目录的 `AGENTS.md` 是 create-next-app 生成的 Next.js 专用提示（Next 16 相对多数模型的
训练数据有破坏性改动），保留着，但它不覆盖项目规则。
