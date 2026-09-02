# 6 Evaluation Semantics（评测结果怎么定义 —— 全项目第一冻结项）

> **这一章冻结之后**，判定引擎、数据库的状态字段、前端展示、报表口径全都以它为准。要改必须先开会。
> **最重要的一条原则**：**"被测 AI 没修好"和"我们平台自己出故障"必须分成两个字段存。**
> 把这两件事混在一个字段里，算出来的解决率就不可信了——因为你分不清 30% 的失败里有多少是 AI 不行，有多少是我们的 Docker 崩了。

## 6.1 用三个互相独立的字段描述结果

一次评测（`EvaluationTaskRun`）跑完之后，结果用三个字段描述。这三个字段**互相独立**，意思是任何一个的取值都不影响另外两个该填什么。

| 字段 | 回答什么问题 | 取值 |
|:---|:---|:---|
| `lifecycle_status` | 它现在走到哪一步了？结束了吗？ | 见 §6.5 |
| `infra_outcome` | **我们的平台**这次跑得对不对？结论可不可信？ | 见 §6.3 |
| `agent_outcome` | **被测 AI** 到底修好了没有？ | 见 §6.2，只有平台跑得没问题时才有意义 |

**一个必须避免的反例**：把 `TIMEOUT`（超时）和 `UNRESOLVED`（没修好）放进同一个枚举。

因为"超时"有两种完全不同的情况：

- 被测 AI 自己想太久超时了 —— 这是 AI 的问题，应该算它没修好。
- 我们的沙箱卡住了导致超时 —— 这是平台的问题，这条结果根本不该算数。

同一个词，两种归属。必须靠 `infra_outcome` 把它们分开。

## 6.2 被测 AI 的结果 — `agent_outcome`

| 值 | 什么情况下填这个 |
|:---|:---|
| `RESOLVED` | **所有** `fail_to_pass` 用例都通过了，**并且所有** `pass_to_pass` 用例也都还通过。两个条件缺一不可 |
| `UNRESOLVED` | 交出了一个能用的补丁，但没同时满足上面两个条件 |
| `EMPTY_PATCH` | AI 正常退出了，但一行代码都没改。**算作没修好**，但单独统计出来 |
| `INVALID_PATCH` | 补丁不是空的，但打不上去（`git apply` 失败）。**算作没修好** |
| `NOT_ATTEMPTED` | 平台自己出故障，AI 压根没开始跑。**不算进分母**（严格口径除外） |

**两个解决率都要算出来**，不然后面一定会有人吵"你这个数是怎么算的"：

```
以下全部只统计 canonical attempt（一道题重试多次时被选定的那一次，
定义见 docs/evaluation-protocol.md C-24）

严格解决率 = RESOLVED 数 / 数据集里的全部题数
            ← 排行榜用这个。跟 SWE-Bench 官方口径一致

有效解决率 = RESOLVED 数
           / agent_outcome ∈ {RESOLVED, UNRESOLVED, EMPTY_PATCH, INVALID_PATCH} 的题数
            ← 我们自己诊断用。分母是"确实拿到了一个能归因于 AI 的结果"的题数

平台故障率 = 计入平台故障率的题数 / 全部题数
```

> 注意有效解决率的分母**不是** `infra_outcome = SUCCESS` 的题数。那样会把 AI 超时、补丁打不上、补丁导致测试超时这些**AI 自己的失败**一起排除掉，算出来的数字会虚高。

**排行榜准入规则**：平台故障题数超过 `floor(总题数 × 0.05)` 的实验结果，**不许进排行榜**。数据库状态记为 `PARTIAL`，前端展示为"降级"，并要求重跑。（60 题最多允许 3 题故障，100 题最多允许 5 题。）

这条规则的作用是把"平台质量"变成一条硬性检查，而不是报告末尾的一句备注。

## 6.3 平台自己的结果 — `infra_outcome`

