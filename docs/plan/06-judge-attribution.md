# 11 Judge Design（判定引擎：怎么判断修好了没有）

## 11.1 第一条原则

> **判定必须完全确定，不能有随机性。禁止用大模型判断 bug 修没修好。**
> 大模型**只能**用在下一章"分析它为什么没修好"，而且它的输出不允许回写判定结果。

为什么这么严格：基准的价值来自可比较。如果判定引擎有随机性，这个月的排行榜和下个月的排行榜就没法放在一起看，整个平台就失去意义了。

## 11.2 判定流程

```
1. 准备一份干净的代码（从 base commit 重新导出，不复用 AI 用过的那份）
2. 打上 AI 的补丁                失败 → INVALID_PATCH / PATCH_APPLY_FAILED
3. 强制还原被保护的文件           git checkout -- <测试文件等>     ← 防作弊
4. 打上官方的测试补丁             失败 → TEST_DISCOVERY_ERROR（题目坏了）
5. 跑测试                        只跑需要的用例，断网，带资源和时间限制
6. 解析报告 → 得到 {用例ID: 状态}
7. 判定：
     f2p_ok = fail_to_pass 里每一条的状态都是 PASSED
     p2p_ok = pass_to_pass 里每一条的状态都是 PASSED
     修好了 = f2p_ok 且 p2p_ok
8. 把每条用例的结果和汇总数据写进数据库
```

**第 3 步为什么还要再还原一次**：生成补丁时其实已经按路径过滤掉测试文件了。这里再强制还原一遍，是第二道防线。两处实现只要有一处写出 bug，基准都不会被攻破。

> **实现回填（E4-T2，2026-09-05）**：第 3 步分两半，缺一不可。
>
> - **已跟踪的文件** → `git checkout HEAD -- <具体路径>`，还原被改的和被删的。
> - **AI 新增的文件** → 逐个删。`git checkout` 只管已跟踪的文件，删不掉新建的未跟踪文件，而 AI 完全可以新建一个 `conftest.py` 做猴子补丁（C-63）。扫的时候要带 `include_ignored=True`，否则 `tests/__pycache__/*.pyc` 列不出来也删不掉。
>
> **踩到的坑：`git apply --3way` 会动索引。** 它走三方合并那条路时会把结果**暂存进索引**，于是 AI 新建的 `conftest.py` 变成"已暂存的新增文件"。这时 `git diff --name-only HEAD` 会列出它，可它在 HEAD 里根本不存在，`git checkout HEAD -- conftest.py` 当场报 `pathspec did not match any file(s) known to git` —— **整条防作弊防线崩在这里**。
>
> 修法是打完补丁立刻 `git reset --quiet HEAD`（只拨索引，不动工作区）。reset 之后状态是统一的：改过的跟踪文件 = 未暂存改动，新建的文件 = 未跟踪文件，正好对应还原那两半；重命名也收敛成"旧路径被删 + 新路径未跟踪"。
>
> 这个坑**只在真的走 `git apply` 时才出现**——直接把文件写进工作区测不出来（那样它只是个普通的未跟踪文件）。回归用例是 `tests/sandbox/test_protected_restore.py::test_patch_added_protected_file_is_deleted`。
>
> 代码在 `backend/app/evaluation/executor.py`。**它不在 `app/judge/`**：import-linter 的分层里 `app.sandbox | app.judge` 是并排的，并排就是互不可见，judge 看不到 sandbox，起不了容器。`app.evaluation` 在 runner 之上，两边都看得见。

**第 5 步为什么只跑部分用例**：`pytest 用例ID1 用例ID2 ...` 可以只跑指定的几条。好处是快（这对 6 小时的目标很关键），代价是发现不了 `fail_to_pass` 和 `pass_to_pass` 之外的问题。

这是有意的取舍：`pass_to_pass` 这个集合本身就是我们定义的"回归检查范围"。题目验证阶段会跑全量测试，评测阶段只跑子集。

## 11.3 测试报告解析器

```python
class TestReportParser(Protocol):
    def parse(self, report_path: Path | None, stdout: str, stderr: str) -> ParsedReport: ...
```

