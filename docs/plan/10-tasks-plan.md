# 21 Initial Epic / Task Tree

**团队假设**：4 人（若实际人数不同，按 §24.6 调整）。
- **A 架构/内核**：sandbox、judge、evaluation 编排
- **B 后端/数据**：DB、API、task builder、挖掘与数据集生产
- **C 算法/Agent**：Runner 适配器、自研 MiniAgent、归因
- **D 前端/报告**：Next.js、排行榜、抽检界面、报告生成

**标注含义**：`P` 优先级 · `C` 复杂度 S/M/L/XL · `E` 估算人日 · `⚙DB` 需迁移 · `🐳` 需 Docker · `🔑` 需 Agent 账号/API · `🌐` 需外部依赖

---

## E0 — Project Foundation（基础设施）

### E0-T1 开发环境与 Docker 打通 ✅ **已完成 2026-09-01**
- **Goal**：本机可用 `docker run`，Python/Node 工具链就绪，确定机器方案
- **Req**：CON-05, CON-07, NFR-08 · **Why**：不解决则 E2 全部无法开工，Week 1 直接停摆
- **Deps**：无 · **Modules**：—
- **Output**：可用 Docker daemon；`docs/env-setup.md`；~~实验机申请单~~ → **结论：无需采购**
- **AC**：~~`docker run --cpus=1 --memory=512m` 成功；`docker info` 显示 cgroup v2；能拉取镜像~~ **全部达成**
- **实际交付**：Docker 29.7.2 / Compose v5.5.0，systemd 托管、开机自启、免 sudo；cgroup v2 + systemd driver、`docker info` 无 warning；`.wslconfig` → 16 vCPU / 11 GiB；dockerd 代理 + registry 镜像源 + 客户端容器代理三处配齐；镜像拉取速率实测 ≈4 MB/s
- **剩余待办**：把手工验证固化为 `scripts/check_env.py`（并入 E0-T2）
- **Risk**：~~中~~ → **已关闭** · **P0 · C:S · E:0.5d · 🐳**

### E0-T2 仓库骨架与工程规范 ✅ 已于 2026-09-02 完成
- **Goal**：monorepo 目录、依赖管理（uv/poetry）、ruff+mypy、pytest、pre-commit、CI、import-linter 边界规则，**以及 §32 的全部版本管理约定**
- **Req**：NFR-05, NFR-07, NFR-08 · **Deps**：E0-T1
- **Output**：可 `make dev` 起 API 与前端骨架；`CONTRIBUTING.md`（含中文注释规范）；`git init` + `.gitignore` + `.env.example`；`main` 分支保护 + 内嵌 DoD 的 PR 模板；commit 规范校验；密钥扫描钩子；`gh` 批量建 Issue 脚本 + Projects 看板
- **AC**：lint/type/test 三条命令全绿；import-linter 规则生效（故意越界的 import 会被拦下）；提交含密钥的测试文件会被钩子拦下；违反 Conventional Commits 的提交被拒
- **详见**：§32 工程流程与版本管理
- **P0 · C:M · E:1.5d**
- **实际交付**（2026-09-02）：`make dev` 同时起后端 :8000 与前端 :3000，前端首页调通 `/api/health`；
  前端类型由 `npm run gen:api` 从后端 OpenAPI 生成，用错字段直接编译不过；
  三条 AC 全部写成了自动化测试（`backend/tests/unit/test_repo_guards.py`，26 条）——
  故意越界的 import 被 import-linter 拦下、六种密钥格式被钩子识别、五种不合规提交信息被拒；
  `scripts/sync_issues.py` 从本任务表生成 GitHub Issue（默认只预览，`--apply` 才写）。
- **仍需人工操作**：GitHub 网页上设 `main` 分支保护；装 `gh` 后跑 `sync_issues.py --apply`；Projects 看板要手建

### E0-T3 数据库 Schema v1 与迁移 ✅ 已于 2026-09-02 完成
- **Goal**：§13 的 17 张表 + 枚举 + 索引落地
- **Req**：FR-03 · **Deps**：E0-T2、**§6 语义冻结**
- **Output**：Alembic 初版迁移；SQLAlchemy 模型；种子数据脚本（agents/agent_configs）
- **AC**：`alembic upgrade head` 在空库成功；`downgrade base` 可回滚；模型与枚举同 §6 完全一致（用一个"枚举一致性"单测锁死）
- **Risk**：低，但**改动代价随时间指数上升** → 必须在 Week 1 定稿
- **P0 · C:M · E:1.5d · ⚙DB**
- **实际交付**（2026-09-02）：17 张表、26 个原生枚举类型、迁移 `0001_initial_schema`；
  46 个测试全绿，其中 31 个是连真库跑的。三处协议约束落到了数据库层面：
  合法组合 CHECK（C-68、C-78）、认定结果部分唯一索引（C-57）、attempt 唯一约束（C-48）。

