# 13 Data Model（PostgreSQL）

## 13.1 设计原则
1. **状态字段一律用原生 enum 或带 CHECK 的 varchar**，禁止裸字符串；
2. **大制品不进库**：日志、轨迹、补丁全文走 ArtifactStore，库里只存 URI + sha256 + size + ≤8KB 摘要；
3. **JSONB 只用在"结构会演化且不需要 join 查询"的地方**：任务原始定义、运行 manifest、Agent 配置、LLM 归因原始响应、成本明细；
4. **不过早拆表**：`evaluation_task_runs` 是宽表（含各阶段时间戳与统计），而不是拆成 5 张阶段表；
5. **任务内容哈希化**：`content_hash` 让"数据集版本"成为可验证的事实。

## 13.2 表清单（15 张）

### A. 基准域

**`repositories`** — 被评测的开源仓库
`id PK` · `full_name UQ` · `url` · `default_branch` · `language` · `stars` · `license` · `is_domestic bool` · `mirror_path` · `created_at`

**`environment_specs`** — 环境规格（镜像的逻辑定义）
`id PK` · `environment_id UQ`(如 `nonebot2__py311__v3`) · `repository_id FK` · `python_version` · `install_command` · `pre_test_command` · `test_command` · `test_framework` · `test_report_path` · `protected_paths jsonb` · `image_tag` · `image_digest` · `build_status enum(PENDING|BUILDING|READY|FAILED)` · `built_at` · `build_log_uri`
索引：`(repository_id)`、`(build_status)`

**`benchmark_tasks`** — 任务本体
`id PK` · `task_id UQ` · `repository_id FK` · `environment_spec_id FK` · `base_commit char(40)` · `issue_title` · `issue_body text` · `issue_language enum` · `source_issue_url` · `source_pr_url` · `fail_to_pass jsonb` · `pass_to_pass jsonb` · `test_patch_uri` · **`test_patch_paths jsonb`**（由 Validator 从 test_patch 推导，纳入 content_hash，禁止下发给 AI，见协议 C-74~C-76）· `gold_patch_uri` · `difficulty enum` · `tags text[]` · `agent_timeout_s` · `test_timeout_s` · `sandbox_cpu numeric` · `sandbox_memory_mb` · `validation_state enum(DISCOVERED|CANDIDATE|VALIDATING|VALID|INVALID|REVIEW_REQUIRED|QUARANTINED)` · `invalid_reason_code` · `validated_at` · `validation_evidence_uri` · `content_hash` · `raw_definition jsonb` · `created_at/updated_at`
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
`id PK` · `name` · `benchmark_set_id FK` · `agent_config_id FK` · `status enum(DRAFT|QUEUED|RUNNING|COMPLETED|PARTIAL|FAILED|CANCELLED)` · `agent_concurrency` · `sandbox_concurrency` · `total_tasks` · `completed_tasks` · `resolved_count` · `infra_failure_count` · `strict_resolve_rate numeric` · `effective_resolve_rate numeric` · `total_cost_usd numeric` · `total_tokens bigint` · `makespan_ms bigint` · `external_wait_ms bigint` · **`protocol_version varchar`**（创建时写入，禁止事后修改，见协议 C-67）· **`retry_count int`** · **`recovered_infra_failure_count int`** · `manifest jsonb` · `started_at` · `finished_at` · `created_by`
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
`api → evaluation/benchmark/report → runner/sandbox/judge/attribution → storage/infrastructure → domain`
`domain` 不依赖任何模块；`sandbox` 不依赖 `runner`（Runner 用 Sandbox，反之不行）。

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

**万一走不通怎么办**：如果自己写的队列出现查不出原因的可靠性问题，并且卡了超过 1 天，就换成 RQ。因为队列实现被隔离在 `infrastructure/queue.py` 一个文件里，作业处理的代码不用动，切换大概 0.5~1 天。这条写进 ADR-003 的风险栏。

## 15.3 全局限流与退避
LLM 提供方 429 是长跑实验的头号杀手。设计一个 `RateLimiter`（按 `agent_config_id` 分桶的令牌桶 + 自适应退避）：连续 429 时自动降低该配置的有效并发（`AGENT_CONCURRENCY -= 1`，下限 1），成功一段时间后缓慢恢复。**所有等待时间累加到 `external_wait_ms`**，用于性能报告中把"平台吞吐"和"外部限流"分开（§4.6 Plan B）。

---

# 16 Frontend Architecture

## 16.1 技术选型
Next.js 15（App Router）· React 19 · TypeScript · Tailwind CSS · shadcn/ui · TanStack Query（服务端状态）· Recharts（图表）· `diff2html` 或自研轻量 diff 渲染。
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
