# 13 Data Model（PostgreSQL）

## 13.1 设计原则
1. **状态字段一律用原生 enum 或带 CHECK 的 varchar**，禁止裸字符串；
2. **大制品不进库**：日志、轨迹、补丁全文走 ArtifactStore，库里只存 URI + sha256 + size + ≤8KB 摘要；
3. **JSONB 只用在"结构会演化且不需要 join 查询"的地方**：任务原始定义、运行 manifest、Agent 配置、LLM 归因原始响应、成本明细；
4. **不过早拆表**：`evaluation_task_runs` 是宽表（含各阶段时间戳与统计），而不是拆成 5 张阶段表；
5. **任务内容哈希化**：`content_hash` 让"数据集版本"成为可验证的事实。

## 13.2 表清单（17 张）

> **2026-09-02 实测回填**：E0-T3 落地时按本节建表，实际数量是 17 张（原文写 15 张，是数错了）。
> 迁移脚本 `backend/alembic/versions/0001_initial_schema.py`，`upgrade head` / `downgrade base` / 再 `upgrade head` 三步都验过。

### A. 基准域

**`repositories`** — 被评测的开源仓库
`id PK` · `full_name UQ` · `url` · `default_branch` · `language` · `stars` · `license` · `is_domestic bool` · `mirror_path` · `created_at`

**`environment_specs`** — 环境规格（镜像的逻辑定义）
`id PK` · `environment_id UQ`(如 `nonebot2__py311__v3`) · `repository_id FK` · `python_version` · `install_command` · `pre_test_command` · `test_command` · `test_framework` · `test_report_path` · **`extra_protected_paths jsonb`** · `image_tag` · `image_digest` · `build_status enum(PENDING|BUILDING|READY|FAILED)` · `built_at` · `build_log_uri`
索引：`(repository_id)`、`(build_status)`
> **2026-09-02 改名**：原字段名是 `protected_paths`，落地时改成 `extra_protected_paths`。
> 原因：协议 C-61 规定环境规格只能在默认清单上**追加**路径，禁止整体替换。
> 叫 `protected_paths` 会让人以为这就是完整清单 —— 某个仓库配错一次，防作弊就整体失效，而且不会报错。

**`benchmark_tasks`** — 任务本体
`id PK` · `task_id UQ` · `repository_id FK` · `environment_spec_id FK` · `base_commit char(40)` · `issue_title` · `issue_body text` · `issue_language enum` · `source_issue_url` · `source_pr_url` · `fail_to_pass jsonb` · `pass_to_pass jsonb` · `test_patch_uri` · **`test_patch_paths jsonb`**（由 Validator 从 test_patch 推导，纳入 content_hash，禁止下发给 AI，见协议 C-74~C-76）· `gold_patch_uri` · `difficulty enum` · `tags text[]` · `agent_timeout_s` · `test_timeout_s` · `sandbox_cpu numeric` · `sandbox_memory_mb` · `sandbox_pids_limit`（2026-09-04 由迁移 0002 补入，issue #60；三个限额并列存列，起容器时直接读）· `validation_state enum(DISCOVERED|CANDIDATE|VALIDATING|VALID|INVALID|REVIEW_REQUIRED|QUARANTINED)` · `invalid_reason_code` · `validated_at` · `validation_evidence_uri` · `content_hash` · `raw_definition jsonb` · `created_at/updated_at`
索引：`(validation_state)`、`(repository_id)`、`(difficulty)`、`(tags) GIN`、`(issue_language)`
> `test_patch` 与 `gold_patch` 存**制品**而非文本列：它们经常几十 KB，且 gold_patch 属于"绝不能误发给 Agent"的敏感内容，放在独立存储更容易做访问控制。

**`benchmark_sets`** — 数据集版本
`id PK` · `slug`(如 `benchmark-cn-v1`) · `version` · `title` · `description` · `status enum(DRAFT|PUBLISHED|ARCHIVED)` · `task_count` · `published_at` · UQ`(slug, version)`

**`benchmark_set_items`** — 数据集快照（关键：冻结版本）
`id PK` · `benchmark_set_id FK` · `benchmark_task_id FK` · `task_content_hash` · `position` · UQ`(benchmark_set_id, benchmark_task_id)`

**`task_candidates`** — 挖掘候选（与 `benchmark_tasks` 分离，避免污染正式表）
`id PK` · `repository_id FK` · `pr_number` · `issue_number` · `raw_payload jsonb` · `prescreen_score` · `prescreen_reason` · `state enum(DISCOVERED|PRESCREENED|PROMOTED|REJECTED)` · `reject_reason` · UQ`(repository_id, pr_number)`

### B. Agent 域

**`agents`** — 参赛者定义
`id PK` · `name UQ`(如 `claude-code`) · `display_name` · `kind enum(MOCK|ORACLE|NOOP|CLI|CUSTOM)` · `adapter_class` · `homepage` · `is_domestic bool`

**`agent_configs`** — Agent × 模型 × 参数 的具体组合（**这才是排行榜上的"参赛者"**）
`id PK` · `agent_id FK` · `label`(如 `aider@deepseek-chat`) · `agent_version` · `model_name` · `params jsonb`(temperature/max_turns/…) · `price_input_per_mtok numeric` · `price_output_per_mtok numeric` · `config_hash` · `enabled bool`
> 把"Agent"与"配置"分开是必要的：同一个 Aider 接 3 个模型就是 3 个参赛者，而适配器只有 1 个。

