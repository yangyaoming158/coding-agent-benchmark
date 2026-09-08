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
  "p2p_sampling": {                              // P2P 是怎么选出来的，见 §7.7
    "strategy": "module_and_random",             // full | module_and_random
    "seed": 20260903,                            // full 策略下为 null
    "total_pool": 1840                           // 候选池里一共有多少条通过的用例
  },                                             // 纳入 content_hash（抽样参数决定 P2P 名单，
                                                 // 而 P2P 名单直接决定判定结论）

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
- 否则 P2P = **与 gold_patch 改动文件同模块的用例** ∪ **随机抽样 200 条**（固定种子），并在任务中记录 `p2p_sampling: {strategy, seed, total_pool}`（字段定义见 §7.1，2026-09-04 补入，issue #60）；
- 运行期用"只跑 F2P ∪ P2P 子集"的命令，显著缩短测试时长（对 MET-02 关键）。

## 7.8 难度分级
不靠拍脑袋：`difficulty` 由三个客观量派生 —— `gold_patch` 改动行数 + 改动文件数 + F2P 用例数。
`easy`: ≤1 文件 且 ≤15 行；`medium`: ≤3 文件 且 ≤60 行；`hard`: 其余。
（可选 P2：用 Mock/基线 Agent 的实测解决率做校准。）

## 7.9 实现落地与待决问题（2026-09-03，E1-T1）

> **本节是追加的实现记录，没有改动 §7.1 ~ §7.8 的任何一条。**
> 下面三处不一致需要走 §9 的变更流程定夺，在那之前代码按"当前写法"运行。

`TaskDefinition`（`backend/app/benchmark/schema.py`）已按 §7.1 逐字段落地（含 2026-09-04 补入的 `p2p_sampling`），
JSON Schema 导出在 `schemas/task.schema.json`（生成物，`make schema` 重出，
CI 有漂移检查）。

### content_hash 的算法

**除 `content_hash` 和 `validation` 外，所有字段都算进哈希。**

不手工维护一份"判定相关字段"清单，理由是清单会烂：以后有人加字段忘了往清单里补，
哈希就悄悄不覆盖那个字段，而且没有任何报错。反过来"除了这两个全算"是默认安全的——
新字段自动被覆盖，漏掉的成本只是"哈希变多了"（顶多误报一次题目变更），
不是"哈希漏了"（漏报等于可复现性是假的）。
`tests/unit/test_content_hash.py` 里有一条用例强制每个模型字段要么在变异表、
要么在豁免表里登记，加字段时不做这个决定就会红。

排除 `validation` 的理由：它记的是验证过程的结果，不是题目内容。发布后每周复验会更新
`validated_at` 和 `image_digest`，算进去的话每复验一次全部数据集快照就集体失配。

规范化方式：递归按键名排序的紧凑 JSON（`ensure_ascii=False`，中文保持可读）。
列表**不在哈希里排序**——`fail_to_pass` 这些集合语义的字段在模型解析时就排序去重了，
规范化留在数据里看得见。将来加一个顺序有意义的列表字段时，
哈希不会悄悄把顺序抹平。

### 三处不一致（2026-09-04 已全部处理，issue #60）

| # | 现象 | 结论 |
|:--|:---|:---|
| 1 | §7.1 的 `content_hash` 是 `"sha256:..."`（71 字符），迁移 0001 的 `benchmark_tasks.content_hash` 是 `CHAR(64)` | **两份文档都不改**：JSON 带前缀、数据库存裸十六进制，转换收在 `hashing.to_bare_hex()` 一处 |
| 2 | §7.1 有 `sandbox_pids_limit`，`benchmark_tasks` 没有对应列（只有 `sandbox_cpu`、`sandbox_memory_mb`） | **迁移 0002 补列**。三个都是起容器时要读的限额，存法不一致的话 E2-T2 得为其中一个写特例，取不到还要兜默认值 —— 而 pids 上限挡的是 fork 炸弹，兜错了防线就没了 |
| 3 | §7.7 说"在任务中记录 `p2p_sampling: {strategy, seed, total_pool}`"，但 §7.1 的字段表里没有这个字段 | **§7.1 补入该字段**（走协议 §9 流程，见下） |

