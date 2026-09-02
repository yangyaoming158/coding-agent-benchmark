# 评测协议 v1.2（草案）

| | |
|:---|:---|
| **状态** | **DRAFT v1.2 —— 已过两轮评审 + 一次机器穷举，仍未冻结** |
| 上次更新 | 2026-09-02 |
| 起草日期 | 2026-09-02 |
| 评审轮次 | 第一轮 2026-09-02（Q1~Q6）· 第二轮 2026-09-02（Q7~Q9 + 7 组内部问题）|
| 冻结还差 | 见 §11 签字表。真值表复查已完成，剩最终签字 |
| 适用范围 | 判定引擎、数据库状态字段、编排层、报表口径、前端展示 |
| 冻结后的效力 | 本文件是上述所有模块的**唯一依据**。代码与本文件不一致时，以本文件为准 |
| 变更方式 | 见 §9 |

---

## 怎么用这份文档开评审会

文档里每一条规定都有编号（`C-01`、`C-02`……）。评审时逐条过，对每一条只做三种表态：

- **通过** —— 没有异议
- **异议** —— 说明理由，当场改或记为待定
- **待定** —— 需要更多信息才能定，记进 §10 的待定清单

**目标不是把每条都定死**，而是把"哪些定了、哪些没定"分清楚。没定的部分写进待定清单，不要含糊过去。

三个词的含义：

- **【必须】** 不做就是违反协议，代码评审时要拦下来
- **【禁止】** 做了就是违反协议
- **【建议】** 有更好的理由可以不这么做，但要在代码注释里写明理由

---

## 1 术语

| 词 | 含义 |
|:---|:---|
| **题目**（task） | 一道评测题：代码快照 + issue 描述 + 验证测试 |
| **base commit** | 代码回退到的那个提交，也就是"bug 还在"的状态 |
| **官方补丁**（gold patch） | 上游项目当初真正修复这个 bug 的代码改动。**永远不给被测 AI 看** |
| **测试补丁**（test patch） | 上游项目当初新增或修改的测试文件改动，由平台施加 |
| **F2P**（fail_to_pass） | 修复前必须失败、修复后必须通过的测试用例 |
| **P2P**（pass_to_pass） | 修复前后都必须通过的测试用例，用来检查有没有把别的功能改坏 |
| **一次评测**（task run） | 一个被测 AI 跑一道题的完整过程 |
| **一次实验**（evaluation run） | 一个被测 AI 跑完一整套题库 |
| **受保护路径** | 被测 AI 改了也会被丢弃的文件路径，主要是测试文件 |

---

## 2 结果用三个字段描述

**C-01【必须】** 一次评测的结果用三个字段记录：`lifecycle_status`、`infra_outcome`、`agent_outcome`。

**C-02【必须】** 这三个字段描述三个不同的维度，**禁止合并存储**。

它们的取值之间**存在合法组合约束**，以 C-09、C-18、C-30、C-68 为准。

> v1.1 措辞修正：原文写的是"任何一个的取值都不得由另外两个推导得出"，这与 C-09（终态决定能否有 `agent_outcome`）、C-18（故障类型决定映射结果）直接矛盾。真正要禁止的是**把三件事塞进一个字段**，不是禁止它们之间有约束关系。

**C-03【禁止】** 把"平台自己出故障"和"被测 AI 没修好"存进同一个字段。

> 为什么：如果合并，算出来的解决率就分不清"AI 不行"和"我们的 Docker 崩了"。这两件事在报告里的结论完全相反。

### 2.1 `lifecycle_status` —— 走到哪一步了

**C-04【必须】** 取值只能是以下之一：

| 值 | 含义 |
|:---|:---|
| `QUEUED` | 排队中 |
| `PREPARING` | 准备代码快照、检查镜像 |
| `AGENT_RUNNING` | 被测 AI 正在容器里干活 |
| `PATCH_CAPTURED` | 已收集它的代码改动并标准化 |
| `TESTING` | 正在跑测试 |
| `JUDGING` | 正在解析测试报告并判定 |
| `ANALYZING` | 正在分析失败原因 |
| `COMPLETED` | **跑完了，拿到了可信的判定结论**（哪怕结论是"没修好"） |
| `FAILED` | **没拿到可信结论**，因平台故障中止 |
| `CANCELLED` | 人工取消 |

**C-04a【必须】** 终态只有三个：`COMPLETED`、`FAILED`、`CANCELLED`。**不设 `TIMEOUT` 终态。**

> 为什么去掉：超时的具体类型已经完整记录在 `infra_outcome` 里（`AGENT_TIMEOUT` / `TEST_TIMEOUT`）。生命周期状态要回答的是另一个问题——**"这次评测拿到可信结论了吗"**。
> 如果保留 `TIMEOUT` 终态，它有时代表"拿到了结论"（AI 超时 → 判没修好），有时代表"没拿到结论"（环境问题导致测试超时），一个状态两种含义，正是本文件 v1.0 里那个矛盾的根源。
> 现在的划分：AI 自己超时 → `COMPLETED`（我们知道答案：它没在预算内做完）；环境问题超时 → `FAILED`（我们不知道它行不行）。

### 2.2 `infra_outcome` —— 这次跑得对不对

**C-05【必须】** 取值只能是以下之一：

`SUCCESS`、`ENV_BUILD_FAILED`、`WORKSPACE_ERROR`、`AGENT_TIMEOUT`、`AGENT_RUNTIME_ERROR`、`AGENT_AUTH_ERROR`、`SANDBOX_ERROR`、`OOM_KILLED`、`TEST_TIMEOUT`、`TEST_DISCOVERY_ERROR`、`PATCH_APPLY_FAILED`、`HARNESS_ERROR`、`CANCELLED`

**C-06【必须】** 判断 `OOM_KILLED` 用 `docker inspect --format '{{.State.OOMKilled}}'` 的返回值。

**C-07【禁止】** 用退出码判断是不是内存超限。

> 为什么：内存超限和我们自己超时强杀，容器退出码**都是 137**。已在开发机实测确认。靠退出码区分会把两种相反的情况判成一样。

### 2.3 `agent_outcome` —— 被测 AI 修好了没有

**C-08【必须】** 取值只能是以下之一：

| 值 | 判定条件 |
|:---|:---|
| `RESOLVED` | 全部 F2P 用例状态为 `PASSED`，**且**全部 P2P 用例状态为 `PASSED` |
| `UNRESOLVED` | ① 有可应用的非空补丁但不满足上一条；**或** ② 协议明确归属于 AI 的失败（超时、自身运行错误），**此时补丁允许为空** |
| `EMPTY_PATCH` | AI 正常退出，标准化后的补丁为空 |
| `INVALID_PATCH` | 补丁非空但打不上去 |
| `NOT_ATTEMPTED` | 因平台故障，AI 从未真正开始 |

**C-08c【必须】** `UNRESOLVED` 与 `EMPTY_PATCH` 的区分标准：AI **正常退出**且补丁为空 → `EMPTY_PATCH`；AI **异常终止**（超时、崩溃）且补丁为空 → `UNRESOLVED`。

