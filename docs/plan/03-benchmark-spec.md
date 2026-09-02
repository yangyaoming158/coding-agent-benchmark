# 7 Benchmark Task Specification

## 7.1 Task Schema（v1，冻结项）

以 JSON 存储（DB 中同时落规范化字段 + 原始 JSONB），配 JSON Schema 校验。

```jsonc
{
  "schema_version": "1.0",
  "task_id": "nonebot__nonebot2-2314",          // {owner}__{repo}-{pr_number}，SWE-bench 兼容命名
  "dataset_id": "benchmark-cn-v1",

  // ---- 仓库与快照 ----
  "repo_url": "https://github.com/nonebot/nonebot2",
  "repo_name": "nonebot/nonebot2",
  "base_commit": "3f2a1c9e...",                  // 40 位全 SHA，必须是 PR 的 parent commit
  "environment_id": "nonebot2__py311__v3",       // 指向 environment_specs，决定镜像

  // ---- 问题描述（给 Agent 的唯一输入）----
  "issue_title": "适配器在断线重连后重复注册事件处理器",
  "issue_body": "...",                            // 已脱敏：剔除 PR 链接/提交哈希/补丁片段
  "issue_language": "zh",                         // zh | en | mixed
  "hints_text": null,                             // 默认 null（不给提示，对齐 SWE-bench Verified）

  // ---- 执行定义 ----
  "install_command": "python -m pip install -e .[test]",   // 仅镜像构建期执行
  "pre_test_command": null,                                 // 可选：构建/迁移
  "test_command": "python -m pytest -rA -p no:randomly --junitxml=/tmp/report.xml",
  "test_framework": "pytest",                               // pytest|unittest|jest|gotest|junit
  "test_report_path": "/tmp/report.xml",

  // ---- 验证测试 ----
  "test_patch": "diff --git a/tests/... ",       // 仅含测试文件的 diff，由 harness 施加
  "test_patch_paths": [                          // test_patch 实际改动的全部路径，由 Validator 推导
    "tests/test_adapter.py",                     // 仓库相对 POSIX 路径，排序去重
    "tests/fixtures/reconnect.json"              // rename/copy 时新旧路径都记
  ],                                             // 纳入 content_hash；导入与验证时重算，不一致则拒收
                                                 // 【禁止下发给被测 AI】详见协议 C-74 ~ C-76
  "fail_to_pass": ["tests/test_adapter.py::test_reconnect_no_duplicate_handler"],
  "pass_to_pass": ["tests/test_adapter.py::test_basic_register", "..."],   // 上限见 §7.7

  // ---- 参考解 ----
  "gold_patch": "diff --git a/nonebot/... ",     // 仅含非测试文件的 diff，永不下发给 Agent

  // ---- 预算 ----
  "agent_timeout_s": 720,
  "test_timeout_s": 480,
  "sandbox_cpu": 1.0,
  "sandbox_memory_mb": 1536,
  "sandbox_pids_limit": 512,

  // ---- 溯源与元数据 ----
  "source_issue_url": "https://github.com/nonebot/nonebot2/issues/2301",
  "source_pr_url": "https://github.com/nonebot/nonebot2/pull/2314",
  "created_at_upstream": "2024-11-03T08:21:00Z",
  "language": "python",
  "framework": "nonebot",
  "difficulty": "medium",                        // easy|medium|hard，见 §7.8
  "tags": ["cn", "domestic-oss", "async", "bugfix"],

  // ---- 完整性 ----
  "content_hash": "sha256:...",                  // 对判定相关字段做规范化哈希
  "validation": {
    "state": "VALID",
    "validated_at": "...",
    "validator_version": "1.0",
    "image_digest": "sha256:...",
    "evidence_artifact_uri": "local://tasks/.../validation.json"
  }
}
```

**为什么没有 `docker_image` 字段**：镜像不是任务的属性而是**环境规格**的属性。多个任务共享同一 `environment_id` → 同一镜像，这是 §10.4 镜像复用与 6 小时目标的前提。任务只引用 `environment_id`，实际 digest 记录在 `validation.image_digest` 与运行 manifest 中。