> **实现回填（E4-T1，2026-09-05）**：返回值原本写的是 `dict[str, TestStatus]`，实现时改成了 `ParsedReport`。原因是协议 C-13b 要求出现 `MISSING` 时先自检三件事（报告是否完整、ID 归一化对不对、有没有收集错误），光给一个状态字典交不出这些信息，判定引擎就只能无条件判 `UNRESOLVED`——那正是 C-13 在 v1.1 修掉的顺序错误。想要原来那个形状，取 `ParsedReport.statuses`。
>
> 代码在 `backend/app/judge/report_parser.py` 和 `backend/app/judge/test_ids.py`。

| 实现 | 首选方式 | 备用方式 |
|:---|:---|:---|
| `PytestParser` | `--junitxml` 生成的 XML 文件 | `-rA` 输出的文本摘要，用正则解析 |
| `UnittestParser` | `unittest-xml-reporting` | 文本里的 `ok` / `FAIL` / `ERROR` |
| `JestParser`（P2） | `jest-junit` | — |
| `GoTestParser`（P2） | `go test -json` | — |

### 用例 ID 归一化：最容易出静默 bug 的地方

pytest 的用例 ID 长这样：`路径::类名::方法名[参数]`。但同一条用例在不同调用方式下，路径部分可能是相对路径、绝对路径，也可能带 `./` 前缀。

举个具体例子：题目里写的是 `tests/test_a.py::test_x`，测试报告里出现的是 `./tests/test_a.py::test_x`。两个字符串不相等，匹配不上。

**后果**：这条用例被判成 `MISSING`（找不到），进而判定为没修好。**所有题目都会莫名其妙地失败，但程序不报任何错。**

所以必须实现 `normalize_test_id()`，统一成"仓库相对路径 + `::` 分隔"，并且写单元测试覆盖至少 6 种写法：相对路径、绝对路径、带 `./` 前缀、参数化用例、类方法、多层目录。

#### 实测：junitxml 的 classname 是点分模块名，不是文件路径

E4-T1 实现时在开发机上用 pytest 9.1.1 跑出来的结论（2026-09-05）。这几条决定了解析器长什么样：

**① `classname` 有歧义。** `tests/sub/test_nested.py::test_deep` 在 XML 里是 `classname="tests.sub.test_nested" name="test_deep"`；类方法把类名接在后面，`classname="tests.test_shapes.TestGroup" name="test_method"`。于是 `a.b.C` 既可能是 `a/b/C.py`，也可能是 `a/b.py` 里的类 `C`，**光看这一个字符串分不出来**。

**② `junit_family` 决定有没有 `file` 属性。** 默认的 `xunit2` 没有；`-o junit_family=xunit1` 会额外写 `file="tests/sub/test_nested.py" line="0"`，歧义当场消失。**解析器两种都支持**：有 `file` 就用它，没有就按 pytest 的默认收集规则（模块叫 `test_*.py`、类以 `Test` 开头）打分挑最可能的切分，其余切法留作备选 ID 一起匹配。

> **已决定不加（2026-09-05）**：`test_command` 保持现状，不加 `-o junit_family=xunit1`。
>
> 两条理由。一是**不需要**：`test_both_junit_families_agree` 证明了两种 family 解析出来逐条相同，加了只是省掉 classname 的猜测环节。二是**保不住**：真实仓库的 `test_command` 是从上游推导的，未必带得上这个参数；只给 Golden 题加，等于让开发时走的路径和真实评测走的不是同一条——那种"本地全绿、真跑才炸"的差异最难查。
>
> 改主意的话要动三处：`backend/cli/golden.py` 的 `DEFAULT_ENVIRONMENT`、`datasets/golden/environments/*.json`（跑 `make golden` 重新生成）、以及本节这段。

**③ 参数化用例里的非 ASCII 会被转义。** `test_param["带空格 的"]` 在 XML 里是 `name="test_param[\u5e26\u7a7a\u683c \u7684]"`，是字面的反斜杠 u 序列，不是中文。题目里的 F2P ID 写中文原文就对不上，要先还原。这是第 7 种 ID 形态。

#### 实测：状态怎么映射

**① `<error>` 和 `<failure>` 不按直觉分。** 测试函数体里 `raise RuntimeError` 记成 `<failure>`（不是 error）；只有 fixture / setup / teardown 里抛异常和收集失败才是 `<error>`。协议 C-10 的 `ERROR` 对应的是后两种。

