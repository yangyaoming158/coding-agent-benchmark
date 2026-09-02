# 19 Architecture Decision Records

统一格式：**Context / Options / Decision / Reason / Trade-offs / Risk**。状态一律为 `Accepted（Week 0 冻结）`，变更需评审。

---

## ADR-001 — Modular Monolith vs Microservices

**Context**：系统含挖掘、验证、编排、沙箱、判定、归因、报告七个关注点；团队 3–5 人；周期 4 周；部署目标是单机 docker compose。
**Options**：A 模块化单体（单代码库、单 API 进程 + 独立 Worker 进程）；B 微服务（每域一服务 + 网关）；C 单进程脚本集合。
**Decision**：**A —— 模块化单体，API 与 Worker 共享代码库、不同入口。**
**Reason**：跨域调用极其频繁（编排→沙箱→判定→归因），微服务会把函数调用变成网络调用，换来的只有部署复杂度；单机部署下微服务零收益。同时 C 无法满足前端与持久化需求。真正需要的"边界"用**目录 + import-linter 规则**强制，而不是用进程边界。
**Trade-offs**：牺牲独立伸缩与独立发布；靠纪律而非物理隔离维持边界。
**Risk**：时间一长，模块之间的界限会被随手写的 import 破坏 → 应对：在 CI 里加 import-linter 检查，谁越界谁的构建就挂。

---

## ADR-002 — FastAPI vs 其他 Python 后端

**Context**：需要 REST API、协议对象强校验、OpenAPI 供前端生成类型、少量长连接。
**Options**：A FastAPI；B Django + DRF；C Flask；D Litestar。
**Decision**：**A —— FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic。**
**Reason**：本项目的核心资产是**协议**（Task Schema、AgentTaskInput、AgentRunResult、Judge Verdict）。Pydantic 让协议既是运行时校验器、又是 OpenAPI 文档、又是前端 TS 类型来源，一处定义三处受益。Django 的 Admin/Auth 优势在这里用不上，反而带来更重的约定。
**Trade-offs**：需自建 Admin 界面（但我们本来就要做前端）；异步/同步混用需注意（Worker 走同步，API 走 async）。
**Risk**：低。

---

## ADR-003 — Celery + Redis vs Postgres 队列

**Context**：评测作业 5–20 分钟，需重试、需并发上限、需状态可查、需可取消；4 周周期；单机部署。
**Options**：A Celery+Redis；B RQ+Redis；C **Postgres `FOR UPDATE SKIP LOCKED` 队列 + 独立 Worker**；D FastAPI BackgroundTasks。
**Decision**：**C。**
**Reason**：① "作业跑到哪一步了"和"评测任务是什么状态"本来就是同一件事，放在同一个数据库里就能在一个事务里一起更新，不会出现两边对不上；② 少装一个中间件，就少一份部署文档、少一类故障；③ 调试时一句 `SELECT * FROM job_queue` 就能看清全部情况，这对实训项目特别重要——答辩时能讲清楚；④ 我们需要的功能只有"租约、重试、退避"三样，大约 150 行代码，比配置 Celery 要学的东西少得多。方案 D 被学校要求和工程常识直接排除。
**Trade-offs**：自行实现租约续期、僵尸回收、优雅停机（有成熟范式，风险可控）；无 Flower 之类现成监控（我们自己做 Run Detail 页，反而更贴合业务）。
**Risk**：自研队列出现难查的可靠性 bug → **缓解：队列实现完全隔离在 `infrastructure/queue.py`，接口与 RQ 对齐；若 1 天内无法解决，切 RQ（预计 0.5–1 天）。**

---

## ADR-004 — Agent on Host vs Agent in Docker

**Context**：被测 Agent 会执行任意 shell 命令、安装依赖、读写文件；需要访问 LLM API；需要并行 8 路。
**Options**：A Agent 与测试同容器；B Agent 在宿主机、测试在容器；C **Agent 容器 + 独立测试容器**。
**Decision**：**C。**
**Reason**：见 §10.1 全表。决定性理由是**判定纯净性**：Agent 若能污染测试环境（装包、改 conftest、留下 `.pth` 文件），解决率就不可信，基准平台失去存在意义。B 还额外引入宿主机安全问题与并发不可控。
**Trade-offs**：多一次容器编排与工作区物化；Agent CLI 需装进镜像、凭据需注入（有成熟做法）。
**Risk**：某些 CLI 在容器内鉴权困难 → 缓解：优先选纯 API Key 鉴权的 Agent（Aider/MiniAgent），凭据以只读挂载 + 环境变量白名单注入；实在不行对该 Agent 单独启用 B 模式并在报告中标注（**但测试永远在纯净容器**，判定纯净性不受影响）。

