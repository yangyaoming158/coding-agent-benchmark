# 32 工程流程与版本管理（补充章）

> 本章是对原报告 §1–§31 的补充。审计发现原规划对 Git、任务跟踪与"完成的定义"只有零散提及（E0-T2 的 pre-commit、§27.4 的 PR CI、§24 的 harness git sha），缺少成体系的约定。本章补齐，并入 E0-T2 的交付范围。

## 32.1 三层分离：规划 / 跟踪 / 完成判定

一个常见的错误是"给每个任务再单独写一份工作清单"。**不要这么做**——那会制造第二个真相源，与 `10-tasks-plan.md` 里已有的 AC 必然漂移，两周后没人知道该信哪一份。这正是我们在 §6 评测语义上极力避免的问题。

正确的分层：

| 层 | 载体 | 变更频率 | 职责 |
|:---|:---|:---|:---|
| **规划层** | `10-tasks-plan.md`（本报告 §21） | 冻结，仅在范围变更时改 | 任务的 Why / AC / 依赖 / 估算 / 优先级 |
| **跟踪层** | GitHub Issues（每任务 1 个 Issue） | 每天 | 状态、负责人、阻塞、进度讨论 |
| **判定层** | 统一 DoD（本节 32.2）+ 任务自带 AC | 一次定义 | 什么叫"做完了" |

**关键**：Issue 的正文**不复制** AC，只写一行链接指向 `10-tasks-plan.md#e2-t2`。Issue 负责"状态与讨论"，文档负责"内容"，各守其职。

## 32.2 统一 Definition of Done（所有任务通用）

与其给每个任务写一份定制清单，不如定义一条**全体适用**的完成标准；任务特有的部分由它自己在 §21 中的 AC 提供。二者合起来就是验收依据。

一个任务只有同时满足下列全部条件才可关闭：

- [ ] **AC 全部达成**，并在 Issue 中贴出**可复核的证据**（测试输出、命令回显、截图），而非仅声明"做完了"
- [ ] **代码经 PR 合入主干**，至少 1 人 review 通过
- [ ] **新增/改动逻辑有对应自动化测试**，CI 全绿
- [ ] **公共接口、领域枚举、复杂算法有中文注释**（NFR-07 是硬性交付要求，不是可选项）
- [ ] **若改动了协议 / 数据库 / 枚举**：同步更新 `docs/` 与 Alembic 迁移，且迁移可 `upgrade` + `downgrade`
- [ ] **若引入新的环境依赖**：更新部署文档与 `scripts/check_env.py`

> 第 1 条的"贴证据"是这套流程里最有价值的一条。它把"我觉得做完了"变成"这是它工作的样子"，成本只有一次复制粘贴，但在 4 人并行开发时能省掉大量返工——也顺带积累了答辩材料。

## 32.3 任务跟踪：为什么用 GitHub Issues

| 方案 | 优点 | 缺点 | 结论 |
|:---|:---|:---|:---|
| 每任务一份 Markdown 清单 | 无 | 双真相源、必然漂移、无状态 | ✗ |
| 单个 `TASKBOARD.md` | 零成本 | 多人并发编辑冲突；不产生过程证据 | △ 备选 |
| **GitHub Issues + Projects 看板** | 状态清晰、可指派、与 PR 自动关联、**天然沉淀工程过程证据** | 需一次性录入约 60 个 Issue | **✓ 推荐** |

高校综合实训通常同时考察"结果"与"过程"。Issue → 分支 → PR → review → 合并 → 自动关闭 Issue 这条链路本身就是过程证据，答辩时可直接展示，**等于免费拿到一份工程规范性的佐证材料**。

录入成本可控：`10-tasks-plan.md` 的任务是结构化的，用 `gh issue create` 脚本批量生成，约 1 小时完成。

**标签体系**（保持最小）：`epic:E0`–`epic:E10` · `P0/P1/P2` · `blocked` · `needs-review`
**里程碑**：`M0`–`M7`，与 §25 一一对应。

## 32.4 Git 仓库约定

### 托管与分支模型
- **托管**：GitHub 私有仓库（如网络不稳可加 Gitee 作为镜像远端）
- **分支模型**：**Trunk-based + 短命特性分支**。4 周 4 人的项目不要用 GitFlow，`develop`/`release` 分支只会增加合并负担
  - `main`：始终可运行、CI 绿。受保护，禁止直接 push
  - `feat/E2-T2-container-runner`：特性分支，命名 `<类型>/<任务ID>-<短描述>`，**存活不超过 2 天**
  - 合并方式：**Squash merge**，保持 `main` 线性、每个提交对应一个任务
- **保护规则**：`main` 要求 PR + 1 人 review + CI 通过

### Commit 规范
Conventional Commits，scope 用 Epic 编号，便于按模块回溯：