### E0-T4 配置、日志、制品存储抽象 ✅ 已于 2026-09-03 完成
- **Goal**：Settings（pydantic-settings）、structlog（含 run_id/task_run_id 上下文）、`ArtifactStore` + `LocalArtifactStore`
- **Req**：NFR-06, FR-19 · **Deps**：E0-T2
- **AC**：ArtifactStore 契约测试通过；日志带结构化上下文；敏感值（API Key）在日志中脱敏
- **P0 · C:S · E:1d**
- **实际交付**（2026-09-03）：`infrastructure/config.py`、`infrastructure/logging.py`、
  `storage/{base,local}.py`；新增 88 个测试（合计 165 个全绿）。三条 AC 各自有测试：
  契约测试 39 条（按接口写，E10-T2 接 MinIO 时加一行参数就能复用）、
  上下文测试验证 `bind_run_context` 可嵌套且退出即还原、
  脱敏测试覆盖三条泄漏路径（字段名、Agent 回显的明文、第三方密钥格式）。
  实测结论回填在 §17.4。

---

## E1 — Benchmark Domain & Task Builder

### E1-T1 Task Schema 冻结与校验器 ✅ 已于 2026-09-03 完成
- **Goal**：§7.1 Schema 的 Pydantic 模型 + JSON Schema 导出 + `content_hash` 规范化算法
- **Req**：FR-04 · **Deps**：E0-T3 · **Modules**：`benchmark/`
- **Output**：`TaskDefinition` 模型、`schemas/task.schema.json`、导入/导出 CLI
- **AC**：Golden Task 的 JSON 能双向序列化且 hash 稳定（同内容不同字段序 → 同 hash）；非法任务被明确拒绝并给出可读原因
- **P0 · C:M · E:1d**
- **实际交付**（2026-09-03）：`benchmark/{schema,hashing,patch_paths}.py`、
  `domain/protected_paths.py`（C-42 清单，放 domain 让 benchmark/runner/judge 共用）、
  `python -m cli.task {import,export,schema}`、`schemas/task.schema.json`（生成物，
  `make schema` 重出，CI 查漂移）。新增 131 个测试（合计 298 全绿）。
  拒收 16 类非法任务（DoD 要求 6 类），另有 3 类走人工复核。
  实现时暴露的三处 Schema/DB 不一致已全部处理（issue #60）：§7.1 补入 `p2p_sampling`、
  迁移 0002 给 `benchmark_tasks` 补 `sandbox_pids_limit` 列、`content_hash` 前缀维持两边格式各异；
  详见 `03-benchmark-spec.md` §7.9。

### E1-T2 Golden Tasks（3–5 道人工任务）
- **Goal**：不依赖挖掘、可在 60 秒内跑完的验证基石
- **Req**：FR-04, MET-05 · **Deps**：E1-T1、E2-T1
- **Output**：`fixtures/golden/` 下 3–5 道任务（含中文 Issue、test_patch、gold_patch），及其 env spec
- **AC**：每道题手工验证 6 步全通过；Oracle 解决率 100%、Noop 解决率 0%
- **Why**：**这是关键路径上唯一不依赖外部世界的输入**，Week 1 内核开发全靠它
- **P0 · C:M · E:1.5d**

### E1-T3 Task Validation Pipeline（8 步验证）
- **Goal**：§7.3 流水线实现 + 证据制品
- **Req**：FR-01, NFR-01 · **Deps**：E1-T1, E2-T2, E4-T2
- **AC**：对 Golden Task 全部判 VALID；对人为构造的 6 种坏任务全部判对应 INVALID reason_code
- **P0 · C:L · E:2d · 🐳**

### E1-T4 GitHub 挖掘器
- **Goal**：GraphQL 批量拉取 merged PR + 关联 Issue → `task_candidates`；缓存与断点续跑
- **Req**：FR-01 · **Deps**：E1-T1 · **Modules**：`benchmark/mining`
- **Output**：`bench mine --repo X --since Y` CLI；候选产出率报表
- **AC**：单仓库能产出 ≥30 条 CANDIDATE；限流下不崩、可续跑；重复运行不产生重复候选
- **Risk**：中（API 限流、关联关系不规范） · **P1 · C:L · E:2d · 🌐**

### E1-T5 候选清洗与 LLM 预筛
- **Goal**：脱敏（去 PR 链接/commit hash/修复代码块）、拆 test_patch/code_patch、抽候选 F2P、LLM 质量打分
- **Req**：FR-01, NFR-04 · **Deps**：E1-T4
- **AC**：脱敏后 Issue 中不含仓库 PR 链接与 40 位 hash（正则断言）；预筛分数分布合理；抽 20 条人工核对一致率 ≥80%
- **P1 · C:M · E:1.5d · 🔑**

### E1-T6 数据集版本化与发布
- **Goal**：`benchmark_sets` + `benchmark_set_items` 快照发布、Oracle/Noop 自检门禁
- **Req**：NFR-02, MET-05 · **Deps**：E1-T3, E4-T4
- **AC**：发布前自动跑 Oracle（要求 100%）与 Noop（要求 0%），不达标拒绝发布
- **P0 · C:M · E:1d · ⚙DB**