### C. 评测域

**`evaluation_runs`** — 一次实验 = Agent 配置 × 数据集
`id PK` · `name` · `benchmark_set_id FK` · `agent_config_id FK` · `status enum(DRAFT|QUEUED|RUNNING|COMPLETED|PARTIAL|FAILED|CANCELLED)` · `agent_concurrency` · `sandbox_concurrency` · `total_tasks` · `completed_tasks` · `resolved_count` · `infra_failure_count` · `strict_resolve_rate numeric` · `effective_resolve_rate numeric` · `total_cost_usd numeric` · `total_tokens bigint` · `makespan_ms bigint` · `external_wait_ms bigint` · **`protocol_version varchar`**（创建时写入，禁止事后修改，见协议 C-67）· **`retry_count int`** · **`recovered_infra_failure_count int`** · **`dirty bool`**（工作区带未提交改动时启动的实验，不得进排行榜，见协议 C-27、C-28）· `manifest jsonb` · `started_at` · `finished_at` · `created_by`
索引：`(status)`、`(benchmark_set_id, agent_config_id)`
> `manifest jsonb` 承载 §24 可复现性的全部字段（镜像 digest 表、harness git sha、数据集哈希、环境变量白名单、随机种子）。

**`evaluation_task_runs`** — 单题单次执行（核心宽表）
`id PK` · `evaluation_run_id FK` · `benchmark_task_id FK` · `attempt_no smallint` · `lifecycle_status enum` · `infra_outcome enum` · `agent_outcome enum` · `queued_at/prepare_started_at/agent_started_at/agent_finished_at/test_started_at/test_finished_at/judged_at/completed_at` · `agent_duration_ms/test_duration_ms/total_duration_ms` · `exit_code` · `tokens_input/tokens_output/tokens_total bigint` · `cost_usd numeric` · `cost_source enum` · `turns` · `patch_artifact_id FK NULL` · `files_changed/lines_added/lines_deleted` · `f2p_passed/f2p_total/p2p_passed/p2p_total` · `error_code` · `error_message_excerpt varchar(2000)` · `worker_id` · `retry_of_id FK NULL` · **`is_canonical boolean`**（这次 attempt 是否被选为统计依据，规则见协议 C-24）· **`raw_patch_empty boolean`** · **`protected_path_edit_attempted boolean`** · **`filtered_change_reasons jsonb`**
索引：`(evaluation_run_id, lifecycle_status)`、`(benchmark_task_id)`、`(agent_outcome)`、UQ`(evaluation_run_id, benchmark_task_id, attempt_no)`、**部分唯一索引 `(evaluation_run_id, benchmark_task_id) WHERE is_canonical`**（保证每题只有一个认定结果）

> **`is_canonical` 为什么必须是显式字段**：一道题重试多次时，被选作统计依据的那一次**不一定是编号最大的**。比如第 1 次就遇到 AI 超时（按协议 C-18 不可重试），它就是认定结果。靠"取最大 attempt_no"推断会算错。协议 C-57、C-58 明确禁止临时推断。
>
> **三个诊断字段是干什么的**：`EMPTY_PATCH` 的含义是"过滤之后补丁为空"，不等于"AI 什么都没做"。AI 可能改了一堆测试文件想蒙混过关，被平台全部丢弃后也是空补丁。这两种行为必须能区分，否则失败分析会得出错误结论。

**`patch_artifacts`** — 补丁及其统计
`id PK` · `evaluation_task_run_id FK` · `kind enum(AGENT_RAW|AGENT_NORMALIZED|GOLD|TEST)` · `uri` · `sha256` · `size_bytes` · `files_changed` · `lines_added` · `lines_deleted` · `is_empty bool` · `applies_cleanly bool`

**`test_results`** — 逐用例结果（判定的证据）
`id PK` · `evaluation_task_run_id FK` · `test_id text` · `role enum(F2P|P2P|OTHER)` · `status enum(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|MISSING)` · `duration_ms` · `message_excerpt varchar(2000)`
索引：`(evaluation_task_run_id)`、`(evaluation_task_run_id, role)`
> 量级：300 run × ~50 用例 ≈ 15,000 行/实验，完全无压力。逐用例入库是"证据可查"的基础，也是失败归因 Stage 2 的数据源。

**`artifacts`** — 统一制品索引
`id PK` · `owner_type enum(TASK|TASK_RUN|EVAL_RUN|VALIDATION)` · `owner_id` · `kind enum(AGENT_STDOUT|AGENT_STDERR|TEST_STDOUT|TEST_REPORT_XML|TRAJECTORY|PATCH|REPORT_HTML|VALIDATION_EVIDENCE|BUILD_LOG)` · `uri` · `backend enum(LOCAL|MINIO)` · `content_type` · `size_bytes` · `sha256` · `compressed bool` · `created_at`
索引：`(owner_type, owner_id)`

### D. 归因与人工域

**`failure_attributions`**
`id PK` · `evaluation_task_run_id FK UQ` · `stage enum(RULE|LLM|HUMAN)` · `category enum(F1..F8|N1|N2)` · `secondary_category` · `confidence numeric` · `judge_model` · `prompt_hash` · `evidence jsonb` · `reasoning_zh text` · `raw_response jsonb` · `status enum(OK|NEEDS_HUMAN|FAILED)` · `created_at`

