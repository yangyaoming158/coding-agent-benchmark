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

### 补记（2026-09-17，E8-T3）：验证跑全量、评测跑子集，顺序不同

上面"验证复用 `execute_tests`，验过了就保证正式评测判得对"漏了一层：**用例子集和顺序**。
S4/S8 跑全量套件（文件顺序），正式评测只跑 F2P ∪ P2P（P2P 字母序）。tortoise 的
`test_init_creates_migrations_package` 只在"它是本进程第一个 import `cli_app` 的用例"时过，
文件顺序里它排第一、字母序里排第七 —— 八步验证 8 道全 VALID，Oracle 门禁 8 道全挂。
处置和根治的取舍在 §8.12 二、三；这一节的判定规则没改。

## 7.11 数据集版本化与发布落地实录（2026-09-10，E1-T6）

> **本节是追加的实现记录。** 它给 §7.4 的最后一句话定了一个读法（第五节），
> 其余 §7 的条文一条没动。

工具：`app/benchmark/dataset.py` + `python -m cli.dataset {stage,gate,publish,show,verify,quarantine}`
（`make dataset-stage` / `dataset-gate` / `dataset-publish` / `dataset-show` / `dataset-verify`）。

### 一、任务卡只写了门禁那一半，AC 是开工前定的

`10-tasks-plan.md` 上这张卡只有一条 AC：「发布前自动跑 Oracle 与 Noop，不达标拒绝发布」。
快照怎么建、版本号怎么定、发布后复验不过怎么隔离，一个字没有。
定下来的十条见任务卡，最要紧的三条是：**冻快照前要重算一遍 `content_hash`**、
**门禁不是两条检查而是三条**、**已发布的版本一行都不改**。

### 二、一行 `benchmark_sets` 不等于"已发布"

这是这张卡唯一真正的设计问题。`evaluation_runs.benchmark_set_id` 是非空外键，
想跑一次 Oracle 门禁就得先有一行 `benchmark_sets`；而门禁的意思又是"不达标不许发布"。
E8-T2 正是因为这条外键把端到端哨兵留给了本卡（§8.11 第十节）。

破法是：**建行和发布是两回事**，发布只由 `status` 表示。

    stage    建一行 DRAFT，当场把题目清单 + content_hash 冻进 benchmark_set_items
    gate     建 Oracle / Noop 两个实验，题从 items 里取，快照摘要写进 run 的 manifest
    publish  重算摘要 → 要求两个门禁实验记的摘要与它一致 → 查门禁 → 全过才 PUBLISHED

不是自欺欺人的理由在于**门禁跑的题就是将要发布的那一批**：`stage` 那一刻就冻死了，
`publish` 不会"再查一次库里现在有哪些 VALID"。中间有人加题、改题、隔离题，摘要就变了，
旧的门禁结果连不上，`publish` 直接拒绝。没有这道锁，"先建 set 再跑门禁"就真是个洞：
拿一份干净的小集合过门禁，再把坏题塞进去发布。

**快照摘要**：每行写成 `task_id:content_hash`，按 `task_id` 排序后拼起来取 sha256。
排序是必须的 —— 同样 22 道题，先入库谁后入库谁不该算成两个数据集。
它同时覆盖"有哪些题"和"每道题是什么内容"，所以一个数就能回答"跑的是不是那一批"。
存进 `benchmark_sets.snapshot_digest` 那一列是为了**发现直接改库**：现算的和存的对不上，
说明有人绕过发布流程动了 items。

摘要放在 `evaluation_runs.manifest` 里传递，没有新开表 ——
那一列的注释原文就写着它装"镜像 digest 表、harness 的 git sha、**数据集哈希**"。

### 三、门禁是三条检查，不是两条

协议 C-50 的字面要求是 Oracle 100% / Noop 0%。只查这两个数会漏掉一整类问题：

**一道题因为平台故障没跑成，它同样不是 `RESOLVED`。** 于是 Noop 那边的"0%"可以被凑出来 ——
门禁看着过了，其实那道题根本没验过。所以第三条是：每道题都要有一条
`infra_outcome = SUCCESS` 的认定结果。Oracle 那一侧不存在这个漏洞（故障会让它掉出 100%），
但一样查，因为"哪几道题没跑成"是排查时第一个要知道的事。

三条检查外加"跑完了没"和"跑的题数对不对"，不合格时**一律点名到具体题号**。
只报一个百分比的话，22 道题里挂了一道，查的人无从下手。

### 四、`content_hash` 冻之前重算一遍

快照冻的是 `benchmark_tasks.content_hash` 那一列，而题目内容在 `raw_definition` 里。
两者对不上时我们不知道该信哪一份，此时冻下去的"身份证"是假的，
以后 `verify` 报出来的漂移全是噪声 —— **比不冻更糟**。

所以 `stage` 会拿 `raw_definition` 重算一遍，对不上就整批拒绝并点名。
口径和 §7.9 给 `test_patch_paths` 立的规矩是同一条：**导入与验证时重算，不一致则拒收**。
22 道题各一次 sha256，几毫秒。

### 五、§7.4 那句话只有一种读法说得通

§7.4 写的是「发布后定期复验（每周一次）不通过的任务自动隔离并从当前 dataset 版本快照中
排除（历史版本不受影响）」。这句话拆成三件事，第三件和 §7.5 直接打架：

**§7.5 存在的全部理由是"三周后重跑得到的是同一批题的同一版本"。** 如果隔离能从已发布版本的
`benchmark_set_items` 里删行，三周后重跑就不是同一批题了。而且"历史版本不受影响"本身讲不通 ——
已发布的版本，发布那一刻起就是历史。

所以取唯一自洽的读法：**发布的版本永远不动，隔离影响的是下一版。**
题被置成 `QUARANTINED`，下一次 `stage` 自动不收它，v2 少一道题，v1 原样保留。
想知道手上这版里有没有已经被隔离的题，用 `dataset verify` 查 —— **报，但不改**。

`verify` 报三类漂移，处置完全不同：

| 漂移 | 意思 | 该干什么 |
|:---|:---|:---|
| 内容哈希变了 | 题目被改过，这一版不再等于现在的库 | 出新版本 |
| 被隔离了 | 复验不通过 | 这一版照旧可复现，下一版会排除它 |
| 题不见了 | 外键是 RESTRICT，正常删不掉 | 有人绕过 ORM 动了库，去查 |

**范围**：本卡做隔离的写入口（`dataset quarantine`）、下一版自动排除、漂移检查。
每周定时复验的调度和"复验失败自动隔离"拆成了 **E9-T5**，理由三条：调度是运维件
（AGENTS.md §11 明确不做调度中间件）；复验是重跑八步验证，22 道题要起 66 个容器，属于机时活；
协议 C-20a 禁止一次失败就隔离，判"复验**也**失败"必须有上一次的记录，那是一张新表。

### 六、顺手修了一个真 bug：投作业不看数据集

`cli/queue.py` 的 `cmd_enqueue()` 和 `cli/experiment.py` 的 `cmd_start()` 原来都写的是
`select id from benchmark_tasks`，**完全不看 `--set` 选的是哪个数据集** ——
那一行只用来取 `benchmark_set_id` 填进实验，题目是全库捞的。

库里只有四道 Golden 题的时候看不出问题。E8-T2 之后库里混着人工终审否掉的 `INVALID`，
照旧写法会把它们一起投进队列，而 Oracle 在坏题上必然掉出 100%，
排查的人会去翻判定引擎。两处都改成从 `benchmark_set_items` 取题。

顺带给两个命令加了 `--version`。不给版本号时的挑法有先后：**先找最新的 `PUBLISHED`**，
没有再退回最新的、有题的 `DRAFT`，并**打印一句提醒**。第二条是给开发期留的 ——
起个 Worker 冒烟一下不该被迫先走完发布流程 —— 但它必须说出来，
悄悄拿一版没过门禁的题去跑，结果会被当成正式数字。

### 七、版本号用计数器，不用两段式

`12-engineering-workflow.md` §32.6 举的例子是 `benchmark-cn-v1@1.0`，而库里 Golden 那行
和 `tests/integration/factories.py` 写的都是 `v1`。挑了后者：`v1` / `v2` / `v3` 单调递增。

理由是两段式要求先定义清楚"什么改动算大版本"，而没人定义过 ——
**没定义的语义比计数器更糟**。快照代数就是个计数器。

`slug` 和 `dataset_id` 也不强求同名：Golden 那批题的 `dataset_id` 是 `golden-v1`，
set 的 slug 是 `golden`。新加的 `benchmark_sets.source_dataset_id` 一列把这层对应记下来，
不然"这一版的题是怎么挑出来的"就只存在于某个人的记忆里。

### 八、发布产物：指纹入库，内容不入库

按 §32.6 的三层分工落地：

| 层 | 落点 | 入不入库 |
|:---|:---|:---|
| 事实来源 | `benchmark_set_items` | 数据库 |
| 完整内容 | `datasets/exports/<slug>@<version>.jsonl` | **不入库**（含 `gold_patch`，协议 C-44）|
| 指纹 | `datasets/manifests/<slug>@<version>.json` | 入库，2 KB 上下 |

指纹里两个哈希都要：`task_hashes_sha256` 是题目清单的摘要（从库里就能重算），
`dataset_sha256` 是导出文件本身的哈希（证明手上那个 jsonl 没被动过）。

导出**逐字节稳定**：行内键排序、行间按 `task_id` 排序。不稳定的话每导一次
`dataset_sha256` 变一个值，指纹就没有意义了。单测里有一条专门拿倒序输入验它。

指纹里还如实记 `dirty`。协议 C-27 要求正式实验前工作区干净，C-28 说脏工作区跑出来的结果
不得进排行榜 —— 完整的强制是 E5-T4 的活，这里只取 `harness_git_sha` 和 `dirty` 两个事实，
`publish` 默认在脏工作区拒绝发布，`--allow-dirty` 放行但在指纹里标着。
**留一个恒为 false 的 `dirty` 比不留更糟。**

### 九、门禁第一次跑就拦下了 benchmark-dev，而且拦得对

**这是这张卡最有价值的一次实测。** 第一轮门禁的结果是：

    oracle  实验 #27  21/22 = 95.5%（要求 100%）  COMPLETED
    noop    实验 #28  0/22  = 0.0%（要求 0%）    COMPLETED
    → 拒绝发布，出问题的题：pallets__click-3058

平台故障 0，两次都跑完了。挂的那道题 F2P 2/2 全过，P2P 1278/1279 —— 差的那一条是
`tests/test_utils.py::test_echo_via_pager[test5-less]`，耗时 15 毫秒，**不是超时，是竞态**：

这条用例喂给 `click.echo_via_pager()` 一个中途抛 `RuntimeError` 的生成器，
断言 pager 一个字都没收到（`expected_pager=''`）。实际 `less` 已经把第一块 `test`
flush 出去了，于是 `assert 'test' == ''` 挂掉。谁先谁后是调度决定的。

**单独重跑三遍：过 / 挂 / 过 —— 而且第二遍挂的是另一条**（`[test6-cat ]`）。
所以飘的不是某一条用例，是整个 `test_echo_via_pager` 参数化家族，也不只是并发的锅。

量出来的规模：

| | |
|:---|---:|
| 22 道题的 `pass_to_pass` 合计 | 29796 条 |
| 其中 `test_echo_via_pager` | **1123 条**（3.8%）|
| 每题 | 7–67 条，平均 51 |
| 观测失败率 | 约 1276 次执行挂 2 次 ≈ 0.16% |
| 反推：一轮 22 题的 Oracle 门禁期望挂几条 | **约 1.8 条** |

**也就是说这个数据集过不了自己的门禁**，重跑只是换一条挂。

更要紧的是另一面：**一条会飘的 P2P 不是回归护栏，是噪声。** 留着它，以后每次正式评测
都有相当概率把一个正确的补丁判成 `UNRESOLVED` —— 那比门禁过不去严重得多，
而且不报错，只会让解决率莫名其妙偏低（AGENTS.md §5.5 说的就是这类问题）。

**E1-T3 的 S8 复跑一条都没测出来**：153 份 VALID 证据里 `flaky` 字段全是空的。
复跑 2 遍去抓 0.16% 的抖动，抓不到是必然的。§7.3 的 S8 早写了"剔除 flaky 用例"，
规矩在，判据不够灵敏。

### 十、剔除不稳定用例：按函数名整族剔，判据放在 `assemble()`

处置是把 `test_echo_via_pager` 整族从 `pass_to_pass` 里剔掉（`assembly.FLAKY_TEST_FUNCTIONS`），
22 道题重新组装、`content_hash` 重算，benchmark-dev 重新定档。三处决定值得记：

**① 按函数名剔，不按具体的参数化 ID。** 飘的是这个函数，不是某一组参数 ——
两次观测挂的是不同的参数（`[test5-less]` 和 `[test6-cat ]`）。只剔观测到挂过的那两条，
剩下 1121 条同族照样会飘。

**② 匹配函数名要精确相等，不能用子串。** click 里有 8 个函数名带 `echo_via_pager`
（`test_echo_via_pager_streams_each_write`、`test_with_echo_via_pager`……），
它们没飘过。按子串匹配会白白丢掉 47 条好护栏。实测：1123 剔成 0，那 47 条一条没少。

**③ 判据必须放在 `assemble()` 里，不能只放在 `select_p2p()` 里。**
两条路都产出题目：探测轮现算 P2P（走 `select_p2p`），组装轮读探测轮**缓存**下来的
那份清单（不走 `select_p2p`）。第一版只改了 `select_p2p`，重跑 assemble 显示
"更新 22"，而 1123 条一条没少 —— **命令说成功了，数据一点没变**。
"一条会飘的用例不许当回归护栏"是题目的性质，判据就该放在产出 `TaskDefinition` 那一步。

顺带给 `cli.promote assemble` 加了 `--redo`：默认只挑 `PRESCREENED` 的候选是对的
（推成题目之后不该再做一遍），但**派生规则改了就必须能重做**，而那时候选早就是
`PROMOTED` 了 —— 表现是"一条候选都选不出来"，看起来像缓存坏了。

`p2p_sampling.total_pool` 跟着改小（只对 `full` 策略）：`full` 的定义就是"候选池全收"，
池子小了这个数不跟着小，§7.7 要求记录的那个数就和实际清单对不上了。

### 十一、隔离的理由不能写进 `raw_definition`

第一版把隔离记录塞进了 `benchmark_tasks.raw_definition`，当场撞墙：
那一列是 `TaskDefinition` 的原文，而它是 `extra="forbid"` 的 ——
**加一个键，这道题就再也解析不回来**，隔离过的题连 `cli.validate run` 都跑不动。

第二个更隐蔽的后果：`content_hash` 算的就是 `raw_definition`，往里加东西等于悄悄改了
题目的身份证，而库里那一列没跟着变，下一次 `dataset stage` 会把它报成"哈希对不上"。

所以隔离记录单独一列 `benchmark_tasks.quarantine`（`{at, from_state, reason}`）。
不复用 `invalid_reason_code`：那七个 code 说的都是"八步验证跑出来不合格"（§7.3），
而隔离的理由是"发布后复验不通过"，硬套一个是在编。

判据也写进了测试，而且是拿**哈希还对不对得上**去判，不是"少了某个键" ——
前者正是 `dataset stage` 会查的那一条，往 `raw_definition` 里加任何东西都会让它红。

### 十二、实测数字（本机，2026-09-10）

剔掉不稳定用例之后重跑，**门禁一次过**：

    oracle  实验 #96  22/22 = 100.0%（要求 100%）  COMPLETED
    noop    实验 #97  0/22  = 0.0%（要求 0%）      COMPLETED
    → ✅ 已发布 benchmark-dev@v1，22 道题

| | |
|:---|---:|
| 快照 | 22 道题，排除 `INVALID` 9 |
| F2P / P2P 合计 | 85 / 28720（剔掉 1076 条不稳定用例后）|
| 快照摘要 | `sha256:300746559b84…` |
| Oracle 一轮墙钟 | 25 秒，单题平均 6.8 秒 |
| Noop 一轮墙钟 | 74 秒，单题平均 9.6 秒 |
| 指纹文件 | 4042 字节 |
| 导出文件 | 2391 KB |