### E1-T7 SWE-bench Verified 子集导入
- **Goal**：官方数据集字段映射 + 官方镜像复用 + 固定种子分层抽样
- **Req**：MET-01 · **Deps**：E1-T1, E2-T2
- **AC**：导入 50 题，Oracle 解决率 = 100%（证明我们的 harness 对官方任务判定正确）
- **P1 · C:L · E:2d · 🌐🐳**

---

## E2 — Sandbox Execution Engine

### E2-T1 工作区物化与防泄题 ✅ 已于 2026-09-04 完成
- **Goal**：bare mirror 管理、`git archive` 物化、历史剥离、`.gitignore` 基线
- **Req**：NFR-02, NFR-04 · **Deps**：E0-T1 · **Modules**：`sandbox/workspace`
- **AC**：物化后 `git log --oneline | wc -l == 1`；`git log --all` 看不到 base 之后的提交；两次物化同一 commit 的目录树哈希一致
- **P0 · C:M · E:1d · 🐳**
- **实际交付**（2026-09-04）：`sandbox/{git_cli,mirror,workspace}.py`。三条 AC 全部达成，
  并且拿到了比 AC 更强的结论：**工作区的树哈希等于上游 commit 的树哈希**（内容、路径、
  权限位一处不差），连 base 提交的 SHA 都是确定的（提交人和时间写死为常量）。
  新增 58 个测试（合计 356 全绿），不需要 Docker，跑完 2 秒。
  三处实现决策记在 `05-sandbox.md` §10.7：基线忽略清单写 `.git/info/exclude` 而不是
  工作区根的 `.gitignore`；base 提交用 `git add -A --force`；物化后自查树哈希，
  `export-ignore` 导致的静默缺文件会被当场拦下。

### E2-T2 容器执行器与资源限额
- **Goal**：`run_in_container()`：CPU/内存/pids/超时/网络策略/env 白名单/非 root/清理
- **Req**：FR-05, FR-06, FR-07, NFR-03 · **Deps**：E0-T1
- **AC**：四条负例测试全过 —— ① 内存炸弹 → OOM_KILLED（**判据用 `docker inspect .State.OOMKilled`，不能用 exit 137——超时强杀的退出码同样是 137**）；② fork 炸弹 → 被 pids 限制拒绝；③ 死循环 → 按时被杀且无残留容器；④ `--network none` 下连接失败
- **前置风险已消除**：四条负例已于 2026-09-01 在本机手工验证全部通过（见 §10.3 实测结论），本任务只需将其固化为 pytest 用例并封装 API，**不再有环境可行性风险**
- **P0 · C:L · E:2d · 🐳**

### E2-T3 镜像分层与构建器
- **Goal**：`bench-base` / `bench-env:{environment_id}` / `bench-agent:{env}-{agent}` 三层；digest 记录；`bench images build/gc`
- **Req**：FR-05, MET-02 · **Deps**：E2-T2
- **AC**：同一 env 重复构建命中缓存；digest 写入 `environment_specs`；构建日志落制品；磁盘水位检查生效
- **Why**：**MET-02 的必要条件**（§18.2）
- **P0 · C:L · E:2d · 🐳**

### E2-T4 出站网络白名单代理
- **Goal**：Agent 阶段只放行 LLM API 域名，禁止 github.com
- **Req**：FR-07, NFR-04 · **Deps**：E2-T2
- **AC**：容器内 `curl https://github.com` 失败、`curl <LLM API>` 成功
- **Risk**：中（HTTPS 代理配置）→ 降级方案见 §10.5
- **P1 · C:M · E:1d · 🐳**

---

## E3 — Agent Runner Framework

### E3-T1 Runner 协议与契约测试套件
- **Goal**：`AgentRunner` Protocol、`AgentTaskInput`/`AgentRunResult` 模型、6 条契约测试
- **Req**：FR-08, NFR-05 · **Deps**：E0-T3
- **AC**：契约测试可对任意适配器复用运行；协议 JSON Schema 导出
- **P0 · C:M · E:1d**

### E3-T2 Mock / Oracle / Noop Runner
- **Goal**：可编程行为的假 Agent（正确补丁 / 错误补丁 / 空补丁 / 超时 / 非法补丁 / 改受保护文件）
- **Req**：NFR-01（可测性） · **Deps**：E3-T1
- **AC**：6 种行为可通过配置精确触发；Oracle 在 Golden 集上 100%，Noop 0%
- **Why**：**让整条评测链在完全不依赖外部 Agent 的情况下可测**
- **P0 · C:S · E:0.5d**

### E3-T3 Patch 捕获与归一化
- **Goal**：`git diff` → 剔除受保护路径/噪声文件 → 统计 → NormalizedPatch
- **Req**：FR-08, NFR-04 · **Deps**：E2-T1
- **AC**：Agent 改测试文件时该改动被剔除；`__pycache__`/`.aider*` 被忽略；输出可 `git apply --3way`
- **P0 · C:M · E:1d**