> 为什么不都算 `EMPTY_PATCH`：`EMPTY_PATCH` 是一个诊断信号，含义是"AI 跑完了但没交出东西"。超时被杀的 AI 不属于这种情况——它可能正要写文件就被杀了。混在一起会让空补丁率这个指标失去意义。

**C-08a【必须】** `EMPTY_PATCH` 的含义是"**标准化之后**的补丁为空"，**不等于**"AI 什么都没做"。

> 为什么要强调：AI 可能改了一堆受保护路径下的文件（比如想改测试蒙混过关），这些改动被平台按 C-41 全部丢弃后，标准化补丁就是空的。这和"AI 完全没动手"是两种截然不同的行为，混为一谈会得出错误结论。
> 失败归因体系里也把"只改了受保护文件"归进了空补丁那一类（`docs/plan/06-judge-attribution.md` §12.1 的 F7），必须靠下面的字段才能区分。

**C-08b【必须】** 每次评测额外记录三个诊断字段：

| 字段 | 含义 |
|:---|:---|
| `raw_patch_empty` | AI 交出来的原始改动是不是空的（过滤之前） |
| `protected_path_edit_attempted` | AI 有没有试图改受保护路径下的文件 |
| `filtered_change_reasons` | 哪些改动被丢弃了、分别因为什么（受保护路径 / 二进制 / 超大文件 / 空 mode 变更） |

**C-65【必须】** 排行榜可以展示 `empty_patch_count` 和 `empty_patch_rate`（分母是题库总题数），列名写作"**有效空补丁率**"。

**C-66【禁止】** 把空补丁率用于排名，或用作并列时的加分项。它**只能作为诊断指标**。

> 为什么：空补丁率低不代表 AI 强。一个胡乱改代码但从不交白卷的 AI，空补丁率是 0，但解决率可能很差。把它计入排名会奖励错误的行为。

**C-09【必须】** 非终态时 `agent_outcome` 一律为 `NULL`。终态时按 C-68 的合法组合表取值。

**C-68【必须】** `lifecycle_status` 与 `agent_outcome` 的合法组合**只有以下六种**，其余组合一律视为程序错误：

| 情况 | `lifecycle_status` | `agent_outcome` | 说明 |
|:---|:---|:---|:---|
| 正常跑完测试 | `COMPLETED` | `RESOLVED` / `UNRESOLVED` / `EMPTY_PATCH` | 拿到了判定结论 |
| AI 超时或自身运行失败 | `COMPLETED` | `UNRESOLVED` | 责任在 AI，结论明确 |
| 补丁打不上 | `COMPLETED` | `INVALID_PATCH` | 责任在 AI，结论明确 |
| 平台故障且 AI 从未启动 | `FAILED` | `NOT_ATTEMPTED` | 没给 AI 机会 |
| AI 已启动，之后平台故障导致无法判定 | `FAILED` | `NULL` | 给了机会但没拿到结论 |
| 人工取消 | `CANCELLED` | `NULL` | — |

**C-69【必须】** `NOT_ATTEMPTED` 当且仅当 `agent_started_at IS NULL`。

**C-77【必须】** `agent_started_at` 的置位时刻定义为：**Agent 容器成功启动、且任务输入已写入其标准输入的那一刻**。

> 为什么要定这么死：整张合法组合表都靠这个字段来区分"没给 AI 机会"和"给了机会但我们没拿到结论"。
> 举个会分歧的例子：鉴权失败（`AGENT_AUTH_ERROR`）——如果是 probe 阶段就发现密钥无效，容器还没起，算未启动；如果是容器跑起来、AI 调 API 才拿到 401，算已启动。按上面的定义，这两种情况分得清。

> 为什么要分"从未启动"和"启动后平台故障"：前者对 AI 完全没有信息量；后者说明 AI 已经消耗了时间和 token（成本要计入），只是我们没能拿到结论。两者混用会让成本统计对不上。

> v1.0/v1.1 修正记录：原 C-09 规定 `FAILED` 时 `agent_outcome` 必为 `NULL`，但故障映射表（C-18）把平台故障全部映射成 `NOT_ATTEMPTED`（非空）。两条直接矛盾。现用 C-68 的组合表统一。

> v1.0 修正记录：原条款与故障映射表（C-18）直接矛盾——映射表规定 `AGENT_TIMEOUT` 判 `UNRESOLVED`，但当时 `AGENT_TIMEOUT` 会落到 `TIMEOUT` 终态，按原 C-09 就必须为 `NULL`。现按 C-04a 取消 `TIMEOUT` 终态后，矛盾消除。

**C-09a【必须】** `AGENT_TIMEOUT` 发生时：仍然收集并保存它已改出来的补丁（供失败分析用），但**不跑测试**，直接判 `UNRESOLVED`，`lifecycle_status = COMPLETED`。

> 为什么不跑测试：时间预算本身就是评测的一部分，超过预算才交付等于没交付。同时省下约 75 秒的测试时间。
> 已知的取舍：如果 AI 在被杀之前其实已经改对了，我们不会发现。补丁仍然存档。
> 成本说明：如果要加"影子判定"，**只有发生超时的那些 task run 各多花约 75 秒**，不是所有题都增加时间。

**C-09b【必须】** 超时时已经产生的补丁必须保存为制品，并统计两个低成本指标：`agent_timeout_count`、`timeout_with_nonempty_patch_count`。

**C-09c【建议】** 影子判定（对超时补丁跑测试但不计分）**放到离线分析任务里做**，不占正式实验的关键路径。影子结果**禁止**影响 `agent_outcome` 和排行榜。

### 2.4 `test_status` —— 单条测试用例的状态

**C-10【必须】** 取值只能是：`PASSED`、`FAILED`、`ERROR`、`SKIPPED`、`XFAIL`、`XPASS`、`MISSING`。

**C-11【必须】** `MISSING` 表示题目里列了这条用例，但测试报告里找不到它。

**C-12【禁止】** 把 `MISSING`、`SKIPPED`、`XFAIL` 当作通过。

**C-13【必须】** 出现 `MISSING` 时，**先做 C-13b 的三项自动检查，再根据检查结果分三种情况处理**，不得无条件判 `UNRESOLVED`：

| 检查结论 | 处理 |
|:---|:---|
| **(a) 平台或解析器的问题**：报告被截断、解析失败、用例 ID 归一化错误 | `FAILED` + `HARNESS_ERROR` 或 `TEST_DISCOVERY_ERROR` + `agent_outcome = NULL`，**计入平台故障率** |
| **(b) AI 补丁导致的**：有实际证据表明补丁破坏了测试收集，或删改了测试 | `COMPLETED` + `UNRESOLVED` |
| **(c) 原因不明** | `COMPLETED` + `UNRESOLVED` + 标记 `TEST_RESULT_INTEGRITY_SUSPECTED`，自动进人工复核 |

> v1.1 修正记录：原条款先无条件判 `UNRESOLVED`，后面的 C-13b 才去查是不是我们自己的解析器出错。顺序反了——如果确认是平台的问题，就不该罚 AI。

**C-13f【必须】** 如果一次实验中情况 (c) 的题目比例超过 5%，整个实验标记为**需人工确认才能发布**。