**② 收集失败的条目长得完全不一样**：`classname=""`、`name="brk.test_broken"`（点分模块名）、`<error message="collection failure">`，pytest 退出码 2。它不是一条用例，要单独摘出来（`ParsedReport.collection_errors`）。

**③ XFAIL 认得出，非 strict 的 XPASS 认不出。** `<skipped type="pytest.xfail">` 是 XFAIL；但非 strict 的 XPASS 在 XML 里就是一个**没有子元素的普通 testcase**，和 PASSED 一模一样。协议 C-10 要求 XPASS 是独立状态，junitxml 表达不了。

解析器**不装作能分出来**：只有 XML 时如实报成 `PASSED`，并把 `ParsedReport.xpass_may_read_as_passed` 置为 True。`strict=True` 的 XPASS 是例外，两边都算成失败但带 `[XPASS(strict)]` 标记，按标记纠正。文本输出反而分得清（`… XPASS (reason)`），所以两边都有时用文本把 `PASSED` 升级成 `XPASS`——**只升这一种**，其余一律以 XML 为准，免得给判定引入第二个真相来源。

#### 实测：文本兜底只能兜住一半

`-v` 的逐条行（`tests/test_a.py::test_x PASSED [ 33%]`）和 `-rA` 的短摘要（`PASSED tests/test_a.py::test_x`）都能解析，但**两种都要靠参数才有**。默认输出的进度点里没有用例 ID；短摘要那一节默认只打印失败和错误——实测 15 条用例的默认 `-q` 输出只捞得到 4 条，8 条通过的一条也看不见。

短摘要还有一个坑：`SKIPPED [1] tests/test_a.py:26: reason` 给的是**文件:行号**，拿不到用例 ID，只能计数不能猜。

所以 `check_integrity()` 把文本兜底一律记成"报告不完整"。理由是"这条用例没出现"既可能是它没跑，也可能是它通过了但没被打印——分不出来的时候记 `MISSING` 再罚 AI，罚的其实是我们自己的 `test_command` 少写了参数（C-13a）。

### `MISSING` 的处理

题目里列了但报告里找不到的用例，状态记为 `MISSING`，**不算通过**（§6.4 已定）。

**`MISSING` 由判定引擎（E4-T3）产生，不是解析器。** 它的定义是"题目里列了、报告里找不到"（协议 C-11），是一次**比对**的结果，只有同时拿着题目和报告才判得出来。解析器只报它看见的东西——`test_parser_never_emits_missing` 把这条钉住了。

解析器要交出来的是自检材料（C-13b 三项），`ParsedReport.check_integrity(expected_ids)` 返回 `IntegrityCheck`：

| 字段 | 对应 C-13b 第几项 | 干什么用 |
|:---|:---|:---|
| `report_complete` / `report_problem` | ① 报告是否完整生成 | 只有完整解析成功的 junitxml 才算完整；截断的和文本兜底的都不算 |
| `reported_ids` / `missing_ids` | ② ID 归一化对不对 | 两边的 ID 摆在一起给人对照 |
| `near_misses` | ② 同上 | 直接指出"节点部分一样、只有路径不同"的嫌疑对，非空基本就是解析器的锅 |
| `collection_error_modules` | ③ 有没有收集错误 | 如实记录，**不判责** |

`IntegrityCheck.blames_harness` 为 True 时走 C-13 的 (a) 分支：`FAILED` + `HARNESS_ERROR`，`agent_outcome = NULL`，计入平台故障率，不罚 AI。

**收集错误不算进 `blames_harness`**：它既可能是 AI 改坏了 import（分支 b），也可能是题目坏了（分支 a），解析器分不出来。那一步归 E4-T3——它手里有"补丁改了哪些文件"，能拿 C-13c 要求的实际证据去判。

## 11.4 补丁归一化

- **输入**：`git diff` 的原始输出，或者 AI 自己打印出来的 diff
- **处理**：
  1. 删掉命中"受保护路径"的文件段（测试文件、`conftest.py` 等）
  2. 删掉二进制文件和超大文件（单文件超过 256 KB）
  3. 行尾统一成 LF
  4. 删掉只改了文件权限、没改内容的段
  5. 算出改了几个文件、加了几行、删了几行、补丁的哈希
- **输出**：一个能用 `git apply --3way` 打上的标准 diff，外加上面那些统计数据（后面做失败归因和报表要用）