#### 关于第 3 条：为什么是补字段而不是别的做法

抽样参数决定 `pass_to_pass` 名单，而 P2P 名单直接决定判定结论 —— 同一道题换个随机种子，
选中的回归护栏就不同，同一个补丁可能一次判过一次判挂。所以它必须在题目定义里、
必须被 `content_hash` 覆盖，否则"同一个数据集版本"给不出同样的判定，NFR-02 就是空的。

§7.7 本来就要求记录它，所以这更像 §7.1 落笔时漏了一个字段，不是设计变更。

**`PROTOCOL_VERSION` 保持 `v1.2`，不升版本**：`p2p_sampling` 不在 `docs/evaluation-protocol.md`
正文里（协议管的是判定语义 C-01 ~ C-79），本次改的是任务规范这个冻结件，
按 AGENTS.md 第 4 节"先说明理由再改"的要求走，变更记录在 issue #60。

`schema_version` 也保持 `"1.0"`：新字段可选（默认 `null`），不带它的老 JSON 照样解析，
属于向后兼容的追加；§7.1 标题写的是"Task Schema（v1，冻结项）"，这仍然是 v1。
真到了删字段或改字段语义的时候再升，那时候升才有意义。

**副作用（已实测）**：加字段会让所有已有题目的 `content_hash` 变掉，因为规范 JSON 多了一个键。
现在改的成本是零 —— 还没发布任何数据集，仓库里只有一个测试样例。
等到数据集发布之后再改，就要重算全部题目的哈希，`benchmark_set_items` 里冻结的快照会集体失配。
**这一条本身就是"现在改而不是以后改"的理由。**

抽样记录还带两条一致性校验（`schema.py::_check_p2p_sampling`）：
`module_and_random` 必须给 `seed`（没有种子就复现不出当初选了哪 200 条，而这正是记录它的全部意义）；
`full` 表示候选池全都当了 P2P，数量对不上说明记录是拼的。

### 校验规则落地情况

硬性拒收（导入即报错，消息里带字段名和实际值）：`base_commit` 非 40 位全 SHA、
`task_id` 不合 `{owner}__{repo}-{pr_number}`、`fail_to_pass` 为空、F2P 与 P2P 有交集、
`test_patch` 碰了非测试文件、`test_patch_paths` 与重算结果不一致（C-74 第 6 条）、
`gold_patch` 为空或命中受保护路径（C-64）、`issue_body` 里有 PR 链接或 diff 块、
`content_hash` 对不上、出现未知字段。

需人工复核（`review_flags()`，对应 §7.4 的 `REVIEW_REQUIRED`，不拒收）：
`issue_body` 短于 200 字、F2P 超过 20 条、P2P 为空。

泄题检测只查两种没有歧义的形式（PR 链接、`diff --git` 块），**不查裸 commit hash**——
用户贴报错日志时带哈希很常见，按那个拒收会误伤一大批好题。LLM 预筛是 E1-T5 的事。

### 受保护路径

C-42 的清单落在 `backend/app/domain/protected_paths.py`（放 `domain` 是因为
benchmark、runner、judge 三边都要用）。C-75 的两份清单拆成
`enforcement_patterns()` 和 `agent_visible_patterns()` 两个函数，
后者不含 `test_patch_paths`（C-76）。**执行**（C-41 剔除、C-16 还原、C-63 删除）
是 E2/E4 的事，本任务只做规则与匹配。

### 从补丁解析路径

`test_patch_paths` 由 `patch_paths.derive_patch_paths()` 从 diff 推导（C-74 第 1 条）。
实现上按 hunk 头 `@@ -a,b +c,d @@` 声明的行数**精确数过内容行**，不按行首前缀 grep：
删掉一行 `-- foo` 之后 diff 里那行长得和文件头一模一样（`--- foo`），
grep 的写法会凭空多出一个"被改的文件"，而这份清单是要并进受保护路径的。
git 对非 ASCII 路径的八进制转义（`"a/\346\265\213.py"`）也要还原，
不然中文文件名和存的路径对不上，第 6 条的防篡改校验会对好题误报。