> 为什么：解析器的系统性 bug 会让大量题目落进 (c)。如果不设这道闸，我们会安静地发布一批被压低的解决率，而且很难察觉。

**C-13a【禁止】** 仅凭出现 `MISSING` 就判定为作弊。

> 为什么：用例 ID 归一化写错本身就会制造大量假 `MISSING`（见 `docs/plan/06-judge-attribution.md` §11.3，这是全项目公认最容易出的静默 bug）。把它直接当作弊证据，会产生大量冤枉的指控，而且会掩盖真正的原因。

**C-13b【必须】** 出现 `MISSING` 时，先自动做三项检查，结果记入复核任务：

1. 测试报告文件是否完整生成（有没有被截断、有没有解析失败）
2. 用例 ID 归一化是否正确（把题目里的 ID 和报告里的 ID 都打印出来对照）
3. 测试收集阶段有没有报错（collection error）

**C-13c【必须】** 只有在发现下列**实际证据**时，才升级为 `TEST_TAMPERING_SUSPECTED`：

- AI 试图修改受保护路径下的文件（`protected_path_edit_attempted = true`）
- AI 修改了影响测试收集的配置（`conftest.py`、`pytest.ini`、`pyproject.toml` 等）
- AI 新增了受保护路径下的文件

**C-13d【必须】** `protected_path_edit_attempted = true` 本身就要触发人工复核，**即使最终没有出现 `MISSING`**。

**C-13e【必须】** 复核任务按 `(task_id, test_id, 错误摘要)` 去重。

> 为什么：同一道坏题会在每个被测 AI 上都触发一次 `MISSING`。不去重的话，一道坏题就能生成几十条复核任务，把队列淹掉。

---

## 3 判定规则

### 3.1 单题怎么判

**C-14【必须】** 判定按以下顺序执行，任何一步失败就按对应的 `infra_outcome` 结束：

```
1. 从 base commit 重新导出一份干净代码       ← 不复用 AI 用过的那份
2. 打上 AI 的补丁                            失败 → PATCH_APPLY_FAILED / INVALID_PATCH
3. 强制还原受保护路径下的所有文件             ← 防作弊第二道防线
4. 打上测试补丁                              失败 → TEST_DISCOVERY_ERROR
5. 跑 F2P 与 P2P 指定的用例，断网，带资源和时间限制
6. 解析报告，得到每条用例的状态
7. 按 C-08 判定
8. 逐条用例的结果写进数据库
```

**C-15【必须】** 第 1 步必须重新导出干净代码。

> 为什么：被测 AI 在自己的工作目录里可能装了包、改了配置、留了临时文件。如果直接拿它的目录跑测试，判定结果会被这些东西影响，不同 AI 之间就没法公平比较了。

**C-16【必须】** 第 3 步的强制还原不能省略，即使第 2 步生成补丁时已经过滤过测试文件。

> 为什么：这是两道独立的防线。任何一处写出 bug，另一处还能挡住。

**C-17【建议】** 第 5 步只跑 F2P 和 P2P 列出的用例，不跑全量测试。

> 代价是发现不了这两个集合之外的问题。这是有意的取舍——P2P 这个集合本身就是我们定义的回归检查范围。题目验证阶段会跑全量。

### 3.2 故障归属映射表

**C-18【必须】** 本表在代码中实现为一张显式的常量表，命名为 `INFRA_TO_AGENT_MAPPING`。

**C-19【禁止】** 把这张表的逻辑散落在各处的 `if` 分支里。

| `infra_outcome` | 责任方 | 映射到的 `agent_outcome` | 计入平台故障率 | 自动重试 |
|:---|:---|:---|:---:|:---:|
| `SUCCESS` | — | 按 C-08 判定 | 否 | — |
| `AGENT_TIMEOUT` | **AI** | `UNRESOLVED` | **否** | 0 |
| `AGENT_RUNTIME_ERROR` | **AI** | `UNRESOLVED` | **否** | 1 |
| `PATCH_APPLY_FAILED` | **AI** | `INVALID_PATCH` | **否** | 0 |
| `ENV_BUILD_FAILED` | 平台/题目 | `NOT_ATTEMPTED`（必然未启动） | **是** | 1 |
| `WORKSPACE_ERROR` | 平台 | `NOT_ATTEMPTED`（必然未启动） | **是** | 1 |
| `SANDBOX_ERROR` | 平台 | 按 C-69 定 | **是** | 2 |
| `OOM_KILLED` | 平台/题目 | `NULL`（必然已启动） | **是** | 1（降配后） |
| `TEST_DISCOVERY_ERROR` | 题目/平台 | `NULL`（必然已启动） | **是** | 1 |
| `HARNESS_ERROR` | 平台 | 按 C-69 定 | **是** | 1 |
| `AGENT_AUTH_ERROR` | 外部服务 | 按 C-69 定 | **是** | 3（带退避） |
| `TEST_TIMEOUT` | 见 C-20 | 见 C-20 | 见 C-20 | 1 |
| `CANCELLED` | 人工 | `NULL` | 否 | 0 |

> **v1.2 修正记录**：原表把所有平台故障一律映射成 `NOT_ATTEMPTED`，与 C-69（`NOT_ATTEMPTED` 当且仅当 `agent_started_at IS NULL`）矛盾。
> `OOM_KILLED` 只可能发生在容器跑起来之后，`TEST_DISCOVERY_ERROR` 只可能发生在判定阶段——这两种情况 AI 早就启动过了，按 C-69 必须是 `NULL`。这个矛盾是 §4.3 的穷举检查发现的。

**C-20【必须】** `TEST_TIMEOUT` 的归属按下面这个固定流程判定，**不允许凭经验直接下结论**：

```
1. 先确认 OOMKilled = false
      若为 true → 按 OOM_KILLED 处理，不走本流程

2. 用同一份标准化补丁重跑一次测试
      【禁止】重新调用被测 AI —— 补丁已经拿到了，重跑 AI 既浪费钱又改变了输入
      若这次通过 → 本次判定有效，按正常流程走

3. 仍然超时 → 跑对照组：base + test_patch（不打 AI 的补丁）
      必须用相同的镜像、相同的资源限制、相同的测试用例列表

4. 对照组正常，只有打了 AI 补丁才超时
      → AI 的问题（多半写出了死循环）
      → agent_outcome = UNRESOLVED，lifecycle = COMPLETED
      → 不计入平台故障率

5. 对照组也超时
      → 本次结果无效
      → agent_outcome = NULL，lifecycle = FAILED
      → 计入平台故障率
      → 该题进入"题目复验"队列

6. 只有题目复验也失败，才把题目标记为 QUARANTINED（隔离）
```

**C-20a【禁止】** 因为一次超时就直接隔离题目。

> 为什么：题目发布前已经要求基线全量测试不能超时（见 `docs/plan/03-benchmark-spec.md` §7.2 第 4 步和 §7.3 的 S4）。正式评测时基线子集反而超时，**更可能是机器负载波动，而不是题目坏了**。一次波动就隔离题目，会把好题误杀。

### 3.3 统计口径

**C-21【必须】** 三个指标的算法固定为：