## 11.5 判定引擎自己怎么验证对不对

这两条测试成本极低，但能挡住绝大多数灾难性错误。

**Oracle 测试**：用官方的正确补丁（`gold_patch`）跑整个数据集，**解决率必须是 100%**。

只要不是 100%，就说明要么有题目本身是坏的，要么判定引擎有 bug。**必须清零之后才能开始真实实验**，否则跑出来的数字全是错的。

**Noop 测试**：用空补丁跑整个数据集，**解决率必须是 0%**。

不是 0% 说明有的题目在还没修复的时候，`fail_to_pass` 测试就已经通过了——这题是坏的。

这两条一上一下，把整个数据集的可信度框住了。§27 的测试策略里管它们叫"哨兵测试"，意思是它们像哨兵一样守在数据集发布这道门前。

---

# 12 Failure Attribution Design（失败原因分析）

"归因"就是回答一个问题：这次 AI 没修好，到底是哪一环出的问题。

## 12.1 失败分类

学校要求分 8 类。本规划用 **8 个"AI 的问题"+ 2 个"不是 AI 的问题"**，后者不计入 AI 的失败分布图，只统计在平台健康度里。这样既满足了"8 类"的要求，又不违背 §6 那条"AI 的问题和平台的问题要分开"的原则。

### AI 的问题（进失败分布图）

| 编号 | 类别 | 什么意思 | 怎么识别 |
|:--|:---|:---|:---|
| F1 | `REQUIREMENT_MISUNDERSTANDING` | 改了代码，但解决的不是 issue 说的那个问题 | 改动的文件和官方补丁完全不沾边，且测试报错信息跟原来一模一样 |
| F2 | `WRONG_FILE_LOCALIZATION` | 找错了文件 | 改动文件集合和官方补丁的文件集合没有交集 |
| F3 | `INCOMPLETE_FIX` | 方向对，但没改完 | 改对了文件，但 `fail_to_pass` 还有没过的 |
| F4 | `INCORRECT_LOGIC` | 地方找对了，逻辑写错了 | 改对了文件，且测试**报错信息变了**（说明代码路径确实走了新逻辑） |
| F5 | `SYNTAX_OR_BUILD_ERROR` | 语法错、导入错、编译不过 | 测试日志里出现 `SyntaxError` / `ImportError` / 收集阶段报错 |
| F6 | `REGRESSION` | 目标测试过了，但把别的功能改坏了 | `f2p_ok` 为真且 `p2p_ok` 为假 → **纯规则判定，不需要大模型** |
| F7 | `EMPTY_OR_INVALID_PATCH` | 没改代码 / 补丁打不上 / 只改了被保护的文件 | 看 `agent_outcome` 即可 → **纯规则判定** |
| F8 | `AGENT_TOOL_OR_BUDGET_FAILURE` | AI 自己的工具调用失败、陷入循环、超时、预算耗尽 | `AGENT_TIMEOUT`，或轨迹里工具报错比例很高，或轮数打满 |

### 不是 AI 的问题（不进失败分布图）

- `N1 INFRASTRUCTURE_FAILURE`：环境、沙箱、平台代码、内存超限
- `N2 TASK_DEFECT`：人工复核后确认**题目本身有问题**，触发该题隔离

> `N2` 这一类很多同类项目都没有，但它很重要：它让人工抽检不只是"纠正分类"，还能反过来改进数据集质量。发现一道坏题 → 隔离 → 重算受影响的历史结果。答辩时这是个加分点。

## 12.2 分四步做归因

```
第一步：规则分类（确定性，零成本，能覆盖 55~70%）
        F6 回归 / F7 空补丁 / F8 超时 / N1 平台故障
              ↓ 规则搞不定的往下走
第二步：提取结构化特征（确定性，给大模型当输入）
        · AI 改的文件和官方补丁改的文件，重合度多少
        · 测试报错信息，修改前后是不是变了
        · 测试日志里的错误类型
        · 轨迹统计：跑了多少轮、工具报错多少次
              ↓
第三步：大模型判断（只处理 F1~F5 这几个分不清的情况）
        temperature=0，强制输出结构化 JSON，
        强制引用证据，置信度低就采样 3 次投票
              ↓
第四步：人工盲检抽查（至少 50 例）
```