## 7.10 验证流水线落地实录（2026-09-07，E1-T3）

> **本节是追加的实现记录，没有改动 §7.1 ~ §7.9 的任何一条。**

八步流水线在 `backend/app/evaluation/validation.py`，命令是
`python -m cli.validate {run,show}`（`make validate-tasks`）。
四道 Golden 题全部判 `VALID`，人为构造的**七种**坏任务各自落到对应的 reason code
（验收标准写的是 6 种，§7.3 列了 7 个 code，7 个都构造出来了）。

### 八步只起三次容器

S5 **不逐条跑测试**，而是查 S4 的全量报告。S4 本来就要求"记录全量用例基线状态"，
junit 报告里每条用例的状态都在，S5 要的"每一条 F2P 都失败"从这张表里直接读得出来。
§7.2(6) 说的 P2P 候选池同样来自这份报告。

对比：`cli/golden.py` 的六步验证是逐条起 pytest 的（`_step_f2p_all_fail`），
因为它只跑指定用例，一次跑完只能得出"至少挂了一条"。F2P 有 20 条就要跑 20 遍。

于是真正起容器的只有三次：S4 基线、S6/S7 打上 gold、S8 复跑。

### 跑测试复用 `execute_tests`，没有第二套实现

    S4      = execute_tests(plan, agent_patch="")           ← 空补丁，等价于 Noop 哨兵
    S6/S7/S8 = execute_tests(plan, agent_patch=gold_patch)  ← 官方补丁，等价于 Oracle 哨兵

不是图省事。验证要是走另一条跑测试的路，"这道题验过了"就**不保证**正式评测时判得对 ——
中间隔着容器规格、断网策略、补丁应用顺序、报告解析、用例 ID 归一化五道关，
任何一道两边不一致，结论都可能不同。协议 C-50 把 Oracle 100% / Noop 0% 定成题库
发布门槛，这条流水线给出的正是每道题的那份证据。

`execute_tests` 为此加了一个参数：`test_ids=()` 表示跑全量套件
（默认仍是 C-17 的 F2P ∪ P2P 子集，正式评测不受影响）。

### 模块为什么放在 `app.evaluation` 而不是 `app.benchmark`

import-linter 的分层里 `app.evaluation | app.benchmark` 是并排的，并排就是互不可见，
放进 `app.benchmark` 就 import 不到执行器。两害相权：

| 方案 | 代价 |
|:---|:---|
| 放 `app.evaluation`（**采用**）| 文件位置和 `11-acceptance-testing-risk.md` §30 的目录草图对不上 |
| 放 `app.benchmark` | 要重写打补丁 + 造容器规格 + 解析报告约 60 行，两套跑测试的代码会漂，上面那条保证也没了 |

`gold_patch` 是**函数参数**、不进 `ExecutionPlan`，所以"官方答案不进执行计划"
（见 `app/domain/execution_plan.py` 的模块文档）那条边界不受影响。

### 七个 reason code 分别在哪一步落地

| 步 | reason code | 触发条件 |
|:---|:---|:---|
| S1 | `REPO_UNAVAILABLE` | 镜像不在本地，且 `repo_url` 拉不到（`golden://` 没有上游）|
| S2 | `COMMIT_MISSING` | `base_commit` 不在镜像里（fetch 一次仍然没有），或物化失败 |
| S3 | `ENV_UNBUILDABLE` | 环境镜像不在本地 |
| S4 / S7 / S8 | `TEST_TOO_SLOW` | 容器跑到 `test_timeout_s` 被杀 |
| S5 | `F2P_NOT_FAILING` | 有 F2P 在基线上不是 `FAILED`/`ERROR`（含 `MISSING`、`SKIPPED`）|
| S7 | `GOLD_NOT_FIXING` | 打完 gold 仍有 F2P 不通过；或 F2P 复跑结果不一致 |
| S8 | `GOLD_REGRESSION` | 基线上通过的 P2P 被 gold 打挂 |

### 三种失败，结论不一样