```
以下全部只统计 canonical attempt（C-24）

严格解决率 = RESOLVED 的题数 / 题库里的全部题数

有效解决率 = RESOLVED 的题数
           / agent_outcome ∈ {RESOLVED, UNRESOLVED, EMPTY_PATCH, INVALID_PATCH} 的题数

平台故障率 = 计入平台故障率的题数 / 题库里的全部题数
```

> **v1.0 修正记录**：原来的有效解决率用 `infra_outcome = SUCCESS` 做分母，这是错的。
> 它会把 `AGENT_TIMEOUT`、`INVALID_PATCH`、以及补丁导致的 `TEST_TIMEOUT` 一起排除掉——但这三种都是**被测 AI 自己的失败**，不是平台故障。排除它们会让有效解决率虚高。
> 新的分母是"我们确实拿到了一个可归因于 AI 的结果"的题数。

**C-22【必须】** 排行榜展示的是**严格解决率**，并在同一行展示平台故障率。

> 为什么用严格口径：跟 SWE-Bench 官方一致，可比。有效解决率只用于我们自己诊断。

**C-23【必须】** 报告中同时给出两个解决率，并注明各自的算法。

**C-24【必须】** 每道题在一次实验中有且只有一个 **canonical attempt（认定结果）**，由固定规则选出，不是"最后一次跑完"这种模糊说法：

> **canonical attempt = 第一个不可重试的结果；如果全都可重试，就是重试次数耗尽后的最后一个自动 attempt。**

"不可重试"指该 `infra_outcome` 在 C-18 映射表中的自动重试次数为 0（例如 `AGENT_TIMEOUT`、`PATCH_APPLY_FAILED`），或者 `infra_outcome = SUCCESS`。

**C-25【禁止】** 人工挑选 canonical attempt，也禁止取多次重试中"最好的一次"。

> 为什么禁止取最优：那等于变相做了多次采样取最优，解决率会虚高；而且不同题目的重试次数不同，结果之间没法比。

**C-53【必须】** 自动重试只能由 `infra_outcome` 和 C-18 规定的固定次数触发。**禁止**由人工判断"这次不太对，再跑一遍"来触发自动重试。

**C-54【必须】** 已经拿到补丁之后才发生的测试阶段故障，重试时**必须复用同一份标准化补丁**，禁止重新调用被测 AI。

> 为什么：重新调用 AI 会改变输入（AI 有随机性），这次重试就不再是"同一个实验的重跑"，而是一次新的采样。同时也白花一次钱。

**C-55【必须】** 实验结束后的人工重跑，必须新建一次 `EvaluationRun`，**禁止**修改已发布实验的结果。

**C-56【必须】** 统计口径按下表分开处理，不能混：

| 统计什么 | 用哪些 attempt |
|:---|:---|
| 解决率（严格 / 有效） | **只看 canonical attempt** |
| 成本、Token 消耗、总耗时 | **累计全部 attempt** |
| 平台故障率 | 只看 canonical attempt |
| `retry_count` | 该题的 attempt 总数减 1 |
| `recovered_infra_failure_count` | 重试后恢复正常的平台故障次数 |

> 最后两个指标要在实验报告里单独展示。它们回答一个重要问题：**这次实验到底顺不顺利**。一个解决率 40%、零重试的实验，和一个解决率 40%、重试了 30 次才凑齐的实验，可信度完全不同。

**C-57【必须】** 数据库中用 `evaluation_task_runs.is_canonical boolean` 显式标记，并配一个部分唯一索引 `(evaluation_run_id, benchmark_task_id) WHERE is_canonical` 保证每题至多一个。

> v1.1 修正记录：原文还写了"或在 `evaluation_runs` 上记 `selected_attempt_id`"，这是错的——一次实验有上百道题，一个字段装不下每道题各自的选择。该方案删除。

**C-70【必须】** "每题恰好一个 canonical attempt"这条只约束状态为 `COMPLETED` 或 `PARTIAL` 的实验。被取消的实验、调度层自己失败的实验不强制。

**C-71【必须】** 设一个**全局最大 attempt 数**（建议 4），一道题的 attempt 总数达到上限后不再重试，直接把最后一次作为 canonical。

> 为什么需要这个上限：C-18 是按错误类型分别规定重试次数的。如果一道题先遇到 `ENV_BUILD_FAILED`（重试 1 次）、再遇到 `SANDBOX_ERROR`（重试 2 次）、再遇到 `OOM_KILLED`（重试 1 次），每种错误各自的预算都没超，但这道题已经跑了 7 次。不设全局上限，重试预算会被不同错误类型轮流重置。

**C-72【必须】** C-20 里那次"不打补丁的对照测试"是**诊断执行，不是一次 attempt**。它不写入 `evaluation_task_runs`，不参与 canonical 选取，其耗时单独记录。

**C-58【禁止】** 靠"取最大的 `attempt_no`"临时推断 canonical attempt。

> 为什么：按 C-24 的规则，canonical 不一定是编号最大的那个。比如第 1 次就是 `AGENT_TIMEOUT`（不可重试），那它就是 canonical，即使后面因为别的原因又产生了记录。

### 3.4 排行榜准入

**C-26【必须】** 平台故障率**大于** 5% 的实验结果不得进入排行榜。**正好等于 5% 可以进入。**

**C-26a【必须】** 允许的平台故障题数用向下取整计算：`floor(题库总题数 × 0.05)`。

> 例：60 题最多允许 3 题平台故障；100 题最多允许 5 题。用整数题数而不是浮点百分比，避免"4.9999% 算不算超"这种争论。

**C-26b【必须】** 不准入的实验，数据库状态记为 `PARTIAL`（C-33 的枚举值），前端展示为"降级"。

> v1.0 修正记录：原文写的是标记为 `DEGRADED`，但运行状态枚举里根本没有这个值，只有 `PARTIAL`。统一用 `PARTIAL`，`DEGRADED` 只作为界面上的中文说法。

**C-59【必须】** 修改 5% 这个门槛，必须发布协议新版本（v1.1、v1.2……），并且排行榜要**按协议版本分开展示**，不能把不同门槛下的结果混排。

**C-60【禁止】** 根据某一次试跑的结果直接调整门槛。

> 为什么：门槛是评判标准的一部分。用一次实验的结果反过来调整评判标准，等于让被评的对象决定及格线。要调也应该基于多次实验的积累，并且走版本升级流程。

**C-27【必须】** 工作区有未提交改动时（`git status --porcelain` 非空），拒绝启动正式实验。

> 为什么：实验记录里存的代码版本号，只有在工作区干净时才能唯一对应一份代码。否则"可复现"是假的。

**C-28【建议】** 调试时可用 `--allow-dirty` 绕过 C-27，但结果必须标记 `dirty = true` 且不得进入排行榜。

---

## 4 状态机

### 4.1 单题状态流转

```
QUEUED → PREPARING → AGENT_RUNNING → PATCH_CAPTURED → TESTING → JUDGING → ANALYZING → COMPLETED
```

任何状态都可以跳到 `FAILED` 或 `CANCELLED`。**没有 `TIMEOUT` 终态**（C-04a）。

### 4.2 四条必须永远成立的规则

**C-29【必须】** 只有 `COMPLETED` 允许 `agent_outcome` 非空。