**整条链是可复现的**：清库重灌一遍（`assemble --limit 30` 等距抽样是确定性的，
抽出来的 30 个 PR 号和复审表逐个相同），重新 stage 出来的快照摘要
和上一轮**逐字节相同**。

隔离那条路也实跑了：`dataset quarantine` 一道题之后，已发布的 v1 仍是 22 道、
`verify` 如实报出"已被隔离 1 道"，而 `stage` 出的 v2 是 21 道，两版摘要不同。

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
| **测试补丁里加了 Python 测试函数的比例** | **≥ 50%** | 候选池深度不等于出题能力：F2P 要的是用例名，只改数据文件的测试补丁抽不出来。**2026-09-14 追加，理由见 §8.8 最后一节** |
| 许可证 | 宽松开源（MIT/Apache/BSD） | 分发任务集需合规 |

**候选池（Week 1 Day 3 用脚本实测打分后定档，不预先承诺）**：中文社区 Python 项目（如 NoneBot 生态、中文 NLP/文本处理工具库、国产 AI 基础库的轻量子项目、国产 Web/运维框架的 Python 组件），叠加 2–3 个国际主流轻量库作对照组。
**筛选脚本产出的一张表**（repo × 候选 PR 数 × 安装耗时 × 测试耗时 × 中文 Issue 比例）就是选型依据，写进报告。

## 8.8 仓库选型实测（2026-09-08，E8-T1）

> **本节是追加的实现记录。** 它**改了 §8.3 的两条阈值**（Python 占比、候选池深度），
> 理由逐条写在下面；§8.1 ~ §8.7 的其余内容一条没动。

工具：`app/benchmark/{github,survey}.py` + `python -m cli.survey {probe,measure,report}`
（`make survey` / `make survey-measure`）。原始数据在 `datasets/survey/repos-2026-09-08.json`，
候选名单在 `datasets/survey/candidates.txt`。

### 定档结果：8 个仓库，国产 4 个，合计候选池约 1144

| 仓库 | 候选池 | 中文 issue | 归属 |
|:---|---:|---:|:---|
| `sqlfluff/sqlfluff` | 682 | 0% | 国际对照 |
| `sgl-project/sglang` | 188 | 0% | 国产 |
| `xorbitsai/inference` | 84 | 39% | 国产 |
| `pallets/click` | 77 | 0% | 国际对照 |
| `tortoise/tortoise-orm` | 51 | 4% | 中文开发者主导 |
| `hiyouga/LlamaFactory` | 25 | 40% | 国产 |
| `Delgan/loguru` | 20 | 1% | 国际对照 |
| ~~`milvus-io/pymilvus`~~ | ~~18~~ | ~~2%~~ | **已去掉，见下** |
| `InternLM/lmdeploy` | 17 | 42% | 国产 |

「候选池」= 近 2 年**关联了 issue 且同时改了测试和源码**的 merged PR 条数。

> **2026-09-08 二次修订（E2-T3 建镜像时发现）**：原定档是 9 个仓库、国产 5 个、
> 候选池约 1162。`milvus-io/pymilvus` **去掉了**，因为它和本平台的物化方案不兼容 ——
> 两条互相独立的原因，见下面第 ⑪⑫ 条。
>
> 代价：8 个仓库 / 4 个国产，仍然满足 §8.3 的"8–15 个、国产至少 4–6 个"，
> **但踩在国产数量的下限上，没有余量**。丢掉的 18 道候选占总数 1.5%，
> 而且 pymilvus 的中文 issue 只有 2%，所以中文覆盖不受影响。
>
> **没有候补可换**：选型数据里国产排在它后面的是 nonebot2（候选池 8，低于门槛 15，
> 而且构建后端是 uv-build 装不上），再往下 modelscope 只有 3。想把国产补回 5 个
> 只能重新扩一轮候选名单，那是 E8 的事。

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

> **2026-09-08 补**：pymilvus 去掉之后是 8 个仓库、国产 4 个，仍然满足 (b)，
> 但**国产数量已经踩在下限上**。再掉一个就不够了，而门槛 15 之下没有候补
> （下一个是 nonebot2，候选池 8）。也就是说这条阈值现在没有余量可让了。

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

**2026-09-08 补：E2-T3 建镜像时量到了其中三个**（`images/envs/*.json` 是配方，
`envs/{environment_id}/builds/{stamp}/` 下有构建日志和依赖锁）：

| 仓库 | clone（浅） | 建 env 镜像 | 收集到的用例 | 镜像增量 |
|:---|---:|---:|---:|---:|
| `pallets/click` | 秒级 | 12.7 s | 33084 | ~3 MB |
| `tortoise/tortoise-orm` | 秒级 | 43.3 s | 2062 | ~190 MB |
| `sqlfluff/sqlfluff` | 秒级 | 53.3 s | 13688 | ~190 MB |
| `hiyouga/LLaMA-Factory` | 秒级 | **10 分 26 秒** | 359 | ~3.3 GB |

"建 env 镜像"含装本体 + 补测试依赖 + 写依赖锁，是**从零构建**（没有层缓存）的墙钟时间，
比 §8.3 那条 120 秒门槛量的口径更全。

前三个轻量仓库都远在门槛之内。**LLaMA-Factory 是第一个真建成的大型国产项目**：
10 分 26 秒、镜像涨 3.3 GB（torch 那一套），换算下来 §8.3 的 120 秒门槛对这一档
**完全不适用** —— 但这正是 ADR-008 说的"可在实验前夜完成"，一次性成本，
和评测时的单题耗时无关。改配方之后重建只要 17.7 秒（13 步里 11 步命中层缓存），
装依赖那一层原样复用。

**`milvus-io/pymilvus` 没建成**，原因不是耗时 —— 见下面第 ⑪⑫ 条，它已经被去掉了。

**剩下四个大型国产项目单独探过一轮**（2026-09-08，只读 GitHub 上的构建元数据，不建镜像），
问的是"它们会不会踩 pymilvus 那两条坑"：

| 仓库 | 子模块 | 版本号从哪来 | 没有 git 元数据时 |
|:---|:---|:---|:---|
| `sgl-project/sglang` | 无 | setuptools-scm | `fallback_version = "0.0.0.dev0"`，注释明写"允许没有 .git 时装" ✅ |
| `hiyouga/LLaMA-Factory` | 无 | 从 `src/llamafactory/extras/env.py` 正则抠 | 和 git 无关 ✅ |
| `InternLM/lmdeploy` | 无 | 从 `lmdeploy/version.py` 读 | 和 git 无关 ✅ |
| `xorbitsai/inference` | 无 | setuptools-scm | `fallback_version = "0.0.0+unknown"`，注释明写为了无 git 构建 ✅ |
| ~~`milvus-io/pymilvus`~~ | **有** | hatchling 自定义钩子调 setuptools-scm | **没有 fallback，直接 LookupError** ❌ |

**四个都不会踩。** 分水岭是 `fallback_version` 配没配 —— 用 setuptools-scm 本身不是问题，
不给退路才是。顺带查了 `.gitattributes`：定档的仓库一个 `export-ignore` 都没有（坑 ⑦ 那条）。

另外三件元数据读不出来、只有真动手才看得见的事，记在这里免得下次重新踩：

- **sglang 的构建文件在 `python/` 子目录**，不在仓库根，配方的 `install_steps` 要
  先 `cd python`；它还要编 Rust 扩展。
- **lmdeploy 的 `setup.py` 按目标设备挑依赖**（`requirements/runtime_{device}.txt`，
  还会去探 CUDA 版本），CPU 机器上装到的和 GPU 机器上不是同一套。
- **sglang 的 git 仓库 341 MB**，`--depth 1` 过代理也慢（实测几分钟才动 200 KB，
  第一次直接 `curl 28 Operation too slow` 断掉）。另外三个是 13/15/85 MB，
  浅克隆几秒到一分钟就好。

四个都要拉 torch，镜像预计 3–5 GB。

顺带纠正 E8-T1 当时的一个判断：**"走代理 clone 这些仓库反复超时"是 `--mirror` 的问题，
不是仓库大小的问题。** `git clone --mirror milvus-io/pymilvus` 过代理跑了 4 分 32 秒之后
`Connection reset by peer`；同一时刻换成 `git clone --bare --depth 1 --single-branch`
**一次就成，2.3 MB、几秒钟**。建镜像只要一个 commit 的文件树，浅克隆完全够用
（放 `var/build-snapshots/`，和 `var/mirrors/` 分开，理由见 `05-sandbox.md` §10.9(7)）。

### 中文占比：如实标注，不够就走 §8.5 的 Plan B

定档的 9 个里，中文 issue 比例最高的是 lmdeploy 42%、LlamaFactory 40%、xorbitsai 39%。
**"国产归属"和"中文 issue 比例"是两回事**：sglang 和 pymilvus 是中文团队主导但 issue 写英文。
§8.3 那条准则要的是归属，§8.5 的"中文优先"看的是语言分布。后者达不到就走 §8.5 已经
写好的 Plan B（在 L0/L1 里人工构造更多中文 issue 任务），**不做机器翻译**。

### 实测踩到的坑（给 E2-T3 建镜像）

每一条都让某个仓库看起来"装不上"或"没有测试"，而真实原因完全不同。

第 ①–⑨ 条来自 E8-T1 的选型实测（2026-09-08）；**第 ⑩⑪ 条是 E2-T3 真建镜像时新踩到的**
（同日），两条都是被"建完自查"当场拦下的，否则会各自留下一个"看起来能用、
实际上一条用例都跑不了"的环境镜像。

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
| 10 | **PEP 735 的 `[dependency-groups]` 对 `pip install -e .` 完全不可见** | tortoise-orm 的测试依赖全写在那儿。装完一切正常，然后 `pytest --collect-only` **72 个模块 import 失败**。要 `pip install --group test .`，而这需要 **pip ≥ 25.1** —— `python:3.11-slim` 自带的是 24.0（2026-09-08，E2-T3 建镜像时被自查当场拦下）|
| 11 | **`git archive` 不导出 git 子模块** | pymilvus 的 `pymilvus/grpc_gen/milvus-proto` 是 gitlink，物化出来的树哈希必然对不上上游。E2-T1 的自查会拦下，但**报错信息原来只提 export-ignore**，会把人引到一个根本不存在的 `.gitattributes` 上。已改成先认子模块 |
| 12 | **版本号从 git 元数据算出来的包，装不进 env 镜像** | pymilvus 用 hatchling 的自定义钩子调 setuptools-scm 取版本，而 env 镜像的构建上下文里没有 `.git`（评测工作区也只有一个合成提交、没有 tag，那是 C-43 的防泄题设计）。结果是 `LookupError: setuptools-scm was unable to detect version`，而官方给的绕过办法 `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_*` **对它不生效**。**有没有 `fallback_version` 是分水岭** —— 剩下四个大型国产项目里 sglang 和 xorbitsai 也用 setuptools-scm，但两家都配了 fallback 并在注释里明写"为了没有 git 元数据时也能装"，所以不受影响 |
| 13 | **"这个环境能不能跑测试"要按仓库自己的口径问** | 从工作区根 `pytest --collect-only` 收全部，会扫到不是测试的东西。LLaMA-Factory 的 `scripts/api_example/` 底下有两个叫 `test_*.py` 的 API 用法示例（import 没装的 `openai` 就报错），还有两个同名的 `test_converter.py`（默认导入模式下撞车）。按我们编的口径是 348 条 + 3 个错，按仓库自己的 `pytest --import-mode=importlib tests/ tests_v1/` 是 **359 条 + 0 个错**。环境是好的，是问法不对 —— 配方因此加了 `test_args`，照抄仓库自己的测试命令 |

另有一条是本工具自己的 bug，一并记下：`subprocess.TimeoutExpired` 当初没被包成
`GitHubError`，探大仓库时一次超时把整轮探测的结果连同烧掉的 API 配额一起扔了 ——
而 `probe_repo` 的设计意图正是"不能因为一个仓库出问题就丢掉前面所有结果"。

### 又给 §8.3 加了一条准则：测试补丁带不带测试函数（2026-09-14，E8-T3）

> **这一节是 E8-T3 第一段的结论，给 §8.3 的准则表加了一行。**
> 上面 §8.8 正文那一轮（E8-T1，2026-09-08）的数**一个都没错**，
> 是**漏了一项没量**，而漏掉的那一项足以掀翻仓库排序。

**E8-T1 把 sqlfluff 排第一，靠的是候选池 682（全场最深）。**
E8-T3 第一段把 8 个仓库的候选全挖出来、全拆过补丁之后，真实排名是这样：

| 仓库 | 候选 | 抽得出候选 F2P | 占比 |
|:---|---:|---:|---:|
| **sqlfluff/sqlfluff** | **676** | **70** | **10.4%** |
| Delgan/loguru | 20 | 14 | 70.0% |
| hiyouga/LlamaFactory | 32 | 23 | 71.9% |
| sgl-project/sglang | 214 | 166 | 77.6% |
| xorbitsai/inference | 75 | 59 | 78.7% |
| pallets/click | 80 | 70 | 87.5% |
| InternLM/lmdeploy | 17 | 15 | 88.2% |
| tortoise/tortoise-orm | 55 | 50 | 90.9% |

**sqlfluff 是唯一的异常值，低了整整一个数量级。** 候选池最深的仓库，
出题能力全场最差 —— 676 条候选出 70 条，比 55 条候选的 tortoise-orm 还少。

原因在补丁里数得出来（`var/mining/patches/sqlfluff__sqlfluff/*.test.patch`，
判据是补丁里有没有新增 `def test_` / `async def test_`）：

```
测试补丁总数                 674
加了测试函数                  69   (10.2%)
改动全在 test/fixtures/ 下   582   (86.4%)
```

sqlfluff 是 SQL linter，修一个 bug 的标准做法是**往语料库里加一条 SQL 加一份期望输出**
（`test/fixtures/**.sql` + `.yml`），不写测试函数。而 §7.2(5) 的 F2P 要的是
"修好之后才由失败变通过的**用例名**"，语料文件里**没有用例名可抽**。

同一个判据跑遍 8 个仓库，和上表几乎逐行吻合：

| 仓库 | 测试补丁 | 加了测试函数 | 占比 | 对应的"抽得出 F2P" |
|:---|---:|---:|---:|---:|
| sqlfluff/sqlfluff | 674 | 69 | **10.2%** | 10.4% |
| hiyouga/LlamaFactory | 32 | 20 | 62.5% | 71.9% |
| Delgan/loguru | 20 | 13 | 65.0% | 70.0% |
| sgl-project/sglang | 214 | 162 | 75.7% | 77.6% |
| xorbitsai/inference | 74 | 58 | 78.4% | 78.7% |
| InternLM/lmdeploy | 17 | 15 | 88.2% | 88.2% |
| pallets/click | 80 | 71 | 88.8% | 87.5% |
| tortoise/tortoise-orm | 55 | 49 | 89.1% | 90.9% |

两列相关得这么紧，说明这不是巧合，是同一件事的两种量法 ——
**所以它可以当选型判据用，而且能在建镜像之前就量出来**。
门槛取 50%：8 个仓库里只有 sqlfluff 被拦下，其余最低 62.5%，有余量。

**为什么 E8-T1 没量到这一项。** 那一轮只查 GitHub 的元数据（PR 数、语言、耗时），
**不拆补丁**；拆补丁抽候选 F2P 是 E1-T5 的活（§8.10 第四节），
而 E1-T5 完成时只对 `pallets/click` 跑过。对另外 7 个仓库第一次跑，
就是 E8-T3 第一段的这一天。选型和拆补丁之间隔着五天和两个 Epic，
**这条缝就是漏量的地方**。

**这一条不改任何已有阈值**，只是加一行。§8.3 原有的八条准则和 §8.8 上面改过的
那两条（Python 占比 ≥50%、候选池深度 ≥15）全部照旧。

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