| 情况 | 结论 |
|:---|:---|
| 命中上表七个 code 之一 | `INVALID` + code（题目原本是 `VALID` 的话记 `QUARANTINED`）|
| 步骤失败但七个 code 都不对应 | `REVIEW_REQUIRED`，原文记进证据 |
| 平台自己出故障（OOM、容器起不来、连不上 docker）| **不下结论**，`state` 为 None，调用方不动题目状态 |

第二行的典型情况：`test_patch` 在 base 上打不上、junit 报告没生成。
硬套一个 code 是在编 —— §7.3 没有对应项，而报错原文比一个错误的分类有用得多。
第三行是底线：把平台故障写成题目无效，真正的原因就再没人去查了。

### 一次超时不等于隔离（C-20a 的边界）

首次验证时 S4 超时就是 `INVALID(TEST_TOO_SLOW)`，这是 §7.3 明写的。
C-20a 禁止的是另一件事：**已发布题目**在正式评测时超时一次就被隔离 ——
那种情况要先按 C-20 跑对照组（E4-T5）。两者不是一回事。

`QUARANTINED` 在本流水线里只有一个来源：`previous_state` 已经是 `VALID` 的题目复验没过。

### 一处细化：声明的 P2P 在基线上就要全过

§7.3 的 S5 只写了检查 F2P。实现里在 S4 顺带查了"声明的 P2P 在 `base + test_patch`
上必须全过"（§7.2(6) 本来就这么定义 P2P），不满足判 `REVIEW_REQUIRED`。

不加这一条的话，一条在 base 上就挂的 P2P 会一路漏到 S8，被记成 `GOLD_REGRESSION` ——
而 gold 根本没碰它，那是**错误的诊断**。

### 不稳定用例只报不改

§7.2(7) 写的是"P2P 连跑 2 次不一致 → 该用例剔除"。实现里**只报不改**：
把该剔的用例列进证据，状态判 `REVIEW_REQUIRED`，不动题目 JSON。

理由是剔除会改 `pass_to_pass`，进而改 `content_hash`，而 `content_hash` 是数据集
快照的身份证（§7.5）—— 验证过程顺手改题目定义，"同一个数据集版本"就不再成立。
改不改由人或者数据集发布环节（E1-T6）决定。不稳定的 F2P 直接判
`GOLD_NOT_FIXING`：它不能稳定通过，就不算修好了。

复跑只跑 gold 那一侧（`--repeat`，默认 2）。F2P 的"必须通过"和 P2P 的"必须仍然通过"
两条断言都在这一侧；基线侧的抖动会表现成"F2P 有时候通过"，由 S5 当场拦下。
再多跑一遍基线会让最贵的一步再贵一倍，收益小得多。

### S3 现在只有 reuse 那一半

§7.3 的 S3 是"build/reuse env image + install"。镜像分层构建是 E2-T3，还没做，
所以这里只查镜像在不在本地、取它的 digest（协议 C-36），不在就判 `ENV_UNBUILDABLE`，
错误信息里明说要先 `make images`。**不会自动 build，也不会自动 pull**（ADR-008）。

### 证据制品

按 §17.2 的命名规范落在 `tasks/{task_id}/validation/{stamp}/` 下：

    evidence.json          结论 + 八步逐步记录 + 镜像身份 + 全量用例基线 + 耗时基线 + 复跑对照
    s4-baseline.junit.xml  三次容器运行各自的 junit 报告和 stdout / stderr
    s7-gold.junit.xml
    s8-rerun-2.junit.xml

`benchmark_tasks.validation_evidence_uri` 指向 `evidence.json`，每份制品在
`artifacts` 表里有一行索引（`owner_type=VALIDATION`、`kind=VALIDATION_EVIDENCE`）。

`{stamp}` 用 `20260907T142514Z` 这种紧凑写法，**不能用 ISO 8601**：
`validate_key()` 只放行 `[A-Za-z0-9._/-]`，ISO 里的冒号会被当场拒收。

### 数据库没动