### E3-T4 AiderRunner（第一个真实 Agent）
- **Goal**：容器内非交互运行 Aider，采集 patch/token/cost/轨迹
- **Req**：FR-09, MET-06 · **Deps**：E3-T1, E3-T3, E2-T3 · **🔑**
- **AC**：在 Golden 集上产出非空补丁且至少解决 1 题；契约测试 6 条全过；token/cost 字段非空
- **P0 · C:M · E:1.5d · 🐳🔑**

### E3-T5 ClaudeCodeRunner
- **Goal**：headless `-p` + `stream-json` 解析 + 凭据注入 + `--max-turns` 预算控制
- **Req**：FR-09, MET-06 · **Deps**：E3-T4 · **🔑**
- **AC**：同上；轨迹能还原工具调用序列
- **Risk**：中（鉴权/并发额度） · **P1 · C:L · E:2d · 🐳🔑**

### E3-T6 自研 MiniAgent
- **Goal**：ReAct 循环 + 工具（read_file/list_dir/grep/apply_edit[/run_tests]）+ token 记账
- **Req**：FR-10, MET-06 · **Deps**：E3-T1 · **🔑**
- **AC**：Golden 集上至少解决 1 题；轨迹为原生结构化 JSONL；单题成本可核算
- **Why**：满足"自研 Agent"要求，且是**外部 Agent 全部失败时的保底参赛者**
- **P1 · C:L · E:2d · 🔑**

### E3-T7 国产 CLI Runner（Qwen Code 等）
- **Req**：FR-09（国产） · **Deps**：E3-T4 · **P1 · C:M · E:1.5d · 🐳🔑**

### E3-T8 ReplayRunner（服务 MET-01 Plan A）
- **Goal**：读取外部已发布的 `预测补丁` 文件，按 strict-patch 模式喂入判定链
- **Req**：MET-01 · **Deps**：E3-T1, E4-T4
- **AC**：能对官方子集完成 replay 并输出逐实例一致率
- **P1 · C:S · E:0.5d**

---

## E4 — Evaluation & Judge Engine

### E4-T1 测试报告解析器（pytest junitxml + 文本兜底）
- **Goal**：`{test_id: status}`；`normalize_test_id()` 覆盖 6+ 种 ID 形态
- **Req**：FR-12, NFR-01 · **Deps**：E0-T2
- **AC**：录制的 10 份真实报告 fixture 全部解析正确；ID 归一化单测覆盖相对/绝对路径、参数化、类方法、嵌套目录
- **Risk**：中（**最易出静默 bug 的模块**，见 §11.3）
- **P0 · C:M · E:1.5d**

### E4-T2 测试执行器
- **Goal**：纯净工作区 → apply agent_patch → 强制还原受保护路径 → apply test_patch → 容器内跑子集 → 收报告
- **Req**：FR-06, FR-12, NFR-04 · **Deps**：E2-T1, E2-T2, E4-T1
- **AC**：Agent 改测试的用例被证明无效（防作弊集成测试）
- **P0 · C:L · E:2d · 🐳**

### E4-T3 Judge（F2P/P2P 判定）
- **Goal**：§11.2 判定逻辑 + `agent_outcome`/`infra_outcome` 映射表
- **Req**：FR-12, NFR-01, NFR-09 · **Deps**：E4-T2
- **AC**：真值表单测全过；同补丁重判 3 次结果与逐用例状态完全一致
- **P0 · C:M · E:1d**

### E4-T4 端到端评测单元 `execute_task_run()`
- **Goal**：把 PREPARING→…→COMPLETED 串起来，含状态持久化、制品落盘、异常映射、清理
- **Req**：FR-06, NFR-06, NFR-09 · **Deps**：E2-*, E3-T2/T3, E4-T3
- **AC**：**Golden Task × MockAgent 全链路跑通并落库**（= M1 里程碑）
- **P0 · C:L · E:2d · 🐳**

---

## E5 — Evaluation Orchestration

### E5-T1 Postgres 队列与 Worker 框架
- **Goal**：SKIP LOCKED 领取、租约续期、僵尸回收、退避重试、优雅停机、孤儿容器回收
- **Req**：FR-11 · **Deps**：E0-T3
- **AC**：杀死 Worker 后作业能被另一 Worker 接管；重试次数与退避符合配置；SIGTERM 后无残留容器
- **P0 · C:L · E:2d · ⚙DB**

### E5-T2 EvaluationRun 编排与双层并发
- **Goal**：展开 N 个 task_run、双层信号量、进度聚合、取消、失败重跑
- **Req**：FR-11, MET-03 · **Deps**：E5-T1, E4-T4
- **AC**：8 并发下内存峰值 <80%；取消能在 30s 内停住；有效并发时间序列可导出
- **P0 · C:L · E:2d**

### E5-T3 限流与自适应退避
- **Goal**：按 agent_config 分桶令牌桶；429 自动降并发；`external_wait_ms` 记账
- **Req**：MET-02 · **Deps**：E5-T2 · **P1 · C:M · E:1d · 🔑**