**`human_reviews`**
`id PK` · `evaluation_task_run_id FK` · `reviewer` · `sample_batch_id` · `blind bool` · `action enum(ACCEPT|CORRECT|MARK_TASK_DEFECT|COMMENT)` · `corrected_category` · `comment text` · `reviewed_at`
索引：`(sample_batch_id)`

**`report_records`**
`id PK` · `evaluation_run_id FK NULL` · `scope enum(SINGLE_RUN|COMPARISON)` · `run_ids bigint[]` · `format enum(HTML|MARKDOWN|JSON)` · `artifact_id FK` · `generated_at` · `params jsonb`

### E. 执行域

**`job_queue`** — Postgres 内建队列（见 §15）
`id PK` · `job_type enum(EVAL_TASK|VALIDATE_TASK|BUILD_IMAGE|ATTRIBUTE|MINE_REPO|GEN_REPORT)` · `payload jsonb` · `priority smallint` · `state enum(PENDING|LEASED|DONE|FAILED|DEAD)` · `attempts smallint` · `max_attempts smallint` · `lease_owner` · `lease_expires_at` · `available_at` · `last_error text` · `created_at`
索引：`(state, available_at, priority)`、`(lease_expires_at) WHERE state='LEASED'`

## 13.3 关键关系
```
repositories 1─n environment_specs 1─n benchmark_tasks n─n benchmark_sets (via benchmark_set_items)
agents 1─n agent_configs 1─n evaluation_runs 1─n evaluation_task_runs
evaluation_task_runs 1─n test_results / 1─n patch_artifacts / 1─1 failure_attributions / 1─n human_reviews
* 1─n artifacts (多态 owner_type/owner_id)
```

## 13.4 不入库的内容
Agent stdout/stderr（可达数 MB）、测试完整日志、junit XML、轨迹 JSONL、HTML 报告、补丁全文、镜像构建日志 —— 全部走 ArtifactStore，库里只留 `artifacts` 索引行 + 2KB 摘要（用于列表页预览与规则归因的快速匹配）。

---

# 14 Backend Architecture

## 14.1 技术选型
Python 3.11+ · **FastAPI**（自动 OpenAPI → 前端类型生成；async 原生；Pydantic v2 做协议校验，与我们"协议冻结"的诉求天然契合）· SQLAlchemy 2.0（同步会话即可，评测是重 IO 但 Worker 独立进程）· Alembic · docker SDK for Python · structlog · pytest。

**为何不是 Django**：我们几乎不需要 Admin/ORM 之外的东西，但非常需要"协议对象 = Pydantic 模型 = OpenAPI = 前端类型"这条链路。
**为何不是纯脚本**：需要前端、需要长期存储、需要并发编排。

## 14.2 模块化单体结构（边界即目录）
```
app/
  api/              HTTP 层：路由、请求/响应模型、依赖注入（薄）
  domain/           纯领域模型与枚举（Evaluation Semantics 的代码化，零外部依赖）
  benchmark/        任务 Schema、校验器、挖掘器、数据集版本
  runner/           AgentRunner 协议 + 各适配器
  sandbox/          Docker 封装、镜像构建、工作区物化、资源限额
  evaluation/       编排、状态机、重试策略
  judge/            报告解析、补丁归一化、F2P/P2P 判定
  attribution/      规则分类、特征提取、LLM Judge
  report/           统计聚合、HTML/Markdown 生成
  storage/          ArtifactStore 抽象与实现
  infrastructure/   DB、队列、配置、日志、指标
  worker/           Worker 进程入口与 job handler
```

**依赖方向（写进 CI 的 import-linter 规则）**：
`api → evaluation/benchmark/report → runner → sandbox/judge/attribution → storage/infrastructure → domain`
`domain` 不依赖任何模块；`sandbox` 不依赖 `runner`（Runner 用 Sandbox，反之不行）。

> **2026-09-04 修正（E3-T1）**：`runner` 原先和 `sandbox/judge/attribution` 并排写在同一层。
> import-linter 里同层并排的含义是**互不可见**，于是"Runner 用 Sandbox"这句话在配置里
> 反而是被禁止的——真实适配器一调 `run_in_container` 起容器就会让 CI 红。
> 现在把 `runner` 单独提一层压在 `sandbox` 上面，两条规则才一致。

## 14.3 核心接口（签名级设计）
```python
# benchmark
def validate_task(task: TaskDefinition, *, sandbox: Sandbox) -> ValidationReport
def publish_set(slug: str, version: str, task_ids: list[str]) -> BenchmarkSet

# sandbox
def materialize_workspace(repo: Repository, base_commit: str, dest: Path) -> Workspace
def run_in_container(image: str, cmd: list[str], *, limits: ResourceLimits,
                     network: NetworkPolicy, mounts, env, timeout_s) -> ContainerResult

# runner
def run_agent(cfg: AgentConfig, task_input: AgentTaskInput, ws: Workspace) -> AgentRunResult

# judge
def normalize_patch(raw: str, protected: list[str]) -> NormalizedPatch
def run_tests(task, patch: NormalizedPatch, *, sandbox) -> TestExecution
def judge(task, execution: TestExecution) -> JudgeVerdict   # → agent_outcome + 逐用例

# evaluation
def create_run(set_id, agent_config_id, opts) -> EvaluationRun     # 展开为 N 个 job
def execute_task_run(task_run_id: int) -> None                      # Worker 主循环调用
def cancel_run(run_id: int) -> None

# attribution
def attribute(task_run_id: int) -> FailureAttribution

# report
def build_run_report(run_id: int, fmt: ReportFormat) -> Artifact
def build_comparison_report(run_ids: list[int], fmt) -> Artifact
```