## 7.2 八个必答问题

### (1) Repo Snapshot 如何保证可复现
不使用"运行时 clone GitHub"。流程：
1. 平台维护 **bare mirror**：`git clone --mirror <repo_url> /var/lib/bench/mirrors/{repo}.git`（一仓库一次，定期 fetch）；
2. 该 mirror 被 **烘焙进 repo 环境镜像**（或通过只读卷挂载给构建器）；
3. 生成工作目录（把指定 commit 的文件树导出来，不带 git 历史）：`git archive --format=tar <base_commit> | tar -x -C /workspace`；
4. `/workspace` 内 `git init && git add -A && git -c user.email=… commit -m "base"`。

结果：工作区内容 = base_commit 树，**且 git 历史里只有一个提交**。同时满足：
- 可复现（archive 输出对同一 commit 字节确定）；
- `git diff` 可用于捕获 Agent 改动；
- **防泄题**（Agent 看不到 base 之后的任何提交、分支、tag、PR）。

### (2) Base Commit 如何冻结
`base_commit` = 修复 PR 的 **第一父提交**（`pr.base.sha` 不可靠——base 分支会前进；必须取 merge commit 的 `parents[0]` 或 PR head 的 `merge_base`）。存 40 位全 SHA，禁止短 SHA/分支名/tag。

### (3) Dependency 如何冻结
四层递进（成本递增，按仓库选择）：
1. **镜像层冻结（默认）**：在 `environment_spec` 构建时安装并 `pip freeze > /opt/env/requirements.lock`，镜像按 digest 引用；
2. **索引冻结**：构建时使用固定 PyPI 镜像 + `--no-deps` 装 lock 文件；
3. **运行期断网**：测试阶段 `--network none`，杜绝任何隐式下载；
4. **上游 lock 复用**：仓库自带 `poetry.lock` / `uv.lock` 时优先使用其锁定版本。

**已知残余风险**：跨越较长时间跨度的任务可能需要不同依赖版本。对策 = `environment_spec` 按 `(repo, 版本区间)` 分桶（SWE-bench 也是这么做的），任务在验证期若装不上就落 `INVALID(ENV_UNBUILDABLE)`。

### (4) Test Command 如何定义
每个 `environment_spec` 定义一次，任务继承并可覆盖。硬性要求：
- 必须输出**机器可解析**的逐用例报告（首选 `--junitxml`，其次 `-rA` 文本 + 解析器）；
- 必须禁用随机顺序插件与缓存（`-p no:randomly -p no:cacheprovider`）；
- 必须能接受**用例 ID 列表参数**（用于只跑 F2P+P2P 子集，这是 6 小时目标的重要优化）；
- 不得包含 `-x`（fail fast）——我们需要全部用例状态。

### (5) FAIL_TO_PASS 如何定义
候选来源：`test_patch` 中**新增或被修改**的测试用例 ID。
但候选 ≠ F2P，必须经**实测证伪**：
```
base + test_patch                → 该用例必须 FAILED/ERROR   （否则不是"揭示 bug"的测试）
base + test_patch + gold_patch   → 该用例必须 PASSED         （否则 gold 没修好，任务坏）
```
只有两条同时满足的用例才进入 `fail_to_pass`。若最终 F2P 为空 → 任务 `INVALID`。

### (6) PASS_TO_PASS 是否必须
**必须。** 没有 P2P，Agent 可以用"删掉相关功能/放宽断言"的方式通过 F2P。P2P 是回归护栏，是"补丁质量"的唯一自动化证据。
定义：在 `base + test_patch` 上就已经 PASSED、且在 `base + test_patch + gold_patch` 上仍 PASSED 的用例。

### (7) 如何避免任务本身就是坏任务
坏任务清单与自动拦截规则：

