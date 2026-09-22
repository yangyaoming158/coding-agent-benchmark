# 架构文档

> 一句话：一个 Python 代码库、两个进程（API 和 Worker）、一个 Postgres（表和队列都在里面）、一台 Docker（评测容器在里面跑）。
> 模块边界靠目录 + `import-linter` 在 CI 里强制，不靠进程边界。

**这份文档给谁看**：要读代码、改代码、或者在答辩时讲清"这东西是怎么做的"的人。
装和用分别见 [`deployment.md`](deployment.md) 和 [`usage.md`](usage.md)。设计的详细推导在 `docs/plan/`（13 份，`report.html` 是合成的单页版），
这里只讲"现在的代码是什么样、为什么"，每个说法都指向具体文件。核对基准：main = `899aed0`（2026-09-21）。

---

## 1. 一次评测是怎么跑的

从"一道题 + 一个被测 AI"到"有结论"，八步，每步一个模块：

```
① 物化工作区    app.sandbox.workspace     从 git 镜像按 base_commit 导出干净代码。没有历史（git archive → git init → 只提交一次），AI 翻不到官方修复
② 跑被测 AI     app.runner.adapters.*     在 Agent 容器里起被测 AI，标准输入喂一行 JSON 任务，标准输出最后一行读回结果
③ 抓补丁        app.runner.patch          git diff 出原始补丁（AGENT_RAW），整段丢掉测试文件 / 二进制 / 噪声 → 标准化补丁（AGENT_NORMALIZED）
④ 再物化一份    app.evaluation.executor   拿**另一份**干净代码打上标准化补丁（不用 AI 用过的那份，协议 C-15）
⑤ 强制还原      同上                      受保护路径（tests/、conftest.py…）再 git checkout 一遍，AI 新建的测试文件逐个删 —— 第二道防线（C-16）
⑥ 跑测试        app.sandbox.container     打上官方测试补丁，在测试容器里跑 F2P ∪ P2P，断网、限 CPU / 内存 / 进程数
⑦ 解析 + 判定   app.judge                 junit XML → 逐条用例状态 → judge()，纯函数，没有大模型
⑧ 落库 + 归因   app.evaluation.persistence / app.attribution   写 evaluation_task_runs、test_results、patch_artifacts；失败的再分类
```

对应的状态机（`evaluation_task_runs.lifecycle_status`）：

```
QUEUED → PREPARING → AGENT_RUNNING → PATCH_CAPTURED → TESTING → JUDGING → COMPLETED / FAILED / CANCELLED
```

把这八步串起来的是 `app/evaluation/task_run.py` 的 `execute_task_run()`；它由 Worker 的作业处理函数 `app/worker/handlers/eval_task.py` 调用。
八步里**每一步的异常都要落到一个 `infra_outcome` 上**，禁止冒出去 —— 冒出去的记录会停在非终态，永远不进任何统计，而且没人会发现。

---

## 2. 地基：三个冻结件

后面所有代码（建表、判定、报表、前端）都从这三样派生，改它们要走变更流程（`AGENTS.md` 第 4 节）。

### 2.1 评测协议（`docs/evaluation-protocol.md`，FROZEN v1.2，79 条编号条款）

核心是一次评测的结果用**三个互相独立的字段**描述：

| 字段 | 回答什么 | 取值 |
|:---|:---|:---|
| `lifecycle_status` | 走到哪一步了 | 上面那条状态机 |
| `infra_outcome` | **平台**有没有正确完成这次评测 | `SUCCESS` / `SANDBOX_ERROR` / `OOM_KILLED` / `AGENT_TIMEOUT` / `TEST_TIMEOUT` / `AGENT_AUTH_ERROR` / `PATCH_APPLY_FAILED` / … 共 13 个 |
| `agent_outcome` | **被测 AI** 有没有把 bug 修好 | `RESOLVED` / `UNRESOLVED` / `EMPTY_PATCH` / `INVALID_PATCH` / `NOT_ATTEMPTED` / `NULL`（没拿到结论） |

为什么拆三个：把"AI 没修好"和"平台挂了"混进一个字段，解决率就不可信了。排行榜用的**严格解决率**分母是全部题数；
**有效解决率**分母是"确实拿到了可归因于 AI 的结果"的题数，只用来自查。平台故障率超过 5% 的实验记 `PARTIAL`，不准入排行榜。

协议在代码里有三处落点，中间没有需要人记得同步的环节：