## 14.4 REST API（P0 子集）
```
GET  /api/health
GET  /api/repositories
GET  /api/benchmark-sets            GET /api/benchmark-sets/{slug}
GET  /api/tasks?set=&state=&q=      GET /api/tasks/{task_id}
POST /api/tasks/{task_id}/validate
GET  /api/agents                    GET /api/agent-configs
POST /api/runs      {benchmark_set_id, agent_config_id, agent_concurrency, sandbox_concurrency}
GET  /api/runs                      GET /api/runs/{id}
POST /api/runs/{id}/cancel          POST /api/runs/{id}/retry-failed
GET  /api/runs/{id}/task-runs?status=
GET  /api/task-runs/{id}            GET /api/task-runs/{id}/tests
GET  /api/task-runs/{id}/artifacts/{kind}     # 302 → 签名 URL 或直接流式返回
GET  /api/leaderboard?set=&metric=
GET  /api/attribution/summary?run_id=
GET  /api/review/queue?batch=       POST /api/review/{task_run_id}
POST /api/reports  {scope, run_ids, format}   GET /api/reports/{id}
```
认证：P0 用**单一管理员 Token**（`X-Bench-Token` header）保护写操作，读接口开放。完整用户体系属于 P2（§29）。

---

# 15 Async Execution Architecture

## 15.1 方案比较

| 方案 | 可靠性 | 重试 | 并发控制 | 任务状态可查 | 部署复杂度 | 4 周成本 |
|:---|:---|:---|:---|:---|:---|:---|
| FastAPI BackgroundTasks | ✗ 进程重启即丢 | 无 | 无 | ✗ | 最低 | — **直接淘汰**：评测跑 10+ 分钟，不能占 HTTP 线程（学校要求也明确排除） |
| Celery + Redis | 高 | 内建 | 内建 | △ 状态在 Redis，与业务库分离 | +2 服务（Redis、Flower） | 中：需处理序列化、结果后端、双份状态 |
| RQ + Redis | 中高 | 内建 | 简单 | △ | +1 服务 | 低 |
| **Postgres 队列 + 独立 Worker 进程** | 中高 | 自实现（~150 行） | 自实现信号量 | **✓ 与业务同库同事务** | **+0 服务** | 低 |

## 15.2 决策：Postgres 队列 + 独立 Worker（ADR-003）

**理由（按重要性）**
1. **状态即领域**：`EvaluationTaskRun` 的状态机本身就是业务核心资产，前端要查、报告要用、答辩要讲。用 Celery 会把"作业状态"和"评测状态"割裂成两套真相，反而增加复杂度。
2. **两件事一起成功或一起失败**："领走这个作业"和"把任务状态改成执行中"可以放在同一个数据库事务里。用 Celery 的话，作业状态在 Redis、任务状态在 PostgreSQL，可能出现"Redis 说跑完了、数据库说没跑"这种对不上的情况。
3. **少一个服务**：4 周项目里，每多一个中间件就多一份部署文档、一份故障模式、一次答辩追问。
4. **可观测**：`SELECT * FROM job_queue` 就能看清一切，调试成本极低。

**实现要点**
```sql
-- 领取（SKIP LOCKED 保证多 Worker 无冲突）
UPDATE job_queue SET state='LEASED', lease_owner=:wid,
       lease_expires_at=now()+interval '30 min', attempts=attempts+1
WHERE id = (SELECT id FROM job_queue
            WHERE state='PENDING' AND available_at<=now()
            ORDER BY priority DESC, id ASC
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```
- **租约续期**：Worker 每 60s 心跳延长 `lease_expires_at`；
- **僵尸回收**：`state='LEASED' AND lease_expires_at < now()` → 重置为 PENDING（`attempts < max_attempts`）或 DEAD；
- **重试退避**：`available_at = now() + 2^attempts * 30s`；
- **优雅停机**：收到 SIGTERM 后不再领新作业，等当前作业结束（最多等 `total_timeout`），并释放租约。

**双层并发信号量**（§4.6）：Worker 进程内两把信号量
`agent_sem = Semaphore(AGENT_CONCURRENCY)`、`sandbox_sem = Semaphore(SANDBOX_CONCURRENCY)`；
一个 task_run 在 AGENT_RUNNING 阶段持 agent_sem，在 TESTING/PREPARING 阶段持 sandbox_sem，**不同时持有两把**（否则退化为单层并发）。
落地在 `app/worker/concurrency.py`（两把信号量）+ `app/evaluation/gate.py`（评测单元这一侧的接口），见 §15.2.2。

**万一走不通怎么办**：如果自己写的队列出现查不出原因的可靠性问题，并且卡了超过 1 天，就换成 RQ。因为队列实现被隔离在 `infrastructure/queue.py` 一个文件里，作业处理的代码不用动，切换大概 0.5~1 天。这条写进 ADR-003 的风险栏。

### 15.2.1 实测回填（E5-T1，2026-09-05）

上面那段 SQL 落地成 `app/infrastructure/queue.py` 之后，有四处和"照着写就行"不一样：

**① `UPDATE ... RETURNING` 要加 `populate_existing`。** 用 SQLAlchemy 的 ORM 版
`update(JobQueue).returning(JobQueue)` 时，如果这条作业已经在当前 session 的身份映射里
（比如刚 `enqueue` 完就 `lease`），RETURNING 回来的是那份**旧**属性 —— `state` 还写着
`PENDING`、`attempts` 还是 0，而数据库里其实已经改了。要显式加
`execution_options(populate_existing=True)`。Worker 每次都开新 session 碰不到这个，
但测试和编排层会。

