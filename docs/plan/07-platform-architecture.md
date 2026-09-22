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
`id PK` · `agent_id FK` · `label`(如 `aider@deepseek-chat`) · `agent_version` · `model_name` · `params jsonb`(temperature/max_turns/…) · `price_input_per_mtok numeric` · `price_output_per_mtok numeric` · `price_cache_read_per_mtok numeric` · `config_hash` · `enabled bool`
> 把"Agent"与"配置"分开是必要的：同一个 Aider 接 3 个模型就是 3 个参赛者，而适配器只有 1 个。

### C. 评测域

**`evaluation_runs`** — 一次实验 = Agent 配置 × 数据集
`id PK` · `name` · `benchmark_set_id FK` · `agent_config_id FK` · `status enum(DRAFT|QUEUED|RUNNING|COMPLETED|PARTIAL|FAILED|CANCELLED)` · `agent_concurrency` · `sandbox_concurrency` · `total_tasks` · `completed_tasks` · `resolved_count` · `infra_failure_count` · `strict_resolve_rate numeric` · `effective_resolve_rate numeric` · `total_cost_usd numeric` · `total_tokens bigint` · `makespan_ms bigint` · `external_wait_ms bigint` · **`protocol_version varchar`**（创建时写入，禁止事后修改，见协议 C-67）· **`retry_count int`** · **`recovered_infra_failure_count int`** · **`dirty bool`**（工作区带未提交改动时启动的实验，不得进排行榜，见协议 C-27、C-28）· `manifest jsonb` · `started_at` · `finished_at` · `created_by`
索引：`(status)`、`(benchmark_set_id, agent_config_id)`
> `manifest jsonb` 承载 §24 可复现性的全部字段（镜像 digest 表、harness git sha、数据集哈希、环境变量白名单、随机种子）。

**`evaluation_task_runs`** — 单题单次执行（核心宽表）
`id PK` · `evaluation_run_id FK` · `benchmark_task_id FK` · `attempt_no smallint` · `lifecycle_status enum` · `infra_outcome enum` · `agent_outcome enum` · `queued_at/prepare_started_at/agent_started_at/agent_finished_at/test_started_at/test_finished_at/judged_at/completed_at` · `agent_duration_ms/test_duration_ms/total_duration_ms` · `exit_code` · `tokens_input/tokens_output/tokens_cache_read/tokens_total bigint` · `cost_usd numeric` · `cost_source enum` · `turns` · `patch_artifact_id FK NULL` · `files_changed/lines_added/lines_deleted` · `f2p_passed/f2p_total/p2p_passed/p2p_total` · `error_code` · `error_message_excerpt varchar(2000)` · `worker_id` · `retry_of_id FK NULL` · **`is_canonical boolean`**（这次 attempt 是否被选为统计依据，规则见协议 C-24）· **`raw_patch_empty boolean`** · **`protected_path_edit_attempted boolean`** · **`filtered_change_reasons jsonb`**
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
`id PK` · `owner_type enum(TASK|TASK_RUN|EVAL_RUN|VALIDATION)` · `owner_id` · `kind enum(AGENT_STDOUT|AGENT_STDERR|TEST_STDOUT|TEST_REPORT_XML|TRAJECTORY|PATCH|REPORT_HTML|REPORT_MARKDOWN|REPORT_JSON|VALIDATION_EVIDENCE|BUILD_LOG)` · `uri` · `backend enum(LOCAL|MINIO)` · `content_type` · `size_bytes` · `sha256` · `compressed bool` · `created_at`
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

## 13.5 运行 Manifest 落地实录（2026-09-11，E5-T4）

> **本节是追加的实现记录。** §13.2 里 `evaluation_runs.manifest` 那一列的说明一个字没动，
> 这里写的是它到底装了什么、以及为什么有些东西**没**装进去。

工具：`app/evaluation/manifest.py` + `app/domain/manifest.py` +
`python -m cli.experiment {manifest,replay}`。**没有新迁移** ——
`manifest` / `dirty` / `protocol_version` 三列在 0001 初始迁移里就有，
E5-T4 之前只是没人往里写。

### 一、任务卡只写了一句话，AC 是开工前定的

原卡的 AC 是「由 manifest 可重建一次等价运行；两次运行的 manifest diff 只在时间戳上
不同」。"可重建"到哪一步、记哪些字段、字段对不上时怎么办，一个字没有。
定下来的 11 条见任务卡，最要紧的三条是：**"可重建"只管输入条件不管结果**、
**C-27 的强制点只能有一个**、**manifest 里必须分出"允许不同"的那几个键**。

### 二、"可重建"划在输入条件，不划在结果

`replay` 保证的是"这一次和那一次跑的是同一批题、同一个参赛者、同一套镜像、同一版代码"。
它**不保证**两次的解决率一样。

这条线不是偷懒，是协议定的：**C-73 写着"测试执行的可复现性是目标，不是保证"**。
把 AC 写成"两次结果必须一样"就和 C-73 打架了 —— §7.11 第九节实测过 click 那一族
竞态用例，同一份补丁三次跑出过"过 / 挂 / 过"。

所以 AC 的机器化形式是：**两次运行 manifest 里"必须相同"的字段逐字相同**。
逐实例一致率是 MET-01 的口径，那是 E10-T5 的活。

三张卡的分工（三者输入不同，不重叠）：

| 卡 | 输入 | 产出 |
|:---|:---|:---|
| **E5-T4（本节）** | **我们自己**某次运行的 manifest | 一个等价的新 run + manifest diff |
| E3-T8 ReplayRunner | **别人**发布的预测补丁文件 | 逐实例一致率 |
| E10-T5 校准实验 | E3-T8 的能力 × 官方子集 | MET-01 偏差报告 |

### 三、C-27 的强制点在 `create_runs()`，但凭证是**必填参数**而不是在里面调 git

协议 C-27 要求工作区不干净时拒绝启动正式实验。强制点放在
`app.evaluation.orchestrator.create_runs()`：生产代码里只有那一处建 `EvaluationRun`
（`cli.experiment start`、`cli.queue enqueue`、`cli.dataset gate`、以后的
`POST /api/runs` 全走它），放各个入口的话，写第四个入口的人一定会漏。

**但 `create_runs()` 自己去调 `git status` 是不行的。** 集成测试也调 `create_runs()`，
而开发时工作区**永远是脏的** —— 那样每个集成测试都会红，而它们要验的东西
和工作区干不干净毫无关系。

拆成两步就同时成立：

    collect_provenance(..., allow_dirty=False)   ← git 在这里调，脏工作区在这里拒绝
            ↓ 返回 RunProvenance
    create_runs(..., provenance=<必填>)          ← 只负责写库

必填参数换到的是一条硬性质：**建不出 `manifest = {}` 的运行**。
E5-T4 之前 `cli.experiment start` 建出来的就是空的（本机库里的 #98 就是），
那种运行事后说不清跑的是哪版代码、哪个镜像。测试则直接构造凭证
（`tests/integration/factories.provenance_for`），一次 git 都不调。

顺带把 `benchmark_set_id` / `agent_config_id` / 两个并发数从 `create_runs()` 的
参数表里删了，全从凭证取。分开传的话，行上写的 `agent_concurrency` 和 manifest 里
记的可以是两个值，而**不一致时没有任何东西会报错**。

### 四、manifest 只装"启动时就知道"的事实，三类东西被挡在外面

1. **跑完才知道的**（解决率、makespan、重试次数、平台故障数）不进。
   这是 C-67 那条纪律的推广：写进去就不许改。一个既装启动条件又装运行结果的
   JSONB，必然要被改第二次。这些字段 `evaluation_runs` 上都有专门的列。
2. **会变的生命周期字段**不进。最典型的是 `benchmark_sets.status`：门禁在 DRAFT 上跑，
   发布之后变成 PUBLISHED。把它记进去，重放时会凭空多出一条差异，
   而数据集内容一模一样 —— 认数据集身份靠的是摘要，不是状态。
3. **密钥**不进。`determinism.agent_env_allowlist` 只记**名字**不记值。
   记值的话，各家的 API Key 会同时进数据库和 `datasets/manifests/`（那个目录是入库的）。

### 五、"必须相同"和"允许不同"必须分两摞

`app/domain/manifest.py` 的 `VOLATILE_KEYS = {created_at, host, replay_of}`
就是任务卡那句"只在时间戳上不同"的机器化定义：**集合之外的键必须逐字相同，
集合之内的如实记录但不参与等价判断**。

为什么非分不可：NFR-02 要的是"**异机**异时复现"。跑在第二台机器上时 `host`
（docker 版本、内核、CPU 数、内存）一定不同 —— 不划出去，这条 AC 在第二台机器上
**永远过不了**；而不记 `host` 的话，两次结果对不上时第一个要问的问题
（"是不是换机器了、docker 换版本了"）没有任何证据可查。