| 值 | 什么时候出现 | 自动重试几次 | 这算谁的问题 |
|:---|:---|:---:|:---|
| `SUCCESS` | 一切正常，判定结论可信 | — | — |
| `ENV_BUILD_FAILED` | 镜像构建不出来、依赖装不上 | 1 | 平台 / 题目 |
| `WORKSPACE_ERROR` | 代码快照准备失败 | 1 | 平台 |
| `AGENT_TIMEOUT` | AI 跑的时间超过 `agent_timeout_s` | **不重试** | **AI**（判为没修好） |
| `AGENT_RUNTIME_ERROR` | AI 进程崩了、退出码非 0、输出不符合协议 | 1 | **AI**（重试后还失败就判没修好） |
| `AGENT_AUTH_ERROR` | 密钥无效、额度用完、限流退避多次仍失败 | 3（带退避） | **外部服务**（不算 AI 能力差） |
| `SANDBOX_ERROR` | Docker 报错、容器建不起来、挂载失败 | 2 | 平台 |
| `OOM_KILLED` | 容器内存超限被系统杀掉 | 1（降配后） | 平台 / 题目 |
| `TEST_TIMEOUT` | 跑测试超过 `test_timeout_s` | 1 | 题目（也可能是补丁写出了死循环 → 判没修好） |
| `TEST_DISCOVERY_ERROR` | 测试框架收集不到用例、报告文件没生成 | 1 | 题目 / 平台 |
| `PATCH_APPLY_FAILED` | 补丁打不上去 | 不重试 | **AI**（→ `INVALID_PATCH`） |
| `HARNESS_ERROR` | 我们自己的代码抛了没接住的异常 | 1 | 平台（必须告警） |
| `CANCELLED` | 人工取消 | 不重试 | 人工 |

### 怎么判断 `OOM_KILLED`

用 `docker inspect --format '{{.State.OOMKilled}}'`，这个字段返回 `true` 才是内存超限。

**不要看退出码。** 内存超限和我们自己超时强杀，退出码**都是 137**。已在开发机上实测确认（见 `05-sandbox.md` §10.3）。靠退出码区分会把两种相反的情况判成一样。

### 哪些算平台故障，哪些不算

上面表里有三行虽然记在 `infra_outcome` 里（因为它们描述"这次执行发生了什么"），但**责任在 AI 身上**：

- `AGENT_TIMEOUT`
- `AGENT_RUNTIME_ERROR`
- `PATCH_APPLY_FAILED`

统计时它们会被映射成 `agent_outcome = UNRESOLVED`，**并且不计入平台故障率**。

真正算平台故障率的只有这七个：`ENV_BUILD_FAILED`、`WORKSPACE_ERROR`、`SANDBOX_ERROR`、`OOM_KILLED`、`TEST_DISCOVERY_ERROR`、`HARNESS_ERROR`、`AGENT_AUTH_ERROR`。

> 这张映射表是整个项目最容易吵架的地方。**必须在代码里写成一张显式的常量表** `INFRA_TO_AGENT_MAPPING`，不要散落在各处的 `if` 分支里——散开写的话，改一处忘一处，两个月后没人说得清某个数字是怎么来的。

## 6.4 单条测试用例的状态 — `test_status`

| 值 | 含义 |
|:---|:---|
| `PASSED` | 通过 |
| `FAILED` | 断言没过 |
| `ERROR` | 用例执行时抛异常，或者收集阶段就报错 |
| `SKIPPED` | 被跳过了 |
| `XFAIL` / `XPASS` | pytest 的"预期会失败"和"居然通过了" |
| `MISSING` | **测试报告里根本找不到这条用例**（被删了、被改名了、或者没被收集到） |

**一条铁律：`MISSING`、`SKIPPED`、`XFAIL` 一律不算通过。**

如果 `fail_to_pass` 里有任何一条变成了 `MISSING`，判 `UNRESOLVED`，并打上 `TEST_RESULT_INTEGRITY_SUSPECTED`（测试完整性异常）的标记，自动进人工复核。

**不要直接当成作弊。** 用例 ID 归一化写错本身就会制造大量假 `MISSING`（见 §11.3，这是全项目最容易出的静默 bug）。只有在发现 AI 确实动了受保护文件之类的实际证据时，才升级为 `TEST_TAMPERING_SUSPECTED`。详细流程见 `docs/evaluation-protocol.md` C-13 系列。

## 6.5 一次评测从头到尾经过哪些状态