- `app/domain/enums.py`：全部枚举。`tests/unit/test_enum_consistency.py` **直接解析协议原文**比对取值。
- `app/domain/protocol.py`：`LEGAL_COMBINATIONS`（三字段的合法组合表）和 `INFRA_TO_AGENT_MAPPING`（从平台结果推 AI 结果的映射表）。判定引擎**查表不写 if**（C-19）。
- `app/infrastructure/models/evaluation.py` 的 `_legal_combination_sql()`：把合法组合表**编译成一条 SQL CHECK 约束**挂在 `evaluation_task_runs` 上。
  违反协议的记录在写库这一层就写不进去，不用等报表算出来才发现。协议改了，Python 常量跟着改，Alembic 会检测到约束变化。

`tests/unit/test_protocol_consistency.py` 在 CI 里持续校验：改了协议没重跑真值表，构建直接红。

### 2.2 任务格式（`docs/plan/03-benchmark-spec.md` §7.1，代码在 `app/benchmark/schema.py`）

一道题 = 仓库快照（`base_commit`）+ issue 原文 + `fail_to_pass`（修好后必须由挂变过）+ `pass_to_pass`（不能被改坏）+ 官方测试补丁 + 官方修复补丁（`gold_patch`，绝不下发给 AI）。
`TaskDefinition` 是 `extra="forbid"` 的 Pydantic 模型，多一个字段就解析不回来；`content_hash` 算的就是它，所以题目的身份是可验证的。

### 2.3 Runner 协议（`docs/plan/04-runner-protocol.md`，代码在 `app/runner/protocol.py`）

平台和被测 AI 之间的接口：标准输入一行 JSON（`AgentTaskInput`：issue、工作区路径、约束；**不含**测试补丁路径、gold patch 等会泄题的字段，`FORBIDDEN_INPUT_KEYS` 挡着），
标准输出**最后一行**一个 JSON（`AgentRunResult`：unified diff、token、成本、`cost_source`）。
新适配器实现 `AgentRunner` 的 `probe()` / `run()`，继承 `tests/contract/runner_contract.py` 的契约测试就能验。现在有八个：
三个哨兵（Oracle / Noop / Mock）、两个真实 CLI（`aider.py`、`claude_code.py`，共用 `cli_text.py` 里的错误映射 `shared_failure()`）、
自研 `miniagent.py`（运行时在 `app/runner/miniagent_runtime.py`，四工具 ReAct）、`stored.py`（读已存补丁）、`prompt.py`（提示词拼装）。

---

## 3. 代码怎么组织：模块化单体，两个入口

一个仓库、一个 Python 包 `backend/app/`，两个启动入口：`app/main.py`（FastAPI，`uvicorn app.main:app`）和 `app/worker/__main__.py`（`python -m app.worker`）。
它们共用全部代码，只是进程不同（ADR-001：4 周、3–5 人、单机部署，跨域调用极其频繁，微服务把函数调用变成网络调用换来的只有部署复杂度）。

```
backend/app/
  api/             HTTP 层：8 个路由文件，一个资源一个；很薄，业务逻辑不写在这里
  worker/          Worker 进程：主循环 loop.py、单实例锁 singleton.py、两层信号量 concurrency.py、磁盘水位 disk.py、handlers/eval_task.py
  evaluation/      编排：task_run.py（八步串起来）、executor.py（打补丁 + 还原 + 跑测试）、orchestrator.py、persistence.py、progress.py、gate.py（发布门禁）、manifest.py、costing.py
  benchmark/       题库：schema.py、mining.py（GitHub 挖掘）、prescreen.py、assembly.py（候选 → 题）、dataset.py（版本化）、swebench_import.py、hashing.py
  report/          报告：aggregate.py（统一中间结构）→ render.py（HTML / Markdown / JSON）→ service.py（登记制品）
  analytics/       评测 API 和报告共用的只读统计口径：leaderboard.py、timing.py、concurrency.py
  runner/          被测 AI 适配器：protocol.py、patch.py（补丁归一化）、adapters/
  sandbox/         Docker 封装：container.py（起容器、限额、OOM / 超时判定）、workspace.py（物化）、mirror.py（git 镜像）、images.py（三层镜像构建器）、git_cli.py
  judge/           判定：report_parser.py（junit XML）、test_ids.py（用例 ID 归一化）、decision.py（judge() 纯函数）
  attribution/     失败归因：rules.py（规则层）、features.py（结构化特征）、llm.py（大模型层）、review.py / review_service.py（人工盲检）
  storage/         制品存储：base.py（ArtifactStore 接口）、local.py（本地文件系统实现）
  infrastructure/  数据库 db.py、队列 queue.py、配置 config.py、日志 logging.py、大模型客户端 llm.py、文件缓存 file_cache.py、models/（17 张表）
  domain/          协议的代码化：enums.py、protocol.py、retry.py（C-18 重试表）、protected_paths.py、patch_paths.py、cost.py、makespan.py、manifest.py、capacity.py、execution_plan.py
backend/cli/       命令行入口，一个模块一组子命令（usage.md 讲怎么用）
backend/alembic/   8 个迁移（0001 建 17 张表 … 0008 报告制品种类）
```