`benchmark_tasks.invalid_reason_code` 从迁移 0001 起就存在（`varchar(100)`），
**不需要新迁移**。七个取值定义成 `TaskInvalidReason`（`app/domain/enums.py`），
**不注册进 `PLATFORM_ENUMS`** —— 那张表是给迁移建原生枚举类型用的，
注册进去会多建一个没人用的类型，`downgrade base` 时还要记得 DROP 它。

### 实测数字（本机，2026-09-07）

- 四道 Golden 题跑完八步（含复跑）合计 **6 秒**，单题 1.4–1.6 秒；
  其中 S4/S7/S8 三次容器各 450–520 ms，S1/S2/S3 合计不到 60 ms。
- 基线耗时（S4 跑完全量套件）**399–516 ms**，远低于 `test_timeout_s`。
- `bench-golden:py311` 在 Docker 29 的 containerd 镜像存储下**有 `RepoDigests`**
  （`bench-golden@sha256:d7815f…`），digest 恰好等于 image Id。所以本地构建的镜像
  也拿得到一个稳定的 digest，只是它不是"能回仓库验证"的那种内容地址。
- 构造坏任务时踩到一处：把 `test_timeout_s` 压到 1 秒**不足以**稳定触发
  `TEST_TOO_SLOW` —— 本机容器起来加跑完七条用例还不到 1 秒。
  改成"`test_command` 睡 30 秒、预算 5 秒"，结果就只取决于这两个数。

---

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

## 8.7 L0 Golden Tasks 落地实录（E1-T2，2026-09-04）

四道题已建好，在 `datasets/golden/`。工具是 `python -m cli.golden {build,verify,list}`，
`make golden` / `make golden-verify` 是快捷方式。

| 题目 | 难度 | bug | 考点 |
|:---|:---|:---|:---|
| `bench-golden__textkit-1` | easy | CSV 单行解析没处理引号里的逗号 | 把字符串扫描器写对 |
| `bench-golden__auth-2` | easy | `verify_password` 用 `or` 短路，空口令放行 | 看懂布尔表达式的短路 |
| `bench-golden__cart-3` | medium | 折扣用 `int()` 截断，每单少收一分 | issue 说了两件事，不能只做一件 |
| `bench-golden__pager-4` | medium | `page_slice` 不校验页码，`page=0` 走进负数切片 | issue 明说"越界返回空列表不要改"，一刀切会打挂 P2P |

§8.2 那五步的做法基本照搬，三处按实现需要具体化了。

### (1) 不用真实第三方库，改成手写的极小仓库

§8.2 写的是"选 2–3 个极轻量的真实 Python 库"。实际做下来发现真实库带来的是纯成本：
装依赖要时间、上游可能变、许可证要交代，而这批题的用途只是"给内核开发一批已知答案"。
四个手写的小仓库（每个 4 个文件、零依赖）把整套验证压到 **4.5 秒**，还能把 bug 设计成
正好覆盖想考的点。

### (2) `base/` + `fix/` 两个目录，补丁是**生成**的不是手写的

源码目录长这样：

    sources/<task_id>/{task.toml, issue.md, base/, fix/}

`base/` 是有 bug 的完整文件树（含仓库原有的测试，它们成为 P2P 的来源），
`fix/` 是修复 PR 改动的文件 —— **源码修复和新测试混在一起**，就像真实 PR 那样。

`build` 造出一个两提交的上游仓库（base → 修复），再按 C-42 的受保护路径清单
把修复提交的 diff 劈成两半：碰测试文件的是 `test_patch`，其余是 `gold_patch`。

这样做有三个好处：手写 diff 极易写错行号；劈开用的是平台过滤 AI 补丁的同一份规则，
不会出现两套口径；两个补丁天然能干净地打在 base 上。

### (3) `base_commit` 是确定性生成的，所以能进版本库

Golden 题没有真实上游。`build` 用固定的提交人和提交时间造上游仓库，
于是 `base_commit` 在任何机器上都一样 —— 这是把它写进版本库里那份任务 JSON 的前提。
`repo_url` 记成 `golden://<owner>/<repo>`，明确表示"生成的，没有上游可拉"。
镜像落在 `var/mirrors/`（gitignore 之内），换机器跑一次 `make golden` 就有。