### 六、"种子"落在 `PYTHONHASHSEED` 上，没有另造一个字段

任务卡 Goal 里点名要记"种子"。查下来平台运行时**没有第二个随机源**：
`random` 只在 `app/benchmark/assembly.py` 的 P2P 抽样里用，那是建题期，
种子记在题目定义里、由数据集摘要覆盖；重试退避是 `2^n × base`，没有抖动。

所以运行侧的"种子"就是 `determinism.env` 里的 `PYTHONHASHSEED=0`，
**不新造一个恒为某值的 `seed` 字段**。理由和 §7.11 第八节对 `dirty` 的推理是同一条
的反面：留一个恒为 false 的 `dirty` 比不留更糟，而留一个没有意义的 `seed`
同样比不留更糟 —— 它会让人以为平台还有一个可调的随机源。

### 七、光比快照摘要抓不到"题目被改了"

`replay` 的第 2 项校验是现算一遍快照摘要。写完才发现它只够抓一半：
**`items_of()` 读的是 `benchmark_set_items.task_content_hash` —— 冻住的那一份**，
题目改了它一动不动。而 Worker 跑题读的是 `benchmark_tasks.raw_definition`，
**活的那一份**。

于是补了第 3 项：直接用 E1-T6 的 `drift()` 查内容漂移 / 被隔离 / 题不见了。
两项抓的是两件事：摘要抓"有人动了题目清单"，漂移抓"清单没动但题被改过"。

### 八、Worker 起容器按 manifest 里的 digest，不按 `environment_specs` 现值

`app/worker/handlers/eval_task.py` 原来是 `image=env.image_tag or DEFAULT_GOLDEN_IMAGE`，
注释里自己写着"协议 C-36 要求引用 digest，那一步在 E5-T4 做"。

关键不只是"改成 digest"，而是 **digest 从哪里取**：从实验自己的 manifest 取，
不从 `environment_specs` 那一行取。那一列会被下一次 `cli.images build` 覆盖 ——
实验建于周一、跑于周三，中间重建过镜像的话，按库里现值跑等于 manifest 说跑的是 A、
实际跑的是 B，**而且不报错**（那一列的 digest 永远是"最新"的，看不出漂移）。

manifest 里同时记 tag 和 digest，因为 docker 的 digest 引用必须带仓库名，
而仓库名只能从 tag 里拆（`bench-env:pallets__click__py311` → `bench-env@sha256:...`）。
本机建的镜像**有** RepoDigest，实测能直接起（见第十节）。
manifest 里没记 digest 时退回按 tag 起，行为和 E5-T4 之前一样 ——
老运行和还没建过镜像的环境走的就是这条路。

### 九、`images gc` 的保护名单漏了一整类

`cli/images.py` 的 `_gc_inputs()` 原来只护 `environment_specs.image_digest` **整列**，
而那一列只有一格：**环境一重建就被新 digest 覆盖**，老实验当初钉的那个立刻变成
"没人引用"，下一次 gc 就删了 —— 而那次实验的可复现性全靠它。
表现是 `replay` 报"镜像已经不在本地"，且无法挽回。

补法是把**所有运行 manifest 里引用过的 digest** 也加进保护名单
（`_digests_in_run_manifests()`）。`gc_candidates()` 的注释原文就写着
"删掉一个还被记着的 digest，等于把那次实验的可复现性抹掉" —— 规矩本来就在，
少的是"还被谁记着"的那一半。

### 十、实测（本机，2026-09-11）

拿已发布的 `benchmark-dev@v1`（22 道题）真跑：

    ① 脏工作区建实验         → 拒绝，点名协议 C-27
    ② --allow-dirty 放行     → 实验 #99，dirty=true 同时落进列和 manifest
    ③ replay --run 99        → 同样先被 C-27 拦，加 --allow-dirty 后建出 #100
    ④ manifest --run 99 --run 100
                             → 必须相同的字段全部一致；
                                差异只有 created_at 和 replay_of，都在 VOLATILE_KEYS 里

`bench-env@sha256:f9c0afc8f30e…` 这个 digest 引用本机实测能直接起容器
（`docker run --rm --network none 'bench-env@sha256:f9c0…' python -c ...` 正常输出）。

---

## 13.6 token→成本估算落地（2026-09-19，E5-T5）

平台现在只补一种结果：适配器返回 `cost_source=unavailable`、没有运行错误、三项 token
完整、三档单价也完整。统一公式在 `app/domain/cost.py`：

```
普通输入 token = tokens_input - tokens_cache_read
cost_usd = (普通输入 × 输入价 + 缓存读取 × 缓存价 + 输出 × 输出价) / 1_000_000
```

缓存读取是输入的一部分，不能再加一次；`tokens_cache_read > tokens_input` 说明数据自相
矛盾，保持 `unavailable`。`reported` 是 Agent 或服务商给出的事实，平台绝不覆盖。
带错误的运行可能只收到了中途 token，同样不估算，避免把部分金额冒充完整账单。

迁移 `0007` 给 `agent_configs` 增加可空的 `price_cache_read_per_mtok`。输入、输出、缓存
读取三档价格会随实验一起写进 manifest（结构版本 1.1）；Worker 优先使用这份快照，
只有 E5-T5 之前的旧 manifest 才回退到数据库现值。这样服务商日后调价不会改变旧实验
的成本口径。MiniAgent 和平台补算共用同一个纯函数，不再维护两套公式。

现有后端排行榜聚合已经分别返回 `reported / estimated / unavailable` 计数，本卡没有改
API，也没有改 `frontend/`。`cli.experiment status` 现在直接显示三档计数；完整 HTML
报告中的成本来源表和成本—能力矩阵留给 E10-T3 报告生成器。已有实验 #125 的 22 次
Aider 实报合计 `$0.3453`，按同批 token 和历史价目估算为约 `$0.3261`，误差 5.6%，
不需要新付费实验也能验证量级。

## 13.7 统一报告生成器落地（2026-09-20，E9-T4 / E10-T3）

`app/report/aggregate.py` 只做查询与聚合，`models.py` 是三种格式共用的版本化中间结构，
`render.py` 才负责排版。解决率、成本来源、分面、阶段耗时、并发曲线和容量外推分别复用
已有 `analytics.leaderboard`、`analytics.timing`、`analytics.concurrency` 和
`domain.makespan`，没有另写一套公式。

入口是 `python -m cli.report generate --run ID [--run ID ...]`。选择的运行必须属于同一个
`benchmark_set` 版本并使用同一个协议版本；不符合排行榜准入规则的真实运行保留在原始
运行表中，但不混进 Agent 对比汇总，并在警告里列出。HTML 是无外部 CDN 的单文件，
Markdown 便于代码评审，JSON 的 `schema_version=1.0` 供后续自动处理。

三份文件挂在首个运行的 `EVAL_RUN` 制品目录，并各写一条 `report_records`；比较报告完整的
`run_ids` 数组仍是权威范围。迁移 `0008` 增加 `REPORT_MARKDOWN` 和 `REPORT_JSON`，避免把
三种文件都谎报成 `REPORT_HTML`。Top-N 失败案例直接链接已有题目执行制品接口，因此本卡
不新增 API，也不改前端。

缺数据不补零：只要一次 attempt 的费用不可用，每题成本就显示“不可用”；当前适配器没有
写 `external_wait_ms` 时，默认值 0 不解释成实测 0%；没有宿主 CPU 采样、LLM 归因、人工
盲检或第三个真实 Agent 时也都主动披露。生成器已经能使用 pilot 数据，但 DEL-03 / DEL-04
是否最终达标仍由 E10-T4 和 E6-T2～T4 提供的实验事实决定。

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
  analytics/        评测与报告共用的只读统计口径
  benchmark/        任务 Schema、校验器、挖掘器、数据集版本
  runner/           AgentRunner 协议 + 各适配器
  sandbox/          Docker 封装、镜像构建、工作区物化、资源限额
  evaluation/       编排、状态机、重试策略
  judge/            报告解析、补丁归一化、F2P/P2P 判定
  attribution/      规则分类、特征提取、LLM Judge
  report/           报告查询、HTML/Markdown/JSON 生成
  storage/          ArtifactStore 抽象与实现
  infrastructure/   DB、队列、配置、日志、指标
  worker/           Worker 进程入口与 job handler