### 依赖方向（`backend/pyproject.toml` 的 `[tool.importlinter]`，CI 里 `lint-imports` 强制）

分层合同，上层可以 import 下层，反过来不行；同一行里用 `|` 并排的**互不可见**：

```
app.api | app.worker                         两个入口互不 import；投作业都走 infrastructure.queue
app.evaluation | app.benchmark | app.report  三条业务线
app.analytics                                评测 API 与报告共用的只读聚合，避免复制解决率 / 耗时 / 并发算法
app.runner                                   单独一层压在 sandbox 上面（见下）
app.sandbox | app.judge | app.attribution    三个互不可见
app.storage
app.infrastructure
app.domain                                   最底层
```

另外三条禁止合同：

| 合同 | 意思 | 为什么 |
|:---|:---|:---|
| `domain` 不依赖任何其他模块 | 它是协议的代码化表达 | 必须能独立读懂、独立测 |
| `sandbox` 不依赖 `runner` | 是 runner 用 sandbox，不能反过来 | 适配器要起容器，容器层不该认识"被测 AI" |
| `judge` 不依赖 `runner` 与 `attribution` | 判定必须独立于"补丁是谁产生的" | 判定纯粹，不被 Agent 的身份或归因结果影响 |

两条实测出来的注脚：`runner` 原来和 `sandbox` 写在同一层，但同层并排就是互不可见，真实适配器一调 `run_in_container` 就红，
2026-09-04 把它单独提了一层；`judge` 看不到 `sandbox`，所以"打补丁、起容器、跑测试"那一串在 `evaluation/executor.py`，`judge` 只做纯解析和判定。
补丁归一化在 `runner/patch.py` 而不在 `judge/`，同样是这条合同逼出来的。

---

## 4. 数据模型：17 张表

全部在 `app/infrastructure/models/`，`__init__.py` 显式导出每一张（少一张就会在迁移里悄悄消失）。设计原则（`07-platform-architecture.md` §13.1）：
状态字段一律原生枚举；大制品不进库；JSONB 只用在"结构会演化且不需要 join"的地方；`evaluation_task_runs` 故意做成宽表不拆。

| 域 | 表 | 一句话 | 文件 |
|:---|:---|:---|:---|
| 基准 | `repositories` | 被评测的开源仓库，含本地 git 镜像路径 | `benchmark.py` |
| | `environment_specs` | 环境规格 = 一个预建镜像的逻辑定义（安装命令、测试命令、`image_digest`）；`extra_protected_paths` 只能在默认清单上**追加**（C-61） | |
| | `benchmark_tasks` | 题本体。`test_patch` / `gold_patch` 存制品不存文本列；`test_patch_paths` 由验证器推导、禁止下发给 AI（C-74～76）；`content_hash` 是题的身份证 | |
| | `benchmark_sets` | 数据集版本。建行 ≠ 发布，`status` 从 `DRAFT` 到 `PUBLISHED` 才算；`snapshot_digest` 用来发现直接改库；`publish_evidence` 记门禁那两次实验 | |
| | `benchmark_set_items` | 快照：版本 × 题 + 当时的 `task_content_hash`，事后能判断"实验用的题和现在库里的是不是同一个" | |
| | `task_candidates` | GitHub 挖出来的候选 PR，和正式题表分开免得几千条噪声污染它 | |
| Agent | `agents` | 适配器定义（`adapter_class` 是导入路径） | `agent.py` |
| | `agent_configs` | Agent × 模型 × 参数 + 三档单价。**排行榜上的选手是它** | |
| 评测 | `evaluation_runs` | 一次实验。`protocol_version` 创建时写死；`dirty`、`leaderboard_excluded_reason` 决定准入；`manifest` 是可复现性清单 | `evaluation.py` |
| | `evaluation_task_runs` | 单题单次执行，**全库最关键的宽表**。三个协议字段 + 八个时间戳 + token / 成本 + `is_canonical` + 三个诊断字段 | |
| | `patch_artifacts` | 原始补丁和标准化补丁**两份都存**，"AI 试图改测试"的证据在前者 | |
| | `test_results` | 逐条用例结果，判定的证据；`test_id` 是归一化之后的 | |
| 制品 | `artifacts` | 统一制品索引（多态 `owner_type` + `owner_id`），日志 / 轨迹 / 报告只在这里留一行 | `artifact.py` |
| 归因 | `failure_attributions` | 一次执行一条归因结论（`stage` RULE / LLM / HUMAN，`category` F1～F8 / N1 / N2） | `attribution.py` |
| | `human_reviews` | 人工盲检记录，`blind` 标志、批次号 | |
| | `report_records` | 生成过的报告，指向 `artifacts` | |
| 执行 | `job_queue` | Postgres 内建作业队列（§5） | `job.py` |