**C-30【必须】** `infra_outcome` 不是 `SUCCESS` 时，`agent_outcome` **要么为 `NULL`，要么只能是** `UNRESOLVED`、`INVALID_PATCH` 或 `NOT_ATTEMPTED`。**禁止**为 `RESOLVED` 或 `EMPTY_PATCH`。

具体取哪个，以 C-68 的组合表为准。

> v1.0/v1.1 修正记录：原条款写成"只能是 UNRESOLVED 或 NOT_ATTEMPTED"，有两处问题——一是与映射表规定的 `PATCH_APPLY_FAILED → INVALID_PATCH` 矛盾；二是它不允许 `NULL`，但人工取消和"AI 启动后平台故障"这两种情况按 C-68 都必须是 `NULL`。现改为"非空时只能是……"。

**C-31【必须】** `AGENT_RUNNING` 是唯一允许容器联网的阶段。`TESTING` 阶段必须加 `--network none`。

**C-32【必须】** 状态只能往前走。重试的做法是新建一条记录（`attempt_no` 加 1），**禁止**把已有记录的状态改回去。

> 为什么：如果允许回退，一次失败的运行被重试成功之后，就再也查不到它第一次为什么失败了。

### 4.3 一次实验的状态

**C-33【必须】** 取值只能是：`DRAFT`、`QUEUED`、`RUNNING`、`COMPLETED`、`PARTIAL`、`FAILED`、`CANCELLED`。

- `COMPLETED`：全部子任务结束，且平台故障题数 ≤ `floor(总题数 × 0.05)` → 可进排行榜
- `PARTIAL`：全部子任务结束，但平台故障题数超标 → 前端显示"降级"，不进排行榜
- `FAILED`：调度层自己挂了

---

## 4.3 合法组合真值表（机器穷举，冻结前的最后一道检查）

把 `lifecycle_status`（10 个取值）× `infra_outcome`（13 个）× `agent_outcome`（6 个）全部 **780 种组合**跑一遍，逐格判定合法性。

由 `docs/_protocol_truth_table.py` 生成，**不是手写的**。改动协议后重跑一次即可确认没有引入新的空洞或重叠。

```
穷举组合总数：780
  合法 110 · 非法 520 · 不可能 150

空洞检查（每个取值是否都能到达）
  lifecycle 终态: ✅ 全部可达
  infra_outcome: ✅ 全部可达
  agent_outcome: ✅ 全部可达

重叠检查（同一 lifecycle+infra 是否有多个 agent_outcome 且缺少区分条件）
  ✅ 无未区分的重叠
  以下 4 处是设计上就靠附加条件区分的，不是问题：
    COMPLETED + SUCCESS            → RESOLVED / UNRESOLVED / EMPTY_PATCH
    FAILED + AGENT_AUTH_ERROR      → NOT_ATTEMPTED / NULL
    FAILED + SANDBOX_ERROR         → NOT_ATTEMPTED / NULL
    FAILED + HARNESS_ERROR         → NOT_ATTEMPTED / NULL
```

### 全部合法组合（共 20 类）

| `lifecycle_status` | `infra_outcome` | `agent_outcome` | 区分条件 |
|:---|:---|:---|:---|
| 全部非终态 | 任意 | `NULL` | 非终态一律为空（C-09） |
| `COMPLETED` | `SUCCESS` | `RESOLVED` | F2P 全过且 P2P 全过 |
| `COMPLETED` | `SUCCESS` | `UNRESOLVED` | 补丁非空但没同时满足两个条件 |
| `COMPLETED` | `SUCCESS` | `EMPTY_PATCH` | AI 正常退出且过滤后补丁为空 |
| `COMPLETED` | `AGENT_TIMEOUT` | `UNRESOLVED` | 补丁可为空（C-08 例外） |
| `COMPLETED` | `AGENT_RUNTIME_ERROR` | `UNRESOLVED` | 每个 attempt 各自定性，与是否还会重试无关 |
| `COMPLETED` | `PATCH_APPLY_FAILED` | `INVALID_PATCH` | — |
| `COMPLETED` | `TEST_TIMEOUT` | `UNRESOLVED` | 对照组正常，只有打了补丁才超时 → AI 的问题（C-20 第 4 步） |
| `FAILED` | `AGENT_AUTH_ERROR` | `NOT_ATTEMPTED` | agent_started_at IS NULL（容器启动前就鉴权失败） |
| `FAILED` | `AGENT_AUTH_ERROR` | `NULL` | agent_started_at IS NOT NULL（跑起来后才 401） |
| `FAILED` | `ENV_BUILD_FAILED` | `NOT_ATTEMPTED` | 只可能发生在 PREPARING，AI 必然未启动 |
| `FAILED` | `WORKSPACE_ERROR` | `NOT_ATTEMPTED` | 只可能发生在 PREPARING，AI 必然未启动 |
| `FAILED` | `SANDBOX_ERROR` | `NOT_ATTEMPTED` | agent_started_at IS NULL（建 Agent 容器就失败） |
| `FAILED` | `SANDBOX_ERROR` | `NULL` | agent_started_at IS NOT NULL（建测试容器时失败） |
| `FAILED` | `OOM_KILLED` | `NULL` | 只可能发生在 AGENT_RUNNING 或 TESTING，AI 必然已启动 |
| `FAILED` | `TEST_TIMEOUT` | `NULL` | 对照组也超时 → 环境问题（C-20 第 5 步） |
| `FAILED` | `TEST_DISCOVERY_ERROR` | `NULL` | 只可能发生在 JUDGING，AI 必然已启动 |
| `FAILED` | `HARNESS_ERROR` | `NOT_ATTEMPTED` | agent_started_at IS NULL |
| `FAILED` | `HARNESS_ERROR` | `NULL` | agent_started_at IS NOT NULL |
| `CANCELLED` | `CANCELLED` | `NULL` | — |

**C-78【必须】** 上表之外的任何组合都视为程序错误，必须在写库前拦下并抛异常，**禁止**静默落库。

**C-79【必须】** 本表由脚本生成。协议中任何影响状态取值的改动，都要重跑 `docs/_protocol_truth_table.py` 并把输出同步回本节。

> 这条检查在 v1.2 起草时就抓到了一个矛盾：C-18 原本把所有平台故障一律映射成 `NOT_ATTEMPTED`，但 `OOM_KILLED` 和 `TEST_DISCOVERY_ERROR` 必然发生在 AI 启动之后，按 C-69 只能是 `NULL`。人工逐条看两遍都没发现，穷举一跑就出来了。

---

## 5 确定性要求

**C-34【必须】** **判定逻辑本身必须完全确定。** 给定完全相同的逐条用例状态，判定函数必须返回完全相同的 `agent_outcome`。这是一个纯函数，不允许有任何随机性。

**C-73【必须】** **测试执行的可复现性是目标，不是保证。** 同一份补丁重复执行，逐条用例结果应当一致；如果不一致，必须记录为**环境波动**或**不稳定用例**并告警，**禁止**在报告中声称测试执行绝对确定。