## 8.9 挖掘器落地实录（2026-09-09，E1-T4）

> **本节是追加的实现记录。** 它**改了 §8.4 的一条做法**（缓存不建 `gh_cache` 表，
> 改成文件缓存），理由写在下面第一条；§8.4 的十步流程和 §8.1 ~ §8.8 的其余内容一条没动。

工具：`app/benchmark/{mining,gh_cache}.py` + `python -m cli.mine {run,report,show}`
（`make mine` / `make mine-report`）。产出率存档在 `datasets/mining/`，
断点续跑的进度在 `var/mining/`（不进版本库）。

### 一、缓存没建 `gh_cache` 表，改成文件缓存

§8.4 写的是"本地缓存（`gh_cache` 表按 URL+etag）"。落地时换成了
`var/gh-cache/<hash前2位>/<sha256>.json`，key 是 `sha256(查询串 + 变量的规范 JSON)`。
三条理由：

1. **GraphQL 没有 ETag。** ETag 是 HTTP GET 的条件请求机制，GitHub 只在 REST 上给。
   GraphQL 全部走 POST 打同一个 URL（`/graphql`），没有 ETag 可存，也没有
   "304 不计配额"这回事。而挖掘的配额几乎全烧在 GraphQL 上 ——
   按 URL+etag 建的表，对真正花钱的那条路一点用没有。
2. **`make check` 会清空开发库**（`downgrade base` + `upgrade head`）。缓存放库里，
   每跑一次 check 就没了；而缓存的全部价值就是"重跑不再花配额"。
3. **它不是评测数据。** 没有外键、不进 API、不上前端、没人 SQL 查它。为它写迁移 +
   回滚 + 模型 + 测试，换来的是"挖矿之前必须先起 Postgres"这个多余依赖。

代价：文件缓存是**本机**的，换台机器重跑要重新烧配额。只有一台开发机，可接受。
真要多机共享时 key 的算法已经定死，搬进表里是纯搬运。

TTL 默认 7 天 —— 挖的是**已合并**的 PR 和**已关闭**的 issue，正文和文件列表基本不再变。

### 二、E1-T4 到哪一步为止

§8.4 那十步逐行归属，**分界线是"要不要改写文本、要不要从 diff 里推导东西"**：

| §8.4 的步骤 | 归谁 |
|:---|:---|
| GitHub Search: merged PR + linked issue | **E1-T4** |
| 取 PR files → 分 test_files / code_files | **E1-T4** |
| 过滤：无 test / 无 code → 丢弃 | **E1-T4** |
| 取 parents[0] → base_commit | **E1-T4** |
| 取关联 Issue title/body | **E1-T4**（只抄原文） |
| → 脱敏（去 PR 链接、commit hash、修复代码块） | E1-T5 |
| 从 test_patch 抽候选 F2P 用例 ID | E1-T5 |
| LLM 质量预筛 → score 0-5 | E1-T5 |
| 按分数分流 | E1-T5 |
| 八步验证流水线 | E1-T3（已完成） |

这条边界不是洁癖：**挖掘失败是网络和配额问题，重跑就好；脱敏失败是泄题**，
漏一条 PR 链接那道题就废了，要靠正则断言加人工抽查兜（E1-T5 的 AC）。
混在一起的话，一次限流重跑会把已经核对过的脱敏结果连带重做一遍。

同理，**E1-T4 不取 PR 的 diff 正文**：拆 `test_patch` / `code_patch` 是 E1-T5 的活，
而一个 PR 的 diff 动辄几十上百 KB，塞进 `raw_payload` 这个 JSONB 列会让一张
几千行的表很难查。这里只存文件路径清单，够 E1-T5 判断要不要去取 diff。

**§7.4 图里的 `CANDIDATE` 指的是"进了 `task_candidates` 这张表"，不是一个状态值。**
库里的 `TaskCandidateState` 只有 DISCOVERED / PRESCREENED / PROMOTED / REJECTED，
E1-T4 的产出一律是 `DISCOVERED`，预筛那几列留空。AC 里的"≥30 条 CANDIDATE"
按"表里 ≥30 行"验收。

被三条自动过滤刷掉的 PR **不落库**（噪声比约 1:2.5，`reject_reason` 那一列是留给
E1-T5 和验证流水线的语义拒绝）；分母记在产出率报表里。

### 三、配额根本不是瓶颈，GitHub 的偶发失败才是

**实测每页只花 1~2 个 GraphQL 点**（25 个 PR，每个展开 100 个文件 + 5 个关联 issue，
`rateLimit.cost` 如实回报）。挖完 `pallets/click` 近两年一共 **26 个点**，
而配额是 5000 点/小时。§8.4 把限流列为主要风险，实测**不成立**。

真正拖后腿的是 GitHub 自己的偶发失败：一轮 13 页里撞了 6 次
`Something went wrong while executing your query`，外加一次代理掐断连接
（`Post "https://api.github.com/graphql": EOF`）。两种都不在 `github.py`
原来的可重试清单里，一次就把整个仓库的作业打断 —— 已补进 `_RETRYABLE`，
每种都有单测（`tests/unit/test_github_retry.py`）。

顺带纠一处日志：原来重试时一律打"撞上限流"，把人往配额那边引。改成"gh 调用失败"。

### 四、三种"不报错的丢数据"

这是这个任务里最值得记的一段。三种都**退出码 0、没有异常、日志干净**，
只表现为产出率莫名其妙低了一点。

**(1) 窗口太宽，超出搜索上限。** GitHub 搜索单个查询最多返回 1000 条，
第 1001 条往后直接拿不到。所以必须按合并日期切窗口（默认 12 段 ≈ 每 2 个月），
段内用游标翻页；某段 `issueCount` 仍 ≥1000 就二分再切。
这一条和 §8.8 记的"抽样要跨时间窗分 6 段取"是两个独立的理由，指向同一个做法。

**(2) 降级响应：`issueCount` 说有 14 条，`nodes` 却是空的。**
2026-09-09 实测撞上，紧跟在一次 `Something went wrong` 重试之后。
退出码 0、没有 `errors` 字段，`github.graphql()` 那一层看不出任何异常。
照收的话这一页会被当成"窗口挖完了"，**14 个 PR 一条不剩地消失**。
第一次真跑 click 就栽在这儿：那一轮报"完成 12/12 窗口"，候选 67 条，
修好之后重跑是 **80 条**——差的 13 条全在这一个窗口里。
现在的做法是重取最多 3 次（重取必须绕过缓存，否则读到的是同一份坏结果），
还不行就写进 `problems`，报表上看得见。

**(3) 窗口收尾数目对不上。** 走完一个窗口时比对"GitHub 说有多少条"和
"实际走到多少条"，对不上就记一条 problem。这是 (2) 之外的兜底，
覆盖中间某一页丢了的情况。

### 五、`base_commit`：一个 94% 命中的告警等于没有告警

§8.4 说"取 parents[0] → base_commit"。这对**普通 merge commit**（parents[0] 就是
合并时的基线）和 **squash merge**（唯一的父提交就是基线）都对，
但对 **rebase merge** 不对：那时的"merge commit"是被 rebase 过的最后一个提交，
它的父提交是倒数第二个 rebase 提交，不是基线。算出来的 base_commit 偏后，
工作区里会已经带上这个 PR 的一部分改动 —— 题目看起来正常，
只是被测 AI 拿到的代码已经修好一半了。

第一版判据是"父提交只有一个 + PR 有多个提交"。它把 squash merge 整批冤枉了：
实测 click 一个窗口 16 条候选，按它 **15 条"可疑"**。

改成精确判据：**`parents[0]` 这个提交是不是也属于本 PR**
（查询里给 parents 加一层 `associatedPullRequests`）。是就说明它是被 rebase 进来的，
不是就说明它是基线上的前一个提交。换成这一条之后，click 近两年 80 条候选
**一条可疑的都没有**。

只报可疑、不丢弃。真正的兜底是 E1-T3 验证流水线的 S5：base_commit 不对的话
F2P 在基线上不会失败，题目会被判 `F2P_NOT_FAILING` 丢掉，混不进数据集。

### 六、实测：`pallets/click` 近两年

选它是因为它的 env 镜像 E2-T3 已经建好（12.7 秒），挖完可以直接接上验证流水线跑闭环。

| | 条数 |
|:---|---:|
| 扫过的 merged PR（`linked:issue` 严口径） | 116 |
| 没改测试 → 丢弃 | 30 |
| 没改源码 → 丢弃 | 6 |
| **候选（落库，`state=DISCOVERED`）** | **80** |

产出率 **69.0%**，13 页，2 年切 12 个窗口，全程 26 个 GraphQL 点。

**和 E8-T1 抽样估的对照：估 77，真挖 80，低估 3.8%。** §8.8 那套
"关联 issue 的 PR 数（精确）× 抽样里合格的比例"是准的。

**一处给 E1-T5 和 E2-T3 的提醒：116 条里有 34 条合并进的不是默认分支**
（click 的 `stable` 维护分支）。这些题的 `base_commit` 在维护分支上，
而 env 镜像的快照取的是默认分支顶端 —— 物化和装依赖能不能对得上要单独验。
`raw_payload.pr.base_ref_name` 记了分支名，筛选的判断留给 E1-T5。

### 七、又踩了一次"管道吃掉退出码"

§8.8 坑 ① 记的是 `cmd | tail` 的退出码永远是 `tail` 的。这次是
`python -m cli.mine ... | tee log`：挖掘明明失败了、`cmd_run` 也返回了 1，
外面看到的却是 `EXIT=0`。写进这里是因为**它已经在两个不同的任务里各咬了一次**。

## 8.10 清洗与预筛落地实录（2026-09-09，E1-T5）

> **本节是追加的实现记录**，没有改动 §8.1 ~ §8.9 的任何一条。

工具：`app/benchmark/{cleaning,prescreen}.py` + `app/infrastructure/llm.py` +
`python -m cli.prescreen {clean,score,report,export-review}`
（`make prescreen-clean` / `make prescreen`）。
打分存档和人工核对对照表在 `datasets/prescreen/`。

### 一、终点是 PRESCREENED，不建题目

E1-T5 把候选从 `DISCOVERED` 推到 `PRESCREENED` 或 `REJECTED`，**不把候选变成题目**。

理由很硬，不是取舍：一道题必须有 `pass_to_pass`，而 §7.10 写死了
"P2P 候选池来自验证流水线 S4 的全量报告"—— P2P 只能靠真跑一遍测试得出来，
而这条命令一个容器都不起。依赖图上 `E1-T5 → E8-T2` 正是这个意思，
建题（含派生 P2P、跑验证）是 E8-T2 的活。

### 二、脱敏剥三样，**普通代码块一律保留**

剥掉的：本仓库的 PR / issue / commit 链接、40 位裸 commit hash、`diff --git` 补丁块
（含没有 `diff --git` 头、只有 `--- / +++ / @@` 三件套的那种）。

**保留的：普通代码块、traceback、短哈希、指向别的仓库的链接、`#123` 裸引用。**

保留这一条和剥掉那一条同样重要。issue 里的代码块**绝大多数是复现代码和报错日志，
不是修复方案**，而贴复现代码恰恰是好 issue 的特征 —— 一律剥掉会把题面剥废。
"这段代码是不是修复方案"没有正则形状，交给 LLM 预筛判：§7.2(7) 那张表里
"Issue 里直接给了修复代码"写的正是"正则 + LLM 预筛"，本来就是两道防线。
实测这道分工是对的：正则一条泄题都没剩下（80 条全过），而 LLM 又挑出 12 条
"正文直接给出修复代码"的。

**顺序有讲究：先剥补丁块，再剥链接和哈希。** 反过来的话，补丁块里的
`index 3f2a1c9e..b7cf0697` 会先被哈希规则改写，补丁块的正则就对不上了，
**整块补丁原样留在题面里，而且不报错**。

**一处和 §7.9 的表面张力，不是矛盾**：导入校验器（`schema.py`）**故意不查裸
commit hash**，因为用户贴报错日志时带哈希很常见，按那个拒收会误伤好题；
而这里要剥掉它。两者方向一致：**建题时脱敏，导入时不拒收。**

### 三、拆补丁的口径和 `cli/golden.py` 完全一样

按整份 `DEFAULT_PROTECTED_PATTERNS` 劈，不是只用 `TEST_CODE_PATTERNS`。三个理由：

1. **`conftest.py` 必须跟着测试走。** 一个 PR 常常同时加一条测试和它要用的
   fixture。conftest 落到 `code_patch` 里的话，打上 `test_patch` 之后新测试会因为
   fixture 不存在而 ERROR —— 看起来像"这道题的 F2P 挂了"，其实是我们劈错了。
2. **`schema.py` 就是这么校验的**：`test_patch` 每条路径都必须命中
   `DEFAULT_PROTECTED_PATTERNS`，`gold_patch` 一条都不能命中。口径不一致的话，
   劈出来的东西根本导不进去。
3. Golden 题也是这个口径。两套口径的话，"这里算测试、那里算源码"迟早出事。

**按"段"取舍，不按行**（复用 E3-T3 的 `iter_diff_sections`）：删掉 hunk 里的几行
之后，hunk 头 `@@ -a,b +c,d @@` 声明的行数就和实际对不上，`git apply` 报 corrupt patch。

跨界的那一段（`src/x.py` 改名成 `tests/x.py`）**两边都不放**，按协议 C-62 整个丢弃，
并记进报表 —— 记下来是为了让"这道题为什么劈不出来"看得见。

**补丁不进 `raw_payload`**，落 `var/mining/patches/<repo>/<pr>.{full,test,code}.patch`。
原始 diff 也留一份：只留劈完的，劈错了就再也看不出错在哪。

### 四、抽出来的是**候选** F2P，不是 `fail_to_pass`

§7.2(5) 规定候选必须经实测证伪（base + test_patch 上必须 FAILED/ERROR，
加 gold 之后必须 PASSED）。字段名和文案都写死"候选"就是防止下一个人当定论用 ——
用错了不会报错，只会让解决率莫名其妙偏低。

抽法：对每个 hunk 重建**改完之后的样子**（上下文行 + 新增行），边走边记
"现在在哪个类、哪个测试函数里"，只要函数名下有任何一行是新增的就记成候选。
这样能同时抓到**整条新加的测试**和**在已有测试里加了断言**——
后者的 `def test_x` 是上下文行，只扫新增行里的 `def test_` 会把它整批漏掉，
而那恰恰是最常见的 bugfix 形状。

类名从 hunk 头 `@@ ... @@ class TestFoo:` 那截上下文里取（git 白送的），
改的是类中间某个方法时全靠它。参数化用例只给基名，靠 E4 的用例 ID 归一化去对。

**踩到的一个坑：空白新增行不算"这条测试被改了"。** 两条测试之间那个 `+` 空行属于
上一条测试的尾巴，算进去的话"在 A 后面新加一条 B"会连 A 一起报成候选 ——
而 A 一个字都没动、在基线上本来就通过，当成 F2P 会让验证流水线判
`F2P_NOT_FAILING`，**整道好题被丢掉**。

### 五、LLM 客户端放 `app/infrastructure/`，是被分层逼的

两个地方要它：E1-T5 的预筛（`app.benchmark`）、E6-T2 的失败归因（`app.attribution`）。
而 import-linter 的分层里 `app.attribution` 在 `app.benchmark` **下面**一层 ——
放 benchmark 里的话 attribution 根本 import 不到，最后只能再抄一份。

不装 SDK：`httpx` 早就是依赖，要的只有一个 `POST /chat/completions`，
DeepSeek / OpenAI / DashScope / 月之暗面都认这套。装 SDK 换来的是多两个包要跟着升级，
而真正难的部分（缓存、重试、记账）它们都不管。

**模型只当筛选器，绝不当裁判**（`AGENTS.md` §5.1）。预筛分数不参与任何解决率计算，
它只决定"这条候选值不值得再往下走"；题目立不立得住由 E1-T3 的八步验证说了算，
那一步一个模型都不调。筛错了最坏是少一道题，判错了才会让排行榜不可信。