关键关系：

```
repositories 1─n environment_specs 1─n benchmark_tasks n─n benchmark_sets（经 benchmark_set_items）
agents 1─n agent_configs 1─n evaluation_runs 1─n evaluation_task_runs 1─n test_results / patch_artifacts / human_reviews，1─1 failure_attributions
任何拥有者 1─n artifacts（多态）
```

`evaluation_task_runs` 上三条把协议钉死的约束（`evaluation.py`）：

- `ck_evaluation_task_runs_legal_combination`：三字段合法组合，SQL 由 `LEGAL_COMBINATIONS` 生成（§2.1）。用 `IS NOT DISTINCT FROM` 不用 `=`，因为 `agent_outcome` 可空、`NULL = '值'` 是 NULL，CHECK 遇到 NULL 会**放行**，这个坑本机复现过。
- `uq_task_run_attempt`（实验, 题, 尝试号）唯一：重试是新建记录，禁止改回旧记录（C-32）。
- `uq_task_run_canonical`（实验, 题）`WHERE is_canonical` 部分唯一索引：每题至多一个认定结果（C-57）。`is_canonical` 是显式字段，禁止靠"取最大尝试号"推断 —— 第 1 次就 `AGENT_TIMEOUT`（不可重试）时它就是结论，哪怕后面还有别的记录。

不入库的：Agent stdout / stderr、测试日志、junit XML、轨迹、补丁全文、HTML 报告、镜像构建日志，全走 §10 的制品存储，库里只留索引 + 2 KB 摘要。

---

## 5. 运行时：Worker 和队列

**队列就是 `job_queue` 一张表 + `FOR UPDATE SKIP LOCKED`**（`app/infrastructure/queue.py`，ADR-003），不引 Redis 和消息中间件。
理由：作业状态和评测状态本来就是同一件事，放同一个库里"领走作业"和"任务改成执行中"在一个事务里完成，不会出现两边对不上；
少一个中间件少一份部署文档、少一类故障；`SELECT * FROM job_queue` 就能看清一切。这个文件不认识"评测"，`payload` 是 JSON 由处理函数解释，
真出了查不出的可靠性问题换 RQ 只改这一个文件。

Worker 主循环（`app/worker/loop.py`）：

```
单实例锁（Postgres advisory lock）──▶ 启动时回收孤儿容器 ──▶ 回收过期租约 ──▶ 磁盘够才领作业
   每条作业一个槽线程：心跳续租（60 秒一次，租约 30 分钟）+ 处理函数 + 收尾 / 失败重排
   收到 SIGTERM：不再领新的，等在跑的收工（最多 WORKER_SHUTDOWN_GRACE_S），再回收一次容器
```

几条设计决定：