> v1.1 修正记录：原 C-34 要求"任何机器上返回完全一样的结果"，但 C-20 又允许第一次测试超时、重跑一次就通过——这等于已经承认了机器负载会改变执行结果。两条不能同时成立。
> 现在拆开：**判定逻辑**是纯函数，100% 确定；**测试执行**受机器状态影响，我们尽力控制（断网、固定环境变量、剔除不稳定用例），但必须诚实地承认它不是绝对的，并把偏差记录下来。
> 这个区分对报告很重要：如果有人质疑结果可复现性，我们能明确说出"哪一部分是保证的，哪一部分是尽力而为并且我们测量了偏差"。

**C-35【必须】** 跑测试时加 `--network none`。

**C-36【必须】** 镜像用 digest（内容哈希）引用，不用 tag。

> 为什么：tag 会被覆盖，digest 不会。

**C-37【必须】** 固定这些环境变量：`PYTHONHASHSEED=0`、`TZ=UTC`、`LC_ALL=C.UTF-8`，并关掉 pytest 的随机排序插件。

**C-38【必须】** 测试命令写死在题目定义里，被测 AI 影响不了它。

**C-39【必须】** 题目验证阶段把 P2P 连跑两遍，两遍结果不一致的用例从名单里剔除并记录。

**C-40【禁止】** 用大模型判断 bug 修没修好。大模型只能用于分析失败原因，且其输出不得回写 `agent_outcome`。

---

## 6 防作弊要求

**C-41【必须】** 被测 AI 的补丁中，命中受保护路径的部分一律丢弃。

**C-42【必须】** 受保护路径的默认清单：

```
# 测试代码（含嵌套目录，不只是仓库根下的 tests/）
tests/**            test/**
**/tests/**         **/test/**
**/test_*.py        **/*_test.py

# 测试收集与运行配置
**/conftest.py      pytest.ini      .pytest.ini
tox.ini             setup.cfg       pyproject.toml

# Python 启动时会被自动导入的文件（可用来做任意注入）
**/sitecustomize.py **/usercustomize.py

# CI 配置
.github/**

# 该题 test_patch 实际改动的每一个文件路径
# —— 包括名字看起来不像测试的 fixture、数据文件、辅助模块
<该题的 test_patch_paths，见 C-74>
```

> **v1.0 遗漏修正**：原清单漏了 `pyproject.toml`，但 `docs/plan/03-benchmark-spec.md` §7.6 已经明确要求保护它（因为 `[tool.pytest.*]` 段落能改变测试行为，简化处理为整文件保护）。协议与已冻结的任务规范不一致，属于必须修掉的缺陷。
> 同时补上：嵌套目录下的测试（原来只写了仓库根下的 `tests/`）、`.pytest.ini`、`usercustomize.py`、以及 `test_patch` 实际触碰的所有路径。最后一条最重要——有些题目的测试改动会带上名字完全不像测试的 fixture 文件，靠通配符是匹配不到的。

**C-74【必须】** 题目 Schema 增加 `test_patch_paths` 字段，存放 `test_patch` 实际改动的全部文件路径：

```jsonc
"test_patch_paths": [
  "tests/test_adapter.py",
  "tests/fixtures/reconnect.json"
]
```

生成与校验规则：

1. 由 Validator 从 `test_patch` 推导，**不是**人工填写
2. 统一转成**仓库相对的 POSIX 路径**（正斜杠、无 `./` 前缀）
3. 排序 + 去重
4. 出现 rename 或 copy 时，**旧路径和新路径都要记录**
5. 纳入 `content_hash` 计算
6. 导入题目和题目验证时**重新计算一遍**，与已存清单不一致就拒绝该题
7. 运行时直接用已验证的清单，不必每次重新解析 diff

> 第 6 条是防篡改：如果有人手工改了 `test_patch_paths` 想放开某个文件的保护，重新计算时就会对不上。

**C-75【必须】** 受保护路径拆成两份，**用途不同，不能混用**：

| 名称 | 内容 | 用在哪 |
|:---|:---|:---|
| `enforcement_protected_paths` | 完整清单，**包含该题的 `test_patch_paths`** | 平台内部执行过滤与还原 |
| `agent_visible_protected_paths` | **只含通用规则**（`tests/**`、`conftest.py` 这类），**不含 `test_patch_paths`** | 下发给被测 AI 的任务输入 |

**C-76【禁止】** 把 `test_patch_paths` 下发给被测 AI，或以任何形式让 AI 推断出它。

> 为什么：`docs/plan/04-runner-protocol.md` 的任务输入里有 `protected_paths` 字段，本意是告诉 AI"改这些也没用"。但如果把该题 `test_patch` 实际触碰的路径也放进去，等于**直接告诉 AI 官方测试补丁改了哪几个文件**——这是明确的定位提示。
> 我们没有下发 F2P 的用例 ID，却通过这个字段泄露了测试补丁的位置，属于同一类泄题。

**C-61【必须】** 每个环境规格（`environment_spec`）只能在默认清单上**追加**路径，**禁止**整体替换或删减默认清单。

> 为什么：允许替换的话，某个仓库的配置写错一次，防作弊就整体失效了，而且不会有任何报错。

**C-62【必须】** 补丁里出现文件重命名或复制时，**旧路径和新路径只要有一个受保护，整个文件的改动全部丢弃**。

> 为什么：否则 AI 可以把 `tests/test_a.py` 改名成 `helper.py` 再改内容，绕过路径匹配。

**C-63【必须】** 第二道防线（C-16 的强制还原）不仅要还原已存在的文件，**还要删除 AI 新增的受保护路径下的文件**。

> 为什么：`git checkout -- tests/` 只能还原已被 git 跟踪的文件，**删不掉 AI 新建的未跟踪文件**。AI 完全可以新增一个 `conftest.py` 来做猴子补丁。

**C-63a【禁止】** 对整个工作区执行无路径限制的 `git clean -fd`。

> 为什么：那会把 AI **合法新增的源代码文件**一起删掉。修 bug 时新建一个模块文件是完全正常的行为，删掉它等于把正确答案删了。

**C-63b【必须】** 只删除"经过路径归一化、并确认命中受保护规则"的新增文件。实现上是先算出要删的具体文件清单，再逐个删，**不是**对目录做通配清理。

**C-64【必须】** 题目验证阶段，如果 `gold_patch` 命中了任何受保护路径，直接判该题**无效**。

> 为什么：官方的正确答案如果需要改受保护文件，那这道题的正确答案会被平台过滤掉，Oracle 哨兵测试（T-7）必然失败。与其等到发布前才发现，不如在验证阶段就拦下来。
> 已知的取舍：这会剔除掉"修复本身就需要改 `pyproject.toml`"的题目（比如加一个依赖）。这类题目对 AI 不公平，剔除是正确的。

**C-43【必须】** 工作目录里不能有 base commit 之后的 git 历史。做法：`git archive` 导出文件树 → `git init` → 只提交一次。

> 为什么：直接 clone 再 checkout 的话，被测 AI 一句 `git log origin/main` 就能翻到官方的修复代码。

**C-44【必须】** 官方补丁不得进入工作目录，也不得出现在下发给被测 AI 的任何数据里。

**C-45【建议】** AI 阶段的联网走域名白名单，只放行大模型 API，禁止访问 github.com。