**温度固定 0**：同一条候选今天 4 分、下个月 2 分的话，这个分数没法当门槛用。

**一个真踩到的坑：`httpx` 默认会读 shell 的代理变量。** 这台机器的
`ALL_PROXY=socks5h://...`，httpx 认它要装 `socksio`，没装就在**建客户端时**
直接 ImportError —— 错误信息只说"缺个包"，完全看不出是环境变量带进来的。
而且国产模型（DeepSeek、通义）本来就不该走境外代理。改成
`trust_env=False` + 显式的 `LLM_HTTP_PROXY` 配置项，让"走不走代理"成为一个可见的决定。

### 六、实测：80 条候选跑一轮

清洗（不花钱）：**80 条全部清洗成功**，取不到 diff 0 条、劈不出补丁 0 条、
**脱敏后残留泄题 0 条**（AC 那条正则断言），共剥掉 38 处链接 / 哈希 / 补丁块。
抽不出候选 F2P 的 10 条（多是只改了 fixture 或参数化数据），候选 F2P 中位数 1 条。

打分（DeepSeek `deepseek-chat`，温度 0）：

| 分数 | 条数 | | 分流 | 条数 |
|---:|---:|---|:---|---:|
| 5 | 47 | | PASS | 53 |
| 4 | 14 | | REVIEW | 14 |
| 3 | 1 | | REJECT | 13 |
| 2 | 14 | | | |
| 1 | 2 | | | |
| 0 | 2 | | | |

六个档都有分布，不是全挤在一格。模型挑出**泄题 12 条、过度指定 4 条、
题面不自足 10 条、定位不了 5 条**；规则那一侧（不问模型就能判的）另标了 12 条。

用量：80 次调用，输入 14.8 万 token、输出 5.3 千 token，一轮下来几毛钱。
**回答按 `(模型, 消息, 温度, prompt 版本)` 缓存**，重跑不再付钱 ——
`PROMPT_VERSION` 必须进 key，改了 prompt 忘了升版本会读到旧回答，
那会让"我改进了 prompt"完全测不出来。

一处值得记的观察：有几条模型在 `reason` 里写"轻微泄题"，`leaks_fix` 却填 `false`
（例如 #1489、#3508）。**没有去调 prompt 把它抹平** —— 这种边界分歧正是
AC 要求"抽 20 条人工核对"要发现的东西，先让人看到，再决定要不要动 prompt。

### 七、分流规则里加了一条 §8.4 没写的

§8.4 只按分数分流（`<2 丢弃；2~4 REVIEW_REQUIRED；≥4 通过`）。实现里加了
**泄题一票否决**：`leaks_fix=true` 一律 REJECT，不管分数多少。

理由：模型说"issue 里已经给了修复方案"而我们还把它当题目，那就是拿答案考人 ——
那道题会被所有 Agent 满分通过，**而排行榜上看不出任何异常**。
分数是连续量、可以擦边；泄不泄题是个是非题，不该被一个 5 分盖过去。
实测这条真的起了作用：12 条泄题里有 3 条模型给了 4 分以上。

### 八、人工核对：一致率 90%，但错的**全在一个方向上**

抽 20 条分层复核（6 个分档全覆盖，固定种子 20260909），一致 18 条 —— **90%，
AC 门槛 80%，达标**。原始对照表在 `datasets/prescreen/review-2026-09-09.csv`，
统计在同目录的 `review-result-2026-09-09.json`。

一致率这个总数意义有限，**按模型判定拆开才看得出问题**：

| 模型判定 | 条数 | 一致 | 一致率 |
|:---|---:|---:|---:|
| REJECT | 5 | 5 | **100%** |
| REVIEW | 4 | 4 | **100%** |
| PASS | 11 | 9 | **82%** |

**两条错的都在 PASS 那一格**，而且分数差也是单向的：人评比模型低 8 条、
持平 12 条、**比模型高 0 条**，平均低 0.7 分。也就是说：

> **模型系统性偏松，但从不偏严。** 它不会冤枉好题（REJECT 一条没错），
> 但会放过烂题。

这个方向对我们不利。§8.4 的分流规定 `≥4 分 → 直接进 VALIDATING`，
**PASS 是唯一不经人眼的那一格**，而错误恰好全长在那里。

### 九、漏掉的那一条泄题：修复方案是用大白话写的

`pallets/click#2933`，模型给 5 分 PASS、`leaks_fix=false`，人评 1 分建议 REJECT。

官方补丁只有一行：

```diff
                 sys.stdout.flush()
+                sys.stderr.flush()
```

而 issue 正文末尾写着："It would only be flushing at the end of an invoke,
which matches what Python does at the end of program execution. ... So why not add it?"

**修复方案就在题面里，但它是一段自然语言论证，不是代码。** 没有代码块、
没有 `diff --git`、没有 PR 链接、没有 commit hash —— 正则层**无形可匹配**
（它该剥的 5 条 PR 链接一条不落全剥了，那部分没失职）。

这正是 §7.2(7) 把这一条写成"正则 + LLM 预筛"两道防线的原因，而**第二道漏了**。
漏的原因八成在 prompt 的措辞：现在问的是"是不是已经把修复方案给出来了
（**贴了改法、指明了改哪一行**）"，括号里那两个例子都是代码/行级的形状，
把模型往"找代码块"上带偏了，一段"你就在结尾加个 flush 呗"的论证不像"贴了改法"。

**没有当场改 prompt。** 改了的话，90% 这个数字描述的就是一份没人复核过的
prompt，比现在更糟 —— 要改就得连着重跑一轮 20 条人工核对。这个取舍留给
E8-T2 做数据集生产时决定，那时才真正要回答"PASS 能不能免检"。

### 十、给 E8-T2 的三个数

数据集生产时要用的，一并记在这里：

1. **PASS 免检的假通过率：2/11 ≈ 18%**（本次抽样）。§8.4 让 PASS 直接进
   VALIDATING，这就是那条路的风险面。
2. **REJECT 的误杀率：0/5**。丢掉的那些可以放心丢，不用回捞。
3. **候选 → PASS 的收率：53/80 ≈ 66%**，再乘上验证流水线的通过率，
   才是"一个仓库能出多少道题"。click 的候选池按 E8-T1 估是 77、实挖 80。

## 8.11 L1 数据集生产落地实录（2026-09-10，E8-T2）

> **本节是追加的实现记录。** 它**给 §7.2(4) 的测试命令补了两个参数**（理由在第六节），
> 其余 §7 / §8 的条文一条没动。

工具：`app/benchmark/assembly.py` + `python -m cli.promote {probe,assemble,export-review,import-review,show,report}`
（`make promote-probe` / `make promote-assemble` / `make promote-review` / `make promote-report`）。

### 一、任务卡只有一行，AC 是开工前定的

`10-tasks-plan.md` 上这张卡只写了 `P1 · C:L · E:3d · 🐳`，没有 Goal / Output / AC。
定下来的七条见任务卡。三条最要紧的：**P2P 必须是实测派生的**、
**每一道进 `benchmark-dev` 的题都要过人眼**、**漏斗每一层的淘汰原因要有分类计数**。

### 二、探测轮是测量，不是验证

顺序是：

    探测（2 个容器）→ 组装定稿题目、算出 content_hash → 八步验证（3 个容器）

探测直接调 `execute_tests` 跑两轮全量（空补丁、gold 补丁），**不套** `validate_task`。
一开始的设计是"探测 = P2P 传空的 validate_task"，改掉是因为第三节那个发现：
F2P 也得从报告里推导，而 S5 会在推导之前就把题拦下来。

这不违反 §7.10 那条"跑测试复用 `execute_tests`，没有第二套实现"——
复用的正是 `execute_tests` 本身。八步流水线是**给定稿题目下结论**用的，
而探测手里那份题还没有 P2P、`content_hash` 和定稿题目对不上，它本来就不是验证。

组装和验证之间不许互相插手：**验证一个字都不改题目定义**。
`content_hash` 是数据集快照的身份证（§7.5），验证过程顺手填 P2P 的话，
"同一个数据集版本"就不再成立——§7.10 拒绝过同样的做法（不稳定用例只报不改）。

### 三、F2P 得从两份报告推导，候选清单只是线索

§8.10 第四节写的是"参数化用例只给基名，靠 E4 的用例 ID 归一化去对"。
**这个假设没兑现。** `ParsedReport.resolve()` 的三层匹配是精确相等、备选 ID、
路径后缀，**没有"基名 → 参数化变体"这一层**。于是：

    候选 F2P：tests/test_options.py::test_usage_show_choices
    报告里的：tests/test_options.py::test_usage_show_choices[text choices]  ← FAILED
              tests/test_options.py::test_usage_show_choices[int choices]   ← FAILED
              …

基名一律判 `MISSING`，题目被误判成 `F2P_NOT_FAILING`。实测 click 的 main 分支
最老五条候选**全军覆没**，而它们都是好题。

**修在组装侧，不动判定路径。** 一个基名对应 N 条状态可能各不相同的用例，
`resolve()` 只能返回一条，硬选一条等于把 A 的结果安到 B 头上；协议 C-11
也是把每条声明的 ID 当**一条**用例。所以题目里存**具体的**用例 ID
（`select_f2p()` 按 §7.2(5) 实测证伪：基线上失败、打完 gold 通过），
判定那一侧就永远是精确匹配。

### 四、P2P 取两轮通过集的交集

§7.2(6) 的定义本来就是"两边都通过"。只用 S4 那一半的话，凡是 gold 顺带改了行为的
用例都会在 S8 被记成 `GOLD_REGRESSION`——**好题被丢掉，而且诊断是错的**：
gold 没有回归，是我们把不该当护栏的用例塞进了护栏。

为此给 `validation.py` 的 `evidence["after_gold"]` 补了一份 `p2p_candidate_pool`，
和 S4 那份对称。这是纯追加，不改任何判定逻辑。

抽样策略按 §7.7 **由实测决定，不预设**：探测轮量到 click 全量套件 4 秒左右
（远低于 180 秒阈值），所以 30 道题全部走 `full`，`seed` 为 null。
`module_and_random` 也实现了并有单测覆盖——E8-T3 上 sqlfluff / LLaMA-Factory 一定会走到。

### 五、从报告里读出来的用例 ID，pytest 自己不认

**这一条最值得记，因为它的失败方式是"每次评测颗粒无收"。**

pytest 生成参数化用例 ID 时会把非 ASCII 字符转义成 `\u5b57` 这种文本，
但它写 junit 报告时写的是真正的字 `字`：

    报告里：  tests/test_termui.py::test_getchar_windows[True-字]
    pytest：  tests/test_termui.py::test_getchar_windows[True-\u5b57]

click 2085 条用例里有 7 条这样。**混进 `pass_to_pass` 一条**，正式评测时
pytest 就报 `ERROR: not found` 并以 **usage error（退出码 4）**收场，
一条用例都不跑——而表现只是"测试报告是空的"。

判据是"全是 ASCII"（`round_trippable()`）：转义只发生在非 ASCII 字符上。
`\x1b[31mred` 这种**纯 ASCII 的转义文本两边写法一致，要留着**，
误杀它的话 click 那批 ANSI 用例会整批退出回归护栏。

顺带验掉了另一个担心：`full` 策略下 2055 条用例 ID 拼成 **157 KB 的 argv**，
容器照跑不误（exec 形式传 argv，不过 shell）。

### 六、环境上补了三件事，两件是给 §7.2(4) 的测试命令补参数

| 补什么 | 不补会怎样 | 实测 |
|:---|:---|:---|
| `apt_packages: ["less"]` | `PAGER=less` 的 24 条参数化用例恒挂 | 它们退出 P2P 护栏；**5 条候选的 F2P 落在 pager 测试上**，会被误判 `GOLD_NOT_FIXING` |
| `--continue-on-collection-errors` | 一个文件收集出错就中断整轮，junit 零条用例 | 新测试 import gold 才加的符号是**正常题目形状**；#3695 从"环境跑不起来"变回一道有 9 条 F2P 的好题 |
| `--timeout=60`（`pytest-timeout`）| 一条挂住的测试拖垮整轮，容器跑到 480 秒被杀 | #2775 修的就是"pager 子进程不退出"，新测试在 base 上卡在 `os.waitpid`；加上之后 15.7 秒跑完，那条如实记成 FAILED |

后两条对**正式评测**同样是改善：被测 AI 的补丁弄坏某个无关文件的 import 或者
写出一个死循环时，不带这两个参数会拿到零条用例、判不出任何东西。

还有一条是 click 专有的，写进它自己的配方 `test_args`：
`-W ignore::pytest.PytestRemovedIn10Warning`。click 的 `pyproject.toml` 写了
`filterwarnings = ["error"]`，把 pytest 9 对"`parametrize` 收到 `itertools.chain`"的
弃用警告升成收集期错误。命令行 `-W` 优先级高于 ini，**只关掉 pytest 自身 API 的
这一类警告**，不动 click 运行时的任何警告断言——等于把 2024 年那会儿 pytest 8
的行为还原回来。2024-10 的 base commit：收集 0 条 → 615 条。

这正是 §7.2(3) 早就点出的"跨越较长时间跨度的任务可能需要不同依赖版本"。
现在用一个参数覆盖了两年窗口，**没有**动用它给的分桶方案；
再碰到别的不兼容就该按 `(repo, 版本区间)` 分桶了。

### 七、零用例的报告不许怪题目

上面那个 pytest 9 的坑第一次出现时，十条候选全被判成 `F2P_NOT_FAILING`——
看起来像"这十道题都是坏题"，其实一道都没问题。

`F2P_NOT_FAILING` 的意思是"这些测试在修复前就通过了"，而事实是
"这些测试压根没执行"。**两者的处置完全相反**：前者该丢题，后者该修环境。

所以 `validation.py` 的 `_run_suite` 加了一道判断：报告里一条用例都没有 →
`REVIEW_REQUIRED`，并把容器输出尾部记进证据。按的是 §7.10 已经立过的规矩：
七个 reason code 没有对应项时不许硬套一个。

### 八、实测漏斗（`pallets/click`，2026-09-10）

| 层 | 条数 | 掉队原因 |
|:---|---:|:---|
| 挖掘候选（E1-T4） | 80 | |
| 预筛 PASS + REVIEW（E1-T5） | 67 | REJECT 13 |
| 抽得出候选 F2P | 59 | 抽不出 8（多是只改 fixture 或参数化数据）|
| **探测通过** | **51（86%）** | `F2P_NOT_FAILING` 5、`ASSEMBLY_FAILED` 3 |
| 入库 | 30 | 按 PR 号等距抽，覆盖两年窗口 |
| 八步验证通过 | 29 | `REVIEW_REQUIRED` 1（issue 正文 173 字）|
| **人工终审收下** | **21** | 否掉 9，分类见第九节 |
| **定档** | **22** | 终审后从备用池补 1 道 easy（#3642）|

三条 `ASSEMBLY_FAILED` **全是同一个原因**：`issue_body` 里有指向 PR 的链接。
这是**清洗和导入校验的口径差**：E1-T5 按"本仓库的链接"剥（§8.10 第二节明写
保留指向别的仓库的链接），而 `schema.py` 按"任何 `/pull/\d+` 链接"拒收，
照的是 §7.9 的原文（没写限本仓库）。3/59 ≈ 5%，先如实记着，没有动任何一侧。

五条 `F2P_NOT_FAILING` 里有两条是新分出来的一格 **`base 上收集不出来`**：
新测试 import 了 gold 才加的符号，base 上整个测试文件收集失败。
**协议 C-12 明写 MISSING 既不算通过也不算失败**，所以它当不了 F2P，题目立不住。
这是现有判定语义表达不了的一类题，单独记一格是为了让"丢的是哪一类"看得见。

`base_commit` 全在镜像里（59/59），S2 一条都没淘汰；
**`stable` 维护分支能用**——物化、装依赖、跑测试和默认分支表现一致，
§8.9 第六节留的那个问题可以关掉了。

