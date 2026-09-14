# click 20 道待审题的终审复核（2026-09-14）

结论：**收 11 条，否 9 条**。与初审的收 12、否 8 相比，只有 **#3534 从 ACCEPT 改为 REJECT**。
最终结论见 [CSV](../datasets/benchmark-dev/review-2026-09-14-parked20.csv)。

本轮由 Codex 复核数据库中的完整题面、F2P 清单、测试补丁和 gold 补丁。
20 道题的数据库补丁文本与 `var/mining/patches/pallets__click/{pr}.{test,code}.patch`
逐份相等。涉及既有行为的判断同时参考补丁上下文；没有把 gold 的实现选择当成题面要求。
本轮是读过初审结论后的复核，**不是盲审，也不是独立的人类复审**。

| 最终结论 | 条数 | PR |
|:---|---:|:---|
| 收 | 11 | 2397 · 2788 · 2818 · 2933 · 3068 · 3244 · 3434 · 3482 · 3507 · 3781 · 3858 |
| 否：题面不足以支撑 F2P | 8 | 2523 · 2727 · 3137 · 3208 · 3228 · 3328 · 3364 · 3695 |
| 否：泄题 | 1 | 3534 |

## 判据与阅读范围

沿用用户给定的线：删去一句话后，如果需求无法确定，这句话应保留；
如果功能需求仍完整，只是少了内部修复方法或位置，这句话属于泄题。
公开 API 名称和参数优先级可以是必要需求，不能仅因与 gold 相同就否决。

题面充分性要结合 base 代码、已有文档和既有接口行为判断。
不能把“issue 没逐一写出所有边界情况”等同于题面不足。
应指出一种满足题面及既有接口要求、却被新增断言拒绝的合理实现。
新增的精确文案、内部状态、返回结构尤其需要核对。

必须读完整的 `issue_body`，包括 `---` 后拼接的其他 issue 和 HTML 注释。
`TaskDefinition.agent_task_input()` 原样传递正文，Runner 的提示词使用 `task.issue.body`。
注释在网页上不可见，不等于 AI 收不到。

## 改判：#3534

初审只记录了前半段“逐块输出”的需求，漏掉追加段落中的修复建议：

> I suspect a flush after this line would do the trick:

后面紧跟 `src/click/_termui_impl.py#L468` 的定位链接，提交哈希虽已清洗，
路径和行号仍在。前面还有完整的慢速生成器复现，说明输出在累计 512 行前不可见。
删除建议和定位链接后，“每块输出应立即可见”的需求仍完整。
因此建议提供的是修复操作和位置，按给定判据改判 **REJECT**。

gold 在 `src/click/termui.py::echo_via_pager()` 的 `pager.write(text)` 后添加
`pager.flush()`，与建议的操作对应；不要误写成 gold 修改的路径也与旧链接完全相同。
F2P `test_echo_via_pager_streams_each_write` 检查相邻写入之间有 flush、写入次数为 4、
结果为 `a\nb\nc\n\n`。本条以泄题作为主要否决理由。

## 原有 8 条否决的复核

| PR | 复核依据 |
|:---|:---|
| 2523 | `test_secho_non_text` 把 `isolation()` 的解包从 2 项改成 3 项。题面只约定三个结果属性；保持两项返回、在 `invoke()` 中组织混合输出的实现仍可满足需求，却会解包失败。gold 删除 `mix_stderr` 是额外差异，不是唯一依据。 |
| 2727 | 题面示例为 `hide=hide`，`test_progressbar_hidden_manual` 使用 `hidden=True`。公开参数名直接冲突，保留否决。 |
| 3137 | `test_hiding_of_unset_sentinel_in_callbacks` 不只检查回调看不到 Sentinel，还精确检查 `Option.process_value` 前后恢复 `Sentinel.UNSET`。这是回调外的内部表示要求。保留回调修改是已有接口应有的行为，不再把它单独列为题面不足。 |
| 3208 | `test_formatting_usage_error_help_hint` 的无冲突用例也要求选 `--help`；题面仅要求避免提示被遮蔽的 `-h`。保留首个可用帮助参数可解决题面问题，却不满足“最长名称”断言。 |
| 3228 | **纠正初审：题面给了子命令 Desired output**，用反引号包住 `hello`。F2P 要求单引号，并对多个建议强制 `(Did you mean one of: 'declare', 'refine'?)`。base 的选项提示在同一补丁中也从 `Possible options` 改成此文案，不能说它是已有格式。 |
| 3328 | **更直接的否决点是圆括号**：题面明确要求 `Name [show_default]:`，F2P 却要求 `(custom)` 等内容。照题面格式实现会失败。`default-is-unset` 只用 UNSET 标记“省略 default 参数”，没有传入私有哨兵；空串用例只检查不出现实际默认值 `actual`，并没有断言完全不显示任何默认值标记。 |
| 3364 | 题面给出旧版按字符拆字符串的现象，并询问升级后为何报错，未约定按空白拆分。F2P 却要求 `3 4 → (3, 4)` 和 `hello world → ('hello', 'world')`。不能从题面唯一推出此规则。 |
| 3695 | **纠正初审：追加正文确实要求弃用两个 stream helper**，四条对应 F2P 有需求依据。其余五个工具名所在段落只问未导出是否有意；F2P 却强制旧属性仍可访问且发 `DeprecationWarning`。未约定这五个工具的兼容过渡策略，因此仍否。`make_short_help` 的拼写可从 base 源码辨认，不独立作为否决理由。 |

这些修正说明：结论一致不代表理由正确。#3228、#3695 都漏读了追加正文，
#3328 则把测试数据里的标记误当成接口输入。

## 11 条接收的依据与局限