> 如果白名单代理调试受阻，退化方案：不给 AI 仓库地址和 PR 编号，并在事后检查它的操作记录里有没有访问原 PR。

---

## 7 数据库字段约定（给 E0-T3 用）

**C-46【必须】** 上述四个枚举在数据库中用原生 enum 类型或带 CHECK 约束的 varchar，**禁止**用裸字符串。

**C-47【必须】** 枚举的取值必须与本文件逐字一致。写一个单元测试锁死这件事：遍历代码里的枚举定义，与本文件的清单比对，不一致就失败。

**C-48【必须】** `evaluation_task_runs` 表对 `(evaluation_run_id, benchmark_task_id, attempt_no)` 建唯一索引。

---

## 8 必须实现的测试（给 E4-T3 用）

**C-49【必须】** 下列断言全部实现为自动化测试，缺一不可：

| 编号 | 断言 |
|:---|:---|
| T-1 | 判定真值表：F2P 全过 + P2P 全过 → `RESOLVED`；其余组合 → `UNRESOLVED` |
| T-1a | **组合合法性**：遍历 §4.3 的 780 种组合，合法的能写库、非法的抛异常（C-78）。测试数据直接由 `docs/_protocol_truth_table.py` 生成 |
| T-2 | F2P 中有 `MISSING` → 按 C-13 的三分支处理；解析器问题走 `FAILED`，不罚 AI |
| T-3 | 空补丁 → `EMPTY_PATCH` |
| T-4 | 补丁打不上 → `INVALID_PATCH` |
| T-5 | `infra_outcome` 非 `SUCCESS` 时 `agent_outcome` 不是 `RESOLVED`（C-30） |
| T-6 | 状态不能回退（C-32） |
| T-7 | 用官方补丁跑整个题库 → 解决率 100%（Oracle 哨兵） |
| T-8 | 用空补丁跑整个题库 → 解决率 0%（Noop 哨兵） |
| T-9a | 判定函数：喂同样的逐条用例状态，返回同样的 `agent_outcome`（C-34，单元测试） |
| T-9b | 测试执行：同一补丁重跑 3 次，结果不一致时被正确标记为环境波动或不稳定用例（C-73，集成测试） |
| T-10 | 被测 AI 改了测试文件 → 该改动被丢弃，判定结果不受影响（C-41） |
| T-11 | 工作目录 `git log --all --oneline \| wc -l` 等于 1（C-43） |
| T-12 | 测试容器内访问外网失败（C-35） |
| T-13 | 内存超限 → `OOMKilled` 为 true，且不被误判成超时（C-06、C-07） |
| T-14 | 用例 ID 归一化覆盖至少 6 种写法（相对路径、绝对路径、`./` 前缀、参数化、类方法、多层目录） |
| T-15 | canonical attempt 选取规则：首个不可重试结果优先，全可重试时取最后一个自动 attempt（C-24） |
| T-16 | AI 新增受保护路径下的文件（如新建 `conftest.py`）→ 该文件被删除，判定不受影响（C-63） |
| T-17 | 补丁把受保护文件改名成普通文件 → 整个文件改动被丢弃（C-62） |
| T-18 | `gold_patch` 命中受保护路径 → 题目验证判无效（C-64） |
| T-19 | 只改受保护文件的 AI → `EMPTY_PATCH` 且 `protected_path_edit_attempted = true`（C-08a、C-08b） |
| T-20 | `infra_outcome ≠ SUCCESS` 时 `agent_outcome` 不能是 `EMPTY_PATCH`（C-30） |
| T-22 | `NOT_ATTEMPTED` 出现时 `agent_started_at` 必为空，反之亦然（C-69、C-77） |
| T-23 | `OOM_KILLED` / `TEST_DISCOVERY_ERROR` 永远不会得到 `NOT_ATTEMPTED`（C-18 修正项） |
| T-21 | 成本与 Token 累计全部 attempt，解决率只看 canonical attempt（C-56） |

**C-50【必须】** T-7 和 T-8 作为题库发布的门槛，不通过不许发布。

---

## 9 变更流程

**C-51【必须】** 本文件冻结后的任何改动，都要走以下流程：

1. 提 issue 说明改什么、为什么、影响哪些模块
2. 至少 1 人 review
3. 通过后更新本文件，版本号加 1，并在下方变更记录里登记
4. 同步更新受影响的代码、数据库迁移、`docs/plan/02-evaluation-semantics.md`

**C-52【必须】** 已发布的实验结果必须记录它当时依据的协议版本号。协议改版后，旧结果不重算，但报告中要注明版本差异。

**C-67【必须】** `evaluation_runs` 表增加 `protocol_version` 字段，在实验创建时写入，**禁止**事后修改。

### 变更记录

| 版本 | 日期 | 改了什么 | 提出人 |
|:---|:---|:---|:---|
| v1.0-draft | 2026-09-02 | 起草 | — |
| v1.1-draft | 2026-09-02 | 第一轮评审结论落地 | 评审 |
| v1.2-draft | 2026-09-02 | 第二轮评审：Q7~Q9 定案 + 修掉 7 组内部问题 | 评审 |
| v1.2-draft | 2026-09-02 | 追加 §4.3 机器穷举真值表；据此修掉 C-18 的第 8 组矛盾 | 穷举检查 |

**v1.2-draft 改动清单**

Q7~Q9 定案：Q7 维持不做影子判定（改为离线分析，C-09b、C-09c）；Q8 接受取消 `TIMEOUT` 终态，以完成本轮修复为生效前提；Q9 存 `test_patch_paths` 清单并由 Validator 强制校验（C-74~C-76）。

修掉 7 组内部问题：

| # | 问题 | 怎么修的 |
|:--|:---|:---|
| 1 | C-02 说三字段不能互相推导，但 C-09/C-18/C-30 正在规定它们的合法组合 | 改为"禁止合并存储，但存在合法组合约束" |
| 2 | C-09 要求 `FAILED` 时 `agent_outcome` 为 `NULL`，映射表却把平台故障全映射成 `NOT_ATTEMPTED` | 新增 C-68 六种合法组合表；C-69 规定 `NOT_ATTEMPTED` 当且仅当 `agent_started_at IS NULL`；C-30 改为"非空时只能是……" |
| 3 | `UNRESOLVED` 定义要求补丁非空，但 AI 可能没改任何文件就超时 | 定义加例外：协议明确归属 AI 的失败允许补丁为空；C-08c 区分它与 `EMPTY_PATCH` |
| 4 | C-13 先无条件判 `UNRESOLVED`，C-13b 才去查是不是我们自己的解析器出错，顺序反了 | C-13 改成先自检再三分支：解析器问题走 `FAILED` 不罚 AI；C-13f 加系统性异常闸门 |
| 5 | canonical attempt 三处边界未定义；`selected_attempt_id` 方案不可行 | C-70 唯一性只约束 COMPLETED/PARTIAL；C-71 全局最大 attempt 上限；C-72 对照测试不算 attempt；删除 `selected_attempt_id` |
| 6 | C-63 的 `git clean -fd` 会删掉 AI 合法新增的源文件 | C-63a 禁止无路径限制清理；C-63b 只删确认命中规则的具体文件 |
| 7 | C-34 要求任何机器结果完全一致，但 C-20 允许重跑后结果不同 | 拆成两条：C-34 判定逻辑是纯函数必须确定；C-73 测试执行可复现是目标不是保证，偏差要记录告警 |