- **一台机器一个 Worker**。单实例锁挡的是同一个库上的第二个；但连不同库的第二个 Worker 会把第一个正在用的评测容器当孤儿杀掉（2026-09-12 实测 8 个），被杀的那道题长得和内存超限一模一样。
- **两层并发**（ADR-012，`app/worker/concurrency.py`）：`AGENT_CONCURRENCY` 管同时有几个在调大模型（IO 密集，受服务商限流约束），`SANDBOX_CONCURRENCY` 管同时跑几个测试容器（CPU / 内存密集）。合成一个数字调不出好配置。`WORKER_SLOTS` 是"同时在途几道题"，设得比两者之和大没有意义。
- **重试是投一条新作业，`attempt_no` 加 1**（`app/domain/retry.py` 按协议 C-18 的映射表决定重不重试；`job_queue.max_attempts` 管的是"处理函数崩了"，两者分开）。拿到补丁之后的故障，重试不许再调 AI（C-54）。
- **attempt 行跑完才建**：领取时就建的话，Worker 被 `kill -9` 会留下一条卡在 `AGENT_RUNNING` 的记录，接手的 Worker 无路可走（回退状态违反 C-32，新建撞唯一约束）。
- 落库事务里**第一件事是锁实验那一行**（`app/evaluation/progress.py` 的 `lock_run`），否则两条作业同时插子行再升级锁会死锁。
- 内存刹车：宿主可用内存低于 `SANDBOX_MIN_AVAILABLE_MB` 就先不开新的测试容器（E9-T2 实测第 5 个满载容器就把 11 GB 的机器压过线）。

---

## 6. 沙箱：双容器、四道防线

**Agent 容器和测试容器是两个容器**（`05-sandbox.md` §10.2 的 Architecture C，ADR-004），工作区通过卷传递。
决定性理由是判定纯净：AI 若能污染测试环境（装包、改 conftest、留 `.pth`），解决率就不可信。

| 防线 | 在哪 | 防什么 |
|:---|:---|:---|
| 工作区没有历史 | `sandbox/workspace.py`：`git archive` 导出 → `git init` → 只提交一次 | AI 一句 `git log` 翻到官方修复 |
| 输入不泄题 | `runner/protocol.py` 的 `FORBIDDEN_INPUT_KEYS`、`test_patch_paths` 禁止下发 | 直接告诉 AI 测试改了哪几个文件 |
| 补丁归一化 | `runner/patch.py`：按**文件段**整段丢掉受保护路径、二进制、超大、噪声文件 | AI 把测试改成 `assert True`。按段不按行，是因为删几行 hunk 头就对不上、`git apply` 报 corrupt |
| 强制还原 | `evaluation/executor.py` 第 3 步：已跟踪的 `git checkout`，AI 新建的受保护文件逐个删；**绝不 `git clean -fd`**（会删掉 AI 合法新增的源文件，C-63a） | 归一化那一步万一有 bug 的第二道独立防线（C-16） |

测试容器的隔离由 `sandbox/container.py` 设：非 root（跟 harness 的 uid，harness 是 root 时退到 nobody）、`cap_drop=ALL`、`no-new-privileges`、
`pids-limit`（挡 fork 炸弹）、内存和 CPU 限额（从 `benchmark_tasks` 三列读）、`--network none`（协议 C-31，测试可能去 PyPI 装包就不可复现了）。
四条负例（内存炸弹被 OOM 杀、fork 炸弹被拦、死循环被按时杀且不留容器、断网真连不上）在 `05-sandbox.md` §10.3 有实测。
Agent 容器接 `internal` 网络 `bench-egress`（没有网关，直连 IP 也不通），只能经代理容器 `bench-egress-proxy` 访问 `SANDBOX_EGRESS_ALLOW` 里的域名（E2-T4，`app/sandbox/egress.py`；代理本身是 `egress_proxy.py` 一段标准库 Python，只认 `CONNECT host:443`）；claude-code 另加 `--disallowedTools WebFetch,WebSearch`。环境变量按白名单注入（`AGENT_ENV_ALLOWLIST`：几家的 Key、`*_BASE_URL`、代理三件套）。

**OOM 和超时的退出码都是 137，不能靠退出码区分**（`AGENTS.md` §5.4）：判据是 `docker inspect` 的 `.State.OOMKilled`，
但并发下 dockerd 有 3–5% 概率漏收 OOM 通知（§10.10 实测）。目前只告警（日志事件 `container_sigkilled_without_oom_flag`）不改判定 —— 改判定要动协议冻结件。
"一个字节都没输出就被 137"是唯一例外，记 `SANDBOX_ERROR`（那是平台自己杀的）。

镜像三层（`sandbox/images.py`，ADR-008）：`bench-base` → `bench-env:<环境>`（配方 `images/envs/*.json` 渲染成 Dockerfile，按配方哈希决定要不要重建）→ `bench-agent`。
把装依赖的开销从"每次评测一遍"变成"每个环境一遍"，是 300 次评测能在 6 小时内跑完的前提。实验按 **digest** 引用镜像（C-36），记在 `manifest` 里，Worker 起容器按 manifest 不按 `environment_specs` 现值。