| 坏任务类型 | 拦截规则 |
|:---|:---|
| Issue 里直接给了修复代码/PR 链接 | 正则 + LLM 预筛，命中则脱敏或丢弃 |
| Issue 描述过短/无信息（"不工作"） | `len(issue_body) < 200 字符` → REVIEW_REQUIRED |
| F2P 在 base 上就通过 | 验证流水线第 5 步拦截 |
| gold patch 打上后 F2P 仍失败 | 第 7 步拦截 |
| gold patch 引入 P2P 回归 | 第 8 步拦截 |
| flaky 用例 | P2P 连跑 2 次不一致 → 该用例剔除；F2P flaky → 任务 INVALID |
| 测试与代码耦合到"只能猜出实现细节"（如断言具体报错文案） | LLM 预筛 + 人工，标记 `over_specified` |
| 任务只需改测试即可通过 | 结构规则：`gold_patch` 非测试改动为空 → 丢弃 |
| 环境不可构建 | 第 3 步拦截 |
| 单测耗时过长（>8 min） | 第 4 步拦截或降级为 `long_running` 标签 |

### (8) 如何判定 Benchmark Task VALID
见下节流水线，全部 8 步通过 ⇒ `VALID`，并写入 `validation` 证据制品。

## 7.3 Task Validation Pipeline

```
S1 clone/mirror fetch          → 失败: INVALID(REPO_UNAVAILABLE)
S2 checkout base_commit(archive) → 失败: INVALID(COMMIT_MISSING)
S3 build/reuse env image + install → 失败: INVALID(ENV_UNBUILDABLE)
S4 apply test_patch, run full suite (baseline)
     记录全量用例基线状态；超时 → INVALID(TEST_TOO_SLOW)
S5 verify F2P candidates FAIL   → 不满足: INVALID(F2P_NOT_FAILING)
S6 apply gold_patch
S7 verify F2P PASS              → 不满足: INVALID(GOLD_NOT_FIXING)
S8 verify P2P still PASS + flaky 复跑 → 不满足: INVALID(GOLD_REGRESSION) / 剔除 flaky 用例
⇒ VALID（写入 image_digest、用例清单、耗时基线）
```
每步的 stdout/stderr/report 全部存为制品，任务详情页可查——**任务本身也要可审计**。

## 7.4 任务候选工作流

```
DISCOVERED → CANDIDATE → VALIDATING → ┬→ VALID ──→ PUBLISHED（进入 dataset）
                                       ├→ REVIEW_REQUIRED →（人工）→ VALID / INVALID
                                       └→ INVALID（附 reason_code）
                                   VALID ──(上游变更/复验失败)──→ QUARANTINED
```
- `REVIEW_REQUIRED` 触发条件：Issue 过短、疑似泄题、F2P 数量异常（0 或 >20）、测试超时接近阈值、LLM 预筛低分。
- `QUARANTINED`：数据集发布后定期复验（每周一次）不通过的任务，自动隔离并从当前 dataset 版本快照中排除（历史版本不受影响）。

## 7.5 数据集版本化
`benchmark_sets` 与 `benchmark_tasks` 之间用**快照表**关联：发布一个数据集版本时，把当时 `VALID` 的任务 id + `content_hash` 冻结进 `benchmark_set_items`。
⇒ 三周后重跑 `benchmark-cn-v1` 得到的是同一批题的同一版本。**这是 NFR-02 的数据侧基础。**

## 7.6 防作弊清单（NFR-04 的可执行定义）

| 攻击面 | 防御 |
|:---|:---|
| 改测试文件让 F2P 通过 | ① Agent 补丁按路径剔除测试文件；② 测试阶段由 harness 强制 `git checkout -- <test_paths>` 后再打 `test_patch` |
| 从 git 历史读官方修复 | 工作区只有 1 个提交（§7.2-1） |
| 联网搜到该 PR | Agent 阶段出网走**域名白名单代理**（仅 LLM API），禁止 github.com |
| 读到 gold_patch | gold_patch 永不进入工作区、永不进入下发给 Agent 的 JSON |
| 修改测试运行配置（conftest/pytest.ini/setup.cfg） | 这些路径纳入"受保护路径"，同样被剔除并还原 |
| 猴子补丁 / sitecustomize.py 注入 | 受保护路径清单包含 `sitecustomize.py`、`conftest.py`；且测试容器从纯净镜像重建，Agent 装的包不带过去 |
| 死循环卡测试 | `test_timeout_s` + `pids_limit` |