**第一步为什么重要**：F6、F7、N1 这三类加起来通常占失败的 30~50%，而且规则判定的准确率接近 100%。它们直接把总体准确率的底板抬得很高。

**这是达成"归因准确率 ≥ 85%"最靠谱的手段**，比反复调整提示词可靠得多。

## 12.3 给大模型看什么、要它输出什么

**输入（严格裁剪，控制成本）**

- issue 标题和正文（最多 3000 字符）
- AI 的补丁（最多 6000 字符，太长就按文件摘要 + 首尾片段）
- 官方补丁的**文件清单和改动规模**——注意：**不给具体代码**
- 失败的测试用例名 + 报错输出（每条最多 2000 字符，最多 3 条）
- 第二步提取的结构化特征
- 轨迹摘要（最后 10 次工具调用）

**为什么不给官方补丁的代码**：给了之后大模型只会说"和参考答案不一样"，这不是归因，是废话。我们要的是它从 issue 和测试报错里推断问题出在哪。

**输出（用 JSON Schema 强制约束格式）**

```jsonc
{
  "category": "INCOMPLETE_FIX",
  "confidence": 0.82,
  "evidence": [
    {"source": "test_log", "quote": "AssertionError: expected 3 handlers, got 2"},
    {"source": "patch", "quote": "@@ -84,6 +84,9 @@ def _register("}
  ],
  "reasoning_zh": "补丁在正确的注册函数里加了去重判断，但只覆盖了……",
  "secondary_category": "INCORRECT_LOGIC"
}
```

- `confidence` 低于 0.6 → 采样 3 次投票取多数；还是不一致 → 标记 `NEEDS_HUMAN` 进人工队列，**不瞎猜**
- 结果按 `(任务运行ID, 提示词哈希, 判定模型)` 缓存，重跑归因不花钱
- 用便宜的快模型即可，单次约 10~20k token，180 例大概 ¥10~40

## 12.4 控制成本和不稳定性

| 做法 | 效果 |
|:---|:---|
| 规则先过一遍 | 减少 55~70% 的大模型调用 |
| 结果缓存 | 调试和重跑不花钱 |
| `temperature=0` + 结构化输出 | 降低随机性 |
| 强制引用证据 | 抑制编造 |
| 不给官方补丁代码 | 避免"照抄答案"式的假归因 |
| 失败重试 3 次 + 退避 | 抗限流和超时 |
| 归因失败不阻塞主流程 | `ANALYZING` 步骤挂了只标记归因失败，`agent_outcome` 不受影响 |

最后一条很重要：**判定和归因是两件事，归因挂了不能影响判定结果。**

---

# 12.5 人工抽检怎么做

## 页面长什么样

一屏三栏对照：

- **左栏**：issue 原文、题目信息（仓库、难度、标签）、官方补丁改了哪些文件
- **中栏**：AI 的补丁（diff 高亮）、AI 的操作轨迹（可折叠时间线）
- **右栏**：每条测试用例的结果表、失败用例的日志片段、**自动归因的类别 + 置信度 + 理由 + 引用的证据**

## 抽检员能做什么

`ACCEPT`（认可）/ `CORRECT`（改判类别，必须填新类别）/ `MARK_TASK_DEFECT`（判定题目有问题，触发隔离）/ `COMMENT`（备注）

## 怎么抽样、怎么算准确率

- **分层抽样**：按自动归因的类别分层，每一类至少抽 5 例（不足就全取），总共至少 50 例。随机种子固定并记录下来，保证别人能抽到同一批。
- **两人独立标注**：两名同学各自独立标，意见不一致的交第三人裁决。
- **算三个数**：
  - `归因准确率 = ACCEPT 的数量 / 抽检总数` ← 这就是"≥85%"这个指标的算法
  - `Cohen's κ`（一致性系数）：衡量两名标注者的意见有多一致。如果两个人自己都对不上，那这个准确率也不可信。
  - **混淆矩阵**：看哪两类最容易被搞混，用来指导分类体系的调整。

## 一个关键设计：盲检

**抽检的时候默认把自动归因的结果藏起来**，标注者先自己判断，提交之后才显示系统给的答案。

不这么做的话，标注者看着系统给的答案打分，很容易顺着它想——85% 这个数字就没有说服力了。提供开关可以关掉盲检，但报告里必须说明用的是盲检模式。