同步修改的规划文档：§6 不变式（补合法组合表）、任务 Schema（加 `test_patch_paths`）、数据模型（`benchmark_tasks.test_patch_paths`）、Runner 协议（说明测试路径不下发）、状态机图（`EMPTY_PATCH` 和 `AGENT_RUNTIME_ERROR` 原来画错了）。

测试断言增至 22 条（T-9 拆成 T-9a / T-9b）。

**v1.1-draft 改动清单**

修掉三处内部矛盾：

| # | 矛盾 | 怎么修的 |
|:--|:---|:---|
| 1 | C-09 规定只有 `COMPLETED` 才能有 `agent_outcome`，但映射表要求 `AGENT_TIMEOUT` 判 `UNRESOLVED` | 取消 `TIMEOUT` 终态（C-04a）。AI 超时算"拿到了结论"，落 `COMPLETED`；环境问题超时落 `FAILED` |
| 2 | C-30 只允许 `UNRESOLVED`/`NOT_ATTEMPTED`，映射表却要求 `PATCH_APPLY_FAILED → INVALID_PATCH` | C-30 补入 `INVALID_PATCH` |
| 3 | 有效解决率用 `infra_outcome = SUCCESS` 做分母，会把 AI 自己的失败也排除掉，导致虚高 | 分母改为 `agent_outcome ∈ {RESOLVED, UNRESOLVED, EMPTY_PATCH, INVALID_PATCH}` |

六个待定问题全部有结论：

| 问题 | 结论 | 新增/修改条款 |
|:---|:---|:---|
| Q1 测试超时算谁的 | 采纳并加固：定成六步对照流程，禁止一次超时就隔离题目 | C-20、C-20a |
| Q2 重试后用哪次结果 | 改写：不用"最后一次跑完"，改为规则选出的 canonical attempt；成本累计全部 attempt | C-24、C-25、C-53~C-58 |
| Q3 5% 门槛 | 保留 5%，明确"大于才不准入"、用 `floor` 算题数、改门槛需升版本 | C-26、C-26a、C-26b、C-59、C-60 |
| Q4 空补丁是否展示 | 展示但只作诊断，不参与排名；并澄清它不等于"没动手" | C-08a、C-08b、C-65、C-66 |
| Q5 MISSING 是否进复核 | 进，但改叫"测试完整性异常"，不能直接当作弊；先自检 ID 归一化，并去重 | C-13、C-13a~C-13e |
| Q6 受保护路径够不够 | 不够。补 `pyproject.toml` 等，并加四条匹配规则 | C-42、C-61~C-64 |

新增测试断言 T-15 ~ T-21。

---

## 10 评审记录与仍待决定的问题

### 10.1 第一轮评审（2026-09-02）—— 已决议

原 Q1~Q6 全部有结论，落地情况见 §9 的改动清单。原表保留在下方存档。

### 10.2 仍待决定

| 编号 | 问题 | 起草建议 | 决定 |
|:---|:---|:---|:---|
| **Q7** | 超时时要不要跑"影子判定" | v1 不做 | ✅ **维持不做**。补丁仍存档，只统计超时数与"超时但补丁非空"数（C-09b）；影子判定日后做成离线分析任务，不占关键路径（C-09c） |
| **Q8** | 取消 `TIMEOUT` 终态是否接受 | 接受 | ✅ **接受**，但以完成本轮全部一致性修复为生效前提 |
| **Q9** | `test_patch` 路径要不要存进题目 Schema | 需要 | ✅ **存清单**，由 Validator 生成并强制校验（C-74）；且必须拆成内部执行用与下发给 AI 用两份，禁止泄露（C-75、C-76） |

### 10.3 第一轮问题存档

**下表是第一轮评审的原始议题，结论已落地，保留作为决策记录。**

| 编号 | 问题 | 起草建议 | 决定 |
|:---|:---|:---|:---|
| **Q1** | `TEST_TIMEOUT` 算谁的问题？ | 按 C-20：基线也超时算题目问题，只有打补丁才超时算 AI 问题 | ✅ **有条件采纳** → 固定为六步对照流程（C-20） |
| **Q2** | 一道题重试多次，用哪次的结果统计？ | 用最后一次跑完的结果，禁止取最优 | ✅ **改写** → canonical attempt（C-24、C-53~C-58） |
| **Q3** | 排行榜准入的平台故障率门槛定 5% 合适吗？ | 暂定 5%，等第一次小规模试跑后用实测数据校准 | ✅ **采纳** → 保留 5%，补充计算与改版规则 |
| **Q4** | `EMPTY_PATCH` 要不要在排行榜单独列一列？ | 要。它区分"试了但没做对"和"根本没动手"，对分析 AI 能力有意义 | ✅ **采纳** → 仅作诊断指标，不参与排名（C-65、C-66） |
| **Q5** | F2P 出现 `MISSING` 时，除了判失败，要不要自动进人工复核队列？ | 要。这是疑似作弊信号，值得人看一眼 | ✅ **采纳** → 改名为测试完整性异常（C-13 系列） |
| **Q6** | 受保护路径清单（C-42）够不够？有没有遗漏的作弊入口？ | 见 C-42 | ✅ **不够** → 已补 `pyproject.toml` 等 + 四条匹配规则 |

---

## 11 评审签字

**当前状态：DRAFT v1.2，尚未冻结。按 C-46、C-47 的要求，E0-T3 建表不得开始。**

分析性的工作已经做完了。冻结现在只差一个**人的决定**，不是还有什么没想清楚。

### 已完成

| 项目 | 状态 |
|:---|:---|
| 第一轮评审（Q1~Q6） | ✅ 2026-09-02，六个议题全部有结论 |
| 第二轮评审（Q7~Q9 + 7 组内部问题） | ✅ 2026-09-02 |
| 三处内部矛盾修复 | ✅ C-09/C-68、C-30、有效解决率分母 |
| 第 8 组矛盾（C-18 与 C-69） | ✅ 由 §4.3 穷举发现并修复 |
| 组合真值表复查 | ✅ 780 种组合，0 空洞 0 未区分重叠 |
| 条款编号完整性 | ✅ C-01 ~ C-79，无重复、无缺号、无悬空引用（CI 中持续校验） |

### 冻结前还可以做（可选）

| 项目 | 说明 |
|:---|:---|
| 条款与测试的双向映射检查 | 列出所有描述**运行时行为**的【必须】条款，标出哪些还没有对应的 T-xx 测试断言。产出一份待补测试的清单，不是"100% 覆盖"的结论 |

### 签字

| 项目 | 结论 |
|:---|:---|
| 冻结日期 | |
| 参与人 | |
| 全部条款逐条过完 | ☐ |
| 冻结版本号 | v1.2 |
| 状态变更为 FROZEN | ☐ |

签字后需要同步做三件事：

1. 把本文件顶部状态改为 `FROZEN v1.2`
2. 在 §9 变更记录里登记冻结日期
3. E0-T3 建表可以开始