### E5-T4 运行 Manifest 与可复现性
- **Goal**：记录镜像 digest 表、数据集哈希、harness git sha、Agent 版本/模型/参数、种子、环境
- **Req**：NFR-02 · **Deps**：E5-T2
- **AC**：由 manifest 可重建一次等价运行；两次运行的 manifest diff 只在时间戳上不同
- **P0 · C:S · E:0.5d**

---

## E6 — Failure Attribution & Human Review

### E6-T1 规则前置分类器 · **P0 · C:M · E:1d**（F6/F7/F8/N1 确定性归类 + Stage2 特征提取）
### E6-T2 LLM-as-Judge 归因 · **P1 · C:L · E:2d · 🔑**（结构化输出、evidence 强制、缓存、低置信投票）
### E6-T3 抽检队列与盲检界面 · **P1 · C:M · E:1.5d**（分层抽样、双人标注、仲裁）
### E6-T4 准确率与 κ 统计 · **P1 · C:S · E:0.5d**（MET-04 的报表）

## E7 — Frontend & Leaderboard

### E7-T1 前端骨架 + API 类型生成 + 布局导航 · **P0 · C:M · E:1d**
### E7-T2 Runs / Run Detail（进度、分组网格、取消重试） · **P0 · C:M · E:1.5d**
### E7-T3 Task Run Detail（Patch Viewer + 测试结果表 + 日志 + 轨迹） · **P0 · C:L · E:2d**
### E7-T4 Leaderboard（多指标排序 + 成本-解决率散点 + 分面） · **P0 · C:M · E:1.5d**
### E7-T5 Benchmarks / Benchmark Detail / Task Detail · **P1 · C:M · E:1.5d**
### E7-T6 Failure Analysis（分布图/热力图/Top 案例） · **P1 · C:M · E:1.5d**
### E7-T7 Human Review 页 · **P1 · C:M · E:1.5d**
### E7-T8 Dashboard · **P1 · C:S · E:0.5d**

## E8 — Benchmark Dataset Production

### E8-T1 仓库选型实测与打分表 · **P0 · C:M · E:1d · 🌐**（安装/测试耗时、候选深度、中文比例 → 定档 8–15 仓库）
### E8-T2 L1 `benchmark-dev` 20–30 题 · **P1 · C:L · E:3d（跨天，含机时）· 🐳**
### E8-T3 L2 `benchmark-cn-v1` 60–100 题 · **P1 · C:XL · E:4d（跨天，含机时）· 🐳**
### E8-T4 校准集 50 题 · **P1 · C:M · E:1d · 🌐🐳**
### E8-T5 数据集质量报告（来源构成/语言分布/难度分布/漏斗数据） · **P1 · C:S · E:0.5d**

## E9 — Performance & Reliability

### E9-T1 Pilot 实验（30 题 × 3 Agent）与容量模型回代 · **P0 · C:M · E:1d**
### E9-T2 并发压测与调优（找到本机最优 P_agent/P_sandbox） · **P1 · C:M · E:1d**
### E9-T3 稳定性加固（孤儿回收、磁盘水位、失败重跑、断点续跑） · **P1 · C:M · E:1d**
### E9-T4 性能报告生成 · **P1 · C:M · E:1d**

## E10 — Deployment / Documentation / Demo

### E10-T1 docker compose 一键部署（api/worker/pg/minio/frontend） · **P0 · C:M · E:1.5d · 🐳**
### E10-T2 MinioArtifactStore 接入与切换验证 · **P1 · C:S · E:0.5d**
### E10-T3 报告生成器（HTML + Markdown + JSON，含每题轨迹链接） · **P1 · C:L · E:2d**
### E10-T4 最终实验（100×3）与对比报告 · **P1 · C:L · E:2d**
### E10-T5 Harness Replay 校准实验（MET-01） · **P1 · C:M · E:1d**
### E10-T6 部署文档 / 使用文档 / 架构文档 · **P0 · C:M · E:1.5d**
### E10-T7 答辩演示脚本与录屏兜底 · **P0 · C:S · E:0.5d**

---

# 22 Dependency Graph