---

## 7. 判定与归因

**判定 100% 由测试结果推导，不用大模型**（ADR-011，`AGENTS.md` §5.1）。`judge/decision.py` 的 `judge()` 是纯函数：
输入 `infra_outcome` + 解析后的逐条用例 + `AgentFacts`，输出三字段 + 逐条用例 + 复核标记。同样的输入今天判和下个月判一样，排行榜才可比。

从 `infra_outcome` 推 `agent_outcome` 查 `INFRA_TO_AGENT_MAPPING`；表里三条要额外证据：`BY_TEST_RESULT`（看用例）、`BY_AGENT_STARTED`（AI 到底启动过没有，`agent_started_at` 的置位时刻协议 C-77 定死了）、
`BY_CONTROL_RUN`（测试超时时先跑不打补丁的对照组，看是死循环还是题本身慢，不传结论就抛异常、不猜）。

用例 ID 归一化（`judge/test_ids.py`）是公认最容易出的静默 bug：`tests/test_a.py::test_x` 和 `./tests/test_a.py::test_x` 是同一个，匹配不上就成了假 `MISSING`，
看起来像作弊其实是解析器错了。单元测试覆盖至少 6 种写法。

**归因**（`attribution/`，ADR-010）三层：规则层 `rules.py` 不调模型判 F6 回归 / F7 空补丁 / F8 超时 / N1 平台故障（占失败的 30～50%，准确率接近 100%）→
大模型层 `llm.py` 只处理规则分不出的 F1～F5，输入按 §12.3 裁剪、结构化输出、evidence 强制、有缓存 → 人工层 `review.py` 分层抽样、盲检、双人标注、第三人仲裁。
**大模型的输出禁止回写 `agent_outcome`**（协议 C-40），它只解释为什么。

---

## 8. 题库流水线

三个来源（ADR-009）：仓库自带的四道 Golden 题（`datasets/golden/`，测试基石）；自建题从 GitHub 挖（`benchmark/mining.py` → `prescreen.py` → `assembly.py`，
现有 `pallets/click` + `tortoise/tortoise-orm` 41 道 VALID，中文题面由 `cli.localize` 导回）；官方 SWE-bench Verified 子集（`benchmark/swebench_import.py`，固定种子抽 100、75 道 VALID）做校准，不进自建题统计。

每道题入库后过**八步验证**（`evaluation/validation.py`，`03-benchmark-spec.md` §7.3）：环境能起、基线上 F2P 全挂、打上 gold 后 F2P 全过、P2P 复跑两遍一致……结论写回 `validation_state`；
人工终审的结论在提交进仓库的 CSV 里（`datasets/*/review-*.csv`），库里不存。

数据集版本化（`benchmark/dataset.py`）：`stage` 冻快照出 DRAFT → `gate` 建 Oracle / Noop 两个门禁实验 → `publish` 查门禁（Oracle 100% 且 Noop 0%，协议 C-50）才发布。
三条哨兵测试（`AGENTS.md` 第 9 节）就是这个基准可不可信的自动断言：Oracle 100%（否则有坏题或判定有 bug）、Noop 0%（否则题没有区分度）、同一补丁判 3 次结果一致。
**不为了过门禁改判定**，剔题的做法是把会飘 / 依赖顺序的用例从 P2P 里去掉（`assembly.py` 的 `ORDER_DEPENDENT_TEST_FUNCTIONS` / `FLAKY_TEST_FUNCTIONS`）重新出版本。

---

## 9. HTTP 接口与前端

`app/api/`，FastAPI + Pydantic v2（ADR-002：协议对象 = 运行时校验器 = OpenAPI 文档 = 前端 TS 类型，一处定义三处受益）。20 个端点：