**② 租约归属要写进 SQL 的 WHERE，不能只在 Python 里判。** `renew_lease` 和 `finish`
都带 `lease_owner = :worker_id`，改不到行就抛 `LeaseLostError`，调用方必须让事务回滚。
挡的场景是：Worker 卡住超过租约时长 → 回收器把作业交给了另一个 Worker → 第一个醒过来
接着写结果。不拦的话同一道题会落两条 attempt 记录、成本被重复计一次。

**③ 时间全部用数据库时钟。** `lease_expires_at`、`available_at` 都写成
`now() + CAST('N seconds' AS INTERVAL)`，不在 Python 端算绝对时间。判断租约是否过期
用的是数据库的 `now()`，两边时钟差几秒就会出现"没到期就被回收"或者"过期很久没人收"。

**④ 僵尸回收之后有退避窗口。** 回收器把作业退回 `PENDING` 时会设
`available_at = now() + 2^attempts × base`，所以**不是**立刻可领。这是有意的：
立刻可领的话，一个必然把 Worker 搞崩的作业会在几毫秒内把重试次数烧光，
而重试的意义正是给外部故障留出恢复时间。

**处理函数跑在独立线程里。** 跑在主线程的话主线程会卡在处理函数里，
`worker_shutdown_grace_s` 就成了摆设 —— 而 docker daemon 偶尔会假死，那时唯一的出路是
`kill -9`，一 `kill -9` 就会留下残留容器，正好是验收标准要挡的那件事。
主线程改成 `join(timeout=1s)` 轮询，信号才处理得到（Python 的信号处理器只在主线程跑）。

**两种重试不能混。** `job_queue.attempts` 管的是"Worker 崩了 / 处理函数抛异常"，
协议 C-18 的映射表管的是"评测本身遇到平台故障"。`execute_task_run()` 不抛异常，
所以跑出 `ENV_BUILD_FAILED` 对队列来说是一次**成功的作业**；评测的重试是**另投一条
作业**（新 `attempt_no`），不是把这条作业重来。混用会让重试预算从 C-18 的 1 次
变成 `max_attempts` 的 3 次。规则实现在 `app/domain/retry.py`。

### 15.2.2 实测回填（E5-T2，2026-09-06）

双层并发落地之后，有五处和纸面设计不一样。前两处是**并发跑起来才会撞上**的问题，
串行跑一万次也遇不到。

**槽位和信号量是两件事。** `worker_slots`（默认 8）管"同时有几道题在途"，
也就是 §4.6 对外声明的那个并行度；两把信号量管"这一刻允许几个在调 AI、
几个在跑测试"。槽位设得比 `agent_concurrency + sandbox_concurrency` 大没有意义 ——
多出来的作业只会占着租约卡在信号量上，既不干活，又让在途任务数这个指标虚高。

**① 落库事务的第一件事必须是锁住实验那一行。** 往 `evaluation_task_runs` 插一行时，
Postgres 会顺手在父行（`evaluation_runs`）上加一把 `FOR KEY SHARE`，防止父行中途被删。
这把锁**互相兼容**，所以两条作业能同时拿到；等它们各自再去要 `FOR UPDATE` 更新进度时，
就成了两边都在等对方放开 —— 教科书式的锁升级死锁。8 槽位实测里真撞了一次
（作业 #133，`DeadlockDetected`，白等 60 秒退避才重试成功）。
改成先 `FOR UPDATE` 再插子表就没有升级这一步（`app/evaluation/progress.py` 的 `lock_run`）。

**② 槽位满的时候不能干等一个轮询周期。** 主循环原来是"没领到活就 `wait(job_poll_interval_s)`"，
而"槽满"和"队列空"是两种情况：槽满的时候要等的是**有槽空出来**，不是 5 秒。
实测（120 条 Oracle 作业）：一批 8 道题一秒跑完，然后机器空转四秒 ——
**70% 的时间在途数是 0，而峰值看起来还是满的 8**。改成等一个"槽位释放"事件之后，
同一批作业 77 秒变成 17 秒，有效并发的 P50 从 0 变成 8。

> 这条也解释了为什么验收标准要的是**时间序列**而不是一个峰值数字：
> 只看峰值，改之前改之后都是 8，什么问题都发现不了。

**③ 连接池要按槽位数算。** 每条在跑的作业占两条连接（处理函数一条、心跳一条），
8 个槽位就是 16 条，而 SQLAlchemy 默认池是 5 条。坐穿之后的表现很难查：
拿不到连接的线程阻塞在 `session_factory()` 上，不报错，只是"并发调高了反而更慢"。
`create_db_engine(pool_size=slots * 2 + 4)`。

**④ 取消是两步，只做第一步不够。** 置一个协作式的取消标志，`execute_task_run()`
在三个阶段边界上查它 —— 但一道题最长的那一段（被测 AI 在容器里跑十几分钟）
正好没有边界。所以第二步是**按 `bench.run_id` 标签前缀把这次实验的容器杀掉**，
`container.wait()` 立刻返回，走到下一个边界就收成 `CANCELLED`。
纪律是**只 kill 不 remove**：删容器是 `run_in_container()` 的 `finally` 的事，
这里抢着删，那边紧接着的 `container.reload()` 会撞 404，一次干净的取消
就变成一条 `HARNESS_ERROR`。