```mermaid
flowchart TD
    E0T1["E0-T1 Docker 打通 ⛔"] --> E0T2["E0-T2 仓库骨架"]
    E0T2 --> E0T3["E0-T3 DB Schema"]
    E0T2 --> E0T4["E0-T4 配置/日志/ArtifactStore"]
    SEM["§6 评测语义冻结"] --> E0T3
    SEM --> E1T1

    E0T3 --> E1T1["E1-T1 Task Schema"]
    E0T1 --> E2T1["E2-T1 工作区物化+防泄题"]
    E0T1 --> E2T2["E2-T2 容器执行器+限额"]
    E2T2 --> E2T3["E2-T3 镜像分层构建"]
    E2T2 --> E2T4["E2-T4 出站白名单代理"]

    E1T1 --> E1T2["E1-T2 Golden Tasks"]
    E2T1 --> E1T2

    E0T3 --> E3T1["E3-T1 Runner 协议"]
    E3T1 --> E3T2["E3-T2 Mock/Oracle/Noop"]
    E2T1 --> E3T3["E3-T3 Patch 归一化"]

    E0T2 --> E4T1["E4-T1 报告解析器"]
    E2T1 --> E4T2["E4-T2 测试执行器"]
    E2T2 --> E4T2
    E4T1 --> E4T2
    E4T2 --> E4T3["E4-T3 Judge"]

    E3T2 --> E4T4["E4-T4 execute_task_run 全链路"]
    E3T3 --> E4T4
    E4T3 --> E4T4
    E2T3 --> E4T4
    E1T2 --> E4T4

    E4T4 -.M1 评测内核.-> M1(("M1"))

    E4T2 --> E1T3["E1-T3 任务验证流水线"]
    E1T1 --> E1T3
    E1T3 --> E1T6["E1-T6 数据集发布"]
    E1T3 --> E8T2["E8-T2 dev 20-30 题"]
    E8T1["E8-T1 仓库选型"] --> E1T4["E1-T4 GitHub 挖掘"]
    E1T4 --> E1T5["E1-T5 清洗+LLM 预筛"]
    E1T5 --> E8T2
    E8T2 --> E8T3["E8-T3 cn-v1 100 题"]
    E1T3 --> E1T7["E1-T7 官方子集导入"]

    E0T3 --> E5T1["E5-T1 队列+Worker"]
    E5T1 --> E5T2["E5-T2 编排+双层并发"]
    E4T4 --> E5T2
    E5T2 --> E5T3["E5-T3 限流退避"]
    E5T2 --> E5T4["E5-T4 Manifest"]

    E4T4 --> E3T4["E3-T4 AiderRunner"]
    E2T3 --> E3T4
    E3T4 --> E3T5["E3-T5 ClaudeCodeRunner"]
    E3T1 --> E3T6["E3-T6 自研 MiniAgent"]
    E3T4 --> E3T7["E3-T7 国产 CLI"]
    E3T4 -.M2 首个真实 Agent.-> M2(("M2"))

    E4T3 --> E6T1["E6-T1 规则归因"]
    E6T1 --> E6T2["E6-T2 LLM 归因"]
    E6T2 --> E6T3["E6-T3 盲检界面"]
    E6T3 --> E6T4["E6-T4 准确率+κ"]

    E0T3 --> E7T1["E7-T1 前端骨架"]
    E5T2 --> E7T2["E7-T2 Runs/Run Detail"]
    E4T4 --> E7T3["E7-T3 Task Run Detail"]
    E5T2 --> E7T4["E7-T4 Leaderboard"]

    E5T2 --> E9T1["E9-T1 Pilot 实验"]
    E8T3 --> E10T4["E10-T4 最终实验 100×3"]
    E3T5 --> E10T4
    E9T1 --> E10T4
    E3T8["E3-T8 ReplayRunner"] --> E10T5["E10-T5 校准实验"]
    E1T7 --> E10T5
    E10T4 --> E10T3["E10-T3 报告生成"]
    E10T1["E10-T1 compose 部署"] --> E10T6["E10-T6 文档"]
```

---

# 23 Critical Path（关键路径）

```
E0-T1 Docker 打通
  → E0-T3 DB Schema（依赖 §6 语义冻结）
  → E1-T1 Task Schema  ──┐
  → E2-T1 工作区物化      ├→ E1-T2 Golden Tasks
  → E2-T2 容器执行器      │
  → E2-T3 镜像构建 ───────┘
  → E4-T1 报告解析器 → E4-T2 测试执行器 → E4-T3 Judge
  → E3-T1 Runner 协议 → E3-T2 Mock Runner → E3-T3 Patch 归一化
  → E4-T4 execute_task_run 全链路                    ★ M1
  → E5-T1 队列/Worker → E5-T2 编排与并发
  → E3-T4 AiderRunner                                 ★ M2
  → E1-T3 任务验证流水线 → E8-T2/E8-T3 数据集生产      ★ M5
  → E10-T4 最终实验 → E10-T3 报告                      ★ M6
```

## 若进度落后，**绝对不能砍**的任务
| 任务 | 砍掉的后果 |
|:---|:---|
| E0-T1 Docker | 什么都跑不了 |
| E0-T3 DB Schema | 后期改表代价指数上升 |
| E1-T1 Task Schema | 协议不冻结 → 全员返工 |
| E1-T2 Golden Tasks | 内核开发失去可测输入，只能等挖掘 |
| E2-T1/T2 工作区+容器 | 无沙箱 = 无隔离 = 无基准 |
| E2-T3 镜像构建 | MET-02 直接出局 |
| E3-T1/T2/T3 协议+Mock+补丁 | 无法脱离外部 Agent 开发与测试 |
| E4-T1/T2/T3 解析+执行+判定 | 无判定 = 无评测 |
| E4-T4 全链路 | M1 不成立，整个项目没有内核 |
| E5-T1/T2 队列与并发 | MET-03 不成立，且 300 次跑不完 |
| E3-T4 至少一个真实 Agent | 变成"只会跑 Mock 的玩具" |
| E1-T3 验证流水线 | 数据集不可信，基准无效 |
| E10-T1 部署 + E10-T6 文档 | DEL-06 缺失，直接影响验收 |