| PR | 复核依据 |
|:---|:---|
| 2397 | 题面明确要求重复参数名发 `UserWarning`。三个 F2P 覆盖重复 argument、短选项、长选项，不锁定警告文案。 |
| 2788 | 题面说明环境变量应激活配置的 `flag_value`，而不是直接成为参数值；F2P 检查激活后得到 `upper`。隐式 `is_flag` 是既有配置行为。 |
| 2818 | 题面明确要求在 Runner 设置 `catch_exceptions`，且显式 `invoke` 参数覆盖 Runner 默认。测试检查覆盖及继承两种行为。这是公开 API 需求，保留 ACCEPT。 |
| 2933 | 题面明确要求在 invoke 结束前刷新 stderr；测试检查未手动刷新的输出被捕获。复现里的旧 `mix_stderr=False` 不影响核心需求。 |
| 3068 | 标题与 traceback 指出 `ctx.invoke` 向命令泄露 Sentinel；两个 F2P 检查未设置默认值的可选参数为 None，与已有用户侧缺省行为一致。 |
| 3244 | 完整正文追加了 `faulthandler.enable()` 的独立复现。F2P 直接检查它不崩溃并执行完命令，不只依赖标题里的 subprocess 场景。 |
| 3434 | 七个 F2P 检查无参数时仍输出用法行。自定义前缀、颜色与长程序名是已有 formatter 行为的延伸，没有新增任意标题。 |
| 3482 | F2P 与题面复现直接对应：调用 pager 不再抛已关闭文件异常，仍输出原文。 |
| 3507 | 普通组的 `[COMMAND]` 在题面明确给出；chain 用例沿用既有格式，只给首个可省略的 COMMAND1 加方括号。 |
| 3781 | 题面明确要求 `edit(filename=Path(...))` 可用。唯一 F2P 覆盖单 Path；编辑结果和返回 None 沿用既有文件编辑接口。 |
| 3858 | 题面明确要求 Path 按 path_type 泛型化，运行时类型参数断言与这个公开 API 要求一致，保留 ACCEPT。**覆盖局限**：唯一 F2P 只查 `typing.get_args(click.Path[pathlib.Path])`；静态类型断言位于 `tests/typing/typing_path.py`，不在 F2P 中。因此当前 F2P 通过不等于已证明 mypy/pyright 的报错消失。 |

## 两批差异与一致性检查

| 审核批次 | 总数 | 收 | 否 | 泄题 | 题面不足 | 不自足 |
|:---|---:|---:|---:|---:|---:|---:|
| 09-10 初批（历史结论，未重审） | 30 | 21 | 9 | 5 | 3 | 1 |
| 09-14 本批初审 | 20 | 12 | 8 | 0 | 8 | 0 |
| 09-14 本轮复核 | 20 | 11 | 9 | 1 | 8 | 0 |

原先的“5 对 0”含有一条可定位的漏审，现在应比较“5 对 1”。
剩余差异仍不能归因于年代或审阅者：两批 PR >3200 的比例相近，只能说明这个粗分组
没有明显差异，不能排除所有年代影响。这里没有做显著性检验。
也不能写“E8-T2 否掉了 #2818”——#2818 属于本批，从未在那 30 条里被否。

本轮 20 条 verdict 中 19 条与初审相同，描述性一致率为 **95%**。
这是已读过初审结论的复核结果，不能替代随机盲审，更不能据此宣称判据已验证可靠。
“随机抽 5 道、不一致超过 1 道就回看判据”可以作为初步触发规则；
没有超过阈值也不证明一致性足够。建议盲审同时比较理由，并保留各自原始判断。
本次没有另外安排盲审，也没有把这个建议列为导入前置条件。

## 导入范围

本次只导入 CSV 终审状态及候选 `final_review`。不修改题目内容、内容哈希、测试或协议。
`import-review` 不修改数据集归属，也不创建快照；来源池中的这批题仍为
`raw_definition.dataset_id = benchmark-dev`，接收后该来源池 VALID 从 22 增至 33。
这不等于已把它们发布进任何数据集。

**新增的 11 道留给 `benchmark-cn-v1` 建版；不得加入或重建已发布的 `benchmark-dev@v1`。**
后续建版还需明确来源选择，不能误以为本次导入已把 dataset_id 改成 benchmark-cn-v1。
本次不执行 stage / gate / publish。

已发布快照预期保持 22 道，摘要：
`sha256:300746559b84b2b9858c14e527a42bf9c2e75cb9ddd7a2aba75e26e3a8b0a11f`。

## 导入与核验记录

执行命令（在 `backend/`，使用已有依赖；uv 缓存放 `/tmp` 以适配沙箱）：

```bash
UV_CACHE_DIR=/tmp/parked-uv-cache uv run --no-sync python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-2026-09-14-parked20.csv \
  --reviewer codex-review-2026-09-14
```

实际输出：

```text
收下 11 条（其中 11 条从 REVIEW_REQUIRED 转成 VALID），否掉 9 条
```

随后只读逐条核对状态、候选的 verdict/reason/reviewer，以及导入前后的完整题目定义。
同时把两张数据集表按 id 排序序列化，比较导入前后的全部行：

```text
benchmark_sets: unchanged
benchmark_set_items: unchanged
20/20 review states and final_review match CSV; task definitions unchanged
benchmark-dev VALID: 33
```

运行 `.venv/bin/python -m cli.dataset verify --slug benchmark-dev --version v1`：

```text
benchmark-dev@v1：22 道题，状态 PUBLISHED
  ✅ 快照摘要一致  sha256:300746559b84…
  ✅ 和现在的题库逐题一致，没有漂移
```

本次只有审核资料和审核状态变更，未修改业务代码，未运行会重建数据库的集成测试。
未创建或发布 `benchmark-cn-v1` 快照；未提交 Git 或创建 PR。