**⑤ 有效并发时序不采样，从时刻列扫出来。** `evaluation_task_runs` 上本来就记了
五个时刻，每行给出三段区间（在途 / AI 在跑 / 测试在跑），做一次扫描线就是并发曲线。
比每秒采样好三点：不用新表新线程、对已经跑完的实验也能出图、而且是精确的。
P50 **按时间加权**算，不是对变化点取中位数 —— 变化点的疏密和实际持续时间没关系。

**实测数字**（Oracle × 4 道 Golden 题 × 30 轮 = 120 条作业，`worker_slots=8`、
`agent=10`、`sandbox=5`）：

| 指标 | 实测 | 说明 |
|:---|:---|:---|
| 在途任务数 | 峰值 **8**、P50 **8** | MET-03 要求峰值 ≥8、P50 ≥8 |
| 同时跑的测试容器 | 峰值 **5**、P50 **5** | 正好卡在 `sandbox_concurrency` 上，第二层确实在起作用 |
| 同时跑的被测 AI | 峰值 1 | Oracle 不调模型，这一层压根没排队；真实 Agent 的数字要另测 |
| 内存峰值 | **28.2%**（11.7 GiB 的机器） | 验收线是 <80% |
| 120 条作业总耗时 | 17 秒 | 单题约 0.9 秒，其中测试容器约 0.7 秒 |
| 作业失败 / 死锁 | 0 | 修①之前同样的负载必现死锁 |

⚠️ **内存这个数字受题目大小主导，不能直接外推到真实数据集。** Golden 题的测试
跑起来只占几十 MB，而 `sandbox_memory_mb` 的硬上限是 1536。按上限算最坏情况：
5 × 1.5 GB = 7.5 GB，加上实测基线 3.2 GB 就是 91%，**超过验收线**。
所以正式实验前二选一：把 `sandbox_memory_mb` 降到 1280（§4.6 已经写了这条建议，
5 × 1.25 + 3.2 = 9.5 GB ≈ 82%，仍然偏紧），或者把 `sandbox_concurrency` 降到 4
（4 × 1.5 + 3.2 = 9.2 GB ≈ 79%）。这件事要在 E9-T2 的并发压测里定档。

**取消实测**：25 个实验、100 条作业，`sandbox_concurrency=1` 让任务堆在信号量上，
跑到一半下取消命令 ——

| 阶段 | 实测 |
|:---|:---|
| 下命令（把实验标 CANCELLED + 掐掉待跑作业） | 0.04 秒 |
| 到"没有活作业、没有残留容器" | **0.87 秒**（验收线 30 秒） |
| 另一次：92 条待跑 + 8 条在跑 | 4.10 秒 |
| 被取消的执行落库 | `CANCELLED / CANCELLED / NULL`，`is_canonical = false` |

被取消的 attempt **不打 canonical、不排重试**：它不是这道题的结论，而是"没来得及跑完"。
打了标就等于人工制造了一个认定结果（协议 C-25 禁止）。C-70 只要求
`COMPLETED`/`PARTIAL` 的实验每题恰好一个 canonical，被取消的实验不受这条约束。

## 15.3 全局限流与退避
LLM 提供方 429 是长跑实验的头号杀手。设计一个 `RateLimiter`（按 `agent_config_id` 分桶的令牌桶 + 自适应退避）：连续 429 时自动降低该配置的有效并发（`AGENT_CONCURRENCY -= 1`，下限 1），成功一段时间后缓慢恢复。**所有等待时间累加到 `external_wait_ms`**，用于性能报告中把"平台吞吐"和"外部限流"分开（§4.6 Plan B）。

---

# 16 Frontend Architecture

## 16.1 技术选型
Next.js 16（App Router）· React 19 · TypeScript · Tailwind CSS · shadcn/ui · TanStack Query（服务端状态）· Recharts（图表）· `diff2html` 或自研轻量 diff 渲染。
API 类型：从 FastAPI 的 OpenAPI 用 `openapi-typescript` 生成，避免手写类型漂移。

**实时性策略**：P0 用 **轮询**（TanStack Query `refetchInterval`：Run Detail 3s、Dashboard 10s）。理由：实现成本≈0、无连接管理、足够满足"看进度"的需求。WebSocket/SSE 归入 P2（§29）。

## 16.2 页面清单（P0 加粗）

| 页面 | 路由 | 核心内容 | 优先级 |
|:---|:---|:---|:---:|
| **Dashboard** | `/` | 数据集/Agent/运行总览、最近运行、当前并发 | P0 |
| **Benchmarks** | `/benchmarks` | 数据集列表：版本、题量、语言分布、来源构成 | P0 |
| **Benchmark Detail** | `/benchmarks/[slug]` | 任务表格（筛选：仓库/难度/语言/状态）、验证证据、Oracle/Noop 自检结果 | P0 |
| Task Detail | `/tasks/[taskId]` | Issue 原文、F2P/P2P 清单、验证流水线证据、各 Agent 在该题的历史表现 | P1 |
| **Agents** | `/agents` | Agent 与配置（模型、单价、版本、probe 状态） | P0 |
| **Evaluation Runs** | `/runs` | 运行列表 + 状态 + 进度条 | P0 |
| **Run Detail** | `/runs/[id]` | 实时进度、按状态分组的任务网格、解决率/成本/耗时汇总、取消/重试失败项 | P0 |
| **Task Run Detail** | `/task-runs/[id]` | **Patch Viewer** + **测试结果表（F2P/P2P 逐条）** + 日志 + 轨迹时间线 + 归因结果 | P0 |
| **Leaderboard** | `/leaderboard` | 多指标排序、成本-解决率散点、按难度/语言/仓库分面 | P0 |
| Failure Analysis | `/analysis` | 归因分布堆叠柱、Agent×类别热力图、Top 失败案例 | P1 |
| Human Review | `/review` | 盲检队列 + 三栏对照 + 准确率/κ 统计 | P1 |
| Reports | `/reports` | 生成/下载 HTML·Markdown·JSON 报告 | P1 |