```
feat(E2): 容器执行器支持 pids-limit 与 OOM 判定
fix(E4): 修正 pytest 用例 ID 归一化对参数化用例的处理
test(E2): 补齐四条沙箱负例
docs(plan): 回填沙箱实测结论与 Docker Desktop 共存约束
chore(E0): 引入 import-linter 模块边界规则
```
类型：`feat` / `fix` / `test` / `docs` / `refactor` / `chore` / `perf`

### Tag 策略
每达成一个里程碑打 tag：`m0-frozen` · `m1-kernel` · `m2-first-agent` · `m3-multi-agent` · `m4-beta` · `m5-dataset` · `m6-experiment` · `m7-submission`

价值不只是纪念：**每次正式 EvaluationRun 的 manifest 都会记录 harness 的 git sha**（§24），tag 让"这份实验结果对应哪版代码"可以一句话说清，答辩时也能展示项目演进。

## 32.5 `.gitignore`：本项目的特殊性

这个项目会产生大量**绝对不能入库**的东西，比一般 Web 项目更需要一开始就设对：

| 必须忽略 | 原因 |
|:---|:---|
| `var/workspaces/**` | 每次评测物化的仓库工作区，数量随运行次数线性增长 |
| `var/artifacts/**` | 日志、轨迹、补丁、测试报告，单次实验可达数百 MB |
| `var/mirrors/**` | Git bare mirror，单仓库可达数百 MB |
| `datasets/exports/*.jsonl` | 数据集导出，MB 级（版本化方式见 32.6） |
| `.env` / `*.env` / `**/secrets*` | **API Key**。本项目要接入多个 LLM 提供方，密钥泄漏风险高 |
| `__pycache__/` · `.venv/` · `node_modules/` · `.next/` | 常规 |
| `*.log` · `.coverage` · `.pytest_cache/` | 常规 |

| 必须入库 | 原因 |
|:---|:---|
| `datasets/golden/**` | Golden Task 是测试基石，体积仅 KB 级 |
| `tests/fixtures/reports/**` | 录制的真实测试报告，解析器单测依赖它 |
| `images/**/Dockerfile*` | 环境定义即代码 |
| `alembic/versions/**` | 数据库演进历史 |
| `docs/**` | 交付物 |

**密钥防线**：`.env.example` 入库（只有键名无值）+ pre-commit 钩子加 `detect-secrets` 或 `gitleaks` 扫描。
> 本次会话中已经出现过一次教训：`.bashrc` 里明文存放的 API Key 在打印配置时被带进了对话记录。密钥必须集中在受 gitignore 保护的单独文件中，并 `chmod 600`。

## 32.6 数据集版本与代码版本的绑定（本项目特有）

评测平台的"可复现"要求**代码版本**与**数据集版本**能一起被锁定，但数据集导出文件太大不适合入库。分层方案：

```
DB:      benchmark_set_items 冻结 task_id + content_hash        ← 事实来源
Artifact: datasets/exports/benchmark-cn-v1@1.0.jsonl（不入库）   ← 完整内容
Git:     datasets/manifests/benchmark-cn-v1@1.0.json（入库，约 2 KB）
         { slug, version, task_count, dataset_sha256,
           task_hashes_sha256, published_at, harness_git_sha }
```

Git 里存的是**指纹**而非内容。任何人拿到仓库 + artifact store，就能校验"我手上这份数据集是不是当初那一份"。

### 一条硬性前置校验（把 Git 状态纳入评测语义）

> **正式 EvaluationRun 启动前，必须校验 Git 工作区干净。**
> `manifest.harness_git_sha` 只有在工作区无未提交改动时才能唯一标识代码状态；否则"可复现"是假的。
> 实现：`git status --porcelain` 非空时，默认**拒绝启动**正式 Run；调试模式可用 `--allow-dirty` 放行，但 manifest 中必须标记 `dirty: true`，且该 Run **不得进入排行榜**。

这条与 §6.2 的 Run Publish Gate 是同一类设计——把"结果可信"变成机器可校验的前置条件，而不是靠人自觉。并入 `scripts/check_env.py` 与 `POST /api/runs` 的校验链。

## 32.7 对 E0-T2 的范围补充

`E0-T2 仓库骨架与工程规范` 的交付物增加：

- `git init` + `.gitignore` + `.env.example` + `README.md`
- `main` 分支保护规则与 PR 模板（PR 模板内嵌 32.2 的 DoD 清单，勾选后方可合并）
- Conventional Commits 校验（commitlint 或 pre-commit 钩子）
- 密钥扫描钩子（gitleaks / detect-secrets）
- `gh` 批量建 Issue 的脚本 + Projects 看板初始化
- CI：PR 触发 lint + type + unit + integration（§27.4）

估算由 **1d 上调至 1.5d**。这半天买到的是四周里所有协作动作的确定性，值得。