---

## ADR-005 — Local Artifact Storage vs MinIO

**Context**：制品总量 5–20 GB；学校建议方案明确写了 MinIO；4 周周期。
**Options**：A 只用本地文件系统；B 只用 MinIO；C **抽象 ArtifactStore，P0 Local、P1 MinIO，配置切换**。
**Decision**：**C。**
**Reason**：Week 1 不该被对象存储的部署与签名 URL 调试卡住；但 MinIO 是学校建议方案的组成部分（且签名 URL 直连能显著减轻 API 服务传输负担），必须交付。抽象层成本极低（约 60 行接口 + 两个实现），收益是"Week 1 不阻塞 + Week 3 平滑升级 + 演示可切换"。
**Trade-offs**：多一层抽象；两套实现都要测（用同一套契约测试跑两遍即可）。
**Risk**：低。

---

## ADR-006 — PostgreSQL 作为唯一主数据库

**Context**：需要关系建模（数据集/任务/运行/结果）、枚举状态、数组与 JSONB、事务性队列、聚合统计。
**Options**：A PostgreSQL；B SQLite；C PostgreSQL + MongoDB；D + ClickHouse 做统计。
**Decision**：**A（PostgreSQL 16）单库。**
**Reason**：`SKIP LOCKED` 队列、JSONB、数组、GIN 索引、窗口函数一次满足全部需求；数据量级（万级行）离需要列存/文档库还差三个数量级。SQLite 无法支撑多 Worker 并发写与队列语义。
**Trade-offs**：无。
**Risk**：无实质风险。**明确记录"不引入第二个数据库"是为了防止范围蔓延。**

---

## ADR-007 — Patch Protocol vs Workspace Mutation

**Context**：学校协议写的是"stdin 任务 → stdout 补丁"，但真实 Agent 均为 workspace-mutation 型。
**Options**：A 强制所有 Agent 输出 stdout patch；B 全部改为 workspace 模式，放弃 stdout 协议；C **协议边界上移到 Adapter：Adapter 仍是 stdin JSON → stdout JSON（含 unified diff），内部支持两种模式。**
**Decision**：**C（默认 workspace-mutation + harness 侧 `git diff`；保留 strict-patch 模式用于 replay 与自研 Agent）。**
**Reason**：A 在真实 CLI 上不可靠（截断、markdown 围栏、行号错误）；B 丢掉了学校明确要求的统一协议形式，也丢掉了"replay 别人已发布补丁"的能力（MET-01 Plan A 依赖它）。C 同时满足协议字面要求、真实 Agent 现实、以及 replay 能力。
**Trade-offs**：Adapter 承担更多职责；需要 `NormalizedPatch` 这一层。
**Risk**：`git diff` 可能捕获 Agent 产生的噪声文件（`__pycache__`、`.aider*`、日志）→ 缓解：工作区内置 `.gitignore` 基线 + 归一化阶段按扩展名/路径过滤 + 单文件 256KB 上限。

---

## ADR-008 — Task Environment Cache Strategy

**Context**：MET-02（6 小时）对依赖安装成本极度敏感（§18.2 已量化：无缓存直接出局）。
**Options**：A 每次运行时安装依赖；B 每题一个专用镜像（≈100 个镜像）；C **仓库/环境级镜像（`environment_spec` 粒度，≈10–25 个）**；D 直接复用 SWE-bench 官方每实例镜像。
**Decision**：**C 为主，D 用于官方校准子集。**
**Reason**：A 增加 5–15 小时，不可接受；B 镜像数量与构建时间线性膨胀（100 × 3–8 min ≈ 5–13 小时构建 + 数百 GB），性价比低；C 把安装成本从 O(运行次数) 降到 O(环境数)，构建总时长 1–3 小时且可提前一晚完成，磁盘 ≤80 GB。D 对官方子集是最省事的选择（官方镜像已针对每实例构建好）。
**Trade-offs**：同仓库跨版本依赖不兼容时需拆 env spec；镜像与任务的对应关系需治理（`environment_id` + digest）。
**Risk**：某仓库依赖漂移导致一个 env 装不住所有任务 → 缓解：验证流水线在 S3 步失败时自动建议拆分 env spec；地板方案是该仓库退化为 B 模式（每题镜像）。