```

**依赖方向（写进 CI 的 import-linter 规则）**：
`api → evaluation/benchmark/report → analytics → runner → sandbox/judge/attribution → storage/infrastructure → domain`
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

> **E6-T3 的例外**：`/api/review/*` 的 GET 也要求管理员 Token。复核详情包含官方补丁
> 的文件/行数摘要，提交后还会显示自动归因，不能作为普通开放读接口。

## 14.5 P0 子集实现落地（2026-09-12，E7-T0）

上面那张表是设计时列的。真按 §16.2 的八个 P0 页面倒推一遍，落地的和它有四处不一样。

### 一、砍掉两个端点，补上四个查询参数

`GET /api/repositories` 和 `POST /api/tasks/{task_id}/validate` **没做**。

前者原本的用途是 Benchmarks 页的"来源构成"，但那一页真正要问的是
"**这一版数据集**里的题来自哪些仓库"，不是"库里一共有哪些仓库" ——
后者在前端没有任何落点。所以构成统计放进了
`GET /api/benchmark-sets/{slug}` 的 `composition` 字段（按语言 / 难度 / 仓库三组）。
后者是写操作，验题走 `make validate-tasks`，八个 P0 页面没有一个要它。

反过来，§14.4 给 `/api/tasks` 写的 `?set=&state=&q=` 三个参数**不够**：
§16.2 的 Benchmark Detail 页写着筛选条件是"仓库 / 难度 / 语言 / 状态"，
所以补了 `repo`、`difficulty`、`language` 三个。这三个参数在设计表里看不出来，
只有对着页面倒推才会发现。

### 二、题目接口不透出两个字段

`gold_patch_uri`（官方修复补丁的位置）和 `test_patch_paths`（官方测试补丁改了哪些文件）
**一个都不返回**。

协议 C-44 禁止把 gold patch 发给被测 AI，C-76 禁止下发 `test_patch_paths`。
这两条管的是"发给 AI 的任务输入"，而读接口是开放的（不需要 token），谁都能拉 ——
把它们放进一个开放的 JSON 接口，等于给绕过任务输入开了第二扇门。
要看官方补丁走命令行（`python -m cli.task show`），那条路上有人在场。

### 三、排行榜的准入口径：协议给的两条不够，一共六条

`/api/leaderboard` 要回答一个协议没覆盖的问题：**哪些运行有资格上榜。**

协议给了两条：C-26 / C-26b（平台故障率超 5% 的记 `PARTIAL`）、C-28（`dirty` 的不进）。
只按这两条筛，库里 18 个实验有 **14 个"合格"** —— 里面有哨兵、有停用的诊断参赛者、
有只跑了 1–2 道题的探测跑，还有 4 个一次模型都没调到的（§18.6 第九节的 #119–#122）。

实现的六条（`app/evaluation/leaderboard.py`）：

| # | 规则 | 依据 |
|:--|:---|:---|
| 1 | `status = COMPLETED` | C-26b 的落点 |
| 2 | `dirty = false` | C-28 |
| 3 | `leaderboard_excluded_reason IS NULL` | 新增，见下 |
| 4 | `agent_configs.enabled = true` | 停用的参赛者不是选手 |
| 5 | `agents.kind` 不是 ORACLE / NOOP / MOCK | 哨兵是量具不是选手 |
| 6 | `total_tasks = benchmark_sets.task_count` | 跑满整份快照才可比 |

第 3–6 条协议里没有。它们回答的不是"这次实验跑得对不对"（那是 C-26 的事），
而是"这个数字能不能和别人的放在一起比"。第 6 条尤其容易漏：严格解决率的分母是
题库总题数（C-21），只跑了 2 道题的探测跑，它的 0% 和跑满 22 道的 0% 不是一个数。

**按 `(参赛者, 协议版本)` 分组**，不是只按参赛者 —— 协议 C-59 要求排行榜按协议版本
分开展示，不能把不同门槛下的结果混排。数据集不进分组键，它是查询参数：
不同数据集的解决率之间没有可比性。

**一行带轮间离散度**（`min` / `max` / `spread` / `run_count`）。
§18.6 第六节实测同一批题跑两遍有五分之一的题会改结论，单轮解决率光凭抖动就差
±9 个百分点，而 MET-01 要求"偏差 ≤5 个百分点" —— 只报一个平均数那个指标没法解释。

### 四、`evaluation_runs` 加了一列：`leaderboard_excluded_reason`（迁移 0006）

上面第 3 条的落点。存的是**理由文本**不是 bool —— 排除是要向人解释的动作，
只记一个 true，半年后没人说得清当初为什么排。

它是被 #119–#122 逼出来的：那四行 `COMPLETED / infra_failure_count=0 / dirty=false /
22 题全有结论`，按协议**完全合格**，但 22 道题的 token 全是 0，一次模型都没调到。
做成一列而不是在查询里现算启发式规则（比如"整场 token 为 0 就算没跑"），理由和 `dirty`
是同一条：排除依据必须是**记下来的事实**，可复核、可撤销，而不是一条藏在 SQL 里、
会误伤将来某个真的一次模型都没调就交空补丁的参赛者的猜测。

填法：`python -m cli.experiment exclude --run 119 --reason "..."`，撤销用 `include`。
**没做成 API 写端点** —— 这种要留痕的判断适合走命令行，不适合在网页上点一下就改掉。
**那四行原有的判定字段一个没动**（`infra_outcome` 仍是 `SUCCESS`、
`agent_outcome` 仍是 `EMPTY_PATCH`）：加一条注，不重写测量结果。

排行榜响应里会把六条规则和被排除的实验连同理由一起返回 ——
**一个不说自己筛掉了什么的排行榜没法复核**，而这里恰好有四条规则协议里没有。

### 五、"报不出成本"不等于"最便宜"

`?metric=cost` 第一版写完，跑真实数据出来是这样：

```
1 claude-code@deepseek-chat   $0.0/题     ← 排第一
2 aider@deepseek-chat         $0.0175/题
```

claude-code 走中转端点，44 次全报 `cost_source=unavailable`（E3-T5 定的规矩），
`total_cost_usd` 因此是 0。用"已知部分 ÷ 全部题数"算，每题就是 $0，于是它以"免费"夺冠。
而 §18.6 第七节手算出来它是 **$0.042/题，比 aider 贵 2.4 倍**。

改法（第一版）：**只要有一次 attempt 报不出成本，`cost_per_task` 就是 `None`**，排序时垫到最后。
金额本身照样放在 `cost_usd_total` 里，配上 reported / estimated / unavailable
三个计数，看的人自己判断那笔钱有多少水分（协议纪律 3 要求三种来源区分显示）。

这不是一个边角情况：库里两个真参赛者，有一个就是全程报不出成本。

**2026-09-21 放宽为"下界"**（E7 前端页面接上真实数据时发现）：E10-T4 两轮里 claude-code
84 次 attempt 缺 2 次（两次 `AGENT_AUTH_ERROR` 的重试，没花钱）、aider 缺 2 次（两次
`AGENT_TIMEOUT`，被平台掐掉、没来得及报），按第一版规则三个参赛者两个没有每题成本、
散点图只剩一个点 —— 为 2/84 把整列抹掉，比给一个标明"只会更高"的下界更误导。现在的规则：

- **一次都报不出**（`reported + estimated == 0` 且 `unavailable > 0`）：仍是 `None`，垫底；
- **部分报不出**：`cost_per_task = 已知部分 ÷ 全部题数`，并置 `cost_lower_bound = true`；
  按成本排名时这样的行排在成本完整的行**后面**（`_sort_key` 的首键），
  "报不出成本 ≠ 最便宜"这条仍然成立；
- 前端把下界显示成 "≥ $x"，散点图画成空心点；报告生成器同样加 "≥"。

代码在 `app/analytics/leaderboard.py:_cost_per_task`，真值在 `tests/unit/test_leaderboard.py`
（`test_partially_unavailable_cost_is_a_flagged_lower_bound` 等四条）。

### 六、补丁正文在另一张表里，端点要跨两张表找

`/artifacts/{kind}` 的 `{kind}` 接受**两套枚举**：`ArtifactKind`（日志、轨迹、
测试报告）和只含两个值的 `AgentPatchKind`（`AGENT_RAW` / `AGENT_NORMALIZED`）。

原因是文件索引确实分在两张表：日志类在 `artifacts`，补丁在 `patch_artifacts`
（后者多出 `files_changed` / `is_empty` / `applies_cleanly` 这些补丁独有的统计，
当初没并进 `artifacts`）。`ArtifactKind` 里那个 `PATCH` 值**全库没有任何一处往里写** ——
2026-09-12 查库，`artifacts` 表 7 种 kind 里没有 PATCH，而 `patch_artifacts`
有 431 + 431 行。

第一版只查 `artifacts`，结果是 §16.2 的 Task Run Detail 页那个 **Patch Viewer
取不到 diff 正文** —— 详情接口按 AC-6 只给补丁的统计，正文只能从制品端点拿，
而那条路 404。对调用方来说这个分表没有意义：它只想问"给我这次执行的某个文件"。

**顺带挡住一个更要紧的**：`PatchKind` 一共**四个**值，除了上面两个还有
`GOLD`（官方修复补丁）和 `TEST`（官方测试补丁）。协议 C-44 / C-76 禁止它们
到达被测 AI，而这是个**不要 token 的开放读接口**。库里现在没有这两种行，
但枚举允许 —— 所以端点的 kind 参数用一个**只含两个值的独立枚举**
（`app/api/task_runs.py` 的 `AgentPatchKind`），不是在函数里加一句 `if`：

- 限制进 OpenAPI，`make gen-api` 生成的前端类型是
  `AgentPatchKind: "AGENT_RAW" | "AGENT_NORMALIZED"`，**根本没有 GOLD 这个选项**；
- 运行时会被忘掉，类型不会。

实测：`GOLD` / `TEST` → **422**（参数非法），`AGENT_NORMALIZED` → 200（9555 字节的真 diff）。

### 七、实测（本机，2026-09-12）

15 个新端点全部可用，`make gen-api` 生成的 16 条路径过 `npm run typecheck`。
拿库里的真实数据跑出来的排行榜，和 §18.6 手算的数字对得上：

| 名次 | 参赛者 | 轮数 | 平均解决率 | 轮间抖动 | 每题成本 |
|---:|:---|---:|---:|---:|:---|
| 1 | claude-code@deepseek-chat | 2（#127/#128） | 86.4% | 0.0% | 不可用（44 次报不出） |
| 2 | aider@deepseek-chat | 2（#125/#126） | 13.6% | 9.1% | $0.0175 |

aider 的 $0.0175/题和 §18.6 第七节那张表**逐位相同**，
9.1% 的轮间抖动就是 #125 的 9.09% 和 #126 的 18.18% 之差。
被挡在榜外的：哨兵 #123/#129、门禁的 #115/#116（`dirty`）、
探测跑 #117/#118/#124（分母不是 22）、诊断参赛者 #130–#132（`enabled=false`）、
人工排除的 #119–#122。

## 14.6 人工盲检端点落地（2026-09-20，E6-T3）

§14.4 原来只有队列和提交两个端点，实际界面还需要一条受保护的详情读取：

```text
GET  /api/review/queue?reviewer=&seed=&target_size=&batch_id=
GET  /api/review/{task_run_id}?batch_id=&reviewer=
POST /api/review/{task_run_id}
```

三个端点都要求 `X-Bench-Token`。队列响应不含自动类别或类别分布；详情接口在当前
reviewer 提交有效类别前，从 JSON 中彻底排除 `automatic_attribution`。提交接口接收
人工所选类别，后端再派生 `ACCEPT` / `CORRECT` / `MARK_TASK_DEFECT`，避免前端在不知道
自动答案时伪造比较结果。

批次没有新表。固定种子、归因截止 ID、目标数和自动归因快照指纹编码在现有
`human_reviews.sample_batch_id` 中；已有列足够保存双人独立标签和第三人仲裁。
详细抽样、动作校验和 N2 隔离范围见 `06-judge-attribution.md` §12.8。

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

### 15.2.3 稳定性加固回填（E9-T3，2026-09-19）

**第二个 Worker 在碰 Docker 之前就被拒绝。** `app/worker/singleton.py` 在 Worker
整个生命周期持有 PostgreSQL 会话级 advisory lock。启动顺序是“取锁 → 容量检查 →
孤儿回收 → 领取作业”，因此第二个 Worker 不会再把第一个 Worker 的在途容器当成孤儿。
锁跟数据库连接绑定，正常退出会主动释放；进程被 `kill -9` 时连接断开，PostgreSQL
也会自动释放。这里不需要锁表，也没有新增迁移。

**磁盘门禁放在领取之前。** `app/worker/disk.py` 检查工作区、本地制品目录和 Docker
root 所在分区。任一处低于 `IMAGE_DISK_MIN_FREE_RATIO`，或者读数失败，Worker 都暂停
领取；数据库里的作业仍是 `PENDING`，领取次数不会增加。在途线程继续收尾。后续轮询
发现水位恢复就自动放行。日志只在“正常 → 暂停 → 恢复”的状态变化时写，避免每次轮询刷屏。

**恢复仍然只用一套队列规则。** Worker 崩溃后，已有的租约回收把作业退回 `PENDING`
并设置指数退避；次数耗尽则进 `DEAD`。完成的作业已经是 `DONE`，不会被再次领取。
评测结论的重试继续由 `app/domain/retry.py` 按 C-18/C-53 决定，和队列的“进程崩溃重做”
分开。E9-T3 只给租约回收日志补了具体的 `requeued_job_ids` / `dead_job_ids`，没有改判定。

**真实恢复用 Golden Oracle 验过。** 测试先领取作业并让租约过期，模拟 Worker 被
`kill -9`；重启后的 Worker 先回收租约，经过退避后完成同一作业。最终只有一条
`RESOLVED` 执行记录，作业是 `DONE`，再次轮询取不到它，残留容器为 0。该测试不调模型。

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

## 16.4 Human Review 落地（2026-09-20，E6-T3）

`/review` 已实现抽检工作台：先输入 reviewer、管理员 token 和固定种子，左侧显示待处理
队列，右侧三栏分别显示题面与官方补丁摘要、Agent 补丁与轨迹入口、逐用例测试证据。
下方提供 F1～F8/N1/N2 选择、备注、双人进度和第三人仲裁状态。

自动归因对照区只渲染后端实际返回的字段；前端没有提前拿到答案再隐藏。管理员 token
存在当前标签页的 sessionStorage（`frontend/src/lib/admin-token.ts`，#126 起和实验页共用一处，
关掉标签页即失效），不写 localStorage，也不打进构建产物。准确率、kappa 和混淆
矩阵属于 E6-T4，本卡只保存计算所需的原始标签。

## 16.5 单题页的归因区块与盲检开关（2026-09-21）

§16.2 给 Task Run Detail 定的最后一项"归因结果"到 E7-T3 合入时还是一段说明文字，
因为 §14.4 里没有归因端点。现在不另开端点：`GET /api/task-runs/{id}` 直接多带一个
`failure_attribution`（`failure_attributions` 那一行的类别、层级、置信度、状态、证据、
中文理由、模型和 prompt 指纹；`raw_response` 不透出）。表上 `evaluation_task_run_id`
是唯一约束，所以是单数、可空。前端按 `evidence` 的形状渲染：规则层 `{rule, facts}`
显示规则名和判据表，LLM 层 `{citations, vote_categories}` 显示逐字引文和投票，
认不出的原样 JSON。类别的中文名和 `/review` 共用 `lib/display.ts` 一份。

**这和 §14.6 的盲检是冲突的**：那三个端点要 token、提交前不返回自动归因，但单题页
的接口是开放的，而盲检队列里就写着 task_run_id。处置是一个后端开关
`BENCH_BLIND_REVIEW`（默认关）：开着时该接口把 `failure_attribution` 置空、并标
`attribution_withheld=true`，字段内容根本不出后端，页面显示"盲检进行中"。抽检那几天
打开，标完关掉，改完重启 api。**没有做成按 `human_reviews` 自动推断**"这道题正在被
盲检"：批次不落表，刚建好、还没人提交时库里没有任何痕迹，正好在最需要盲的时候露答案；
而且开放接口每次都要重算一遍分层抽样。开关要人动一下手，但没有空窗，
和 §12.5 说的"提供开关可以关掉盲检，但报告里必须说明"是同一件事。

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
> **2026-09-08（E2-T3）**：镜像构建的制品实际落在
> `envs/{environment_id}/builds/{stamp}/` 下（`build.log`、`requirements.lock`、`build.json`），
> 比上表**多一级时间戳**。原因是调环境镜像时最常做的事就是对比"上次能装、这次装不上"
> 的两份日志和两份依赖锁，覆盖式的 key 就没得比了。
> `environment_specs.build_log_uri` 指向最新那一次。

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

> ⚠️ **上面这张表的三个输入已被实测推翻（2026-09-12，E9-T1，细账见 §18.6）。**
> 表本身不改 —— 它是当初那版模型，`tests/unit/test_makespan.py` 按它逐行钉着，
> 改了就看不出"当初假设了什么、实测差多少"。要算现在的数用
> `python -m cli.experiment timing`，别手算这张表。
>
> | 输入 | 这张表写的 | 实测（22 道 click 题 × 2 个真实 Agent × 2 轮） |
> |:---|:---|:---|
> | `S` | 1.7 min | **0.13 min**（7.8 秒），差 13 倍 |
> | `P_agent` | 8 / 10 / 12 随配置 | **有效值 = `min(agent_concurrency, worker_slots)`**，仓库默认值下是 8 不是 10 |
> | 调度损耗 | 假设 25% | **实测 20.1%**（假设偏保守，方向是对的） |
>
> 实测 `A = 1.31 min`，回代之后 300 次投影 **0.99 小时**，MET-02 余量 301 分钟。
> 结论 1 和 2 的**方向**都被证实了：瓶颈确实在 Agent 侧，确实是最敏感的变量。
> 只有量级错了 —— 而且是错在保守那一侧。

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

## 18.5 并发定档实测（2026-09-12，E9-T2）

> **本节是追加的实现记录。** §18.1–18.4 的容量模型一个字没动，这里写的是
> 在这台机器上实测之后，三个并发数定成了多少、依据是什么、以及测出来的两件
> 和原来的说法不一样的事。

工具：`python -m cli.stress {sweep,hold,oom}` + `app/domain/capacity.py`（容量模型）
+ `app/infrastructure/hostmem.py`（内存水位）。产物在 `var/stress/`（不入库）。

### 一、定档结果

| 参数 | 原来 | 定档 | 依据 |
|:---|--:|--:|:---|
| `AGENT_CONCURRENCY` | 10 | **10** | 不受本机约束，受服务商限流约束（§4.6） |
| `SANDBOX_CONCURRENCY` | 5 | **4** | 4 个容器满载时宿主 68.8%，5 个就 80.7% 过线 |
| `WORKER_SLOTS` | 8 | **8** | MET-03 要求在途并行度 ≥8，不能再低 |
| `AGENT_MEMORY_MB` | （无，捡 1536） | **1024** | 两个 Agent 镜像空载实测 100 / 317 MB |
| `SANDBOX_MIN_AVAILABLE_MB` | （无） | **2048** | 一个 1536 MB 的测试容器 + 512 MB 余量 |

### 二、原来那笔内存账漏了一半：Agent 容器

§4.6 算的是 `5 × 1.5 GB + 3.2 GB 基线 = 91%`，**只数了测试容器**。

但 `worker_slots=8` 的含义是"同时最多 8 道题在手上"，而一道题在任一时刻都占着
一个容器 —— Agent 阶段一个、测试阶段一个。所以最坏情况是 **8 个容器**，不是 5 个。

更要命的是那 8 个里有几个是 Agent 容器，而 Agent 容器的内存上限当时是
**捡来的**：`aider.py` / `claude_code.py` 的 `_spec()` 不传 `limits`，
吃的是 `ResourceLimits()` 按测试容器定的 1536 MB。没人选过这个数。

现在两种容器各有各的默认值（`app/domain/capacity.py`），Agent 那份从配置来。

### 三、真题压不动这台机器，所以分两轨测

`benchmark-dev@v1` 那 22 道题（pallets/click）测试阶段平均 4.4 秒、占一百多 MB。
拿它压内存，量到的是启动开销。§4.6 那段"内存数字受题目大小主导"的警告，
实际情况比警告说的还极端。

所以内存那一层单独压：容器直接调 `run_in_container` 起，不过队列、不进数据库、
**不往数据集里加压测题**（那种题要进 `benchmark_tasks`、过八步验证、
`content_hash` 进快照，代价比一个脚本大得多）。

### 四、A 轨：换并发数，吞吐怎么变（Oracle × 22 题 × 5 轮 = 110 条作业/组）

| `SANDBOX_CONCURRENCY` | makespan | 重跑 | 在途峰值/P50 | 测试容器峰值 | 宿主内存峰值 |
|--:|--:|--:|:--:|--:|--:|
| 3 | 151.3 s | — | 8 / 8 | 3 | 23.7% |
| 4 | 122.5 s | 127.7 s | 8 / 8 | 4 | 24.8% |
| 5 | 114.4 s | 110.2 s | 8 / 8 | 5 | 26.0% |
| 6 | 98.0 s | — | 8 / 8 | 6 | 27.3% |
| 8 | 90.5 s | — | 8 / 7 | 8 | 29.1% |

两次重跑的差是 **4.2%**（s4）和 **3.8%**（s5）—— 这就是噪声底，比它小的差别不算差别。

三件事：

1. **MET-03 在每一组里都达标**（在途峰值和 P50 都是 8）。在途数由槽位决定，
   不由沙箱并发决定 —— 排队等名额的题也算"已开始还没结束"（§4.6 的定义）。
2. **内存在每一组里都远低于 80%**。这个数只说明 click 的测试轻，**不能外推**。
3. 吞吐一路涨到 8。但 `sandbox=8` 等于**第二层信号量彻底不起作用**
   （槽位也是 8），真碰上吃内存的题就没有任何东西拦着 8 个测试容器一起跑。

### 五、B 轨：容器真吃满上限，宿主在第 5 个就过线

每个容器申请并**写满** 1400 MB、持有 15 秒（`cli.stress hold`）：

| 同时满载的容器 | 宿主峰值占用 | 最低可用 |
|--:|--:|--:|
| 4 | **68.8%** | 3728 MB |
| 5 | 80.7% | 2310 MB |
| 6 | 91.5% | 1015 MB |

**这张表定了 `SANDBOX_CONCURRENCY = 4`**：它是"测试容器全部吃满时宿主仍在
80% 线内"的最大值。没有继续往 7、8 压 —— 再往上宿主会开始换页，
而把 Postgres 顶出内存换来的只是一个已经能推出来的数字。

吞吐代价只落在**哨兵那种"全是测试阶段"的跑法**上（4 路 125 秒 vs 6 路 98 秒）。
接真实 Agent 之后一道题七分多钟里只有一分多钟在跑测试（§18.1），
同时处在测试阶段的题**期望值不到 2 个** —— 这一层是安全阀，不是吞吐旋钮。
§18.2 的 makespan 模型也说得通：`300 × 1.7 / 4 = 128 分钟` 仍小于 Agent 侧的
180 分钟，6 小时目标不受影响。

### 六、硬墙仍然超线，这件事写出来而不是掩盖

按"所有容器同时吃满声明上限"算，定档之后是
`4 × 1536 + 4 × 1024 + 基线` = **95%（基线 1.2 GB）到 108%（基线 3.2 GB）**。
在 MET-03 要求 8 路在途的前提下，这台机器上**没有任何一组并发数能让硬墙进线** ——
要进线得把两种容器的上限压到 900 MB 以下，而 claude-code 光启动就要 317 MB。

所以把这件事变成**看得见**的：Worker 启动时算一遍，超线打 `capacity_over_budget`
告警（`app/worker/loop.py` 的 `_log_capacity()`），并且有一条单元测试钉住
仓库默认值算出来的那个数（`tests/unit/test_capacity.py`）。

真正的运行期保护是**内存刹车**：拿到沙箱名额之后、起容器之前看一眼宿主
`MemAvailable`，低于 2048 MB 就等，等超过 120 秒就放行并告警
（`app/worker/concurrency.py`）。放行而不是死等，是因为内存不一定是我们占的 ——
别的进程吃满内存时死等会让整个 Worker 停摆。

### 七、`.State.OOMKilled` 漏报：和"容器死得多快"相关，不只是并发

issue #85 的决策门要一个数。故意制造真实 OOM，每组 200 个容器：

| 容器内存上限 | 并发 | 单容器中位存活 | 漏报 | 比例 |
|--:|--:|--:|--:|--:|
| 1536 MB（生产口径） | 4 | 0.91 s | **0** | 0% |
| 1536 MB（生产口径） | 8 | 2.95 s | **0** | 0% |
| 256 MB | 8 | 0.73 s | 3 | 1.5% |
| 256 MB | 4 | 0.47 s | 6 | 3.0% |

§10.10 的结论是"开关是并发，不是 CPU 负载"。这一轮把它**细化**了：并发确实是
前提（§10.10 串行 105 个 0 次），但在并发里，**容器活得越短漏得越多** ——
同样 8 路，活 2.95 秒时 0/200，活 0.73 秒时 3/200；4 路活 0.47 秒时反而是 6/200。
机制说得通：容器的 cgroup 在 containerd 把 `memory.events` 读出来之前就被拆了，
死得越快，这个窗口越窄。

**对 #85 的结论**：按生产口径（1536 MB 上限、4 路和 8 路）合计 400 个容器
**0 次漏报**，95% 置信上界约 0.75%（rule of three）。真实评测里的测试容器要跑
几十秒才可能 OOM，属于"死得慢"那一档，风险比 §10.10 的 3–5% 低一个量级。
**但不是零** —— 快死容器那两组是实打实的 9 次。建议：不把改协议设成
E10-T4 的前置条件，但 issue 保持打开，把这组数据贴进去。

### 八、顺带修掉的：重灌规程少一步

清库重灌时 `promote-assemble` 会把**全部**探测通过的候选组装成题（51 道），
而 E8-T2 的人工终审只覆盖了其中 31 道。不补一步的话，
`dataset stage` 会把 20 道**没人审过**的题一起冻进快照，
快照摘要和已发布的 `benchmark-dev@v1` 对不上。

规程补的那一步是"把没有终审结论的题退回 `REVIEW_REQUIRED`"（AGENTS.md §12）。
补完之后重灌出来的快照摘要是 `sha256:300746559b84…`，和 §7.11 记的**逐字相同**。

## 18.6 Pilot 实测与容量模型回代（2026-09-12，E9-T1）

> **本节是追加的实现记录。** §18.1–18.4 的表一个字没动（`tests/unit/test_makespan.py`
> 按它逐行钉着），§18.2 末尾加了一个指向本节的订正块。这里写的是**第一次拿真实
> 被测 AI 在真题上量出 `A`** 之后，模型里有哪三个数是错的、错多少、结论变没变。

工具：`python -m cli.experiment timing`（阶段耗时 + 回代投影）
+ `app/domain/makespan.py`（§18.2 的公式，纯函数）
+ `app/evaluation/timing.py`（从时刻列算 `A` 和 `S`）。产物在 `var/pilot/`（不入库）。

样本：`benchmark-dev@v1` 的 **22 道 click 真题 × 2 个真实 Agent × 2 轮 = 88 次**，
外加 Oracle 对照 1 轮 22 次。实验 #125–#129，全部 `dirty=false`，
harness sha `8df9206`。**卡面写的"30 题 × 3 Agent"库里不存在**，
偏离的理由记在 `10-tasks-plan.md` 的卡里。

### 一、回代结果：MET-02 达标，余量比原来以为的大得多

| 输入 | §18.2 原假设 | 实测 | 差多少 |
|:---|:---|:---|:---|
| `A`（Agent 阶段均值） | 6 min | **1.31 min**（claude-code；aider 0.84） | 小 4.6 倍 |
| `S`（其余阶段之和） | 1.7 min | **0.13 min**（7.8 秒） | 小 13 倍 |
| 有效 `P_agent` | 10（"已实施"那行） | **8** | 见第二节 |
| 调度损耗 | 假设 25% | **实测 20.1%** | 假设偏保守 |

投影 300 次（MET-02 的口径）：

```
Agent 侧    300 × 1.31 / 8 = 49.2 min     ← 瓶颈
Sandbox 侧  300 × 0.13 / 4 = 10.0 min
单题下限                     10.5 min
makespan ≈ 49.2 × 1.201    = 59.1 min = 0.99 小时
```

**MET-02（≤6 小时）达标，余量 301 分钟。** 按跑之前定好的降级表（`DEGRADATION_BANDS`）
命中第一档 `ok`，不降级。

**`A` 还能涨到 7.99 分钟**才会压线。这个数比投影值有用 —— `A` 由外部大模型决定，
不由我们决定，所以真正该记的是"外部慢到什么程度我们还扛得住"。
实测 1.31 分钟离 7.99 有 6 倍余量。

§18.2 结论 1 和 2 的**方向都被证实了**：瓶颈确实在 Agent 侧，`A` 确实是最敏感的变量。
错的只是量级，而且错在保守那一侧。

**这个结论经不经得起换仓库？** 上面那组数是 22 道 click 题量出来的，
E8-T3 换成别的仓库（sqlfluff、tortoise-orm 那种）`A` 和 `S` 都会变。
所以把实测的损耗系数 20.1% 固定住，只换 `A` 和 `S` 重算一遍：

| 情形 | `A` | `S` | 投影 | 判定 | 命中档位 |
|:---|--:|--:|--:|:--|:--|
| 本次实测（click） | 1.31 | 0.13 | 0.98 h | 达标 | `ok` |
| 测试慢到 §18.1 的典型值 75 秒 | 1.31 | 1.63 | 2.45 h | 达标 | `ok` |
| Agent 慢 3 倍 + 测试 75 秒 | 4.00 | 1.63 | 3.00 h | 达标 | `ok` |
| 回到 §18.2 原始假设 | 6.00 | 1.70 | 4.50 h | 达标 | `ok` |
| Agent 均值 8 分钟 | 8.00 | 1.70 | **6.00 h** | **压线** | `raise_slots` |

**只有 `A` 涨到 8 分钟才翻盘，测试再慢 12 倍都不要紧。** 这比"余量 301 分钟"那句
更有用：换仓库主要影响 `S`，而 `S` 根本不是瓶颈；真正要盯的还是 `A`，
和 §18.2 结论 2 说的是同一件事。

这张表是用 `app/domain/makespan.py` 算的，不是手算 —— 换了假设重算一遍是一行代码，
这也正是把那张手算表变成纯函数的理由。

### 二、`P_agent` 的有效值是 8，不是 10 —— 这次是实测，不是读代码推的

§18.2 表里"本机调 .wslconfig 后（已实施）"那行写 `P_agent=10 → 180 分钟`。
**在仓库默认值下拿不到这个 10。**

    有效 P_agent = min(agent_concurrency, worker_slots) = min(10, 8) = 8

一道题在任一时刻只占**一个**槽位（`app/worker/concurrency.py`：物化 → 调 AI →
跑测试，三段前后相接不嵌套），所以槽位数就是在途题数的上限，而"在跑的 Agent"
是在途题的一个子集。

先是从代码推出来的，然后并发曲线把它测了出来 ——
`python -m cli.experiment concurrency --run 125 … --run 129`：

| 曲线 | 峰值 | P50 | 说明 |
|:---|--:|--:|:---|
| `in_flight` | **8** | **8** | MET-03 达标，这是第一次在真实 Agent 负载上验 |
| `agent` | **8** | 7 | 顶在 8 而不是 10 —— 槽位封顶，看得见 |
| `sandbox` | 4 | **0** | 顶在信号量上；P50 是 0 |

要真拿到 10，得把 `worker_slots` 提到 10 —— 那正是降级表第三档的动作，这次没触发。

`sandbox` 的 P50 是 **0**，比 §18.5 预测的"期望值不到 2 个"还低：一道题 79 秒里
只有 4 秒在跑测试，所以多数时刻**一个测试容器都没在跑**。
`SANDBOX_CONCURRENCY=4` 这层是安全阀，不是吞吐旋钮，这一条又被证实一次。

### 三、`S` 小了一个数量级，原因是题目轻不是平台快

§18.1 给测试阶段的典型值是 75 秒，实测 click 是 **4.0–7.5 秒**：

| Agent | prepare | agent | test | judge | 单题总计 | `S` |
|:---|--:|--:|--:|--:|--:|--:|
| aider | 0.3 s | 50.5 s | 7.5 s | 0.0 s | 58.6 s | **8.0 s** |
| claude-code | 0.1 s | 78.7 s | 4.0 s | 0.0 s | 82.9 s | **4.2 s** |
| oracle | 0.4 s | 0.0 s | 4.7 s | 0.0 s | 5.0 s | **5.0 s** |

**这个数不能外推。** 和 §18.5 那条警告同一个道理：click 的测试跑起来只要几秒，
E8-T3 换成别的仓库（sqlfluff、tortoise-orm 那种）会完全不同。所以这里记的是
**分布**而不只是均值：`test` 的 P95 是 11.1 秒、最大 68.1 秒 ——
最慢那道题比中位数慢 14 倍，外推时要按 P95 算，不是按均值。

`judge` 一栏全是 0.0 秒。归因还没接（E6），现在只有判定，几毫秒。

### 四、损耗系数怎么算才不是垃圾 —— 两次都算错过

这一项踩了两次坑，都是**分子分母口径不一致**，都不报错。

**第一次（探测跑）：反算出 296%。** 4 次运行、8 个槽位，模型给的理论下限
`N·A/P = 2.1 分钟`，实测 8.3 分钟。看起来像这台机器烂得不能用，其实那批里有一次
单独跑了 8.25 分钟 —— **一道题不能拆开让两个槽位一起跑**，所以最慢那道题本身就是
makespan 的下限，而原公式里没有这一项。

修法是给模型补第三项：

    makespan ≥ max( N·A/P_agent , N·S/P_sandbox , 最慢的那一道题 ) × (1 + 损耗)

对 `N=300` 这一项永远不是瓶颈（最慢一道题 ≤ 12 分钟的硬超时 vs Agent 侧 49 分钟），
所以 §18.2 的结论不受影响。它只在拿小批次反算损耗时起作用。
配套加了 `saturates_slots`（`N ≥ 2 × P_agent`）：填不满槽位的批次根本没排过队，
反算不出调度损耗，CLI 这时退回 §18.2 的假设并说明原因。

**第二次（正式跑）：反算出 0%。** 这批 110 次运行是**混合负载** ——
Oracle 的 Agent 阶段是 0 秒、aider 50 秒、claude-code 79 秒，而投影用的 `A`
取的是最慢那个（claude-code）。拿最慢的均值乘总次数当分子，
算出来 18.0 分钟，**比实测的 14.2 分钟还大**，于是损耗是负数、被钳到 0，
看着像零调度开销。

修法是本批理论下限按**真实工时求和**算，不按"均值 × 次数"：

```
Agent 工时求和 94.8 min / 8 = 11.8 min   ← 理论下限
实测 makespan              = 14.2 min
损耗系数 = 14.2 / 11.8 − 1 = 20.1%
```

20.1% 和 §18.2 假设的 25% 很接近，**假设偏保守，方向是对的**。

教训一句话：**投影用最慢的 Agent（要上界），反算用真实工时求和（要口径一致）**，
两处不是同一个数。

### 五、第一次真实解决率：换个 Agent 外壳差 4.7 倍

库里在这之前所有解决率都是 Oracle（照抄答案）和 Noop（空补丁）刷出来的。

| Agent | 第 1 轮 | 第 2 轮 | 44 次合计 | 平均轮数 | 结论 |
|:---|--:|--:|--:|--:|:---|
| aider | 9.1%（2/22） | 18.2%（4/22） | 6 解决 / 3 空补丁 / 35 没修好 | 2.3 | — |
| claude-code | **86.4%**（19/22） | **86.4%**（19/22） | 38 解决 / 6 没修好 | 27.4 | — |
| oracle 对照 | 100%（22/22） | — | — | — | 哨兵正常 |
| 平台故障 | 0 | 0 | 0（准入上限 1，C-26a） | — | C-26 过 |

**同一批题、同一段提示词（`prompt.py` 共用）、同名的底座模型 `deepseek-chat`，
解决率差 4.7 倍。** 这是 E3-T5 那组 Golden 对比的放大版，也是"评 Agent 而不是评模型"
值得单独立项最有力的证据。

差异的机制看轮数就清楚：claude-code 平均跑 27.4 轮，aider 只有 2.3 轮。
aider 两轮就交卷，多数时候补丁改错了地方。

**这个对比仍要留余地**（和 E3-T5 那条一样）：两边打的是 DeepSeek 的两个不同端点
（aider 走 OpenAI 兼容、claude-code 走 Anthropic 兼容），同名不等于同一份权重。

### 六、单轮结果不能当结论，这次量出来是 18–23%

E3-T4 说过"同一批题跑两轮结果不同"。这次有了分母：

| Agent | 两轮结论一致 | **两轮翻盘** | 两轮都解决 | 两轮都没解决 |
|:---|--:|--:|--:|--:|
| aider | 17 / 22 | **5 / 22（22.7%）** | 1 | 17 |
| claude-code | 18 / 22 | **4 / 22（18.2%）** | 17 | 1 |

**五分之一的题在两次完全相同的运行之间会改结论。** 所以排行榜上的单轮解决率，
光凭这个抖动就能差出 ±2 道题（±9 个百分点）。E10-T4 的最终实验必须多轮取样，
而且报告里要给出轮间离散度，不能只报一个数 —— MET-01 要求"偏差 ≤5 个百分点"，
而抖动本身就有 9 个百分点，不说清这件事那个指标没法解释。

### 七、成本：aider 自报，claude-code 只能手算

| Agent | `cost_source` | 44 次总花费 | 每题 | 输入 token | 缓存命中 |
|:---|:---|--:|--:|--:|--:|
| aider | `reported` | **$0.77** | $0.0175 | 2.79 M | 33% |
| claude-code | `unavailable` | **$1.85（估）/ $10.93（上界）** | $0.042 / $0.2485 | 39.01 M | **95.8%** |

claude-code 全部 44 次都报 `unavailable` —— 这是 E3-T5 定的规矩（配了 `base_url`
就一律报不可用，因为 CLI 那张价目表算出来的数差一个数量级）。所以它的金额是
**手算的**，单价不是猜的，是**从 aider 自报的成本反解出来的**：
`$0.27/M 输入 + $1.10/M 输出` 能把 aider 两笔实报都算到 **3.0%** 以内。
上界把 95.8% 的缓存命中也按全价算，缓存感知那一列按便宜一个数量级算。

平台侧要把这个数自动填上，是 E5-T5（#75）的事，本卡不做。
**§9.4 那张"成本-能力矩阵"现在画得出来了**：claude-code 的点在
（86.4%，$0.042/题），aider 在（13.6%，$0.0175/题）——
贵 2.4 倍，解决率高 6.4 倍。

### 八、`A` 的上限由硬超时封着，这次一次都没撞上

`agent_timeout_s=720`（12 分钟）给 `A` 封了顶：一次超时就往均值里塞满 12 分钟。
**实测 88 次运行超时 0 次**，最慢的一道题是 aider 的 10.5 分钟（630 秒），
离硬超时还差 90 秒。

差得不多，所以这条要盯着：aider 的 `agent` 阶段 P95 是 5.4 分钟、最大 10.5 分钟，
分布右尾很长。E8-T3 换成更大的仓库之后，右尾很可能就顶穿 720 秒。
反算一下承受力：假定没超时的题平均 5 分钟，超时率要满足 `5 + 7t ≤ 7.99`，
也就是 **`t ≤ 43%`** —— 余量不小，但超时率必须单列进报告，
混在均值里看不出来（`cli.experiment timing` 已经单列）。

**想靠调低 `agent_timeout_s` 压 `A` 是不行的**：它进 `content_hash`
（`app/benchmark/schema.py` 的 `_hash_payload()`），改了 `benchmark-dev@v1`
当场作废要重发一版。和 §4.6 里"1280 MB 那条建议没落实"是同一个坑。

### 九、跑这一轮撞出来的两个归类 bug（已修，见 E3 的卡）

第一次跑 pilot 时 DeepSeek 余额耗尽，一次照出一对反方向的归类错误，
两个都不报错、只让数字悄悄错掉：

1. **漏判**：`402 Insufficient Balance` 被判成 `AGENT_RUNTIME_ERROR`。按 C-18
   那是**被测 AI 的错**、不计入平台故障率 —— 于是 88 次全灭的四个实验以
   `status=COMPLETED / infra_failures=0 / resolved=0/22` 收场，
   **C-26 的 5% 准入门槛查不出任何异常**，一个 0% 解决率照样能进排行榜。
2. **误判**：`\b401\b` 命中 aider 进度条里的 `401.79it/s`（`.` 是词边界），
   一道题被判成鉴权失败、白重试 3 次，还会让人往"Key 配错了"的方向查。

两个都在 `app/runner/adapters/cli_text.py`，改完拿那 88 条真实 stdout 逐条重判，
**88 认出、0 漏**。原文存进 `tests/fixtures/cli_text/` 当夹具。

余额不足严格讲是计费问题不是凭据问题，单开一个 `AGENT_BILLING_ERROR` 更准确，
但那要动协议的 `infra_outcome` 枚举（冻结件），得走 §9 的变更流程，另提提案。

**另一半 2026-09-17 在 E3-T9（#96）补上**：限流 / 供应商 5xx / 连不上 → `external_service_error`
→ `AGENT_AUTH_ERROR`；容器零输出 + 137 → `sandbox_killed` → `SANDBOX_ERROR`（§18.7 二之补那 8 次的样子）。
判据集中在 `cli_text.shared_failure()`，两个适配器都走它；细账在 E3-T9 的卡。

> ⚠️ **库里的实验 #119–#122 那四行 `resolved=0/22` 是这个 bug 的产物，不是测量结果。**
>
> 那是余额耗尽那一轮，88 次运行一次模型都没调到。它们在库里的样子是
> `status=COMPLETED / infra_failure_count=0 / resolved_count=0`，
> **看起来和一次正常跑出 0% 的实验一模一样** —— 这正是这个 bug 最危险的地方。
>
> 数据**故意留着不改**，两个理由。一是**没有重算的路**：`infra_outcome` 是适配器
> 在跑的时候写下的一次性判断，没有任何东西会去重新推导它 —— 这次的改动也不是协议改版
> （版本号还是 v1.2），所以谈不上"按新协议重算"。二是这四行本身就是上面那个判断的证据。
> 这和协议"冻结后的效力"第 3 条同一个态度：旧结果不重算，但要在报告里注明差异 ——
> 这条注就是。修好之后**新**的同类运行会按 C-26b 记 `PARTIAL`，历史这四行不追认。
>
> 所以查实验历史时：**E9-T1 的 pilot 数据是 #125–#129，不是 #119–#122。**
> 另有 #117 / #118 是探单价的探测跑（各 2 道题），#124 是充值后的 1 道题自检。

### 十、一个留在库里的坑：制品目录不跟着清库

`var/artifacts/runs/<run_id>/` 用实验号做目录名，而**清库重灌之后实验号从小往大重新发**，
制品目录不跟着清。所以 `runs/122/` 下面可能同时躺着两次不同实验的文件。

统计漏判率时被这个绊了一下：按目录 glob 数出 4 条"漏判"，
其实是上一代 #122 留下的 Oracle 老文件。**要按库里的 `benchmark_task_id` 取路径，
不要 glob 目录。** 没改代码 —— 制品带实验号是对的，错的是"拿目录当数据源"。

## 18.7 那 4.7 倍差距是不是我们把 aider 配歪了（2026-09-12，E9-T1 追查）

> §18.6 第五节报了"claude-code 86.4% vs aider 13.6%，差 4.7 倍"。
> 这个数**只有在"两边都被公平地配置过"的前提下才是结论**，否则它量的是我们的配置。
> 本节是那次追查：四条证据，一个结论，外加三件顺带查出来的事。

### 一、先排掉最可疑的：aider 找不到该改的文件

claude-code 有 Read / Grep / Bash，靠工具翻仓库；aider 只有一张 4096 token 的
repo-map（click 有 147 个文件），而我们**没往 chat 里加任何文件**，
命令就是 `aider --model … --message "<题面>"`。看起来是个巨大的先天劣势。

**实测不是。** 拿 44 次运行产出的补丁和官方补丁比"改了哪些文件"：

| Agent | 次数 | 空补丁 | **碰到正确文件** | 只碰错文件 |
|:---|--:|--:|--:|--:|
| aider | 44 | 3 | **36（82%）** | 5 |
| claude-code | 44 | 0 | **44（100%）** | 0 |

aider 在 82% 的运行里改的就是官方补丁改的那个文件。日志里能看到它怎么做到的：
第一轮它直接说"我需要看 `src/click/core.py` 和 `src/click/parser.py`，请加进 chat"，
然后 `--yes-always` 就把文件加进来了。**定位不是瓶颈。**

真正的分布是这样：

| aider 的 44 次 | 结果 |
|:---|:---|
| 碰到正确文件 36 次 | 其中只解决 6 次，**30 次改对了地方、改错了内容** |
| 只碰错文件 5 次 | 全部没解决 |
| 空补丁 3 次 | — |

**主要失败模式是"右文件、错改法"，占 44 次里的 30 次。**

### 二、第二个可疑点：一边会跑测试验证，另一边不会

数了两边的工具调用（44 次运行合计）：

| Agent | 工具调用 | 输出里出现真实测试结果的运行数 |
|:---|:---|--:|
| claude-code | Bash 724 · Read 243 · Edit 86 | 41 / 44 |
| aider | **一个都没有** | 0 / 44 |

aider 在 `--message` 模式下是一次性的：说完就退，不跑测试。
**而这一条是我们的配置决定的** —— aider 有 `--auto-test` + `--test-cmd`，我们没给。

所以这条必须实测，不能靠推。给 aider 开上 `--auto-test`，跑同一批 22 道题：

| 跑法 | 严格解决率 | 平均轮数 | 成本 |
|:---|--:|--:|--:|
| aider 基线 第 1 轮（#125） | 9.1%（2/22） | 2.3 | $0.35 |
| aider 基线 第 2 轮（#126） | 18.2%（4/22） | 2.3 | $0.42 |
| **aider + `--auto-test`（#132）** | **18.2%（4/22）** | **3.9** | $0.68 |
| claude-code（#127 / #128） | 86.4% / 86.4% | 27.4 | 报不出 |

轮数从 2.3 涨到 3.9、成本涨 1.8 倍，说明**开关真的生效了**（日志里有
`1 failed, 905 passed, 30000 deselected, 1 xfailed in 2.57s` 这种真实测试输出，
22 次里 8 次跑到了测试那一步）。

但解决率 **18.2% 正好等于基线两轮里较好的那一轮**。而 §18.6 第六节量过
aider 的轮间抖动是 **22.7%（5/22 道题会改结论）** —— 18.2% 落在基线自己的
噪声里，**不构成改善**。

**结论：4.7 倍的差距不是"没给 aider 验证循环"造成的。** 给了，差距没动。

### 二之补：这个实验做错过两次，两次都是我自己的问题

两次都**没有报错**，都是"看起来跑完了、其实治疗没施加"，所以记下来：

**第一次（#130）：`--test-cmd` 在容器里根本跑不起来。** 我给的是
`python -m pytest -x -q -p no:randomly --timeout=60`，而 agent 镜像
（`bench-agent:py311-aider`）**没装 pytest-timeout**，pytest 直接报
`unrecognized arguments: --timeout=60` 就退了。aider 老老实实把命令打在 stdout 上，
但一条测试结果都没有 —— 而解决率照样打出个 18.2%，看起来像一次有效实验。

**第二次（#131）：我同时开了两个 Worker。** 新 Worker 启动时的孤儿回收
（`startup_reaped_containers count=8`）把**老 Worker 正在用的 8 个容器杀了**。
表现是 8 次 `exit_code=137`、`stdout_bytes=0`、6 秒就死 —— 很像 OOM，其实是我自己。

这一次顺带让 **issue #85 那条告警第一次在真实场景里响了**：老 Worker 的日志里
`container_sigkilled_without_oom_flag` **正好 8 次**，和那 8 次失败一一对应。
告警干得对，而且它的语义（"有人 SIGKILL 了容器，我们不知道是谁"）比判成 OOM 更准 ——
这次的凶手确实不是 OOM，是回收器。`ContainerResult.sigkilled_without_oom_flag`
的注释早就写了这种可能（"取消实验时我们自己也会 SIGKILL 容器"），实测坐实了。

**教训进 AGENTS.md**：一台机器同时只跑一个 Worker。第二个一起来就会把第一个的容器
当孤儿回收掉，而被杀的那一方看到的是"退出码 137、没有 OOM 标记"。

### 三、顺带查出来的真问题：agent 容器不是一个能跑项目测试的环境

追第一次失败时发现的，比 aider 那件事更值得记。

`bench-agent:py311-aider` 里装着 pytest 9.1.1 和**一个正式发布版的 click 8.3.1**，
但工作区那份 click **没有以 editable 方式装进去**。后果是在 `/workspace` 里裸跑
`python -m pytest`，`import click` 命中的是 site-packages 里那个 8.3.1，
**不是被测 AI 刚改过的源码**：

```
① python -m pytest -x -q tests/test_basic.py            → 1 failed, 1 passed
② PYTHONPATH=/workspace/src python -m pytest …          → 109 passed
```

**同一份代码、同一条命令，差别只在 PYTHONPATH。** ① 测的是装好的旧 click。

这不影响判定的正确性 —— 判定跑在 **env 镜像**里（`bench-env:pallets__click__py311`，
`install_command` 是 `pip install -e .`），那边是对的。受影响的是**被测 AI 自我验证**
这件事：任何想跑测试确认自己改对了的 Agent，要么报错，要么静悄悄地验了个错的东西。

claude-code 的日志正好印证：22 次运行里 `ModuleNotFoundError` 出现 **18 次**，
而"N passed"也出现 18 次 —— 它一边撞环境一边试出了能跑的办法。
**也就是说它那 41/44 次"跑了测试"里，有多少是真的在测自己改的代码，这次没法确认。**

留着不改，理由是改动比它看起来大：要么给 agent 镜像也做 editable 安装
（那 agent 镜像就得按题目环境分别构建，E2-T3 的镜像矩阵翻一倍），
要么在下发的题面里告诉 Agent 该怎么设 PYTHONPATH（那等于我们替它想办法，
而且路径是逐仓库不同的）。**这条记进 E9-T4 / E10-T4 的已知限制。**

### 四、结论怎么写进报告

"claude-code 比 aider 高 4.7 倍"这句话，现在可以带着三条限定说出去：

1. **不是定位问题**：aider 82% 的运行改的是正确文件；
2. **不是"没给它验证循环"**：给了 `--auto-test`，解决率落在基线噪声里；
3. **两边的模型端点不同**（aider 走 OpenAI 兼容、claude-code 走 Anthropic 兼容），
   同名不等于同一份权重 —— 这条还没排除，要排除得给两边接同一个端点。

剩下最可能的解释是**轮数**：claude-code 平均 27.4 轮、aider 3.9 轮（开了 auto-test 之后）。
一次性交卷和反复改之间的差别，这才是"Agent 外壳"的价值所在 ——
和 §18.6 第五节的说法一致，但现在它是排除了两个替代解释之后的结论，不是唯一的猜想。

**还没排除的那条（第 3 点）留给 E3-T6**：自研 MiniAgent 可以让两边打同一个端点，
那时候才能把"外壳的差异"和"端点的差异"彻底分开。

### 复现这次追查

诊断用的参赛者在库里是 `aider-autotest` / `aider@deepseek-chat+autotest`，
已置 `enabled=false`（行留着，#130 / #131 / #132 三个实验还引用着它）。
它和 `aider` 唯一的区别是 `params.extra_args`：

```
["--auto-test", "--test-cmd", "PYTHONPATH=/workspace/src python -m pytest -x -q"]
```

**它不在 `cli/seed.py` 里**，清库重灌不会重建 —— 这是有意的，它是一次性的诊断
参赛者，不是排行榜上的选手。要再查一次，照上面那行 `extra_args` 建一个配置即可。

顺带一处工具上的小摩擦：`cli.experiment start --agent NAME` 用
`scalar_one_or_none()` 按 Agent 名字取配置，所以**一个 Agent 名下挂两个配置会直接抛错**。
想对比同一个适配器的两套配置，只能像这次一样另建一个 Agent 行指向同一个
`adapter_class`。E10-T4 要横向比配置时会撞上这一条。