### 九、PASS 不免检，人工终审放在验证**之后**

§8.4 写的是"≥4 分直接进 VALIDATING"。验证照跑，但**题目进 `benchmark-dev`
之前加一道人工终审**（`cli.promote export-review` / `import-review`）。

理由是一条硬的：**验证流水线对泄题完全无感。** 题面里写着修复方案的题，
八步会全过、判 VALID——它甚至更容易过，因为 gold 一定修得好。
§8.10 第十节量出来的 PASS 假通过率 18%，这 18% 一条都不会被验证挡掉。
§8.1 给 L1 写的来源本来就是"自动挖掘 + **人工终审**"。

**顺序反过来才是浪费**：先人工再验证要看 59 条，其中一大半会被淘汰。
先验证再人工只看活下来的，按 §8.4 的 ≤3 min/题算，30 题一个半小时。

没有动预筛的 prompt。§8.10 第九节说了改 prompt 就得连着重跑一轮 20 条人工核对；
这次终审本身会产出新的一批对照数据，要不要调留给 E8-T3。

人工否掉走 §7.4 的状态机 `REVIEW_REQUIRED →（人工）→ INVALID`，
**不写 `invalid_reason_code`**：七个 code 说的都是"跑出来不合格"，
而人工否掉的理由是题面问题，硬套一个是在编。理由原文记进候选的
`raw_payload.final_review`。

**实测：30 道全看完，收 21 否 9**（之后又补审 1 道，定档 22）。

| 否掉的类型 | 条数 | 题号 |
|:---|---:|:---|
| 泄题（自然语言，正则无形可匹配）| 5 | #2517 #2622 #2800 #3299 #3508 |
| 题面不足以支撑 F2P 的要求 | 3 | #2796 #3391 #3473 |
| 不自足（原因写在别的 issue 里）| 1 | #3235 |

对上 LLM 预筛的分档看：

| 预筛判定 | 条数 | 人工否掉 | 否掉率 |
|:---|---:|---:|---:|
| PASS | 27 | 7 | **26%** |
| REVIEW | 3 | 2 | 67% |

**§8.10 第十节抽样量到的 PASS 假通过率是 18%（2/11），这一轮全量是 26%（7/27）——
方向一致，数还更大**，而且否掉的七条里有**两条是满分 5.0**。
"PASS 能不能免检"这个留给本任务的决定，答案是不能。

**难度分布是终审的副作用，得回头看一眼。** 收 21 条之后 medium 17 / hard 4，
**一道 easy 都没有** —— 唯一那道 easy 恰好被终审否了。L1 的用途是 Agent 适配器联调，
全是 medium 以上意味着每次冒烟都要跑一道中等难度的题。
从备用池补审了 1 道 easy（#3642，1 文件 3 行）收下，定档 **22 道**。
教训是：**终审是逐条判的，但难度/语言这些分布要在终审之后整体再看一遍**，
不然会得到一个"每条都合格、整体却偏"的题库。

### 十一、只读题面判不出好坏，得对着 F2P 和 gold 补丁看

第一轮终审只读了题面，收 24 否 6。第二轮对着**实际 F2P 清单和官方补丁**再看一遍，
又否掉三条 —— 而且是两类只读题面**根本发现不了**的问题：

1. **题面不足以支撑 F2P 的要求。** #2796 题面只说"希望 `Choice` 支持 Enum"，
   而 F2P 还要求支持自定义类型、改变方法签名，也没说枚举按名称还是按值匹配；
   #3391 的 F2P 要求 `capture="fd"/"sys"` 参数和嵌套捕获，题面一个字没提；
   #3473 的 F2P 强制要求 `"Positional arguments:"` 这个标题、还要排在 `"Options:"` 之前，
   题面没约定任何格式。**被测 AI 完全照题面做也会挂** —— 这种题测的是"猜不猜得中官方设计"，
   不是"会不会修 bug"。
2. **大白话泄题要对着补丁才认得出。** #2800 题面说"创建 context 时没有用 `with`，
   必须调用 `__exit__` 清理资源"，官方补丁正是给 `make_context()` 加上 `with`；
   #3299 题面末尾建议"先检查 `default_value` 是不是字符串再和 `""` 比"，
   官方补丁就是加了 `isinstance(default_value, str)`。单看题面像是在描述现象。

所以 `export-review` 的 CSV 补了 `fail_to_pass` 和 `gold_patch` 两列。
gold 补丁进这张表不算泄题：它给复审人看，从来不下发给被测 AI（协议 C-44）。

**两个第一轮犯的错，记在这儿免得下次再犯**：

- **判重复题只比了正文前 400 字。** 当时把 #3508 和 #3126 写成"正文完全相同"，
  实际是 5283 vs 6276 字 —— 后者追加了一段回归分析，而**追加的那段本身就泄题**
  （给出"保留原分隔符、转义换行"的改法），还暴露了 #3126 的编号和短提交哈希。
  结论没变（都该否），但依据错了，按错依据写进存档就等于给下一个人一条假线索。
- **`import-review` 漏了状态机的一条边。** 第一版只实现了
  `REVIEW_REQUIRED →（人工）→ INVALID`，`→ VALID` 那条没写。
  结果是收下 21 条、库里只有 20 道 `VALID` —— 被人看过并且收下的题**永远卡在
  `REVIEW_REQUIRED`**，而 E1-T6 按 `VALID` 挑题，它会静悄悄地掉出数据集。
- **`assemble --limit` 的语义反了。** 第一版把它当成"这一次加多少道"，
  于是同一条命令跑两遍会从剩下的候选里接着做，把数据集从 30 道撑到 51 道 ——
  **不产生重复题，但集合悄悄变大了**，而这比重复更难发现：重复题一眼看得出来，
  多出来的题看起来跟正常入库的一模一样。改成"数据集最终要有多少道"之后，
  跑第二遍是空操作，想补题把数字调大即可。

### 十、E8-T2 到哪一步为止

**交付 `benchmark_tasks` 里的题，不碰 `benchmark_sets`。**

| | E8-T2 | E1-T6 |
|:---|:---|:---|
| 写哪张表 | `benchmark_tasks`、`task_candidates` | `benchmark_sets`、`benchmark_set_items` |
| Oracle / Noop | **每道题一份**（S4 = Noop 哨兵，S6/S7 = Oracle 哨兵，§7.10）| **数据集级门禁**，不达标拒绝发布 |

`benchmark_tasks` **没有 `dataset_id` 列**——数据集版本化整件事就是那两张快照表，
而它们是 E1-T6 卡片明写的 Goal。题目的归属只写在 `raw_definition.dataset_id` 里，
E1-T6 按它挑题，再连同 `content_hash` 一起冻进 `benchmark_set_items`。

**这条边界有一个直接后果**：AC 里那条"跑一次 Oracle / Noop 实测"没做。
`evaluation_runs.benchmark_set_id` 是非空外键，走一次正式评测就得先建一行
`benchmark_sets` —— 那正是 E1-T6 的 Goal。每道题的哨兵证据已经在验证证据里了
（S4 空补丁 = Noop，S6/S7 gold 补丁 = Oracle），端到端那一次留给 E1-T6 连同门禁一起做。

## 8.5 中文优先的具体落实
- 数据集统计页展示 `issue_language` 分布，`zh` 占比作为公开指标；
- Issue 为英文但仓库为国产项目时，**不做机器翻译**（翻译会引入信息失真，损害基准可信度），如实标注；
- 平台 UI、报告、任务集元数据全中文；
- 若 `zh` 占比不足，Plan B：在 L0/L1 中人工构造更多中文 Issue 任务（这些是我们自己写的，语言可控）。

### Plan B 落地实录（2026-09-18，E8-T3 追加）

**结论**：`benchmark-cn-v1@v2` 已发布，41 道题面全部是中文（40 道改写 + tortoise-2255 原生中文），
门禁 Oracle #141 41/41、Noop #142 0/41、0 平台故障、0 次 `container_sigkilled_without_oom_flag`。
v1 一行没动。

**做了什么**：`cli/localize.py`。`draft` 把 VALID 的自建题导成对照表（英文原文 + 空的 `zh_title` / `zh_body`），
`import` 把复核过的中文题面导回库。导入只换 `issue_title` / `issue_body`，`issue_language` 置 `zh`，
`tags` 加 `issue-rewritten-zh`，`content_hash` 重算；测试补丁、gold 补丁、F2P / P2P、环境、预算一个字不动。
所以八步验证不用重跑 —— 验证验的是测试和补丁，不是题面 —— `validation_state` 保持原样。

**改写规则**：不是翻译（本节第二条禁止机器翻译，理由是失真）。对着原 issue、F2P 用例名和官方补丁，
用中文重写一段"像中国用户报 bug"的题面：原文没说的不加（加了等于泄题，
`docs/review-2026-09-14-parked20.md` 否掉的 8 道就是判据），原文说了的不漏（漏了 F2P 撑不住）。

**谁写的、谁审的（报告必须照这句披露）**：初稿 Claude Opus 5（AI），复核 Codex（AI，逐题写了保留了什么、
没加什么），用户核对复核表后拍板导入。**没有人逐题改写，"人工"两个字不能用。**
对照表 `datasets/benchmark-dev/localize-2026-09-18.csv` 进版本库，`drafter` / `reviewer` / `verdict` / `note`
四列是唯一记录：41 行里 40 ACCEPT、1 REJECT（tortoise-2255 原题就是中文，REJECT 只表示跳过改写，题照常在 v2 里）。

**MET-05 的账怎么变**：语言分布从 v1 的 4/41 中文（10%）变成 v2 的 41/41（100%），两个发布版合计 41/100。
但这 40 道只算"题面是中文的题"，不算国产项目或中文社区的题（仓库还是 click 和 tortoise），
质量报告里"其中改写成中文"单独一列（`datasets/quality/quality-2026-09-18.md` 第三节），不许并进"中文"一列不说来源。
MET-05 降级线"自建中文 ≥40"按题面语言算到线（41），达标线 ≥60 仍未达标 —— 自建题总数就是 41。

**版本和摘要**：v2 快照 `sha256:19a2508ae0e1…`，指纹 `datasets/manifests/benchmark-cn-v1@v2.json`
（dirty=true，门禁在未提交的工作区上跑，和前几版一样）。v1（`1701c943ff5b…`）不动：
`dataset verify --version v1` 报"快照摘要一致、内容变了 40 道"，这是预期内的 —— 题面变了 `content_hash` 就变，
v1 是英文题面的存档，不再等于现在的库。

**重灌**：`promote assemble` 组装出来的永远是候选里的英文原文，重灌要在 `park-unreviewed` 之后、
`dataset-stage` 之前跑 `cli.localize import`（AGENTS.md §12 已写），不跑的话题面退回英文、摘要和 v2 对不上，而且不报错。

## 8.6 SWE-bench Verified 子集导入（服务 MET-01）
- 用官方数据集（HuggingFace `princeton-nlp/SWE-bench_Verified`）的字段直接映射到我们的 Schema：`instance_id→task_id`、`repo`、`base_commit`、`problem_statement→issue_body`、`patch→gold_patch`、`test_patch`、`FAIL_TO_PASS→fail_to_pass`、`PASS_TO_PASS→pass_to_pass`、`environment_setup_commit→environment_id 分桶依据`；
- 环境优先复用**官方评测镜像**（`swebench/sweb.eval.x86_64.<instance_id>`），拉不动时退回自建 env spec；
- 抽样：固定种子分层随机（按 repo 分层）取 50–100 题；
- 这批任务**只用于校准**，不混入 `benchmark-cn-v1` 的解决率统计。

### 落地实录（2026-09-15，E1-T7）

> **本节是追加的实现记录**，§8.6 上面四条一条没动。工具：`app/benchmark/swebench_import.py`
> + `python -m cli.swebench {fetch,screen,sample,estimate,pull,mirror,import,report}`
> （`make swebench-*`），验证走 `python -m cli.validate run --dataset swebench-verified-subset --scope declared`。
> 数据集 slug 单独是 `swebench-verified-subset`（§8.1 的 L2'），`dataset_id` 同名。

#### 一、官方数据怎么拿：datasets-server 的 JSON 分页，不装 pyarrow

HF 上的官方文件是 parquet，读它要 pyarrow（几十 MB 的依赖，只为这一件事）。
改走 `https://datasets-server.huggingface.co/rows`（一页最多 100 行，带 `truncated_cells`
标记）：500 行 7 页，实测一格都没被截断，最长的补丁也完整。落 `var/cache/swebench/verified.jsonl`，
`verified.meta.json` 记行数、sha256 和 HF 仓库的 git 修订号（`c104f840cc67…`），
以后有人问"你导的是哪一版 Verified"，答得出来。
这台机器过代理时 100 行一页常超时，默认改成 50 行一页 + 退避重试。

#### 二、字段映射：§8.6 那张表逐项落实，另加两条清洗

| 官方字段 | 落到 | 备注 |
|:---|:---|:---|
| `instance_id` | `task_id` | 格式本来就是 `{owner}__{repo}-{pr}`，`TASK_ID_PATTERN` 直接认 |
| `repo` | `repo_name`；`repo_url` 拼 `https://github.com/{repo}.git` | |
| `base_commit` | `base_commit` | |
| `problem_statement` | `issue_body`；第一行兼作 `issue_title` | 官方没有单独的标题字段 |
| `hints_text` | **不用**，`hints_text=null` | 那是修复前的 PR 讨论，可能带答案；§7.1 默认就是对齐 Verified 不给提示 |
| `patch` | `gold_patch` | |
| `test_patch` | `test_patch` | |
| `FAIL_TO_PASS` | `fail_to_pass` | 官方存的是 JSON **字符串**，要先解开 |
| `PASS_TO_PASS` | `pass_to_pass` | 同上；**剔掉不是用例 ID 的条目**（见下） |
| `environment_setup_commit` | 环境分桶依据 | 官方镜像一题一个，桶只在退回自建时用；tag 里记 `swebench-env-<sha12>` |
| `version` / `difficulty` | tag | `swebench-version-5.1`、`swebench-difficulty-15-min-1-hour`；§7.8 的派生难度照算，不被官方标注替代 |

两条清洗：

1. **`PASS_TO_PASS` 里有不是用例的东西。** `pytest-dev__pytest-5262` 和 `-7521` 的 P2P 里各有一条
   `[100%]` —— 官方日志解析器把进度百分比当成了用例名。判据只有一条：不含 `::` 的不是 nodeid，剔掉并记数。
2. **P2P 不记 `p2p_sampling`。** 官方的 P2P 是"测试补丁碰到的文件里、修复前后都通过的用例"，
   不是 §7.7 那种抽样，硬填一个 `strategy` 是在编。

#### 三、离线筛：500 → 175，先剔判定引擎读不了和沙箱跑不了的

| 格子 | 数量 | 说明 |
|:---|---:|:---|
| `REPO_NOT_PYTEST` | 314 | django 231 + sympy 75：官方测试命令是 `runtests.py` / `bin/test`，不出逐用例 junit，官方靠解析终端日志判定，我们按 §7.2(4) 只吃机器可解析的报告；**requests 8**：见下 |
| `ISSUE_LEAKS_FIX` | 8 | 题面里带 PR 链接（7）或贴了 diff（1），§7.2(7) 的规则拒收 |
| `TEST_PATCH_NON_TEST_PATH` | 2 | `pytest-dev__pytest-5631` 改了 `testing/python/integration.py`、`sphinx-doc__sphinx-7910` 改了 `sphinx/testing/util.py`，按 C-42 不算测试文件 |
| `GOLD_TOUCHES_PROTECTED` | 1 | `pylint-dev__pylint-4661` 的 gold 动了 `setup.cfg`（C-64） |
| **池** | **175** | 9 个仓库 |

