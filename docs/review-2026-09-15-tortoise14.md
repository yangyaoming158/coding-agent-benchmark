# tortoise-orm 14 道题终审复核（2026-09-15）

**复核结论：收 8、否 6；已于 2026-09-15 导入数据库，导入记录见文末。**
最终结论见 [CSV](../datasets/benchmark-dev/review-2026-09-15-tortoise14.csv)。
初审为 Claude 的收 7、否 7；Codex 本轮复核改动三条：

| PR | 初审 | 复核 | 主要依据 |
|:---|:---|:---|:---|
| 2128 | REJECT | ACCEPT | 同一 AlterField 长度变更的方言实现，题面未要求只修 PostgreSQL；base 有各后端完整列定义的 SQL 写法。 |
| 2142 | ACCEPT | REJECT | 保留 DEFAULT/COMMENT 是需求；必须只发一条 SQL、必须使用 MODIFY COLUMN 则不是。 |
| 2269 | REJECT | ACCEPT | base 已明确支持关联过滤，并承诺 update 更新查询集中的所有对象。两个类各写一处不构成否决理由。 |

| 最终结论 | 条数 | PR |
|:---|---:|:---|
| 收 | 8 | 2081 · 2125 · 2128 · 2129 · 2145 · 2236 · 2255 · 2269 |
| 否：题面不足以支撑全部 F2P | 6 | 2076 · 2084 · 2086 · 2106 · 2109 · 2142 |

两道中文题 #2236、#2255 保留接收。接收率为 8/14 ≈ 57.1%。

## 审核范围与身份

本轮只读读取数据库中完整 `issue_body`、F2P、base commit 和题目定义，对照测试补丁、
gold 中相关改动及争议题的 base 代码/文档。14 道题的数据库 `test_patch`、`gold_patch`
与 `var/mining/patches/tortoise__tortoise-orm/{pr}.{test,code}.patch` 全部逐份相等。

这是已读过初审结论的 Codex 复核，不是盲审，也不是独立人类终审。
复核这一步没有更改数据库状态、题面、补丁、内容哈希或任何数据集快照；导入在复核之后由用户确认执行（见文末）。
审核归属应记录“Claude 初审，Codex 复核”，不能把模型审核记为某个人已经亲自逐题审核。

## 判据修正

继续使用“删去修复建议后，功能需求是否仍完整”区分泄题和必要需求。
判断测试是否超出题面时，同时考虑 base 的公开接口、已有文档和合理的兼容要求。

**删除初审里“同一路径就收、另写一段就否”的规则。** 一个 bug 可以在不同后端或
兄弟类中各出现一次；一个函数也可能同时承担几个独立功能。改几处不能决定题是否公平。

数据库出现在复现环境中，也不自动等于“只允许修这个数据库”。反过来，
知道另一个后端怎么写 SQL，也不能单独证明那部分属于需求；要看是否还是同一个
公开操作，以及测试是否新增了 API、输出规则或独立功能。

## #2128：改为接收

base：`332709fa1a737408a53241a57ae30b8b70db853a`。

题面标题提 PostgreSQL，正文要求 AlterField 检测 `max_length` 变化并生成类似的 SQL。
没有“只修 PostgreSQL”这句限制。三个 F2P 都把同一个 nullable CharField 从 32 改为 64，
没有额外引入默认值、注释、新方法名或新配置。

base 的 `schema_editor/base.py::alter_field()` 接收同一字段的新旧定义，再委派到后端。
`mysql.py::_alter_field()` 和 `mssql.py::_alter_field()` 已分别用完整列定义处理 null 变更，
现有写法就是对应测试所用的 `MODIFY COLUMN` / `ALTER COLUMN`、引用方式及 NULL 后缀。
因此可以依据现有方言实现完成相同的长度变更，不需要猜一项新的产品需求。

“只修 PG 的补丁会失败”是事实，但不足以证明测试不公平：本轮把同一公开 AlterField
操作在已有后端上的行为一致性视为合理要求。此处是范围判断，不声称所有审阅者都会同意。
也不声称改完 base 后两个 override 会自动正确；确实需要检查后端实现。

## #2142：改为否决

base：`5e65d839c67aeebadc44611c557c840adb200896`。