```
QUEUED            排队中
  → PREPARING     准备代码快照、检查镜像
  → AGENT_RUNNING 被测 AI 在容器里干活（整个流程中唯一允许联网的阶段）
  → PATCH_CAPTURED 用 git diff 收集它改了什么 → 过滤掉测试文件 → 存成文件
  → TESTING       用干净的代码重来一遍：打上它的补丁 + 官方测试补丁，断网跑测试
  → JUDGING       解析测试报告 → 逐条比对 → 得出 agent_outcome
  → ANALYZING     先用规则分类失败原因，规则搞不定的再交给大模型
  → COMPLETED     结束
```

终态只有三个：

- `COMPLETED` —— 跑完了，拿到了可信结论（哪怕结论是"没修好"）
- `FAILED` —— 因平台故障中止，**没**拿到可信结论
- `CANCELLED` —— 人工取消

**没有 `TIMEOUT` 终态。** 超时的具体类型已经完整记在 `infra_outcome` 里了。AI 自己超时算"拿到了结论"（它没在预算内做完），落 `COMPLETED`；环境问题导致测试超时算"没拿到结论"，落 `FAILED`。

一个状态两种含义会直接产生矛盾，所以不设这个终态。理由见 `docs/evaluation-protocol.md` C-04a。

### 四条必须永远成立的规则（写进单元测试）

1. 非终态时 `agent_outcome` 一律为 `NULL`。终态时按下面这张**合法组合表**取值，其余组合都是程序错误：

   | 情况 | `lifecycle_status` | `agent_outcome` |
   |:---|:---|:---|
   | 正常跑完测试 | `COMPLETED` | `RESOLVED` / `UNRESOLVED` / `EMPTY_PATCH` |
   | AI 超时或自身崩溃 | `COMPLETED` | `UNRESOLVED` |
   | 补丁打不上 | `COMPLETED` | `INVALID_PATCH` |
   | 平台故障且 AI 从未启动 | `FAILED` | `NOT_ATTEMPTED` |
   | AI 已启动，之后平台故障 | `FAILED` | `NULL` |
   | 人工取消 | `CANCELLED` | `NULL` |

   `NOT_ATTEMPTED` 当且仅当 `agent_started_at IS NULL`。

2. `infra_outcome` 不是 `SUCCESS` 时，`agent_outcome` 要么为 `NULL`，要么只能是 `UNRESOLVED`、`INVALID_PATCH` 或 `NOT_ATTEMPTED`。
3. `AGENT_RUNNING` 是唯一允许容器联网的阶段。`TESTING` 阶段必须加 `--network none`。
4. 状态只能往前走，**不能回退**。重试的做法是新建一条记录（`attempt_no` 加 1），而不是把原来那条改回去。

第 4 条是为了保住历史。如果允许状态回退，一次失败的运行被重试成功后，你就再也查不到它第一次为什么失败了。

## 6.6 整个实验（EvaluationRun）的状态

`DRAFT → QUEUED → RUNNING → COMPLETED | PARTIAL | FAILED | CANCELLED`

- `COMPLETED`：所有子任务都结束了，且平台故障题数 ≤ `floor(总题数 × 0.05)` → **可以进排行榜**
- `PARTIAL`：所有子任务都结束了，但平台故障题数超标 → 前端显示"降级"，不进排行榜
- `FAILED`：调度层自己挂了（比如镜像仓库整体连不上）

## 6.7 "判定必须可复现"到底是什么意思

> 给定同样的四样东西——题目版本、补丁内容、镜像 digest、平台代码版本——判定引擎在任何时间、任何机器上都必须返回**完全一样**的结论和**完全一样**的逐条用例状态。

靠这五件事保证：

1. 跑测试时加 `--network none`，杜绝外部依赖偷偷变化。
2. 镜像用 digest（内容哈希）引用，不用 tag——tag 会被覆盖，digest 不会。
3. 固定几个影响随机性的环境变量：`PYTHONHASHSEED=0`、`TZ=UTC`、`LC_ALL=C.UTF-8`，并关掉 pytest 的随机排序插件（`-p no:randomly`）。
4. 测试命令写死在题目定义里，被测 AI 影响不了它。
5. 题目验证阶段把 `pass_to_pass` 连跑两遍，两遍结果不一致的用例（不稳定用例）直接从名单里剔除并记录。

**验收方法**：拿 5 道人工构造的题目，用官方的正确补丁跑 3 轮，要求 15 次判定结论全部一致，且每条用例的状态也完全一致。