**`psf/requests` 是实测之后才排除的**：`psf__requests-2317` 全链路跑到 S4，官方 133 条 P2P
在 base 上就挂 52 条（`test_DIGEST_*`、`TestTimeout::*`……），8 条 F2P 里
`test_HTTP_302_ALLOW_REDIRECT_GET` 这类全打 httpbin.org。官方 harness 跑测试时有网，
我们的沙箱按 C-31 断网 —— 和 E8-T3 判 xorbitsai 不可用是同一个病，给沙箱开网不是解法。

#### 四、抽样：种子 20260915，按仓库分层，最大余数法，非空层至少 1 道

| 仓库 | 池 | 配额 |
|:---|---:|---:|
| astropy/astropy | 22 | 6 |
| matplotlib/matplotlib | 31 | 8 |
| mwaskom/seaborn | 2 | 2 |
| pallets/flask | 1 | 1 |
| pydata/xarray | 19 | 6 |
| pylint-dev/pylint | 9 | 3 |
| pytest-dev/pytest | 18 | 5 |
| scikit-learn/scikit-learn | 31 | 8 |
| sphinx-doc/sphinx | 42 → 40 | 11 |

层内用 `random.Random(f"{seed}/{repo}")` 打乱后取前 N 个，所以把总数从 50 调到 60，
前 50 道原封不动、只多出 10 道；每层没抽中的按打乱顺序记成"备选"，要补题从头取，不用换种子。
名单落 `datasets/swebench/sample-seed20260915-n50.json`（进仓库），`cli.swebench sample --check`
能核对文件和重算结果逐字相同。

**2026-09-16 名单重抽过一次。** 下午发现官方数据里有半截的用例 id（见七点六第 5 条），
筛子加了一条"F2P 全是半截 id 的归 NO_F2P"，sphinx 池从 42 缩到 40（`sphinx-doc__sphinx-8621`
和 `sphinx-doc__sphinx-8265`），池子 175 → 173。层内打乱是对整个池做的，池变了 sphinx 这一层的顺序就变了：
同一种子重抽，其他 8 个仓库的 39 道一道不动，sphinx 换掉 5 道（8120、8459、8595、8621、9602
出，10673、7757、8475、9320、9461 进）。**以仓库里的名单文件为准**，`--check` 对得上；
旧名单里那 5 道在库里的行已在 19:00 清掉（没有任何实验或快照引用它们），冻快照只含最终名单。

#### 五、官方镜像怎么用：三件事要绕，一个发现要记

官方镜像叫 `swebench/sweb.eval.x86_64.<instance_id>`，但 Docker Hub 不许仓库名里有 `__`，
官方换成了 `_1776_`、整个小写（`astropy__astropy-12907` → `…astropy_1776_astropy-12907:latest`）。
镜像里是 `/testbed` 下的仓库加一个 conda 环境 `testbed`。要在我们的执行器里跑它，三件事：

1. **conda 没激活。** 起容器不走 shell，`.bashrc` 里的 `conda activate` 不会执行。
   测试命令直接写 `/opt/miniconda3/envs/testbed/bin/python -m pytest …`，不依赖 `PATH`。
2. **`pip install -e .` 指向 `/testbed`，不是 `/workspace`。** 不处理的话测试 import 到的是镜像里
   没打补丁的代码，Oracle 必然 0%。处理办法是命令前加 `env PYTHONPATH=/workspace[/src|/lib]`：
   `PYTHONPATH` 排在 site-packages 前面，也就排在 editable 安装的 `.pth` / import hook 前面。
   flask、pytest 是 `src/` 布局，matplotlib 的包在 `lib/`，其余在仓库根（`IMPORT_ROOTS`）。
3. **编译产物只在 `/testbed` 里。** astropy / matplotlib / scikit-learn 的 `.so` 是建镜像时就地编译的，
   `git archive` 物化出来的工作区没有。`pre_test_command` 把 `/testbed` 里**被 git 忽略的文件**
   （`git ls-files --others --ignored --exclude-standard`）复制到工作区里不存在的位置：
   只拷 git 忽略的，就不可能盖掉源文件，也不可能把被测 AI 删掉的文件还原回来。
   `__pycache__` / `build/` 跳过。

三件事有没有真的绕过去不靠肉眼：gold 补丁只存在于 `/workspace`，测试要是 import 了 `/testbed`，
**Oracle 就过不了** —— 门禁本身就是证明。第 2 条另有一个直接的实测（`psf__requests-2317` 的镜像，
把工作区里的包挪到 `src/` 下模拟 flask / pytest 的布局，再改一个版本号）：

| 命令 | `requests.__file__` | 版本 |
|:---|:---|:---|
| 不加 `PYTHONPATH` | `/opt/miniconda3/envs/testbed/lib/python3.9/site-packages/requests/__init__.py` | 2.4.3（镜像里的）|
| `env PYTHONPATH=/workspace/src …` | `/workspace/src/requests/__init__.py` | 2.4.3+workspace |

不加的话被测 AI 改的代码根本没被 import，而且不报错。包在仓库根的仓库（astropy 等）
靠 `python -m` 自带的"当前目录排最前"就够了，加 `PYTHONPATH` 只是统一写法。

一个发现：**`/testbed` 的 HEAD 不是 `base_commit`**。2025 年后的官方 harness 建镜像时在 base 之上
又提交了一个叫 "SWE-bench" 的 commit（`psf__requests-2317` 实测只改文件模式，`chmod -R 777` 的结果，
一行内容都没变）。探测时按"base 是 HEAD 或 HEAD 的父提交"核对。

**官方镜像只能用在测试阶段。** `/testbed/.git` 是完整 clone，`git log --all` 能翻到修复。
Agent 阶段用的是 `bench-agent` 镜像、只挂我们物化的工作区，碰不到它；但谁要是把 Agent 放进官方镜像跑，
§5.3 的防泄题就破了。

#### 六、验证只跑声明的用例：`--scope declared`

§7.3 的 S4 跑全量套件，为的是从全量报告里派生 P2P 候选池（§7.2(6)）。官方题的 P2P 是官方定的，
不需要候选池；而 astropy / scikit-learn 的全量套件要跑几十分钟，三轮下来一道题一小时。
`ValidationRequest.suite_scope="declared"` 让 S4 / S7 / S8 都只跑 F2P ∪ P2P —— 和正式评测跑的
集合一样（C-17），对"这道题判得对不对"没有损失。证据文档 `task.suite_scope` 记着用的是哪一种，
证据结构版本升到 1.1。挖掘题**不要**用它，那样候选池是空的。

#### 七、镜像体积与网络：38.9 GB，这是这张卡真正的墙

`cli.swebench estimate` 只读 manifest：50 个镜像去重后 133 层、**38.9 GB**（压缩后下载量）。
基础层（ubuntu + build-essential + miniconda，0.66 GB）全部共享；env 层按 `environment_setup_commit`
分桶，同桶共享（sphinx 11 道只有 6 个桶）；matplotlib 一桶 2.4 GB（带 texlive）。
这台机器过代理拉 Docker Hub 的速度在 0.2–6 MB/s 之间晃，而且**会整条连接卡死**
（`docker pull` 26 分钟进账 1 KB/s），所以 `cli.swebench pull` 带 300 秒无进度即掐断重试，
拉完的层 docker 留着，重跑接着拉。镜像加速站（daocloud）对 `swebench/*` 返回 403，dockerd 自动退回直连。

#### 七点五、拉不动怎么办：两条实测出来的路（2026-09-16）

镜像站（`docker.1ms.run`）只对它缓存过的层快，没缓存的层它要先去 Docker Hub 抓，这期间给的是
0–11 KB/s；一夜下来 50 个镜像只拉到 2 个。换了两条路，都验证通过：

**路 1：Windows 侧下载，WSL 侧装载**（`cli.swebench fetch-blobs` + `load`，`scripts/swebench_fetch_loop.sh`）。
同一个 VPN 代理，从 Windows 走 `127.0.0.1:10808` 是 **14–15 MB/s**，从 WSL 走 `172.30.80.1:10808`
只有 0.08–1.2 MB/s，关掉 vEthernet 的 LSO 也没用 —— 瓶颈是 WSL 到宿主那一跳。`curl.exe` 是 Windows
自带的 curl，从 WSL 里能直接调，走 Windows 的网络栈；层下到 `D:\Documents\swebench-blobs`
（C 盘没空间，D 盘根目录 Windows 用户也写不了），WSL 从 `/mnt/d` 读回来校验 sha256、拼成 OCI 布局
流式喂给 `docker load`，不落中间文件。实测一个 420 MB 的层 27 秒。线路仍会抖（偶发整条挂死、
跳转后不认 Range），所以 30 秒低于 50 KB/s 就掐断续传、坏了就删掉重来、外面套循环。
git 镜像同理：matplotlib 8 个 base_commit 在 WSL 里拉了三轮都是坏包，Windows 的 `git.exe`
95 秒一个，拉完从 D 盘那个 bare 仓库 `git fetch` 进 WSL 镜像。

**路 2：按官方配方本机建**（`cli.swebench build`，`app/benchmark/swebench_recipes.py`，配方原文
`datasets/swebench/build-specs.json`）。官方镜像本来就是从公开的构建脚本建出来的
（`swebench==3.0.15` 的 `MAP_REPO_VERSION_TO_SPECS`，50 道题 20 个环境全能生成），脚本要下载的
东西 —— miniconda、conda 包、pip 包、apt 包 —— 清华源全有，国内直连 9 MB/s。只改四处：
apt / miniconda / conda / pip 源换清华；`git clone` GitHub 换成解开本地 git 镜像导出的 tar 再
`git init` 提交一次（顺带把 `/testbed` 的全史剥掉）；matplotlib 要的 qhull 源码包从本地拷；其余一行不动。
实测 base 181 秒、pytest 的 env 160 秒、instance 13 秒，`pytest-dev__pytest-5809` 八步全过 VALID。
产物和官方镜像**配方相同、二进制不同**（conda 那部分不钉版本），`images.json` 记 `source: local-build`，
题目 tag 记 `official-recipe-local-build`，报告里分得开。

两条路谁先出来用谁，官方二进制优先。

#### 七点六、路 2 真跑一遍踩到的五个坑（2026-09-16 下午，50 道全部走完）

路 2 最后成了主路：48 道走本机建（47 成、1 败），2 道之前已从官方拉到，全程约 3 小时。官方配方
"一行不动"这个说法不成立 —— 配方是 2024 年中写的，今天从清华源装到的工具链已经换代，
一共改了五处，每处都是先在容器里复现、再改 `swebench_recipes.py` / `swebench_import.py`，
单测里各有一条对应：

1. **并行两个 conda 求解会把机器压死。** 11 GB 内存的机器，两个 matplotlib 的 `conda env create`
   各占 5 GB，换页到 55 分钟没解完；杀掉后 `--jobs 1` 单跑。`scripts/swebench_build_then_validate.sh`
   第二轮固定 `SWEBENCH_JOBS=1`。
2. **conda 混着 defaults 和 conda-forge 解不出来。** 单跑也 58 分钟、7.6 GB 没结果；yml 加一行
   `nodefaults` 后 7 分钟解完（3 个环境 11 / 17 / 22 分钟建成）。yml 里 conda-forge 本来排第一，
   包本来就优先从它取，影响很小。`rewrite_env_script` 只做这一件事。
3. **pip 26 删了 `--no-use-pep517`**（scikit-learn 的安装命令用它）。去掉开关，`--no-build-isolation`
   还在，行为一样。
4. **隔离构建环境里的新 setuptools 没有 `pkg_resources`**（astropy 3.x 的 setup.py 要它），
   **docutils 0.22 去掉了 `docutils.utils.roman`**（sphinx 3.x / 4.x 的 latex writer 一导入就炸，
   测试的 `app` fixture 会加载 latex builder，6 道 sphinx 题因此在 S4 报 ERROR / 一条都收集不到）。
   都是"官方镜像那一代"和"今天"的差距，处理方式是 `setup_repo.sh` 开头写一个 `PIP_CONSTRAINT`
   文件：`setuptools<70`、`docutils<0.22`，只管构建那一步，不进运行时。sphinx 6 道 `--force` 重建后
   `test_gettext_definition_terms` 从 ERROR 变 passed。
5. **官方数据里的用例 id 有半截的。** SWE-bench 是从 pytest 日志按空白切 id 的，参数里带空格的
   就断了（`test_stem[png-w/` 其实是 `test_stem[png-w/ line collection]`）。这种 id 交给 pytest 是
   "not found"，**整场一条都不跑** —— 50 道里 12 道有，7 道因此停在 S4。判据"方括号没配对"，
   F2P / P2P 都剔（`clean_test_ids`），F2P 剔空的归 `NO_F2P`（样本里没有）。

另外两处是探测脚本自己的问题，不是配方：官方最老的环境是 python 3.6（scikit-learn 0.2x、astropy 3.x），
`subprocess.run(capture_output=)` 是 3.7 才有的，8 道题镜像建好了、探测报 TypeError；改成
`stdout=PIPE` 写法。`re.sub` 的替换串会把 `\n` 当转义，改成传函数。

建不出来的只剩 `astropy__astropy-8707`（astropy 3.1，2019 年）：它的 `setup.py` 要 `astropy_helpers`
子模块，官方 clone 也没带子模块，退而从 PyPI 装 `astropy-helpers`，今天的 pip 装不上这个 2019 年的包。
一道题，记"拉不到也建不出"，不追。

#### 八、漏斗（AC 8）—— 2026-09-16 18:00，50 道全部走完

`make swebench-report --save` 生成，原件 `datasets/swebench/import-report-2026-09-16.md`。

| 层 | 数量 | 说明 |
|:---|---:|---:|
| 官方题数 | 500 | `princeton-nlp/SWE-bench_Verified`，HF 修订 `c104f840cc67` |
| 离线筛掉：REPO_NOT_PYTEST | 314 | django 231 + sympy 75 + requests 8 |
| 离线筛掉：ISSUE_LEAKS_FIX | 8 | |
| 离线筛掉：NO_F2P | 2 | F2P 全是半截 id（sphinx 8265、8621），16 日下午加的筛子 |
| 离线筛掉：TEST_PATCH_NON_TEST_PATH | 2 | |
| 离线筛掉：GOLD_TOUCHES_PROTECTED | 1 | |
| 离线筛通过（抽样池） | 173 | 9 个仓库 |
| 抽样后 | 50 | 种子 20260915（16 日重抽，见四） |
| 环境镜像备好 | 49 | 2 道官方镜像（flask-5014、pylint-6386），47 道本机按官方配方建；建不出：astropy-8707 |
| git 镜像备好 | 50 | |
| 入库 | 49 | |
| 八步验证：VALID | **42** | 含 flask-5014 按"题面短"政策终审收下的 1 道 |
| 八步验证：REVIEW_REQUIRED | 6 | 见下表；**16 日晚人工终审 0 收 6 否**，已导回库变 INVALID |
| 八步验证：INVALID(COMMIT_MISSING) | 1 | astropy-7606：仓库带 git 子模块，`git archive` 物化不了（`workspace._verify` 拦下） |

| 仓库 | 池 | 抽中 | VALID |
|:---|---:|---:|---:|
| astropy/astropy | 22 | 6 | 4 |
| matplotlib/matplotlib | 31 | 8 | 6 |
| mwaskom/seaborn | 2 | 2 | 2 |
| pallets/flask | 1 | 1 | 1 |
| pydata/xarray | 19 | 6 | 5 |
| pylint-dev/pylint | 9 | 3 | 2 |
| pytest-dev/pytest | 18 | 5 | 5 |
| scikit-learn/scikit-learn | 31 | 8 | 8 |
| sphinx-doc/sphinx | 40 | 11 | 9 |

停在 REVIEW_REQUIRED 的 6 道，每道都在容器里复现过原因。**人工终审（2026-09-16 晚）：6 道全部否掉**，
理由在 `datasets/swebench/review-2026-09-16-final-official.csv`，已用 `cli.promote import-review` 导回（库里 42 VALID / 7 INVALID）。
否的口径是"按当前题目定义或当前环境不收"，不是题本身坏：pylint-4604 和 sphinx-8475 是结构性的（测试补丁依赖 gold、要联网），
另外 4 道修依赖或去掉挂掉的 P2P 后可以重验再审，但改官方题的定义要有逐题覆盖机制，现在没有，留给要抽 60 的时候一起决定。