初审关于 MySQL 注释的主要判断是对的：重定义列时不能无意丢掉未修改的 COMMENT。
题面还明确要求未修改的数据库默认值继续保留，以及单独修改 description 必须生效。
这些不是新增需求。

问题在 `tests/migrations/test_schema_editor_backends.py` 的执行方式断言：

- `test_mysql_alter_field_max_length_preserves_db_default` 和
  `test_mysql_alter_field_null_change_preserves_db_default` 都要求
  `len(client.executed) == 1`，并要求该条 SQL 含 `MODIFY COLUMN`。
- `test_mysql_alter_field_description_change` 同样限制 SQL 条数和 MODIFY。
- 两条 null/description 组合用例筛选包含 `MODIFY COLUMN` 的语句，要求恰好一条且含 COMMENT。

“数据库最终定义正确”不能推出这些执行方式约束。例如针对题面字段可以生成：

```sql
ALTER TABLE `profile`
  CHANGE COLUMN `name` `name` VARCHAR(20) NOT NULL
  DEFAULT '' COMMENT 'user name';
```

它保持列名，改变长度，并在同一条语句保留默认值和注释。MySQL 官方文档明确支持
用相同的旧/新列名通过 CHANGE 修改定义，并说明 CHANGE/MODIFY 都需显式保留列属性。
但该写法没有 `MODIFY COLUMN` 字样，会被对应 F2P 拒绝。
依据：[MySQL 8.4 ALTER TABLE，Renaming, Redefining, and Reordering Columns](https://dev.mysql.com/doc/refman/8.4/en/alter-table.html)。

另一个自然实现方向是沿用 base 的属性分支：MODIFY 后另发 ALTER COLUMN SET DEFAULT。
base 的 MySQL editor 已有 `ALTER_FIELD_SET_DEFAULT_TEMPLATE`，但测试的单条 SQL 断言会拒绝它。
这个两步方案存在中间状态，所以**不把它当作唯一反例**；上面一次 CHANGE 的反例没有此问题。

这里的 SQL 正确性依据是官方语法和语义；本轮没有启动 MySQL 实跑，不能将它记为运行验证。
结论是当前 F2P 锁定了未约定的执行方式，不是说官方修复错误。未来若改测试或补充需求，
应重新建题、验证和审核，不能在本轮偷偷更改现有题目。

## #2269：改为接收

base：`a324edc43fecdd260d71d212b894f4376ca33677`。

初审说“base 文档没有 update 支持关联字段过滤的承诺”，遗漏了接口组合的依据：

- `docs/query.rst:111` 起介绍关联实体过滤，例如 `Event.filter(tournament__name='World Cup')`。
- `docs/query.rst:65` 起说明 QuerySet 可执行的操作，并通过 autodoc 展开 QuerySet 接口。
- `tortoise/queryset.py:772` 的 `QuerySet.update()` 文档为
  `Update all objects in QuerySet with given kwargs.`，实现还把 `_q_objects` 传给 UpdateQuery。

这三处合起来支持“先按关联字段筛出对象，再更新这些对象”。F2P 没要求特定 SQL、子查询
别名或辅助函数，只检查匹配行更新、未匹配行保留；delete 用例同理。
因此按已有公开接口组合接收。题面没有写 UPDATE 仍是需要记录的边界，但不能仅因两种
操作位于两个类就否决，也不能把错误行为存在于 base 当作它合理的证明。

## 其余 11 条

| PR | 结论 | 复核依据 |
|:---|:---|:---|
| 2076 | 否 | 除关联模型创建顺序外，F2P 要求新增 `validate_relations_initialized()`、特定错误文字、禁止 clone、200 模型少于 2 秒及 writer 精确输出。三条关联测试末尾也调用了那个新增方法，不能说它们完全只测题面需求。51 个文件只是背景，独立新增断言才是依据。 |
| 2081 | 收 | 两条 URL 与题面直接对应，断言沿用 expand_db_url 的配置结构。题面列 urlparse 报错位置和 SQLAlchemy 对照，没有给 gold 的“先编码 userinfo”改法。 |
| 2084 | 否 | 题面未明确 schema 是否预先存在、谁负责创建；四条 PG F2P 强制 CREATE SCHEMA、safe/unsafe 行为及一次创建规则。只在已存在的 schema 中正确建表仍会失败。`where table exists` 不能证明 schema 一定已存在；MySQL 覆盖本身不单独否决。 |
| 2086 | 否 | `test_context_default_timezone_settings` 把默认 use_tz 从 False 改为 True。修复 naive datetime 被转成 aware 不需要改这个默认值；此点足以否决。 |
| 2106 | 否 | 题面要 schema generation 和 migration 一致；F2P 强制新建 SqlDefault/Now API、repr、哈希及方言输出，要求超出 SQL 一致性。 |
| 2109 | 否 | RandomHex、布尔默认值、自引用外键等 F2P 是独立需求。初审把全部 13 条 backend 用例称为与 unique 无关不准确，其中两条检查 unique 约束创建/删除的引用方式。26 条仅触发人工复核，不是自动否决阈值。 |
| 2125 | 收 | 题面明确区分有注解与无注解的反向关系，测试检查保留前者、排除后者。 |
| 2129 | 收 | 题面要求 CreateModel 尊重 db_default，测试检查 DEFAULT 子句。CharField 与题面 DecimalField 属于同一默认值要求，不以字段类型不同否决。 |
| 2145 | 收 | 三类关系都检查删除模型后目标模型保留、被删模型不能再查到，符合迁移状态要求。不能声称“任何 FK 修法”都会自动覆盖三类关系。 |
| 2236 | 收 | 继承排序、子类显式覆盖、显式空值退出继承，均符合既有继承方式和题面目标。没有要求新增私有名称。 |
| 2255 | 收 | 测试直接检查多对多关联导致的重复行在 distinct().count() 中不再重复计数，1/2 的期望值来自测试数据。 |

## 统计、后续导入与未审内容

本轮与初审 11/14 条 verdict 一致，约 78.6%；有三条改判。这是看过初审后的复核，
不能当成盲审一致率或 κ 统计。文档中使用“同一代码路径”替代需求判断，确实暴露了口径问题。

click 的 70% 指 09-10 初批的 21/30；09-14 那批复核后是 11/20 = 55%。
不能把 70% 写成所有 click 题的统一接收率，也不能凭 14 道断言“大 PR 导致转化率低”。
文件数可用于安排审核顺序，不能代替审核；`schema.py::review_flags()` 对超过 20 条 F2P
只要求人工看，不应在本文擅自改成直接跳过。

若其他题状态不变，导入本 CSV 后，click 33 + tortoise 8 的已审接收小计为 41，距离 60 为 19。
这是这两批的预计小计，不代表已经导入，也不代表已发布 benchmark-cn-v1。
新增接收题只留给后续 `benchmark-cn-v1` 建版；`benchmark-dev@v1` 不修改。

**Codex 同意按本轮修订后的 CSV 导入；本次由用户另行执行。**
本轮只修改审核 CSV 和本文，没有运行 import-review、stage、gate、publish 或平台集成测试。

工作区另有初审期间修改的 `backend/cli/promote.py` 和
`backend/tests/integration/test_promote_persistence.py`，涉及带连字符仓库名的 PR 号匹配。
本轮未修改它们，也未将本次题目复核视为对那项代码变更或测试结果的批准。

## 导入与核验记录（2026-09-15）

用户确认按修订后的 CSV 导入。执行命令（在 `backend/`）：

```bash
uv run python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-2026-09-15-tortoise14.csv \
  --reviewer codex-review-2026-09-15
```

实际输出：

```text
收下 8 条（其中 0 条从 REVIEW_REQUIRED 转成 VALID），否掉 6 条
```

「0 条转成 VALID」是对的：唯一一道 `REVIEW_REQUIRED`（#2109）判的是否，走的是 `→ INVALID` 那条边。

导入后只读核对：

```text
14/14 review states and final_review match CSV（8 VALID / 6 INVALID；candidate.raw_payload.final_review 的 verdict 与 reviewer 逐条一致）
task definitions: unchanged（14 道的 content_hash 与 raw_definition 的 md5 导入前后相同）
benchmark_sets: unchanged（benchmark-dev@v1 仍是 22 题，PUBLISHED，摘要 300746559b84… 未变）
```

题池现状（`benchmark-dev` 下）：click VALID 33 / INVALID 18，tortoise VALID 8 / INVALID 6。**VALID 合计 41 道**，距 E8-T3 AC 6 的 ≥60 还差 19。