## **可以砍**（按砍除顺序）
1. E2-T4 出站代理（降级为轨迹检测泄漏）
2. E7-T8 Dashboard、E7-T5 部分页面
3. E3-T7 国产 CLI（用 MiniAgent + 国产模型顶替）
4. E6-T2 LLM 归因降级为纯规则 + 人工（MET-04 用规则覆盖率说明）
5. E10-T2 MinIO（保留 Local，说明抽象层已就绪）
6. E8-T3 题量 100 → 60 自建 + 40 官方子集
7. E10-T4 全量实验 100×3 → 100×2 + 30×1

---

# 24 4-Week Plan

> 与学校路线图的差异及理由见 §24.5。总体思路：**Week 1 造内核（不碰真实 Agent），数据生产从 Week 1 后半并行常驻后台。**

## Week 1 —— Minimum Evaluation Kernel（M0 → M1）

| 日 | A 内核 | B 数据/后端 | C Agent | D 前端 |
|:--|:--|:--|:--|:--|
| D1 | ~~E0-T1 Docker 打通~~ ✅ **已提前完成**；**§6 语义评审冻结**（本日最高优先） | E0-T2 骨架 | 协助 E0-T2；调研各 CLI 非交互参数 | 前端脚手架 |
| D2 | E2-T1 工作区物化+防泄题 | **E0-T3 DB Schema** | E3-T1 Runner 协议 | E7-T1 骨架+类型生成 |
| D3 | E2-T2 容器执行器（负例测试） | E0-T4 配置/日志/ArtifactStore；**E8-T1 仓库选型实测** | E3-T2 Mock/Oracle/Noop | E7-T1 续 |
| D4 | E2-T3 镜像分层构建 | **E1-T1 Task Schema 冻结** | E3-T3 Patch 归一化 | E7-T2 Runs 列表（对 Mock 数据） |
| D5 | **E4-T1 解析器 + E4-T2 测试执行器** | **E1-T2 Golden Tasks ×3** | 协助 E4-T2；契约测试套件 | E7-T3 雏形 |
| D6/7（机动） | **E4-T3 Judge + E4-T4 全链路** | E1-T3 验证流水线起步 | — | — |

**Week 1 出口（M1）**：`Golden Task × MockAgent → 补丁 → Docker 测试 → RESOLVED` 全链路跑通并落库，前端能看到这条记录。
**Week 1 硬规定**：不接任何真实 Agent。真实 Agent 是 Week 2 的事；Week 1 的价值在于内核可测。

## Week 2 —— 真实 Agent + 并发 + dev 数据集（M2 → M3）

| 日 | A | B | C | D |
|:--|:--|:--|:--|:--|
| D1 | E5-T1 队列/Worker | E1-T3 验证流水线完成 | **E3-T4 AiderRunner** | E7-T2/T3 完善 |
| D2 | E5-T1 续（租约/回收/停机） | E1-T4 GitHub 挖掘器 | E3-T4 续 → **M2** | E7-T4 Leaderboard |
| D3 | **E5-T2 编排 + 双层并发** | E1-T5 清洗+LLM 预筛 | E3-T5 ClaudeCodeRunner | E7-T4 续 |
| D4 | E5-T2 续；E5-T4 Manifest | **E8-T2 dev 20–30 题（后台跑）** | E3-T5 续 | E7-T5 Benchmarks 页 |
| D5 | E9-T2 并发压测调优 | E1-T6 数据集发布 + Oracle/Noop 门禁 | E3-T5 收尾 → **M3** | E7-T3 Patch Viewer 完善 |

**Week 2 出口（M3）**：2 个真实 Agent × `benchmark-dev`(20–30 题) 并发跑完，排行榜出数。

## Week 3 —— 数据集扩容 + 归因 + 抽检 + 校准（M4 → M5）

| 日 | A | B | C | D |
|:--|:--|:--|:--|:--|
| D1 | E9-T3 稳定性加固 | **E8-T3 cn-v1 生产（常驻后台）** | **E3-T6 自研 MiniAgent** | E7-T6 Failure Analysis |
| D2 | E5-T3 限流退避 | E8-T3 续；REVIEW_REQUIRED 人工过审 | E3-T6 续 | E7-T6 续 |
| D3 | **E9-T1 Pilot 30×3 + 容量回代** | **E1-T7 官方子集导入** | **E6-T1 规则归因** | E7-T7 Human Review 页 |
| D4 | E10-T1 compose 部署 | E8-T4 校准集 50 题 | **E6-T2 LLM 归因** | E7-T7 续；E7-T8 Dashboard |
| D5 | E10-T2 MinIO 接入 | E8-T5 数据集质量报告 | E3-T7 国产 CLI；E3-T8 Replay | E10-T3 报告模板 |

