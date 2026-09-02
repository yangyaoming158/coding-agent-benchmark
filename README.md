# AI Coding Agent 评测基准平台

给 AI 编程助手打分的平台。

从真实开源项目里挑一个已经被修复过的 bug，把代码回退到修复之前，把当时的 issue 描述交给被测的 AI，让它自己改代码。改完之后用项目原本的测试来验证它改对了没有。做法参考 SWE-Bench。

高校软件工程综合实训项目，周期 4 周。

## 当前状态

**规划已完成，业务代码还没开始写。**

已完成：

- 13 份规划文档（`docs/plan/`），含需求拆解、可行性分析、架构设计、12 条架构决策记录、四周计划、风险登记
- 开发环境就绪：Docker 29.7.2，沙箱的资源限制和网络隔离已实测验证通过

下一步：冻结评测语义（§6），然后搭仓库骨架。

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

后端 Python + FastAPI，前端 Next.js + React，数据库 PostgreSQL，制品存储 MinIO，沙箱 Docker。

选型理由见 [`docs/plan/08-adr.md`](docs/plan/08-adr.md)。