`build --check` 比对生成结果和仓库里的 JSON，对不上就非零退出，CI 里由
`test_generated_json_matches_sources` 覆盖。

### 六步验证具体是哪六步

§7.3 的八步流水线是 E1-T3 的活（要进容器、要出证据制品）。E1-T2 的验收标准写的是
"手工验证 6 步"，落地成下面这六条，全部可自动跑：

| 步骤 | 验的是什么 | 依据 |
|:---:|:---|:---|
| 1 | 物化：工作区历史只有一个提交，树哈希等于 base 树 | C-43 |
| 2 | 补丁体检：`gold_patch` 不碰受保护路径；`test_patch` 只碰测试文件；两者都能打上 | C-64、§7.1 |
| 3 | `base + test_patch` 上，**每条** F2P 都失败 | §7.2(5)，**Noop 解决率 0% 的依据** |
| 4 | `base + test_patch` 上，P2P 全部通过 | §7.2(6) |
| 5 | `base + test_patch + gold_patch` 上，F2P 全部通过 | §7.2(5)，**Oracle 解决率 100% 的依据** |
| 6 | 同上状态，P2P 仍然全部通过 | §7.2(6) |

**第 3 步是逐条跑的，其余整批跑。** 差别在要证明的命题：整批跑只能得出"至少挂了一条"，
而第 3 步要证明**每一条**都挂 —— 漏掉一条在 base 上就通过的 F2P，Noop 哨兵就会给出
非零解决率。整批跑之所以够用，是因为 pytest 只有在全部通过时才返回 0，
任何一条挂了、或者任何一个用例 ID 不存在（退出码 4），都不是 0。

验证时在本机直接起 pytest 子进程，**不进容器**：跑的是我们自己写的代码，没有不可信输入，
而目的只是"这批题自己站得住"。真正的评测必须进沙箱，那是 E2-T2 / E4-T2 的事。

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

## 8.8 仓库选型实测（2026-09-08，E8-T1）

> **本节是追加的实现记录。** 它**改了 §8.3 的两条阈值**（Python 占比、候选池深度），
> 理由逐条写在下面；§8.1 ~ §8.7 的其余内容一条没动。

工具：`app/benchmark/{github,survey}.py` + `python -m cli.survey {probe,measure,report}`
（`make survey` / `make survey-measure`）。原始数据在 `datasets/survey/repos-2026-09-08.json`，
候选名单在 `datasets/survey/candidates.txt`。

### 定档结果：9 个仓库，国产 5 个，合计候选池约 1162

| 仓库 | 候选池 | 中文 issue | 归属 |
|:---|---:|---:|:---|
| `sqlfluff/sqlfluff` | 682 | 0% | 国际对照 |
| `sgl-project/sglang` | 188 | 0% | 国产 |
| `xorbitsai/inference` | 84 | 39% | 国产 |
| `pallets/click` | 77 | 0% | 国际对照 |
| `tortoise/tortoise-orm` | 51 | 4% | 中文开发者主导 |
| `hiyouga/LlamaFactory` | 25 | 40% | 国产 |
| `Delgan/loguru` | 20 | 1% | 国际对照 |
| `milvus-io/pymilvus` | 18 | 2% | 国产 |
| `InternLM/lmdeploy` | 17 | 42% | 国产 |

「候选池」= 近 2 年**关联了 issue 且同时改了测试和源码**的 merged PR 条数。
后五个的安装/测试耗时还没量到（见"没量到的那一格"）。

### 怎么量的

分两段，因为最贵的一步是 `pip install`：

- **`probe`**（只查 GitHub，每个仓库 13 次查询）：许可证、语言占比、候选池深度、中文 issue 比例
- **`measure`**（起容器）：clone → 装本体计时 → 补测试依赖 → 跑全量测试计时

候选池深度 = **关联 issue 的 merged PR 数（精确）× 抽样里"带测试且带源码"的比例**。
前者用 GitHub 搜索的 `linked:issue` 直接拿；后者搜索表达不了，只能逐个 PR 展开文件列表，
一页 25 个就要几十点配额，所以抽样。