> **受保护路径** 分两份，用途不同，**不能混用**（协议 C-75）：
>
> - `enforcement_protected_paths`（平台内部执行用，完整）：`tests/**`、`test/**`、`**/tests/**`、`**/test/**`、`**/test_*.py`、`**/*_test.py`、`**/conftest.py`、`pytest.ini`、`.pytest.ini`、`tox.ini`、`setup.cfg`、`pyproject.toml`（`[tool.pytest*]` 段落有风险 → 简化为整文件保护）、`**/sitecustomize.py`、`**/usercustomize.py`、`.github/**`，**外加该题的 `test_patch_paths`**。
> - `agent_visible_protected_paths`（下发给 AI 用）：**只含上面的通用规则，不含 `test_patch_paths`**。
>
> 为什么要拆：把该题 `test_patch` 实际触碰的路径下发给 AI，等于直接告诉它官方测试补丁改了哪几个文件，是一种定位提示。我们没下发 F2P 用例 ID，不能从这个字段漏出去。
>
> 三条匹配规则（协议 C-61 ~ C-64）：环境规格只能**追加**不能替换；重命名或复制时**新旧路径任一受保护就整个文件丢弃**；第二道防线除了还原已有文件，**还要删除 AI 新增的受保护文件**（但只删确认命中规则的具体文件，禁止对目录做无限制清理）。

## 7.7 P2P 规模控制
全量 P2P 可能有数千条，跑一遍很贵。策略：
- 若全量套件 ≤ 3 分钟 → P2P = 全量通过用例；
- 否则 P2P = **与 gold_patch 改动文件同模块的用例** ∪ **随机抽样 200 条**（固定种子），并在任务中记录 `p2p_sampling: {strategy, seed, total_pool}`；
- 运行期用"只跑 F2P ∪ P2P 子集"的命令，显著缩短测试时长（对 MET-02 关键）。

## 7.8 难度分级
不靠拍脑袋：`difficulty` 由三个客观量派生 —— `gold_patch` 改动行数 + 改动文件数 + F2P 用例数。
`easy`: ≤1 文件 且 ≤15 行；`medium`: ≤3 文件 且 ≤60 行；`hard`: 其余。
（可选 P2：用 Mock/基线 Agent 的实测解决率做校准。）

---

# 8 Benchmark Construction Strategy

## 8.1 三级 Benchmark（协议完全一致，只有来源和规模不同）

| Level | 名称 | 规模 | 来源 | 用途 | 时间点 |
|:---|:---|:---:|:---|:---|:---|
| L0 | `golden-tasks` | 3–5 | **人工构建**：取小型仓库，人为注入 bug 并写 F2P 测试 | 验证评测内核；单元/E2E 测试基线；答辩演示 | Week 1 Day 2 |
| L1 | `benchmark-dev` | 20–30 | 精选仓库自动挖掘 + 人工终审 | Agent 适配器联调、并行调度压测 | Week 2 末 |
| L2 | `benchmark-cn-v1` | 60–100 | 全量挖掘 + 审核 | 最终实验主数据集 | Week 3 末 |
| L2' | `swebench-verified-subset` | 50–100 | SWE-bench Verified 官方实例导入 | **校准集**，服务 MET-01 | Week 3 |

**硬性要求**：四者共用同一 Task Schema、同一 Validator、同一 Judge。Level 只是 `dataset_id` 不同。

## 8.2 L0 Golden Tasks 的构造方法（Week 1 就要有）
不依赖 GitHub 挖掘，2 小时内可造：
1. 选 2–3 个**极轻量**的真实 Python 库（安装 <20s，测试 <10s）；
2. 在其某个函数中**人为引入一个真实感 bug**（边界条件、并发重复注册、编码处理…）→ 这就是 `base_commit` 的状态（用本地 fork 提交）；
3. 写一条揭示该 bug 的测试 → `test_patch` + `fail_to_pass`；
4. 修复 = `gold_patch`；
5. 写一段**像人写的中文 Issue**（只描述现象，不给方案）。

