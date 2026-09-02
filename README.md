# AI Coding Agent 评测基准平台

给 AI 编程助手打分的平台。

从真实开源项目里挑一个已经被修复过的 bug，把代码回退到修复之前，把当时的 issue 描述交给被测的 AI，让它自己改代码。改完之后用项目原本的测试来验证它改对了没有。做法参考 SWE-Bench。

高校软件工程综合实训项目，周期 4 周。

## 当前状态

**地基搭完了，业务逻辑还没开始写。**

已完成：

- 13 份规划文档（`docs/plan/`），含需求拆解、可行性分析、架构设计、12 条架构决策记录、四周计划、风险登记
- 开发环境就绪：Docker，沙箱的资源限制和网络隔离已实测验证通过
- **评测协议 v1.2 已冻结**（`docs/evaluation-protocol.md`）—— 79 条规定，780 种状态组合穷举验过
- **数据库 17 张表 + 迁移**，协议里三条最要命的规定落成了数据库约束
- 前后端骨架跑通：`make dev` 能同时起 API 和前端，前端类型从后端 OpenAPI 生成

下一步：Task Schema 冻结（E1-T1）、工作区物化与防泄题（E2-T1）。

## 快速开始

```bash
make install     # 装前后端依赖 + git 钩子
make db-up       # 起本地 Postgres（容器，端口 5433）
make migrate     # 建表
make seed        # 写入三个哨兵 Agent
make dev         # 后端 :8000 前端 :3000
```

想动手改代码，先看 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

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

后端 Python 3.11 + FastAPI + SQLAlchemy 2.0，前端 Next.js 16 + React 19 + TypeScript，数据库 PostgreSQL 16（队列也建在里面，不引 Redis），制品存储 MinIO，沙箱 Docker。

选型理由见 [`docs/plan/08-adr.md`](docs/plan/08-adr.md)。