| 资源 | 端点 | 写 |
|:---|:---|:---|
| 健康 | `GET /api/health`（库连得上 + 迁移在最新） | |
| 数据集 | `GET /api/benchmark-sets`、`GET /api/benchmark-sets/{slug}` | |
| 题 | `GET /api/tasks`（`set` / `state` / `repo` / `difficulty` / `language` / `q` 过滤）、`GET /api/tasks/{task_id}`（不透出 `gold_patch_uri` 和 `test_patch_paths`） | |
| Agent | `GET /api/agents`、`GET /api/agent-configs` | |
| 实验 | `GET /api/runs`、`GET /api/runs/{id}`、`GET /api/runs/{id}/task-runs`、**`POST /api/runs`**、**`POST /api/runs/{id}/cancel`**、**`POST /api/runs/{id}/retry-failed`** | ✔ |
| 单次执行 | `GET /api/task-runs/{id}`（带 `failure_attribution`；`BENCH_BLIND_REVIEW=true` 时置空、标 `attribution_withheld`，盲检期间用）、`GET /api/task-runs/{id}/tests`、`GET /api/task-runs/{id}/artifacts/{kind}`（流式返回制品；kind 收制品种类和 `AGENT_RAW` / `AGENT_NORMALIZED` 两套枚举） | |
| 排行榜 | `GET /api/leaderboard`（`set` / `version` / `metric` / `facet`；响应带准入规则原文和被排除实验） | |
| 失败分析 | `GET /api/analysis`（`set`+`version` 取该版排行榜准入的实验，或 `run=…&run=…`；`failures` 段和报告 JSON 同一个函数 `app/report/aggregate.failure_summary()`；盲检开关开着时逐案例答案置空） | |
| 人工复核 | `GET /api/review/queue`、`GET /api/review/{task_run_id}`、**`POST /api/review/{task_run_id}`** | ✔（GET 也要令牌） |

认证是单一管理员令牌（`X-Bench-Token` 头），写操作都要；`/api/review/*` 的 GET 也要，因为复核详情含官方补丁摘要。
**没配 `ADMIN_TOKEN` 进程拒绝启动**（`api/app.py`）—— 默认放行的部署从外面看和配好了的一模一样，起不来是看得见的。
错误响应统一 `{code, message}`。排行榜的准入六条写在 `api/leaderboard.py` 的 `ELIGIBILITY_RULES`，随响应返回。

前端 `frontend/`：Next.js 16 + React 19 + TypeScript，类型从后端 OpenAPI 生成（`make gen-api`，**不手写**，手写的漂移了不报错只会运行时拿到 undefined）。
十个页面（`docs/usage.md` §7.3 有表）：总览 `/`、数据集 `/benchmarks` 与 `/benchmarks/[slug]`、题 `/tasks/[taskId]`、Agent `/agents`、
实验 `/runs` 与 `/runs/[id]`、单次执行 `/task-runs/[id]`、排行榜 `/leaderboard`、人工盲检 `/review`（E6-T3）。展示口径（枚举翻译、格子判定、
排行榜数字、谁算参赛者）是 `src/lib/*.ts` 里的纯函数，`npm run check` 的断言脚本钉住。管理员令牌在页面上输入、只存当前标签页的 sessionStorage，
不打进 JS。实时性用轮询（Run Detail 3 秒、列表和首页 10 秒，只在有活着的实验时开），不做 WebSocket（§29 NOT NOW）。

---

## 10. 制品存储

`app/storage/base.py` 的 `ArtifactStore` 接口（`put` / `get` / `open` / `url` / `exists` / `delete`），`create_artifact_store()` 按 `ARTIFACT_BACKEND` 建实现（ADR-005）。
现在只有 `LocalArtifactStore`（`local.py`），落在 `ARTIFACT_LOCAL_ROOT`（默认 `var/artifacts/`，相对路径以**仓库根**为基准，不然 API 和 Worker 会写到两个目录）：

```
var/artifacts/
  tasks/{task_id}/gold_patch.diff
  runs/{run_id}/task-runs/{task_run_id}/agent_stdout.log.gz      文本制品一律 gzip
  runs/{run_id}/reports/{时间戳}/report.{html,md,json}
```

配成 `minio` 时 `create_artifact_store()` **明确抛 `NotImplementedError`**，不静默退回本地 —— 那样会让"以为存进对象存储了、其实写在容器本地磁盘上"拖到演示时才发现。

---

## 11. 部署形态

`docker-compose.yml`：postgres / migrate（一次性 `alembic upgrade head`）/ api / worker / frontend，外加按需的 cli。
**代码不打进镜像，仓库按宿主机原路径挂进容器**：Worker 用宿主机的 dockerd 起评测容器（挂 `docker.sock`，DooD），
交给 dockerd 的路径按宿主机文件系统解释，容器里的路径必须和宿主机一模一样，否则评测容器拿到空目录且不报错。
Worker 不以 root 跑（入口脚本切成仓库属主的 uid），不发布端口；`api` 不挂 socket。细账、坑和四条必读约束在 `deployment.md`。