| 题 | 卡在哪 | 复现出来的原因 |
|:---|:---|:---|
| pylint-4604 | S4 一条都收集不到 | 测试模块 `from pylint.constants import IS_PYPY`，这个名字是 gold patch 加的；base 上整个文件 import 不了。官方 harness 只在打完 gold 后跑 P2P，我们的 S4 要 P2P 在 base 上先过 —— 语义差异，不是环境问题 |
| xarray-6744 | 3 条 dask 变体 P2P 在 base 上 FAILED | 官方说它们改前就过，我们环境里 dask 2022.8.1 下改前就挂（center 那个 bug 对 dask 也生效）；官方镜像里的 dask 版本不同 |
| matplotlib-20859 | `test_warn_big_data_best_loc` 在 base 上 FAILED | 按耗时发警告的用例，机器快慢决定过不过；第一轮它是打完 gold 才挂（GOLD_REGRESSION），第二轮改成 base 上挂 —— 就是不稳 |
| matplotlib-24149 | pandas 相关 P2P 全 ERROR | conda 给的 pandas 2.3 要 numpy ≥ 1.26，官方 pip 钉的是 numpy 1.25.2；yml 里 `pandas!=0.25.0` 没封顶 |
| sphinx-8475 | `test_build_linkcheck` 3 条 FAILED | linkcheck 要联网，沙箱断网（C-31）—— 和 requests 被整仓排除是同一个原因 |
| sphinx-9461 | `test_uninitialized_attributes` 在 base 上 FAILED | 未查到底；autodoc 对 3.11 类型注解的行为差异可能性大 |

**Oracle / Noop 门禁（AC 4 / 5）—— 2026-09-16 19:07 过了。** 旧名单的 5 道 sphinx 行清掉后冻快照
42 道（清单哈希 `270b811d…`），Oracle 实验 #133 **42/42 = 100%**，Noop 实验 #134 **0/42 = 0%**，
84 个作业并行 8 跑了 7 分钟（只跑 F2P ∪ P2P）。已发布 `swebench-verified-subset@v1`
（指纹 `datasets/manifests/swebench-verified-subset@v1.json`，dirty=true —— 门禁是在未提交的工作区上跑的，
指纹里如实记着）。这就是 MET-01 要的那句话：**判定引擎对 42 道官方题的判决和官方一致。**

`psf__requests-2317` 那一趟（S1–S4，5 秒）是这条链路的第一份证据：官方镜像 + 声明的 141 条用例，
报告完整、8 条 F2P 在 base 上全部 FAILED、用例 ID 一条不漏地对上了 —— 它被排除是因为 P2P 要联网，
不是链路不通。

#### 九、同种子抽到 75（2026-09-16 晚）：官方题 59 道 VALID，MET-05 到 100

**为什么**：八那一轮结束时官方 42 + 自建 41 = 83，离 MET-05 的 100 差 17。四里说过层内顺序是固定的，
加题不用换种子，所以直接 `cli.swebench sample --n 75`：**前 50 道和 `sample-seed20260915-n50.json`
逐字相同**（池摘要同为 `7973719e3a9e…`），只多出 25 道。已验的 42 道一道没动；`DEFAULT_SAMPLE_SIZE`
和 Makefile 的 `SWEBENCH_N` 改成 75，n50 那份名单留在仓库里，它是 v1 的证据。

| 仓库 | 池 | 配额 50 → 75 | 新题 | 新题 VALID |
|:---|---:|:---:|---:|---:|
| astropy/astropy | 22 | 6 → 9 | 3 | 2 |
| matplotlib/matplotlib | 31 | 8 → 13 | 5 | 1 |
| mwaskom/seaborn | 2 | 2 → 2 | 0 | — |
| pallets/flask | 1 | 1 → 1 | 0 | — |
| pydata/xarray | 19 | 6 → 8 | 2 | 2 |
| pylint-dev/pylint | 9 | 3 → 5 | 2 | 2 |
| pytest-dev/pytest | 18 | 5 → 8 | 3 | 3 |
| scikit-learn/scikit-learn | 31 | 8 → 13 | 5 | 5 |
| sphinx-doc/sphinx | 40 | 11 → 16 | 5 | 2 |

**环境层只多了 3 个，不是 7 个。** 开工前按 `version` 数的是 7 个新环境（astropy 4.3、pylint 2.10、
pytest 5.4 / 6.0、sphinx 3.3 / 3.5 / 7.1），实际导出 `build-specs.json` 后是 20 → 23：官方的 env key
是**安装脚本的哈希**，脚本一样的版本共用一层 —— astropy 4.3 和 5.0 / 5.1 一层，sphinx 3.x 到 7.x 全在
一层里（16 道题一个 env）。真正新建的只有 pylint 2.10、pytest 5.4、pytest 6.0，各约 100 秒，都不是
`conda env create` 的环境，没有 conda 求解那一步。`export_swebench_specs.py` 加了 `--n`，
`test_swebench_recipes.py` 的断言改成 75 / 23。

**instance 镜像 25 道建了 24 道**（约 55 分钟，`SWEBENCH_JOBS=1`）：sphinx-8120 是 16 日下午重抽前建过的，
直接复用；astropy-8707 仍然建不出（七点六末尾那个 `astropy_helpers` 的原因）。第一遍 21 成 3 败，
3 败又是两个"官方镜像那一代 vs 今天"的坑，接在七点六的五条后面：

6. **matplotlib 的 `setup.py` 编译时自己去下 FreeType 2.6.1**（sourceforge / savannah，走代理）。
   之前 9 道 matplotlib 是运气好；这次 20826 卡了 17 分钟没动、26113 三个地址全失败。
   它下载前先查 `~/.cache/matplotlib/<sha256>`，命中就不联网，所以把源码包（2.3 MB，sha256
   `0a3c7dfbda6d…`，和 `setupext.py` 里写的一致，3.4–3.7 每个提交都是这个值）`COPY` 到那个位置，
   脚本一字不改。缓存在 `var/cache/swebench/freetype-2.6.1.tar.gz`，`ensure_freetype()` 校验 sha256。
   重建后两道各 2.5 分钟。
7. **仓库没有 `setup.py` 的，`pip install -e .` 之前把 pip 按回 24。** pylint-7277 的 base 只有
   `pyproject.toml` + `setup.cfg`，声明 `setuptools~=62.6`，没有 PEP 660 的 `build_editable`。
   官方那一代的 pip 24 会退回 `setup.py develop`（用 setup.cfg 顶上），pip 25 删了这条退路，
   26 直接报 "missing the 'build_editable' hook"。先在容器里验过 pip 24.3.1 装得上
   （`pylint from /testbed/pylint/__init__.py`），再改配方：`build_instance` 用 `git cat-file -e`
   看 base_commit 有没有 `setup.py`，没有才在 `conda activate testbed` 后面插一行 `pip install 'pip<25'`。
   样本里没有 `setup.py` 的 5 道（seaborn ×2、flask、sphinx-11445 用 flit，本来就支持 PEP 660）
   之前建好的不重建。

**八步验证（只验 25 道新题，`cli.validate run --task …`）**：第一遍 14 VALID / 10 REVIEW_REQUIRED /
1 INVALID，6 分钟。**不要用 `make swebench-validate` 补新题**：它按 `--dataset` 选题，会把 42 道
VALID 和 6 道人工否掉的题一起重验，人工结论被盖掉。

10 道 REVIEW 里有 3 道是"S4 一条都没跑"，逐道在容器里复现，根因是**同一类**：官方 P2P 名单里混着
执行器按名字选不中的 id，pytest 遇到一条选不中就整场退出（exit 4），一条用例都不跑；官方 harness 是
整文件跑再解析日志，碰不到这个。三种样子，没有一条纯字符串规则能全认出来：

| 题 | 坏 id | 为什么选不中 |
|:---|:---|:---|
| pytest-7324 | `test_valid_idents[:::]`、`[a:::c]` | 参数里带 `::`，pytest 5.4 的命令行按 `::` 切 nodeid（45 条收集到 43 条、0 条运行）|
| pytest-7521 | `test_capsysbinary.py::test_hello` | pytester 子会话里的 id，顶层没这个文件（官方日志解析串进来的，和 `[100%]` 同类；collected 0 items）|
| pylint-7277 | `test_stdin[/mymodule.py]` | 参数是绝对路径且被截过，本环境里真实 id 是 `test_stdin[/workspace/tests/mymodule.py-mymodule-…]`，任何环境都对不上 |

八里留下的那句"改官方题的定义要有逐题覆盖机制"就在这里落地：`datasets/swebench/p2p-overrides.json`
（题 → 剔掉的 P2P id + 理由 + 日期）+ `app/benchmark/swebench_overrides.py`。规矩三条：**只许剔 P2P、
不许碰 F2P**（F2P 是"修好了"的定义）；要剔的 id 必须真在官方 P2P 里，写错一个字直接报错；剔过的题打
`swebench-p2p-override` 标签，报告里分得开。单测拿仓库里那份清单对着官方数据逐条核。三道剔掉 2 / 1 / 1 条
P2P 后重验，全部 VALID。

剩下 7 道 REVIEW_REQUIRED 都是"P2P 在 base 上就挂"（`review-2026-09-16-n75-official.csv`，
0 道能按"题面短"政策自动收）。每道的原因在验证证据里，按上一轮的口径分类：

| 题 | 挂的 P2P | 像哪一类 |
|:---|:---|:---|
| matplotlib-20488 | `test_https_imread_smoketest` | 要联网（C-31），和 sphinx-8475 一样是结构性的 |
| sphinx-8269 | `test_build_linkcheck` 3 条 | 同上 |
| matplotlib-24026、26113 | `test_bar_pandas*` 全 ERROR | 和 24149 一样：conda 给的 pandas 2.3 对 numpy 1.25 |
| matplotlib-22871 | `test_auto_date_locator_intmult_tz`、`test_date2num_dst_pandas` | 依赖版本（pandas / tz） |
| astropy-13033 | `timeseries/tests/test_sampled.py::test_fold` | 未查到底 |
| sphinx-8120 | `tests/test_intl.py::test_text_docfields` | 未查到底，gettext / docutils 版本可能性大 |

**16 日深夜人工终审：0 收 7 否**，同 16 日晚那 6 道的口径 —— 要联网的、依赖版本的、根因没查到底的，
都不拿 `p2p-overrides.json` 硬剔（"这条 P2P 该不该剔"是题目定义的取舍，留到查清根因再议）。
理由逐条写在同一份 CSV 里，已用 `cli.promote import-review` 导回，库里官方题 **59 VALID / 15 INVALID /
0 REVIEW_REQUIRED**。注意官方题没有候选行，`import-review` 记不进 `raw_payload.final_review`，
终审理由只存在这份 CSV 里 —— 它必须跟着进仓库，重灌时从它导回（§12 的重灌规程已列）。

另有 1 道 INVALID：sphinx-8721，`test_viewcode_epub_default` 在 base 上就 PASSED（`F2P_NOT_FAILING`），
不是环境问题。

**漏斗（75 道）**，原件 `datasets/swebench/import-report-2026-09-16-n75.md`（50 道那份不动）：

| 层 | 数量 | 说明 |
|:---|---:|:---|
| 抽样后 | 75 | 种子 20260915，同一池（173） |
| 环境镜像备好 | 74 | 2 道官方镜像 + 72 道本机建；建不出：astropy-8707 |
| git 镜像备好 | 75 | |
| 入库 | 74 | |
| 八步验证：VALID | **59** | 42 + 17；其中 3 道剔过 P2P |
| 八步验证：REVIEW_REQUIRED | 0 | 上表 7 道，16 日深夜人工 0 收 7 否，已导回 |
| 八步验证：INVALID | 15 | 人工否的 6 + 7 + COMMIT_MISSING 1（astropy-7606）+ F2P_NOT_FAILING 1（sphinx-8721） |

**Oracle / Noop 门禁 —— 2026-09-16 22:54 过了。** 冻快照 v2 59 道（清单哈希 `4f5b44b88b46…`），
Oracle 实验 #135 **59/59 = 100%**，Noop 实验 #136 **0/59 = 0%**，118 个作业 0 平台故障、
0 次 `container_sigkilled_without_oom_flag`，6 分钟跑完。已发布 `swebench-verified-subset@v2`
（指纹 `datasets/manifests/swebench-verified-subset@v2.json`，dirty=true，同 v1 的原因）；
**v1 不动**（42 道，`270b811d…`，`benchmark-dev@v1` 的 `300746559b84…` 也没变）。

**MET-05 对账**：官方 59 + `benchmark-dev` 41 = **100**，刚好到线，**余量 0**：7 道 REVIEW 全否了，
官方题这一轮的终数就是 59。（**2026-09-17 补**：那 41 道当时只有 22 道在发布版里，另 19 道不在任何快照中；
现已冻成 `benchmark-cn-v1@v1` 发布，Oracle 41/41、Noop 0/41，两份发布版合计 100 道可评测的题，见 §8.12。）**底线的第二句"自建中文题 ≥40"没达标**：库里 VALID 里 `issue_language=zh`
的只有 benchmark-dev 2 道（另 2 道中英混）+ golden-v1 4 道，最多 8 道；官方 59 道全是英文，一道帮不上。
按 §4.1 的条款在报告里如实说明，附 §8.8 那张"中文 Python 项目全是 AI 基建、测试要下模型"的漏斗；
E1-T8（Go）2026-09-16 降为 P2，缺口按 §8.5 Plan B 人工构造中文 Golden 题顶。
成活率 25 道新题 17 道（68%），比预估的 84% 低，差在 matplotlib（5 道只活 1 道，pandas 那个坑占 2 道）。

#### 十、同种子抽到 100（2026-09-18）：v3 发布 75 道，MET-05 总数到 116

**这次只扩样本，不改任务格式、判定或已发布版本。** 默认抽样数由 75 调到 100，种子仍是
20260915，抽样池仍是 173 道（摘要 `7973719e3a9e…`）。名单原件
`datasets/swebench/sample-seed20260915-n100.json`，`cli.swebench sample --check` 回显
“和重算结果逐字相同”。原 n75 的 75 道全部保留、相对顺序不变，新增 25 道；
**不是整个数组的前 75 个位置相同**，新题按仓库插入，交接时的“前 75 道逐字相同”应按此更正。
n50 / n75 名单和 v1 / v2 发布指纹均保留。

**漏斗**，原件 `datasets/swebench/import-report-2026-09-18.md`：

| 层 | 数量 | 说明 |
|:---|---:|:---|
| 官方题数 / 离线筛通过 | 500 / 173 | 筛选规则未变 |
| 抽样后 / git 镜像备好 | 100 / 100 | 固定种子，9 个仓库 |
| 环境镜像备好 / 入库 | 98 / 98 | 2 道官方镜像 + 96 道按官方配方本机构建 |
| VALID | **75** | 原 59 + 新增 16 |
| REVIEW_REQUIRED | 0 | 本轮人工终审 1 收 6 否，已导回 |
| INVALID | 23 | 19 道一般拒收 + 3 道 COMMIT_MISSING + 1 道 F2P_NOT_FAILING |

报告旧表头“官方镜像拉得到”实际统计所有就绪镜像，不代表 98 道都从官方仓库拉取；
官方镜像只有 flask-5014、pylint-6386，其余 96 道的本机构建名单附在报告中。
astropy-8707 / 8872 没有可用镜像：astropy 3.1 的安装依赖 `astropy_helpers` 子模块。
配方原件 `build-specs.json` 覆盖 100 道、25 个环境层（n75 是 23 个），matplotlib 配额 17 道。
本次收尾只复用已有镜像，门禁前按库内 digest 核对 75/75 全部存在，没有拉取或构建镜像。

