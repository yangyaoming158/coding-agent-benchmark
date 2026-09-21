# AI Coding Agent 评测基准平台

给 AI 编程助手打分的平台。

从真实开源项目里挑一个已经被修复过的 bug，把代码回退到修复之前，把当时的 issue 描述交给被测的 AI，让它自己改代码。改完之后用项目原本的测试来验证它改对了没有。做法参考 SWE-Bench。

高校软件工程综合实训项目，周期 4 周。

## 当前进度

**以 `docs/plan/10-tasks-plan.md` 里标 ✅ 的任务为准**：

```bash
grep -c '^### .*✅' docs/plan/10-tasks-plan.md   # 已完成
grep -c '^### E'    docs/plan/10-tasks-plan.md   # 总数
```

这里不写具体数字。手写的进度描述一定会过期，而过期的进度会**误导接手的人**——
尤其是 AI 编程助手，它会把 README 当成前提去做判断。任务表是每个任务做完时
顺手更新的，那才是可信的那一份。

几个不会变的事实：评测协议 `docs/evaluation-protocol.md` 是 **FROZEN v1.2**，
79 条规定、780 种状态组合穷举验过；数据库 17 张表，协议里三条最要命的规定
落成了数据库约束；规划文档 13 份在 `docs/plan/`。

## 快速开始

**先确认操作系统**：需要 Linux 或 **Windows + WSL2**。原生 Windows 跑不了，
理由和怎么办见 [`CONTRIBUTING.md`](CONTRIBUTING.md) 第 1 节。

```bash
make install     # 装前后端依赖 + git 钩子
make db-up       # 起本地 Postgres（容器，端口 5433）
make migrate     # 建表
make seed        # 写入三个哨兵 Agent
make dev         # 后端 :8000 前端 :3000
```

上面是开发模式。**不想装 uv / Node、只想把平台跑起来**（比如验收、演示、换一台机器），
走 docker compose 一键部署：[`docs/deployment.md`](docs/deployment.md)。

三份交付文档（DEL-06）：

| 想知道 | 看 |
|:---|:---|
| 怎么装 | [`docs/deployment.md`](docs/deployment.md) —— 每条命令都在本机跑过，末尾有验收记录 |
| 怎么用（建镜像 → 灌题 → 配 Agent → 建实验 → 看进度 → 排行榜 / 报告 → 归因 / 抽检 → 发布数据集） | [`docs/usage.md`](docs/usage.md) |
| 怎么做的（模块分层、17 张表、沙箱、判定、没做的和为什么） | [`docs/architecture.md`](docs/architecture.md) |

想动手改代码，先看 [`CONTRIBUTING.md`](CONTRIBUTING.md)。
**接手别人已经跑起来的环境**，看那份文档的「接手一份已有的环境」一节 ——
`git clone` 拿不到镜像、缓存和密钥，那几样得单独交接。

## 怎么读这份规划

推荐直接打开 **`docs/plan/report.html`**，它是 13 份文档合成的单页版本，带目录导航和架构图。

或者按需读 `docs/plan/` 下的 markdown 源文件，索引见 [`docs/plan/README.md`](docs/plan/README.md)。

改完 markdown 后重新生成 HTML：

```bash
python3 docs/plan/_build_report.py .
```

## 参与开发前必读

[`AGENTS.md`](AGENTS.md) —— 开发规则，包括沟通方式、三个不能随便改的冻结件、五个最容易踩的坑、完成标准、Git 约定。

AI 编程助手也读这个文件（`CLAUDE.md` 只是指向它的入口）。

## 技术选型

后端 Python 3.11 + FastAPI + SQLAlchemy 2.0，前端 Next.js 16 + React 19 + TypeScript，数据库 PostgreSQL 16（队列也建在里面，不引 Redis），制品存储本地文件系统（MinIO 抽象层已就绪、实现未接，见 `docs/architecture.md` §12），沙箱 Docker。

选型理由见 [`docs/plan/08-adr.md`](docs/plan/08-adr.md)。