开发模式（`make dev` + `make db-up` + `make worker`）是另一套，不做 `docker-compose.dev.yml`。

---

## 12. 没做的，和为什么

评测平台的品格是诚实，降级必须主动披露（`11-acceptance-testing-risk.md` §26.2）。

| 项 | 状态 | 为什么 | 现在怎么办 |
|:---|:---|:---|:---|
| **MinIO 制品存储**（E10-T2） | 未做 | 单机部署下本地文件系统够用；对象存储的部署和签名 URL 调试在 4 周里排不进关键路径（砍单第 5 条） | 抽象层已就绪：`ArtifactStore` 接口 + `ARTIFACT_BACKEND` 一个配置项切换，`artifacts.backend` 列也已有 `MINIO` 取值。加实现不改任何调用点；配成 minio 现在会明确报错而不是静默退回 |
| **Harness Replay**（E3-T8 ReplayRunner + E10-T5，服务 MET-01 复现性） | 未做 | 要把外部已发布的预测补丁按 strict-patch 模式灌进判定链、逐实例比一致率，代码量不大但要花时间在拿数据和对账上；最后一周优先保证 P0 交付 | 报告按 §26.2 主动披露"未做 + 原因"。已有的替代证据：确定性哨兵放大到整个数据集（Oracle ×3 逐题一致，`03-benchmark-spec.md` §8.13）、两轮真实实验逐题翻转的补丁指纹比对证明翻转全来自 AI |
| LLM 归因**跑真模型**（E6-T2） | 代码完成，未付费运行 | 177 道规则分不出的失败要它或人工分 F1～F5；DeepSeek 余额有限，跑之前要负责人拍板 | `cli.attribute llm --dry-run` 可看会处理哪些 |
| 准确率与 κ（E6-T4） | 未做 | 依赖人工盲检先有 ≥50 例标注（MET-04） | 标签已存 `human_reviews`，报表是半天代码 |
| 前端 E7 各页 | 队友在做 | — | 用 `/docs` 里的 HTTP 接口看 |
| 国产 CLI Runner（E3-T7） | 未做 | P1，三个 Agent（两个主流 CLI + 一个自研）已满足 MET-06 降级判据 | 新适配器按 §2.3 的三步接 |
| Kubernetes、微服务、消息队列、WebSocket 轨迹流、多租户、pass@k、全语言… | 明确不做 | `11-acceptance-testing-risk.md` §29 有每一条的理由和"什么时候才值得做" | — |

已知的限制（不是没做，是做了但有边界）：`.State.OOMKilled` 并发下 3–5% 漏报（§6）；一台机器只能跑一个 Worker（§5）；原生 Windows 跑不了（`CONTRIBUTING.md` §1）；
排行榜不会因为"整场 token 为 0"自动排除实验，排除是人工写理由的动作（`leaderboard_excluded_reason`），因为排除依据必须是记下来的事实。

---

## 13. 工程规范

- 语言：后端 Python 3.11+（开发机 venv 是 3.12，`uv.lock` 两个版本都成立），前端 TypeScript。标识符英文，注释中文（公共接口、领域枚举、复杂算法必须有，交付硬性要求）。
- 检查：`ruff`（含 `scripts/` 和 `docs/` 下的脚本）、`mypy --strict`、`lint-imports`、`pytest`，`make check` 一次跑完；CI（`.github/workflows/ci.yml`）同一套。git 钩子另拦密钥和提交信息格式。
- 测试（`backend/tests/`，109 个文件）：`unit/`、`integration/`（`@pytest.mark.db`，**会清空它连上的库**，所以 Makefile 把测试指到独立的 `bench_test`）、`sandbox/`（`@pytest.mark.docker`，每日跑）、`contract/`（适配器契约）、`e2e/`；`@pytest.mark.agent` 的会花钱，手动触发。
- 数据库变更：改 `models/` → `alembic revision --autogenerate` → 迁移必须可回滚（`make migrate-check` 查模型和迁移对不对得上）。
- 文档：`docs/plan/*.md` 是设计推导和实测回填，改完 `python3 docs/plan/_build_report.py .` 重生成 `report.html`，不手改。实测结论回填到对应章节并注明日期。
- 规则的完整版在 `AGENTS.md`（人和 AI 编程助手共用），动手流程在 `CONTRIBUTING.md`。