## 16.3 UI 纪律
- **不做**：登录美化、暗黑模式切换动效、复杂设计系统、页面转场动画、自定义图表引擎。
- **要做**：表格能筛能排、diff 能看清、日志能搜、长列表虚拟滚动、进度不刷屏。
- 一条实用规则：**任何页面在 3 次点击内能到达"某个 Agent 在某道题上为什么失败"的完整证据。** 这是评测平台的核心用户旅程，也是答辩演示主线。

---

# 17 Artifact Storage

## 17.1 抽象
```python
class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes | IO, *, content_type: str, compress: bool = True) -> ArtifactRef
    def get(self, key: str) -> bytes
    def open(self, key: str) -> IO[bytes]
    def url(self, key: str, *, expires_s: int = 3600) -> str | None   # MinIO 返回签名 URL；Local 返回 None
    def exists(self, key: str) -> bool
    def delete(self, key: str) -> None
```
实现：`LocalArtifactStore`（P0，落 `/var/lib/bench/artifacts`，API 通过 `/api/.../artifacts/{kind}` 流式返回）、`MinioArtifactStore`（P1，S3 兼容，签名 URL 直连，减轻 API 负担）。
切换只靠配置 `ARTIFACT_BACKEND=local|minio`，**业务代码零改动**——这条是 ADR-005 的核心论据。

## 17.2 Key 命名规范
```
tasks/{task_id}/gold_patch.diff
tasks/{task_id}/test_patch.diff
tasks/{task_id}/validation/{validated_at}/evidence.json
envs/{environment_id}/build.log.gz
runs/{run_id}/task-runs/{task_run_id}/agent_stdout.log.gz
runs/{run_id}/task-runs/{task_run_id}/agent_patch.diff
runs/{run_id}/task-runs/{task_run_id}/test_report.xml.gz
runs/{run_id}/task-runs/{task_run_id}/trajectory.jsonl.gz
runs/{run_id}/report.html
```
- 全部文本制品 **gzip 压缩**后存储（日志压缩比常 10:1）；
- 每个制品记录 `sha256`，支持完整性校验与去重；
- 保留策略：任务/数据集制品永久；运行制品默认永久（磁盘充裕），提供 `bench artifacts gc --before <date>` 手动清理。

## 17.3 容量估算
单次 task_run 制品：agent_stdout 0.2–3 MB（压缩后 20–300 KB）+ 轨迹 50–500 KB + 测试日志 10–200 KB + 补丁 2–20 KB ≈ **压缩后 100 KB – 1 MB**。
300 次实验 ≈ **30 MB – 300 MB**。全项目（含验证期数千次任务验证）≈ **5–20 GB**。本机 920 GB 可用，**存储不是瓶颈**，镜像才是（≤80 GB）。

## 17.4 实现落地与实测结论（2026-09-03，E0-T4）

`ArtifactStore` 协议与 `LocalArtifactStore` 已实现（`backend/app/storage/`），39 条契约测试全绿。
几处规格在实现时被收紧，都是踩到具体问题之后定的：

**key 里不带 `.gz`，压缩由存储层负责。** §17.2 那张表写的是**磁盘上的路径**；
调用方给的 key 是 `runs/12/task-runs/340/agent_stdout.log`，落盘才变成 `....log.gz`。
这样某类制品将来改成不压缩，key 不用跟着改，数据库里已有的行也不用迁移。
key 自带 `.gz` 会被直接拒绝——否则"这份文件到底压没压"有两个互相矛盾的答案。

**`sha256` 和 `size_bytes` 记的是原始内容，不是压缩后的。** 哈希是内容的身份证，
按压缩后算的话，同一份日志换个压缩级别哈希就变了，去重和完整性校验一起失效。
实际占用的磁盘另记在 `stored_bytes`（不入库，只用来算压缩比）。

**gzip 输出是确定性的。** 默认行为会把当前时间和 `fileobj.name` 写进 gzip 文件头，
于是同一份内容压两次得到不同的字节——而这里的 `fileobj` 是带随机 UUID 的临时文件，
那段 UUID 会原样进到每个制品的文件头里。实现里显式设了 `mtime=0` 和 `filename=""`。
相同内容 → 相同文件，这是"结果可复现"的前提。

**写入用"临时文件 + `os.replace()`"，不直接写目标文件。** 评测容器被 OOM 杀掉是这个项目的
日常（协议 C-06），中途夭折会留下一个长度不对但看着正常的文件，读出来是半截日志且不报错。

**key 校验挡路径穿越。** key 里要拼进从 GitHub 挖来的仓库名和题目 ID，属于外部数据。
`../../etc/passwd` 这类写法在四个入口（put/get/exists/delete）一律拒绝；
另有一层 `resolve()` 之后的边界检查，挡制品目录里指向外面的软链。

