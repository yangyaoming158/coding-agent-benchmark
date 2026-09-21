# 前端

[AI Coding Agent 评测基准平台](../README.md) 的 Web 界面。

Next.js 16（App Router）· React 19 · TypeScript · Tailwind CSS · TanStack Query · Recharts。

**当前进度：E7-T1 ~ E7-T5 已完成**（前端骨架与导航、Runs / Run Detail、
Task Run Detail、Leaderboard、Benchmarks / Benchmark Detail / Task Detail），
外加 E6-T3 的人工复核页 `/review`。E7-T6 Failure Analysis、E7-T8 Dashboard
尚未开工，卡片见 [`docs/plan/10-tasks-plan.md`](../docs/plan/10-tasks-plan.md)。

## 跑起来

```bash
npm install          # 首次，或换机器之后
npm run dev          # 开发服务 → http://localhost:3000
```

在仓库根目录用 Makefile 也可以：

```bash
make dev             # 同时起后端 :8000 和前端 :3000
make dev-web         # 只起前端（后端没起的话首页会显示"连不上后端"）
make web-install     # 装前端依赖
make web-lint        # eslint + tsc
```

其他常用脚本：`npm run typecheck`（next typegen + tsc --noEmit）、
`npm run build` + `npm run start`（生产构建与启动）。Node 版本以 CI 为准：22。

### 环境变量与管理员令牌

只读页面不需要任何环境变量（后端地址默认 `http://localhost:8000`）。
要改后端地址就建 `frontend/.env.local`：

```bash
NEXT_PUBLIC_API_BASE=http://localhost:8000        # 可选，不写就是这个默认值
```

**管理员令牌不走环境变量。** 写接口（新建实验、取消、重试、人工复核）要带
`X-Bench-Token` 头，值和后端 `.env` 里的 `ADMIN_TOKEN` 是同一个串 —— 在页面上
有写按钮的地方（新建实验面板、实验详情、`/review`）粘贴一次，存在**本标签页**的
sessionStorage 里（`src/lib/admin-token.ts`），关掉标签页即失效。

不用 `NEXT_PUBLIC_ADMIN_TOKEN` 的原因：`NEXT_PUBLIC_*` 会打进所有人都下载得到的 JS，
而 compose 部署构建镜像时只传 `NEXT_PUBLIC_API_BASE`，令牌根本到不了页面。

后端起法、`.env` 各项含义见仓库根目录 [`README.md`](../README.md) 与 `.env.example`。

## 已完成页面

| 路由 | 页面 | 卡 |
| --- | --- | --- |
| `/` | 总览（Dashboard）：几版数据集、几个参赛者、跑了多少次实验、正在跑的进度、最近 5 次；底下保留平台自检 | E7-T8 |
| `/benchmarks` | 数据集版本列表：有哪几版、各多少题、发布状态 | E7-T5 |
| `/benchmarks/[slug]` | 数据集详情：语言/来源构成、门禁自检证据、逐题表格（仓库/难度/语言/状态 + 搜索，五个条件全走后端参数） | E7-T5 |
| `/tasks/[taskId]` | 单题详情：Issue 原文、F2P/P2P 清单、验证证据与隔离记录、各 Agent 历史表现 | E7-T5 |
| `/runs` | 实验运行列表 + 新建实验 | E7-T2 |
| `/runs/[id]` | 运行详情：进度、分组网格、取消与重试 | E7-T2 |
| `/task-runs/[id]` | 单题运行详情：判定三字段、Patch Viewer、测试结果表（有挂的默认只列失败）、日志搜索、轨迹时间线、失败归因（#127） | E7-T3 |
| `/agents` | Agent 与配置：Agent / 版本 / 模型 / 单价（$/MTok） | E7-T1 |
| `/leaderboard` | 排行榜：数据集下拉、多指标排序、成本-解决率散点、分面矩阵、口径自证；`?set=<slug>&version=<v>` 深链 | E7-T4 · E7-T5 |
| `/review` | 人工复核：抽检队列、三栏证据、盲态分类（E6-T3） | E6-T3 |

侧边导航在 `src/components/app-nav.tsx`，只挂已经可达的页面；新页面做完往
`NAV_ITEMS` 加一行即可。

## 数字语义靠断言脚本钉死

页面上的数字怎么读 —— 构成合计对不对得上、门禁证据缺字段算「判不了」还是 0、
题号精确匹配（`a-1` ≠ `a-11`）、报不出成本的行不许进散点图 —— 都编码在
`src/lib/*.ts` 的纯函数里。判错了页面照常渲染，只是结论安静地错，所以每块配一个
断言脚本：

```bash
npm run check                # 五个一起跑
npm run check:display        # src/lib/display.ts
npm run check:task-detail    # src/lib/diff.ts、trajectory.ts、search.ts
npm run check:leaderboard    # src/lib/leaderboard.ts
npm run check:tasks          # src/lib/tasks.ts
npm run check:dashboard      # src/lib/dashboard.ts（谁算参赛者、几版数据集、什么算正在跑）
```

零额外依赖（用仓库自带的 tsc 编译成 JS 再动态 import），不用起服务；全过时末尾打印
「全部通过」。

## API 类型不要手写

```bash
make gen-api    # 需要后端在跑
```

它从后端的 `/openapi.json` 生成 `src/lib/api-types.ts`。改完后端接口跑一次，
前端用错字段的地方会直接编译不过。手写的类型漂移了不会报错，只会在运行时拿到 `undefined`。

生成的文件**不要手改**，改了下次生成就没了。

## 界面纪律

见 [`docs/plan/07-platform-architecture.md`](../docs/plan/07-platform-architecture.md) §16.3。一句话版本：

- **不做**：登录美化、暗黑模式切换动效、复杂设计系统、页面转场动画
- **要做**：表格能筛能排、diff 能看清、日志能搜、长列表虚拟滚动、进度不刷屏

一条实用规则：**任何页面在 3 次点击内能到达「某个 AI 在某道题上为什么失败」的完整证据。**
这是这个平台的核心用户旅程，也是答辩演示的主线。

两条从真实数据上踩出来的（2026-09-21）：

- **长内容一律在容器里滚**（`max-h-[70vh] overflow-auto`）：日志前 1500 行平铺曾把单题运行页
  撑到 31,511 像素，后面的轨迹和归因没人翻得到。
- **表格短列不折行**（`whitespace-nowrap`，容器 `overflow-x-auto`）：不然状态列会变成"已/完/成"三行竖排。

## 开发规则

见仓库根目录的 [`AGENTS.md`](../AGENTS.md) 和 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。

本目录的 `AGENTS.md` 是 create-next-app 生成的 Next.js 专用提示（Next 16 相对多数模型的
训练数据有破坏性改动），保留着，但它不覆盖项目规则。