**Week 3 出口（M4/M5）**：平台 Beta 全功能可用；`benchmark-cn-v1` 发布（Oracle 100%/Noop 0% 门禁通过）；Pilot 实测数据回代容量模型，据此决定 Week 4 是否降级。

## Week 4 —— 最终实验 + 报告 + 交付（M6 → M7）

| 日 | 全员 |
|:--|:--|
| D1 | **镜像全量预热（提前一晚启动）**；E10-T5 Harness Replay 校准实验（MET-01）；E6-T3 抽检批次生成 |
| D2 | **E10-T4 最终实验 100×3 启动**（监控 makespan / external_wait / infra 失败率）；抽检双人标注 |
| D3 | 实验收尾与失败项重跑；E6-T4 准确率+κ 统计；E9-T4 性能报告 |
| D4 | E10-T3 完整报告生成（对比报告 + 失败归因 + 性能）；E10-T6 文档（部署/使用/架构） |
| D5 | 回归测试全绿；E10-T7 演示脚本 + 录屏兜底；答辩排练 |

**Week 4 出口（M7）**：全部交付物就绪，演示可在 5 分钟内完成一次完整闭环。

## 24.5 与学校路线图的差异（必须向老师说明）

| 学校计划 | 本规划 | 理由 |
|:---|:---|:---|
| 第 1 周"任务构建器爬取 30+ 题" | 第 1 周只做**仓库选型 + 3–5 道 Golden Task**，挖掘从 W1D3 起后台常驻，dev 20–30 题在 **W2D4** | 挖掘产出率仅 1–8%，且验证需要沙箱先就绪；先造内核后造数据才不返工 |
| 第 2 周"任务集扩至 100 题" | **第 3 周**完成 100 题 | 100 题需 15+ 小时验证机时 + 人工过审，W2 同时要接 2 个 Agent，不可能并行完成 |
| 第 2 周"2 种 Agent 适配" | 一致（Aider + Claude Code） | — |
| 第 3 周"归因+排行榜+并行调度" | 并行调度**提前到 W2**，归因/抽检留 W3 | 并行是最终实验的前置条件，越早越好；且它在关键路径上 |
| 第 4 周"完整实验+报告+答辩" | 一致，但**镜像预热提前到 W3 末夜间** | 避免实验日被镜像构建吃掉几小时 |

## 24.6 团队人数不同的调整
- **3 人**：合并 A+C（内核与 Agent 同人），砍 E7-T6/T7/T8 到 P2，题量目标降为 60 自建 + 40 官方。
- **5 人**：第 5 人专职 E8 数据生产与人工过审（这是最吃人力且最容易拖期的一环），题量可冲 100 自建。

---

# 25 Milestones

| ID | 名称 | Entry Criteria | Exit Criteria | 目标日期 |
|:--|:---|:---|:---|:---|
| **M0** | Architecture Frozen | 本规划评审通过 | §6 评测语义、§7 Task Schema、§9 Runner 协议三项**签字冻结**；DB Schema v1 定稿；Docker 可用；实验机方案确定 | W1D2 |
| **M1** | Evaluation Kernel Works | M0 完成；Golden Task ≥3 | `Golden × Mock` 全链路 COMPLETED 落库；Oracle=100%、Noop=0%；防作弊两条负例测试通过；沙箱四条负例测试通过 | **W1D5** |
| **M2** | First Real Agent Works | M1；Aider 镜像就绪；API Key 可用 | AiderRunner 在 Golden 集产出真实补丁且至少解决 1 题；契约测试 6/6；token/cost 采集正常 | W2D2 |
| **M3** | Multi-Agent Benchmark Works | M2；`benchmark-dev` ≥20 题；队列与并发就绪 | 2 个真实 Agent × dev 集并发跑完，infra 失败率 <10%，排行榜出数 | W2D5 |
| **M4** | Platform Beta | M3；归因与抽检页可用 | 全部 P0 页面可用；归因流水线产出分类；一键 compose 部署成功；MinIO 可切换 | W3D4 |
| **M5** | 100-Task Dataset Ready | M4；挖掘与验证流水线稳定 | `benchmark-cn-v1` 发布（总量 ≥100，或至少达到 §4.1 定的底线），Oracle 测试 100%、Noop 测试 0% 两道门槛通过；数据集质量报告出具 | W3D5 |
| **M6** | Final Experiment Complete | M5；镜像全量预热完成；Pilot 数据回代通过 | 100×3 实验完成（或降级方案完成），makespan 与 external_wait 有实测数据；校准实验完成；抽检准确率与 κ 出具 | W4D3 |
| **M7** | Submission Ready | M6 | 六项交付物齐备；回归测试全绿；部署文档经"干净机器"验证；演示脚本 + 录屏兜底就绪 | W4D5 |

**里程碑纪律**：M1 是唯一不可延期的里程碑。若 W1D5 未达 M1，立即触发范围收缩（先砍 §23"可以砍"清单的 1–3 项），而不是顺延——因为 M1 之后的所有工作都建立在它之上。