**`ARTIFACT_LOCAL_ROOT` 的相对路径按仓库根目录解析，不按当前工作目录。**
API 是 `cd backend && uvicorn` 起的，Worker 和 CLI 在仓库根起，按当前目录解析会得到
两个不同的目录，表现是"写进去的制品读不出来"，而且不报错。
已用 `scripts/check_env.py` 的"制品目录可写"一项实测确认（cwd 在 `backend/` 时仍落在仓库根的
`var/artifacts`）。

---

# 18 Performance Model（容量模型）

## 18.1 单题时间预算
| 阶段 | 乐观 | 典型 | 悲观 | 说明 |
|:---|--:|--:|--:|:---|
| PREPARING（archive + 起容器） | 5s | 15s | 40s | 镜像已预建，无 pip install |
| AGENT_RUNNING | 90s | **360s** | 720s（硬超时） | 主导项，取决于 Agent 与题目难度 |
| PATCH_CAPTURED | 1s | 3s | 10s | |
| TESTING（只跑 F2P∪P2P 子集） | 20s | **75s** | 480s（硬超时） | 精选仓库时通常 <90s |
| JUDGING + ANALYZING | 2s | 8s | 30s | 归因异步化后可不计入关键路径 |
| **合计** | **~2 min** | **~7.7 min** | **~21 min** | |

## 18.2 Makespan 计算

makespan 指**从第一道题开始跑到最后一道题结束的总墙钟时间**，不是所有题耗时之和。

设：`N=300` 次运行，AI 干活阶段平均 `A=6 分钟`，其余阶段（准备 + 跑测试 + 判定）平均 `S=1.7 分钟`。
两层并发数分别是 `P_agent` 和 `P_sandbox`，理论最短时间是：

```
makespan ≥ max( N·A / P_agent , N·S / P_sandbox )
```

| 配置 | Agent 侧 | Sandbox 侧 | 理论 makespan | 6h 达标? |
|:---|--:|--:|--:|:--:|
| 本机当前 8C/10G：`P_agent=8, P_sandbox=4` | 300×6/8 = **225 min** | 300×1.7/4 = **128 min** | **≈3.8 h**（+调度损耗 25% ≈ 4.7h） | **✓（有余量）** |
| **本机调 .wslconfig 后 16C/11G：`P_agent=10, P_sandbox=5`**（已实施） | 300×6/10 = **180 min** | 300×1.7/5 = **102 min** | **≈3.0 h**（+25% ≈ 3.8h） | **✓✓ 已达成，零成本** |
| 本机保守：`P_agent=6, P_sandbox=3` | 300 min | 170 min | ≈5.0h（+25% = 6.3h） | **△ 临界** |
| 云主机兜底 16C/32G：`P_agent=12, P_sandbox=8` | 150 min | 64 min | ≈2.5h（+25% = 3.1h） | **✓✓ 仅在需要余量时按小时租用** |
| 无预建镜像（+2 min/题装依赖） | — | 300×3.7/4=278 min | ≈4.6h + Agent 225 min 叠加 | **✗** |
| Agent 均值 10 min（悲观） | 375 min | 128 min | ≈6.3h（+25% = 7.8h） | **✗ → 触发降级** |

**结论**：
1. **6 小时目标在"预建镜像 + Agent 均值 ≤6–7 min + P_agent≥8"下可达**，且**在本机即可完成，无需采购硬件**——把 `.wslconfig` 调到 16 vCPU / 12 GB 后余量从 1.3h 提升到 2.2h；
2. **最敏感的变量是 Agent 均值耗时**，而它由外部 Agent 决定，不由我们决定 → 因此 `agent_timeout_s=720` 这个硬超时是**性能保障机制**而非仅仅是安全机制；**注意 Agent 侧（180 min）远大于 Sandbox 侧（102 min），说明内存受限导致的 `P_sandbox` 下调不会影响 6 小时目标——瓶颈不在这里**；
3. Pilot 实验（30 题 × 3 Agent）必须在 Week 3 完成，用实测 A 值回代本模型，Week 4 才知道要不要降级。

## 18.3 瓶颈清单与归属
| 瓶颈 | 归属 | 缓解 |
|:---|:---|:---|
| Agent LLM 响应延迟 | **外部** | 提高 P_agent；分列 `external_wait_ms` |
| LLM 提供方 429 / 并发上限 | **外部** | 自适应退避；分 Agent 分时段；换配置 |
| 依赖安装 | 平台 | **预建镜像**（已解决） |
| Git clone | 平台 | 本地 mirror + `git archive`（已解决） |
| 测试执行 CPU | 平台 | 仓库选型限制测试 ≤180s；只跑子集；P_sandbox 限流 |
| Docker 容器启停开销 | 平台 | 复用镜像；避免每阶段多余容器 |
| 内存 | 平台 | P_sandbox 限流；`--memory` 硬限 |
| Postgres | 平台 | 无压力（万级行） |
| 磁盘 IO | 平台 | 工作区放 SSD；及时清理 |

## 18.4 性能报告必含项（DEL-05）
总耗时 makespan · 等外部服务的时间占比 · 各阶段耗时的 P50 / P95 / 最大值（P50 是中位数，一半的任务比它快；P95 是把所有任务按耗时排序后第 95% 那个值，用来看最慢的那批有多慢）· 实际并发数随时间变化的曲线 · 每个 AI 的 token 和费用分布 · 每题费用的 P50 / P95 · CPU 和内存峰值 · 平台故障率 · **换成 16 核 32 G 机器后的推算耗时**。