**抽样跨时间窗分 6 段取，不是从最新的一路往下翻。** 这一条是被数据教的：
nonebot2 最近几百个关联 issue 的 merged PR 几乎全是插件商店的 registry 条目
（只改一个 `assets/plugins.json5`），按尾部抽样算出来的比例接近 0。

"这个 PR 带了测试改动吗"用 `app/domain/protected_paths.py` 的 `TEST_CODE_PATTERNS` 判，
**不是**整份 `DEFAULT_PROTECTED_PATTERNS`（后者含 `pyproject.toml`、`.github/**`，
拿它判会把只改配置的 PR 算成"带了测试"）。为此把那份清单拆成
`TEST_CODE_PATTERNS` + `TEST_CONFIG_PATTERNS` 两个具名视图，内容和顺序一字未改。

### 改了 §8.3 的两条阈值

**1. Python 占比：≥80% → ≥50%**（`MIN_PYTHON_RATIO`）

这一条是**在修测错的东西，不是放宽标准**。GitHub 的 `languages` 数的是仓库里所有文件的
字节数，而 §8.3 给这条准则写的理由是"环境构建与测试解析最成熟"。三个被误杀的实例：

| 仓库 | 占比 | 被什么稀释 |
|:---|---:|:---|
| sqlfluff | 71% | SQL 1.5MB —— 它是 SQL linter，测试语料就是 `.sql` 文件 |
| nonebot2 | 63% | MDX 205KB + TypeScript 131KB —— 仓库里带着 Docusaurus 官网 |
| jieba | 52% | "OpenEdge ABL" 6.8MB —— linguist 把词典 `dict.txt` 误判成了源码 |

三个都是纯 Python 项目，环境构建和测试解析一点不受影响。取 0.50 而不是更低，
是因为它同时保证了"Python 是占比第一的语言"—— 真正多语言的仓库（mmcv 39%）照样进不来。

**2. 候选池深度：≥80 → ≥15**（`MIN_CANDIDATE_PRS`）

§8.3 同时要求两件事，而真实数据下它们打架：

- (a) 每个仓库候选池 ≥80
- (b) 定档 8–15 个仓库，其中国产至少 4–6 个

32 个候选里够 (a) 的只有三个（sqlfluff 682、sglang 188、xorbitsai 84），国产两个。
满足 (a) 就满足不了 (b)。

**让 (a) 让步**，因为它本来就是"题量够不够"的代理指标，而题量已经不缺了：那三个深仓库
合计 954 个候选，即使验证流水线只留下 15%，也有 140 道题 —— M5 的 100 道光靠它们就够。
还需要更多仓库是为了**多样性**和**国产覆盖**，那正是 (b) 要的东西，砍掉 (b)
等于砍掉这个基准的两个立项理由。

取 15 是让 (b) 成立的最低门槛（9 个仓库、国产 5 个）。再低会放进只出得起个位数题目的
仓库，那些仓库的 env 镜像建起来不划算（E2-T3 的成本）。

### 一个走了弯路才看清的结论

第一批 20 个候选（12 个国产轻量工具库 + 国际对照）**全军覆没**，最好的国产仓库
只有 8 个候选。当时的判断是"中文生态题源不够"，**这个判断是错的**。

真实原因是候选名单选窄了：挑的全是**轻量工具库**（pyecharts、python-pinyin、wechatpy），
这类项目两年也攒不出几十个"带测试的 bugfix PR"—— 和语言无关，同量级的国际库一样浅
（httpx 严口径 1 个、jsonschema 2 个）。有深度的是**大型项目**，而国产这边不缺：
sglang 188、xorbitsai 84。

代价是这些项目依赖重（torch 那一套），`pip install ≤120s` 那条门槛多半过不去 ——
这是**题源深度和环境构建成本的取舍**，不是"中文还是英文"的取舍。