---

## ADR-009 — Benchmark 来源：自建 vs GitHub Mining vs SWE-bench 子集

**Context**：学校要求 ≥100 题、中文优先、含国产开源；同时要求复现公开 Agent 结果（MET-01）。两个要求指向不同的数据来源。
**Options**：A 纯自建（人工造题）；B 纯 GitHub 挖掘；C 纯 SWE-bench 子集；D **三者组合**。
**Decision**：**D —— `golden-tasks`(人工 3–5) + `benchmark-cn-v1`(挖掘 60–100，中文优先/国产) + `swebench-verified-subset`(官方 50–100，校准专用)。**
**Reason**：A 无法达到 100 题规模且缺乏真实性；B 单独使用无法服务 MET-01（我们的题没有公开基线）；C 单独使用违背"中文优先/国产开源"的明确要求。D 让每个数据集各司其职：人工集验内核、挖掘集当主交付、官方集做校准。**这也是本规划对学校两条看似矛盾的要求给出的统一解。**
**Trade-offs**：要维护三条数据来源与两套导入逻辑（但共用同一 Schema 与 Validator，增量很小）。
**Risk**：挖掘产出不足 100 → 缓解见 §4.1 的降级阶梯（地板：总量 ≥100，自建中文 ≥40，并公开来源构成）。

---

## ADR-010 — Failure Attribution Pipeline

**Context**：MET-04 要求归因准确率 ≥85%；LLM 调用有成本与随机性；学校要求"LLM-as-Judge + 人工抽检"。
**Options**：A 全部交给 LLM；B 全部用规则；C **规则前置 → 特征提取 → LLM（仅模糊区间）→ 人工盲检**。
**Decision**：**C。**
**Reason**：A 成本高、随机性大、且对 `REGRESSION`/`EMPTY_PATCH` 这类有确定性证据的类别属于杀鸡用牛刀且更易出错；B 无法处理"理解偏差 vs 修不完整"这类语义判断。C 用确定性证据把 55–70% 的样本锁死在接近 100% 的准确率上，把 LLM 的不确定性限制在剩余样本内，是达成 85% 最稳的路径。
**Trade-offs**：规则集需要随数据积累迭代；两阶段增加实现复杂度。
**Risk**：LLM 在 F1/F3/F4 之间混淆 → 缓解：强制 evidence 引用、低置信度多数投票、必要时合并类别并在报告中说明。

---

## ADR-011 — 判定引擎不使用 LLM（新增，为明确边界）

**Context**：学校原文提到 LLM-as-Judge，容易被误读为"用 LLM 判断是否修好"。
**Decision**：**`agent_outcome` 100% 由测试结果确定性推导；LLM 仅用于失败归因，且其输出不得回写判定。**
**Reason**：基准的价值来自可复现性。LLM 判定会引入不可复现的噪声，使不同时间的排行榜不可比。
**Trade-offs**：无法评价"补丁写得优不优雅"这类主观维度（本项目明确不评价）。
**Risk**：无。**这条 ADR 的作用是防止后续开发中有人"图省事"越界。**

---

## ADR-012 — 双层并发模型（新增，为对齐 MET-03 与硬件现实）

**Context**：本机 8 vCPU / 9.7 GB；Agent 阶段 I/O 密集、测试阶段 CPU/内存密集；学校要求并行度 ≥8。
**Options**：A 单一并发数 = 8（8 个测试容器 → 内存超限）；B 单一并发数 = 4（满足内存但不满足 ≥8）；C **双层信号量：`AGENT_CONCURRENCY=8~12`、`SANDBOX_CONCURRENCY=4~8`**。
**Decision**：**C，并把"并行度"定义为"同时在途的 EvaluationTaskRun 数量"。**
**Reason**：这是同时满足"指标 ≥8"和"物理内存 9.7 GB"的唯一诚实做法，且在工程上本就更优（不同阶段资源画像不同，本就该分开限流）。
**Trade-offs**：调度实现稍复杂；需要在报告中解释口径。
**Risk**：口径被质疑 → 缓解：在性能报告中给出**有效并发时间序列图**（实测同时在途任务数），用数据而非定义说话。