价值：① 内核开发不被"挖掘进度"阻塞；② 单元测试有稳定 fixture；③ 答辩时可当场跑完整闭环（30 秒内出结果）。

## 8.3 仓库选型准则（决定 L1/L2 成败）

| 准则 | 阈值 | 理由 |
|:---|:---|:---|
| 语言 | Python 优先（≥80%） | 环境构建与测试解析最成熟；多语言留 P2 |
| 测试框架 | pytest（可 junitxml） | 解析器只需写好一个 |
| 依赖体量 | `pip install` ≤ 120s，无 CUDA/无系统级重依赖 | 直接决定镜像构建与验证吞吐 |
| 全量测试耗时 | ≤ 180s | 决定验证与评测时长 |
| 中文 Issue 比例 | 越高越好 | 服务"中文优先" |
| 国产/中文社区归属 | 至少 4–6 个仓库 | 服务"含国产开源项目" |
| 近 2 年 merged PR 关联 Issue 且含测试改动 | ≥ 80 个 | 保证候选池够深 |
| 许可证 | 宽松开源（MIT/Apache/BSD） | 分发任务集需合规 |

**候选池（Week 1 Day 3 用脚本实测打分后定档，不预先承诺）**：中文社区 Python 项目（如 NoneBot 生态、中文 NLP/文本处理工具库、国产 AI 基础库的轻量子项目、国产 Web/运维框架的 Python 组件），叠加 2–3 个国际主流轻量库作对照组。
**筛选脚本产出的一张表**（repo × 候选 PR 数 × 安装耗时 × 测试耗时 × 中文 Issue 比例）就是选型依据，写进报告。

## 8.4 挖掘流水线（自动化边界清晰）

```
[自动] GitHub Search: repo:X is:pr is:merged  linked:issue
[自动] 取 PR files → 分为 test_files / code_files
[自动] 过滤：无 test_files → 丢弃；无 code_files → 丢弃
[自动] 取 parents[0] → base_commit
[自动] 取关联 Issue title/body → 脱敏（去 PR 链接、去 commit hash、去代码块中的最终修复）
[自动] 从 test_patch 抽取候选 F2P 用例 ID
[LLM ] 质量预筛：Issue 是否自足？是否泄题？是否过度指定？ → score 0-5
[自动] score<2 丢弃；2≤score<4 → REVIEW_REQUIRED；≥4 → 直接进 VALIDATING
[自动] 8 步验证流水线（§7.3）
[人工] REVIEW_REQUIRED 队列（目标 ≤3 min/题）
```

**API 限流**：GitHub 认证用户 5,000 req/h，GraphQL 5,000 point/h。用 GraphQL 批量拉取 + 本地缓存（`gh_cache` 表按 URL+etag），避免重复消耗。挖掘作业设计为**可中断可续跑**。

## 8.5 中文优先的具体落实
- 数据集统计页展示 `issue_language` 分布，`zh` 占比作为公开指标；
- Issue 为英文但仓库为国产项目时，**不做机器翻译**（翻译会引入信息失真，损害基准可信度），如实标注；
- 平台 UI、报告、任务集元数据全中文；
- 若 `zh` 占比不足，Plan B：在 L0/L1 中人工构造更多中文 Issue 任务（这些是我们自己写的，语言可控）。

## 8.6 SWE-bench Verified 子集导入（服务 MET-01）
- 用官方数据集（HuggingFace `princeton-nlp/SWE-bench_Verified`）的字段直接映射到我们的 Schema：`instance_id→task_id`、`repo`、`base_commit`、`problem_statement→issue_body`、`patch→gold_patch`、`test_patch`、`FAIL_TO_PASS→fail_to_pass`、`PASS_TO_PASS→pass_to_pass`、`environment_setup_commit→environment_id 分桶依据`；
- 环境优先复用**官方评测镜像**（`swebench/sweb.eval.x86_64.<instance_id>`），拉不动时退回自建 env spec；
- 抽样：固定种子分层随机（按 repo 分层）取 50–100 题；
- 这批任务**只用于校准**，不混入 `benchmark-cn-v1` 的解决率统计。