顺带否掉一条曾经看好的思路：中文项目普遍写"修复 #123"而不是 `fixes #123`，
GitHub 的 `linked:issue` 认不出后者。原以为放宽关联口径能救回一批，实测**基本没用**——
pyecharts 1 → 6，nonebot2 反而更低（8 → 4），akshare 693 个 merged PR 宽口径抽 150 个
**一个合格的都没有**。瓶颈是"带测试的 bugfix PR"本身稀少，不是关联方式。
宽口径那一列作为诊断留在数据里，不参与门槛。

### 没量到的那一格

后五个（大型国产项目）的安装/测试耗时**没量到**：走代理 clone 这些仓库反复超时
（35MB 拉了十分钟没完）。这一格留给 E2-T3 —— 它建 env 镜像时本来就会产生真实构建耗时，
比这里用简化脚本量的准。定档不等这个数：`shortlist()` 的判据是"没有任何一条门槛判**不过**"，
而不是"每条都测过"，否则等于让一个工具的局限决定数据集的构成。

前 20 个轻量候选的实测数字倒是全齐：**安装 2–21 秒、测试 4–19 秒**，
§8.3 的 120/180 秒门槛对这一档根本不起作用。

### 中文占比：如实标注，不够就走 §8.5 的 Plan B

定档的 9 个里，中文 issue 比例最高的是 lmdeploy 42%、LlamaFactory 40%、xorbitsai 39%。
**"国产归属"和"中文 issue 比例"是两回事**：sglang 和 pymilvus 是中文团队主导但 issue 写英文。
§8.3 那条准则要的是归属，§8.5 的"中文优先"看的是语言分布。后者达不到就走 §8.5 已经
写好的 Plan B（在 L0/L1 里人工构造更多中文 issue 任务），**不做机器翻译**。

### 实测踩到的坑（给 E2-T3 建镜像）

每一条都让某个仓库看起来"装不上"或"没有测试"，而真实原因完全不同。

| # | 坑 | 表现 |
|:--|:---|:---|
| 1 | **管道会吃掉退出码** | `cmd \| tail` 的退出码是 `tail` 的，永远 0。第一版因此把 20 个仓库全报成"安装成功"，那一轮数据整个作废。`sh` 没有 `pipefail`，要写成 `cmd > f 2>&1; rc=$?; tail f; exit $rc` |
| 2 | **`cap_drop=ALL` 让容器里的 root 反而写不进挂载目录** | root 丢了 `CAP_DAC_OVERRIDE` 就不能无视文件权限。手工 `docker run` 复现不出来，极易误判成"这个仓库装不上"。解法是跟着宿主机 uid 跑 |
| 3 | **容器不继承宿主机代理** | 不传 `HTTP_PROXY` 那几个，pip 一路超时 |
| 4 | **直连 PyPI 大面积超时** | 必须走国内镜像（后端自己的 `pyproject.toml` 早就这么配了，风险 R18）|
| 5 | **`HOME` 和 `TMPDIR` 都不能留在容器的 `/tmp`** | `/tmp` 是 tmpfs，吃的是内存额度。非 root 装不进系统 site-packages，pip 退回 user 安装写 `$HOME/.local`，装 torch 那一套直接 `No space left on device`。**两个都要挪到磁盘上**（只挪一个不够，这一条踩了两次）|
| 6 | **`addopts` 里引用的 pytest 插件不在，就一条用例都跑不了** | 退出码 4，量到的"测试耗时"是**失败所需的时间**。预装 pytest-cov / asyncio / mock / timeout 能救回一批 |
| 7 | **只有 `setup.py` 的老项目在构建隔离里缺 setuptools** | 先装 setuptools 没用（构建隔离是独立环境），要 `--no-build-isolation` |
| 8 | **nonebot2 的构建后端是 `uv-build`，pip 装不上** | 这类仓库的 env 镜像得用 uv |
| 9 | **linguist 会把数据文件误判成源码** | jieba 的词典被算成 "OpenEdge ABL" 6.8MB |

另有一条是本工具自己的 bug，一并记下：`subprocess.TimeoutExpired` 当初没被包成
`GitHubError`，探大仓库时一次超时把整轮探测的结果连同烧掉的 API 配额一起扔了 ——
而 `probe_repo` 的设计意图正是"不能因为一个仓库出问题就丢掉前面所有结果"。

---

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