**新增题验证与终审**：25 道中 24 道入库并验证，初验 15 VALID / 7 REVIEW_REQUIRED / 2 INVALID；
7 道终审由用户拍板，CSV `datasets/swebench/review-2026-09-18-n100-official.csv` 已导回，
1 收 6 否后新增 16 VALID、8 INVALID，累计 75 VALID / 23 INVALID。

| 题 | 终审 | 理由（CSV 保留原文） |
|:---|:---|:---|
| matplotlib-23314 | REJECT | 命名空间包 `mpl_toolkits` 的 conftest 从 `/testbed` 与 `/workspace` 导入冲突，基线收集失败 |
| matplotlib-24970 | REJECT | pandas 2.3 与 numpy 1.25.2 不兼容，基线 P2P 报 ImportError |
| matplotlib-25122 | REJECT | pytest 9 将类作用域 fixture 的弃用告警作为错误，642 条 P2P 在基线 ERROR |
| pylint-4551 | REJECT | 测试补丁导入 gold 才新增的 `get_annotation`，基线模块收集失败，P2P 也为空 |
| sphinx-10323 | REJECT | pygments 行号渲染格式不同，基线 P2P 断言失败 |
| sphinx-9229 | REJECT | autodoc 输出与预期不符，根因未查清，未靠剔 P2P 放行 |
| sphinx-9711 | ACCEPT | 八步通过，只因官方 P2P 为空进入人审；1 条 F2P 在 base 失败、gold 通过 |

顺手修复 `cli/promote.py` 的 CSV 单元格上限：matplotlib-25122 的 `review` 列约 289 KB，
超过默认 128 KB，`import-review` 原来会崩溃；现在设置 `csv.field_size_limit(sys.maxsize)`，
`test_park_unreviewed.py::test_a_huge_review_cell_does_not_break_reading` 覆盖超长单元格读取。

**门禁与发布（2026-09-18）**：Oracle #143 **75/75 = 100%**，Noop #144 **0/75 = 0%**，
150 次评测均完成，0 平台故障、0 次 `container_sigkilled_without_oom_flag`；正式门禁函数回显
`gate_ok: True`、`problems: ()`。Worker 已优雅退出，进程列表确认没有残留。
已发布 `swebench-verified-subset@v3`，清单哈希 `6003a0519a52…`，导出哈希 `5640bd34bb05…`；
发布指纹 `datasets/manifests/swebench-verified-subset@v3.json` 记录完整哈希和实验号，
本地日志 `var/swebench-logs/gate-v3-143-144.log`（不入库）。工作区未提交，门禁与发布指纹均
如实标 `dirty=true`，门禁实验不得进入排行榜；正式实验仍需干净工作区。

**MET-05 对账**：官方 v3 的 75 道 + 已发布 `benchmark-cn-v1@v2` 的 41 道 = **116 道**，
比总数底线 100 多 16 道；两套数据集仍分别评测、分别统计解决率。
中文题面 Plan B 的实现与证据由 E8-T3 分支交付，这批题面是 AI 出稿、AI 复核、用户核对拍板后
改写成中文，不称为“人工改写”；本分支不包含该分支代码。

## 8.12 `benchmark-cn-v1@v1` 发布与数据集质量报告落地实录（2026-09-17，E8-T3 收口 + E8-T5）

> 工具：`python -m cli.dataset {stage,gate,publish}`（E1-T6 那套，`DATASET=benchmark-dev SLUG=benchmark-cn-v1`）
> + `python -m cli.quality report`（新，`make quality-report`）。报告原件 `datasets/quality/quality-2026-09-17.{md,json}`。

### 一、为什么要单独发这一版

MET-05 数的是**可评测的题**，`make enqueue` 和 `cli.experiment start` 只从 `benchmark_set_items` 取题。
9 月 16 日对账时说的"自建 41"里，只有 22 道在 `benchmark-dev@v1` 里，click 后审收下的 11 道和 tortoise 的
8 道**不在任何发布版里** —— 最终实验（E10-T4）一道都选不出来。所以把 `benchmark-dev` 下全部 41 道 VALID
冻成一版新的 set，slug 用规划里一直叫的 `benchmark-cn-v1`（E8-T3 Output、ADR、M5、README 都是这个名字），
`source_dataset_id` 记 `benchmark-dev`，题的 `dataset_id` 不改。**`benchmark-dev@v1` 不动**（22 道，
`sha256:300746559b84…`，发布后 `dataset verify` 零漂移）。

### 二、门禁第一轮拦下 8 道 tortoise 题，拦得对，但原因和 E1-T6 那次不一样

    oracle  实验 #137  33/41 = 80.5%（要求 100%）  COMPLETED
    noop    实验 #138  0/41  = 0.0%（要求 0%）    COMPLETED
    → 拒绝发布：tortoise__tortoise-orm-2081、2125、2128、2129、2145、2236、2255、2269

8 道全是 tortoise，click 33 道全过；8 道**挂的是同一条 P2P**，F2P 全过，P2P 只差这一条：
`tests/cli/test_cli.py::test_init_creates_migrations_package`，耗时 3 毫秒，
`AssertionError: assert False, where False = exists()` —— 断言 `tmp_path/cli_app/migrations` 存在，它不存在。

**不是竞态，不是环境，是执行顺序。** 这条用例往 `tmp_path` 写一个 `cli_app` 包、把 `tmp_path` 加进
`sys.path`、再让 CLI `import cli_app.models` 并在包旁边建 `migrations/`。同文件里其他用例动手前都
`sys.modules.pop("cli_app", None)`，**唯独它没有**（它在文件里排第一，作者默认没人在它前面）。
只要前面任何一条用例已经 import 过另一个 `tmp_path` 下的 `cli_app`，Python 直接用缓存的模块，
`migrations/` 就建到那个旧目录里去了。

为什么八步验证过了、门禁挂了：

| | 跑什么 | 顺序 | 这条用例 |
|:---|:---|:---|:---|
| 八步验证 S4 / S8（`--scope full`） | 全量套件 | pytest 收集顺序 = 文件顺序 | `test_cli.py` 里第一条，`cli_app` 还没被谁 import 过 → **过** |
| 正式评测（执行器） | F2P ∪ P2P | 按存进题目的顺序，P2P 是 `select_p2p()` `sorted()` 过的**字母序** | `test_downgrade_*` ×4、`test_heads_*`、`test_history_*` 六条排它前面 → **必挂** |

容器里复现三遍，结论稳定：只跑它 → 过；整个文件按文件顺序 → 16 过；按题目里的字母序喂 16 条 →
只挂它一条。8/8 道题在门禁里的表现一致，不是概率事件。

**这是验证流水线的一个真空档，记下来。** §7.10 说验证复用 `execute_tests`，"验过了就保证正式评测判得对"，
这话在**用例子集和顺序**这一层不成立：验证跑全量、评测跑子集，两边顺序不同。C-50 门禁正是为此设的，
它也确实拦住了。但它只在发布前拦一次，而这类用例进了 P2P 之后每次正式评测都会把正确补丁判成
`UNRESOLVED`（和 §7.11 九说的 pager 用例同一个后果，只是这个是 100% 而不是 0.16%）。
根治要么让验证也按题目顺序跑一遍声明的子集（多起一次容器），要么让 P2P 按套件收集顺序存
（会改所有已发布题的 `content_hash`）—— 两条都没动，留给 E9-T5 复验那张卡一起议。

### 三、处置：照 E1-T6 剔 pager 的办法，多加一类排除理由

`assembly.py` 加了 `ORDER_DEPENDENT_TEST_FUNCTIONS`（按函数名精确匹配，和 `FLAKY_TEST_FUNCTIONS` 并列），
两处过滤（`select_p2p()`、`assemble()`）改走同一个判据 `unfit_for_p2p()`，以后再加一类理由只改一处。
没有把它塞进 `FLAKY_TEST_FUNCTIONS`：它不飘，名字说"飘"就是撒谎，三周后有人看到会去查根本不存在的竞态。

```
cli.promote assemble --redo --repo tortoise/tortoise-orm    # 14 道（8 VALID + 6 INVALID）
  每道 P2P −1，p2p_sampling.total_pool 跟着 −1，validation_state 不动，content_hash 全变
make dataset-stage DATASET=benchmark-dev SLUG=benchmark-cn-v1
  刷新草稿 v1，摘要 c04252899f60… → 1701c943ff5b…，第一轮门禁作废
make dataset-gate SLUG=benchmark-cn-v1 ALLOW_DIRTY=1 && make worker
    oracle  实验 #139  41/41 = 100.0%  COMPLETED   墙钟 74 秒
    noop    实验 #140  0/41  = 0.0%    COMPLETED
    平台故障 0，container_sigkilled_without_oom_flag 0 次
make dataset-publish SLUG=benchmark-cn-v1 ALLOW_DIRTY=1
  ✅ 已发布 benchmark-cn-v1@v1，41 道
```

没有重跑八步验证：改动只是从 P2P 里去掉一条，剩下每一条在原验证证据里都已在基线和 gold 上过了两遍，
门禁是发布级的证据（§7.11 十二同样没重验）。`dirty=true` 同前三版的原因：门禁在未提交的工作区上跑
（`assembly.py` 的改动就是这次的）。

| | |
|:---|---:|
| 快照 | 41 道（click 33 + tortoise 8），排除 `INVALID` 24 |
| F2P / P2P 合计 | 120 / 56795（剔掉 8 条依赖顺序的用例后）|
| 快照摘要 | `sha256:1701c943ff5b…` |
| 导出文件 | 4538 KB，`sha256:b6b560cc3e02…` |
| Oracle 一轮墙钟 | 74 秒 |

### 四、质量报告（E8-T5）：只数发布版里的题

`cli/quality.py` 按 `benchmark_sets → benchmark_set_items → benchmark_tasks` 这条链数，
库里 VALID 但没冻进发布版的题不算 —— 算进去就是 §一说的那个虚报。四张表 + 两条漏斗，
数字全部来自库，2026-09-17 的结果：

| | `benchmark-cn-v1@v1` | `swebench-verified-subset@v2` | 合计 |
|:---|---:|---:|---:|
| 题数 | 41 | 59 | **100** |
| 仓库 | click 33、tortoise 8 | 9 个（sklearn 13、sphinx 11、pytest 8、matplotlib 7、xarray 7、astropy 6、pylint 4、seaborn 2、flask 1）| 11 |
| 国产仓库 | 0 | 0 | 0 |
| 中文（zh + mixed） | 2 + 2 = 4（10%） | 0 | **4（4%）** |
| 难度 easy / medium / hard | 3 / 31 / 7 | 43 / 12 / 4 | 46 / 43 / 11 |
| F2P 合计 / 中位 | 120 / 2 | 89 / 1 | |
| P2P 合计 / 中位 | 56795 / 1554 | 10167 / 59 | |
| `test_timeout_s` | 480 | 1800 | |

**MET-05 底线第二句"自建中文题 ≥40"的最终账**：发布版里中文 4 道；库里另有 Golden 4 道全中文
（`golden-v1`，L0，不在发布版里，不算可评测的题）。9 月 16 日说的"最多 8 道"就是 4 + 4。
按 §4.1 如实标，缺口的原因是 §8.8 那张表：中文 Python 项目全是 AI 基建，测试要下模型权重，
沙箱断网跑不了。
**2026-09-18 更新**：Plan B 落地后 v2 的题面 41/41 是中文（其中 40 道改写），账在 §8.5 的落地实录里；上面这段是 v1 的记录，不改。

**自建题漏斗按仓库**（8 个定档仓库第一次并排放进一张表，原件在报告第六节）：

| 仓库 | 候选 | 预筛 PASS / REVIEW | 抽得出候选 F2P | 探测 OK | 入库 | VALID | 终审 收 / 否 | 进集 |
|:---|---:|:---|---:|---:|---:|---:|:---|---:|
| pallets/click | 80 | 53 / 14 | 59 | 51 | 51 | 33 | 33 / 18 | **33** |
| tortoise/tortoise-orm | 55 | 33 / 13 | 42 | 14 | 14 | 8 | 8 / 6 | **8** |
| sqlfluff/sqlfluff | 676 | 65 / 587 | 65 | 0 | 0 | 0 | — | 0 |
| sgl-project/sglang | 214 | 111 / 55 | 127 | 0 | 0 | 0 | — | 0 |
| xorbitsai/inference | 75 | 43 / 21 | 51 | 1 | 0 | 0 | — | 0 |
| hiyouga/LlamaFactory | 32 | 16 / 12 | 19 | 0 | 0 | 0 | — | 0 |
| Delgan/loguru | 20 | 10 / 5 | 11 | 0 | 0 | 0 | — | 0 |
| InternLM/lmdeploy | 17 | 10 / 3 | 11 | 0 | 0 | 0 | — | 0 |

进集 0 的六个仓库原因各不同：xorbitsai 探过 17 条只过 1 条、测试要下模型（§E8-T3 卡②）；
sqlfluff 全量套件 10 分钟、内存撞上限（②）；sglang / LlamaFactory / loguru / lmdeploy 没建镜像、没探过。
官方题那条漏斗（500 → 173 → 75 → 74 → 59）和 §8.6 九一致，报告里原样复用 `cli.swebench report` 的表。

## 8.13 最终实验前的确定性检查（2026-09-20，E10-T4 口径 AC 1）

§9 第三条"确定性哨兵"原来只在单题上做 3 次；E10-T4 口径把它放大到整个发布版：两套发布版各跑 Oracle 3 轮 +
Noop 1 轮，三轮每道题的 `agent_outcome` **和逐用例状态**都必须完全一致。全部通过，两版都不用重新 stage。

**环境**：main `7a0bd59`（#116 合并后，`git status --porcelain` 为空，八个实验全部 `dirty=false`）；
单 Worker，`agent=10 / sandbox=4 / slots=8`；不调任何模型、不花钱。命令（都在 `backend/` 下）：

```bash
python -m cli.experiment start --agent oracle --set benchmark-cn-v1 --version v2 --rounds 3 --name "E10-T4 确定性检查 Oracle · benchmark-cn-v1@v2"
python -m cli.experiment start --agent noop   --set benchmark-cn-v1 --version v2 --name "E10-T4 确定性检查 Noop · benchmark-cn-v1@v2"
python -m cli.experiment start --agent oracle --set swebench-verified-subset --version v3 --rounds 3 --name "E10-T4 确定性检查 Oracle · swebench-verified-subset@v3"
python -m cli.experiment start --agent noop   --set swebench-verified-subset --version v3 --name "E10-T4 确定性检查 Noop · swebench-verified-subset@v3"
python -m app.worker   # 464 条作业，22:13 起跑，22:36 全部收尾（23 分钟）
```

**结果**：

| 数据集 | 实验 | 解决 | 平台故障 | 重试 | makespan |
|:--|:--|--:|--:|--:|--:|
| `benchmark-cn-v1@v2`（41 道） | Oracle #149 / #150 / #151 | 41/41 × 3 | 0 | 0 | 86 / 112 / 104 s |
| | Noop #152 | 0/41（41 道全 `EMPTY_PATCH`） | 0 | 0 | 127 s |
| `swebench-verified-subset@v3`（75 道） | Oracle #153 / #154 / #155 | 75/75 × 3 | 0 | 0 | 272 / 284 / 335 s |
| | Noop #156 | 0/75（75 道全 `EMPTY_PATCH`） | 0 | 0 | 374 s |

**一致性**（SQL 直接比 `evaluation_task_runs` 和 `test_results`）：逐题 `agent_outcome` / `infra_outcome`
三轮一致 41/41、75/75；逐用例把每道题每轮的 `(role, test_id, status)` 按序拼成签名，三轮签名完全相同的
41/41、75/75，差异 0 道——中文集每轮 56,915 条用例、官方题每轮 11,894 条。Worker 日志
`container_sigkilled_without_oom_flag` 0 次、error 级 0 条，结束后无残留容器。

顺带一个观察：三轮 makespan 逐轮变长（86 → 112 → 104、272 → 284 → 335），是同一台机器上镜像层缓存和
其他进程的波动，不是判定问题——判定结果一个用例都没变。
