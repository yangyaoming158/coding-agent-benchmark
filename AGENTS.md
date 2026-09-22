# 项目开发规则

> 本文件是这个项目的**唯一开发规则来源**，面向所有 AI 编程助手（Claude Code、Codex、Cursor、Aider 等）和人类开发者。
> `CLAUDE.md` 只是指向本文件的入口，不重复内容。

---

## 1. 这个项目是什么

给 AI 编程助手打分的平台。

流程是这样的：从真实开源项目里挑一个已经被修复过的 bug，把代码回退到修复之前的状态，把当时的 issue 描述交给被测的 AI，让它自己去改代码。改完之后，用项目原本的测试来验证它改对了没有。

参考 SWE-Bench 的做法（这是学术界评测 AI 编程能力的通用基准）。

**当前阶段：M1（评测内核）已达成 —— Golden Task 全链路跑通并落库，Oracle 哨兵 100% / Noop 哨兵 0%。**

仓库里现在有：评测协议正文（`docs/evaluation-protocol.md`，FROZEN v1.2）、后端骨架
（FastAPI + SQLAlchemy + Alembic，17 张表）、前端骨架（Next.js，类型从 OpenAPI 生成）、
配置 / 结构化日志 / 制品存储抽象、以及整套工程规范（ruff、mypy strict、import-linter、
pre-commit、GitHub Actions）。业务逻辑（建题、跑沙箱、判定、归因、报表）还没开始写。

**进度以 `docs/plan/10-tasks-plan.md` 里标 ✅ 的任务为准。** 别依赖这段话——
它是手写的，一定会过时；任务表是每个任务做完时顺手更新的，更可信。

---

## 2. 动手之前先读什么

不要跳过这一步。这个项目的坑大多不在代码里，而在协议定义里。

| 你要做什么 | 必读 |
|:---|:---|
| 任何工作 | `docs/plan/README.md`（摘要）· `docs/plan/12-engineering-workflow.md`（本文件的详细版） |
| 碰判定逻辑 / 数据库枚举 | `docs/plan/02-evaluation-semantics.md` ⚠️ 冻结件 |
| 碰任务格式 / 数据集 | `docs/plan/03-benchmark-spec.md` ⚠️ 冻结件 |
| 接入新的 AI 助手 | `docs/plan/04-runner-protocol.md` ⚠️ 冻结件 |
| 碰 Docker / 沙箱 | `docs/plan/05-sandbox.md` |
| 碰判定引擎 / 失败归因 | `docs/plan/06-judge-attribution.md` |
| 碰数据库 / 后端 / 前端 | `docs/plan/07-platform-architecture.md` |
| 想知道"为什么这么设计" | `docs/plan/08-adr.md`（12 条架构决策记录） |
| 领任务 | `docs/plan/10-tasks-plan.md`（任务树）· `docs/plan/11-acceptance-testing-risk.md` §31（首批任务） |
| 部署平台（compose 一键起） | `docs/deployment.md`（每条命令本机跑过；代理三处、Docker Desktop 共存、端口、DooD 权限） |
| 用平台（建镜像 → 灌题 → 配 Agent → 建实验 → 看进度 → 报告 → 归因 → 发布） | `docs/usage.md` |
| 先看一眼整体（模块分层、17 张表、没做的和为什么） | `docs/architecture.md`（和 `pyproject.toml` 的 import-linter 合同、`models/` 对过） |

`docs/plan/report.html` 是**自动生成**的，不要手工编辑。改完 md 之后跑 `python3 docs/plan/_build_report.py .` 重新生成。

---

## 3. 怎么跟人说话

这条规则的优先级很高，写在技术规则前面。

**默认读者是一名软件工程专业的本科生。** 他懂编程、Git、数据库这些基础，但不知道你脑子里正在想什么。

### 十条规则

1. **先说结论，再说原因。** 不要从抽象理论开始铺垫。
2. **用普通工程语言。** 能说"订单服务直接改了库存表"，就不要说"跨域所有权冲突"。
3. **不要自己造术语。** 除非是行业里本来就有的标准说法。
4. **第一次出现专业术语时立刻解释。** 例如："这里有循环依赖（A 依赖 B，同时 B 又依赖 A）。"
5. **一句话只讲一个判断。** 不要在一句话里套三层因果。
6. **引用真实代码，不要泛泛而谈。**
   - 不好："存在职责边界问题。"
   - 好："`OrderService.createOrder()` 里直接改了库存，本来这件事应该由 `InventoryService` 做。"
7. **重要问题按四步讲：** 问题是什么 → 为什么会发生 → 会造成什么实际影响 → 应该怎么改。
8. **删掉一个词不损失信息，就删掉它。**
9. **不要写报告腔和论文腔。** 类似"语义所有权漂移""架构收敛""行为契约熵"这种，一律不要。
10. **抽象原则必须落到具体的文件、类、函数或数据流上。**

### 对比

不好：

> 当前架构边界呈现隐式的跨域生命周期耦合，产生语义所有权模糊，并提升编排复杂度。

好：

> 订单服务现在直接控制库存状态。
>
> 这会让两个服务粘在一起：库存逻辑一改，订单服务可能也得跟着改。
>
> 建议订单服务只发一个 `OrderCreated` 事件，由 `InventoryService` 自己决定怎么扣库存。

### 发送前自查

静默检查一遍，不要把检查过程输出出来：

- 有没有哪句话要读两遍才懂？
- 有没有自己造的词？
- 有没有只讲了道理，没说代码里到底发生了什么？
- 有没有能说得更简单的地方？
- 一个没参与过这段开发的人能看懂吗？

有问题就先改，再输出。

---

## 4. 三个冻结件（改之前必须先讨论）

这三样东西是整个项目的地基。后面所有代码——建表、判定、报表、前端展示——都从它们派生。改动的代价随时间指数上升。

**如果你觉得需要改其中任何一条，先停下来说明理由，不要直接改。**

### 4.1 评测语义 —— 已冻结

正式协议文本：[`docs/evaluation-protocol.md`](docs/evaluation-protocol.md)，**FROZEN v1.2（2026-09-02）**，共 79 条编号条款。

`docs/plan/02-evaluation-semantics.md` 讲的是设计理由，协议讲的是精确定义。**两者冲突以协议为准。**

协议冻结后**不得直接修改**，必须走它 §9 的变更流程：提 issue → 至少 1 人 review → 更新协议并升版本号 → 同步更新代码、数据库迁移和规划文档。

CI 里有持续校验（`backend/tests/unit/test_protocol_consistency.py`），改了协议但没重跑真值表会直接让构建失败。

核心内容：

一次评测的结果用三个**互相独立**的字段描述：

- `lifecycle_status`：现在走到哪一步了
- `infra_outcome`：**平台**有没有正确完成这次评测
- `agent_outcome`：**被测 AI** 有没有把 bug 修好

把"AI 失败"和"平台故障"混进同一个字段，解决率就不可信了。这是最容易被写坏的地方。

### 4.2 任务格式（`03-benchmark-spec.md`）

每道题包含：代码仓库快照、issue 描述、`fail_to_pass` 测试（修好之后必须由失败变通过）、`pass_to_pass` 测试（不能被改坏）。

### 4.3 Runner 协议（`04-runner-protocol.md`）

平台和被测 AI 之间的接口：标准输入喂一个 JSON 任务，标准输出的**最后一行**返回一个 JSON 结果，结果里带一段 unified diff（补丁）。

---

## 5. 五个最容易踩的坑

新接手的人（包括 AI）最常在这几处出错。

### 5.1 不要用大模型判断 bug 修没修好

判定必须 100% 由测试结果推导，不能有任何随机性。同一个补丁，今天判和下个月判必须得到一样的结论，否则排行榜不可比。

大模型**只能**用在"分析它为什么没修好"这一步，而且它的输出不能回写判定结果。

### 5.2 被测 AI 改了测试文件，那部分改动必须丢掉

否则它把测试改成 `assert True` 就"通过"了。

两道防线：
1. 生成补丁时，按路径过滤掉测试文件（`tests/`、`conftest.py`、`pytest.ini` 等）。
2. 跑测试前，再强制 `git checkout` 还原一次这些文件，然后才打上官方的测试补丁。

### 5.3 工作区里不能有 base commit 之后的 git 历史

如果直接 `git clone` 再 `checkout`，被测 AI 一句 `git log origin/main` 就能翻到官方的修复代码。

正确做法：`git archive <base_commit>` 导出文件树 → `git init` → 只提交一次。这样工作区里只有一个 commit。

### 5.4 OOM 和超时的退出码都是 137，不能靠退出码区分

这两种情况在语义上完全相反：

- 内存超限（OOM）是**平台**的问题，应该重试。
- 执行超时是**被测 AI** 的问题，应该判定为"没修好"。

判据用 `docker inspect --format '{{.State.OOMKilled}}'`。

**但这个字段在并发下会漏报**（2026-09-11 实测，细账在 `05-sandbox.md` §10.10）：
同时起十几个容器时，dockerd 有约 3–5% 的概率**完全收不到** OOM 通知，
于是一次真的内存超限长成"退出码 137 + `OOMKilled=false` + 没超时"。
串行跑 105 次一次都没漏 —— 早先"实测可靠"那句话是串行测出来的，条件不一样。

漏报之后 Agent 阶段会把它记成 `AGENT_RUNTIME_ERROR`，**等于把平台的内存问题
算到被测 AI 头上**，而且不计入平台故障率，排行榜上看不出异常。
目前的处置只是**告警**（日志事件 `container_sigkilled_without_oom_flag`），
判定没改 —— 改判定要动协议 C-06/C-07 这两条冻结件。

**唯一的例外是"一个字节都没输出就被 137"**（E3-T9，2026-09-17）：那不是漏收的 OOM，是容器还没开口
就被平台自己杀了（第二个 Worker 的孤儿回收，§18.7），记 `SANDBOX_ERROR`。有输出的 137 维持原判。

**写新适配器时，"这次失败该记在谁头上"不要自己判。** 判完 OOM / 超时 / "算不算失败"之后调
`app/runner/adapters/cli_text.shared_failure()`，鉴权、余额、限流、5xx、容器被杀都在那一处；
散成各写一套，加一条规则就要改几个地方，漏一个就是几十次评测记成"AI 自己崩了"（#96 就是这么来的）。
往判据里加东西时**只认带锚的形态**（`litellm.XxxError`、`"text":"API Error: …"`），不认裸词 ——
AI 的对话里什么词都有，1931 份真实日志里 52 份含 "429"，没有一次是真的限流。

### 5.5 测试用例 ID 必须归一化

`tests/test_a.py::test_x` 和 `./tests/test_a.py::test_x` 是同一个用例，但字符串不相等。匹配不上就会被当成"用例不存在"，进而判定为失败。

这类 bug 不会报错，只会让解决率莫名其妙地偏低，非常难查。必须有专门的单元测试覆盖至少 6 种 ID 写法。

**验证过了不等于评测时一定过。** 八步验证跑的是全量套件（文件顺序），正式评测只跑 F2P ∪ P2P
（P2P 字母序）。一条只在"它先跑"时才过的用例，验证全绿、Oracle 门禁必挂（2026-09-17，tortoise 8 道
全栽在 `test_init_creates_migrations_package` 上，细账在 `03-benchmark-spec.md` §8.12）。
这类用例和会飘的用例一样不许进 P2P：`assembly.ORDER_DEPENDENT_TEST_FUNCTIONS` /
`FLAKY_TEST_FUNCTIONS`，按函数名精确匹配，两处过滤都走 `unfit_for_p2p()`。
C-50 门禁就是为这种事设的，**不要为了过门禁去改判定**。

---

## 6. 什么叫"这个任务做完了"

每个任务在 `docs/plan/10-tasks-plan.md` 里有自己的验收标准（AC）。除此之外，下面六条对**所有任务**都适用：

- [ ] AC 全部达成，并且**贴出可以复核的证据**（测试输出、命令回显、截图），不能只说"做完了"
- [ ] 代码通过 PR 合入 `main`，至少 1 人 review
- [ ] 新增或改动的逻辑有对应的自动化测试，CI 全绿
- [ ] 公共接口、领域枚举、复杂算法有中文注释（这是交付硬性要求）
- [ ] 如果改了协议、数据库或枚举：同步更新 `docs/` 和数据库迁移脚本，且迁移可以回滚
- [ ] 如果引入了新的环境依赖：更新部署文档和 `scripts/check_env.py`

第一条最重要。"贴证据"把"我觉得做完了"变成"这是它工作的样子"，成本只有一次复制粘贴。

---

## 7. Git 约定

### 分支

- `main`：始终可运行、CI 绿。受保护，不能直接 push。
- 特性分支：`feat/E2-T2-container-runner`，即 `<类型>/<任务ID>-<短描述>`，**存活不超过 2 天**。
- 合并方式：**Squash merge**，保持 `main` 线性。

不要用 GitFlow。4 周的项目开 `develop`/`release` 分支只会增加合并负担。

### Commit 信息

用 Conventional Commits，scope 写 Epic 编号：

```
feat(E2): 容器执行器支持 pids-limit 与 OOM 判定
fix(E4): 修正 pytest 用例 ID 归一化对参数化用例的处理
test(E2): 补齐四条沙箱负例
docs(plan): 回填沙箱实测结论
chore(E0): 引入 import-linter 模块边界规则
```

类型：`feat` / `fix` / `test` / `docs` / `refactor` / `chore` / `perf`

### 绝对不能提交进仓库的东西

| 路径 | 为什么 |
|:---|:---|
| `var/workspaces/**` | 每次评测物化的代码工作区，数量随运行次数线性增长 |
| `var/artifacts/**` | 日志、补丁、测试报告，单次实验几百 MB |
| `var/mirrors/**` | Git 镜像仓库，单个仓库几百 MB |
| `var/build-snapshots/**` | 建镜像用的浅克隆（E2-T3），和 mirrors 分开放 |
| `datasets/exports/*.jsonl` | 数据集导出文件，MB 级 |
| `.env` / `*.env` / `**/secrets*` | **API 密钥**。这个项目要接好几个大模型服务商，泄漏风险高 |

必须提交的：`datasets/golden/**`（测试基石，KB 级）、`tests/fixtures/**`、`images/**/Dockerfile*`、`alembic/versions/**`、`docs/**`。

### 跑正式实验之前，工作区必须是干净的

每次实验都会把当前代码的 git commit id 记进结果里。如果工作区有未提交的改动，这个 id 就不能唯一代表代码状态，"可复现"就是假的。

所以：`git status --porcelain` 不为空时，拒绝启动正式实验。调试时可以用 `--allow-dirty` 绕过，但结果会被标记为 `dirty`，**不能进排行榜**。

---

## 8. 代码规范

- **语言**：后端 Python 3.11+，前端 TypeScript
- **注释**：公共接口、领域枚举、复杂算法必须有中文注释
- **标识符用英文**：变量名、函数名、类名、测试函数名一律用英文。中文只用在注释、文档字符串和文档里。
  原因：中文标识符会被 ruff 的命名规则判为不合规（N802/N806），而且在 Python 生态里不常见。
  "中文注释"是交付要求，"中文变量名"不是
- **架构**：模块化单体（一个代码库，API 和 Worker 是两个不同的启动入口）
- **模块依赖方向**（用 import-linter 在 CI 里强制）：

```
api → evaluation / benchmark / report
    → runner / sandbox / judge / attribution
    → storage / infrastructure
    → domain
```

`domain` 不依赖任何其他模块。`sandbox` 不能依赖 `runner`（是 runner 用 sandbox，不能反过来）。

- **目录结构**：见 `docs/plan/11-acceptance-testing-risk.md` §30

---

## 9. 测试要求

### 三条"哨兵测试"

这三条把"这个基准可不可信"变成了可以自动验证的断言，比人肉相信可靠得多。

1. **Oracle 哨兵**：用官方的正确补丁跑整个数据集，解决率必须 **100%**。不是 100% 就说明有坏题或者判定引擎有 bug。**每次发布数据集前必须跑，作为发布门槛。**
2. **Noop 哨兵**：用空补丁跑整个数据集，解决率必须 **0%**。不是 0% 说明有的题目在修复前测试就已经通过了。
3. **确定性哨兵**：同一个补丁重复判定 3 次，每条用例的状态必须完全一致。

### 沙箱负例（已在开发机验证通过，见 `05-sandbox.md` §10.3）

内存炸弹要被 OOM 杀掉、fork 炸弹要被进程数限制拦住、死循环要被按时杀掉且不留残留容器、`--network none` 下要真的连不上网。

### 分层

单元测试和集成测试每次提交都跑（3 分钟内）。带 `@pytest.mark.docker` 标记的每日跑。消耗大模型额度的适配器测试手动触发。

### ⚠️ 跑集成测试会清空它连上的那个库

`tests/integration/conftest.py` 的 `engine` 夹具开头是 `downgrade base` + `upgrade head`，
整库连表带数据抹掉重建。不只是 `make check`，**单跑一个集成测试文件也会**。
表还在、数据没了，看起来很像"数据库自己出了问题"（2026-09-09 因此排查过两次）。

**所以测试默认跑在独立的 `bench_test` 库上**（#88，2026-09-12）。`make test` /
`make check` / `make test-docker` / `make test-all` 都会把 `BENCH_DATABASE_URL`
指到 `bench_test`，库不存在就自动建（`make db-test`）。开发库 `bench` 碰都不碰，
测试里调 CLI 的那些用例也一样落在测试库里。

**但这只覆盖"从 Makefile 跑测试"这一条路。** 绕开 Makefile 就又连回开发库了：

```bash
cd backend && uv run pytest tests/integration/test_mining_persistence.py   # 这一条就把开发库清了
```

`conftest.py` 里有一道 `warn_if_this_is_not_a_test_database()`：库名里不含 `test`
就打一条醒目的警告（不拒绝——CI 的库名未必叫这个）。看见那条警告就说明你正在清开发库。
手工 `alembic downgrade base` 同理，那条路上没有任何保护。

**那道保护只挡 Worker，不挡重灌。** `refuse_if_a_worker_is_working()` 查的是
`job_queue` 里没过期的租约；而 `make validate-tasks` / `cli.promote assemble` 这些重灌命令
一条租约都不占，测试照清不误。2026-09-10 又踩了一次：重灌跑到一半（八步验证在跑），
在另一个终端跑了几条集成测试，31 道题连同刚验完的结论一起没了。
**重灌期间一条集成测试都别跑**，包括只跑一个文件的。

跑完按第 12 节的**重灌规程**照抄一遍。挖掘和预筛的数据不用重新花钱——
GitHub 响应和大模型回答都有本地文件缓存（`var/cache/`），重灌走缓存。
真正费时间的只有起容器那两步（探测 + 验证），加起来二十分钟左右。

---

## 10. 开发环境须知

开发机是 Windows 11 + WSL2（Ubuntu 24.04）。有几个已经踩过的坑：

**Docker 已装好**：29.7.2，原生 engine（不是 Docker Desktop），systemd 托管，开机自启，免 sudo。

**网络要走代理**，而且必须配三个地方，少一个就表现为"拉不动镜像"：

| 配置点 | 位置 |
|:---|:---|
| dockerd 代理 | `/etc/systemd/system/docker.service.d/http-proxy.conf` |
| 镜像加速 | `/etc/docker/daemon.json` 的 `registry-mirrors` |
| 容器内代理 | `~/.docker/config.json` 的 `proxies.default` |

代理地址是 WSL 的网关 IP，**`wsl --shutdown` 之后可能会变**。变了之后上面三处都要同步更新。用 `ip route show default` 取，不要写死。

**一台机器同时只跑一个 Worker。** 第二个 Worker 一起来，它启动时的孤儿回收
（`startup_reaped_containers`）就会把**第一个 Worker 正在用的容器**当孤儿杀掉。
被杀的那一方看到的是"退出码 137、`OOMKilled=false`、没超时"——
**和一次真的内存超限长得一模一样**（2026-09-12 E9-T1 实测，8 个容器被这样杀掉，
细账在 `07-platform-architecture.md` §18.7）。

怎么认出来：日志里 `container_sigkilled_without_oom_flag` 这条告警响了，
而同一时间另一份 Worker 日志里有 `startup_reaped_containers`。
`make worker` 之前先确认没有别的在跑：

```bash
ps -eo args | grep '[a]pp\.worker'
```

**不要在 Docker Desktop 里为这个 WSL 发行版开启集成。** 开了之后它会接管 `/var/run/docker.sock`，导致 docker 命令连到另一个守护进程上，表现是镜像和容器"凭空消失"。

**跑最终实验前退出 Docker Desktop。** 所有 WSL 发行版共用 `.wslconfig` 里的内存额度，Docker Desktop 开着会占掉留给测试容器的内存。

资源：16 vCPU / 11 GiB（由 `.wslconfig` 控制）/ 920 GB 可用磁盘。

---

## 11. 明确不要做的事

这个项目非常容易过度开发。下面这些一律不做：

Kubernetes、微服务拆分、消息队列中间件、分布式调度、自研容器运行时、多租户、完整权限系统、微调专用判定模型、跑 SWE-Bench 全量 500 题、接入 10 个以上 AI 助手、支持所有编程语言、WebSocket 实时轨迹、花哨的 UI 动画。

完整清单和理由见 `docs/plan/11-acceptance-testing-risk.md` §29。

另外三条：

- **不要在第一周碰真实的 AI 助手。** 第一周的目标是用 Mock（假的 AI）把整条链路跑通。
- **不要为了"看起来专业"引入技术。** 判断标准是"能不能在 4 周内交付并演示"。
- **不要绕过冻结件。** 需要改就先说，不要直接改。

---

## 12. 常用命令

```bash
# 重新生成规划报告（改完 docs/plan/*.md 之后）
python3 docs/plan/_build_report.py .

# 环境自检（E0-T2 完成后可用）
python3 scripts/check_env.py

# Agent 容器的出网笼子（E2-T4）：跑真实 AI 之前先起代理、再验收，五条全绿才建实验
python -m cli.egress up && python -m cli.egress check

# 确认连的是原生 docker 而不是 Docker Desktop
docker info --format '{{.Name}} {{.DockerRootDir}}'
# 期望输出：DESKTOP-D3QQNH3 /var/lib/docker
```

```bash
# 后端（都在仓库根目录跑，Makefile 会自己 cd 进 backend）
make install         # 装依赖 + 装提交钩子
make check           # 提交前跑一遍：lint + 类型 + 模块边界 + 测试
make test            # 只跑测试（跳过需要 Docker 和真实大模型的）
make db-test         # 建测试库 bench_test（上面两条会自动调，一般不用手动跑）

# 并发压测（E9-T2，要 Docker；产物落 var/stress/）
make stress-sweep STRESS_SANDBOX=4   # 真实负载：makespan + 并发曲线 + 内存时序
make stress-hold  STRESS_SANDBOX=4   # 容器真吃满上限，看宿主水位
make stress-oom   STRESS_SANDBOX=4   # 故意 OOM，数 .State.OOMKilled 漏报几次

# 数据库（端口 5433，不是 5432 —— 避开这台机器上别的项目）
make db-up           # 起本地 Postgres 容器
make migrate         # 升到最新
make migrate-check   # 检查模型和迁移有没有对不上
make seed            # 写入三个哨兵 Agent 的种子数据
make db-psql         # 连进去看
make db-reset        # 删掉容器和数据重来

# 前后端一起起
make dev             # 后端 :8000 + 前端 :3000，Ctrl-C 一次停掉两个
make dev-api         # 只起后端
make dev-web         # 只起前端

# 前端
make web-lint        # eslint + tsc
make web-build       # 生产构建
make gen-api         # 从后端 OpenAPI 生成前端类型（需要后端在跑）

# docker compose 一键部署（E10-T1，细节见 docs/deployment.md）
make compose-up      # 建两个镜像 + 起 postgres / migrate / api / worker / frontend，等到全部 healthy
make compose-ps      # 五个服务的状态
make compose-logs SERVICE=worker
make compose-cli CMD="python -m cli.seed"   # 所有 python -m cli.* 在容器里跑
make compose-down    # 停掉，数据卷保留
# ⚠ make compose-smoke 别在开发主仓库跑：compose 起的新库实验号从 1 起，制品会写进现有的 var/artifacts/runs/1/
```

### 清库之后怎么重灌

第 9 节说的"按规程重灌"就是这一串。**顺序不能换**：`promote-assemble` 会建出
`environment_specs` 那一行，而 `cli.images build` 的写回**只更新已有的行、不建行**——
反过来跑的话镜像 digest 永远是空的，而协议 C-36 要求引用镜像用 digest 不用 tag。

```bash
make migrate                       # 表结构
make seed && make seed-tasks       # 哨兵 Agent + 四道 Golden 题
# ⚠ `make seed` 也是**改了 cli/seed.py 之后必须重跑**的：它按 label 更新已有配置行的单价和 params。
# 2026-09-20 起 aider / claude-code 各有两份配置（旧 @deepseek-chat 留给 pilot 历史，新 @deepseek-flash
# 跑最终实验），`--agent aider` 不再唯一，实验命令要带 `--config aider@deepseek-flash`（Makefile 用 CONFIG=）。
# deepseek-chat 已是 deepseek-flash 的别名（2026-09-20 实测），别再拿旧配置跑新实验。
# Golden 四个镜像的 digest 写回库
cd backend && uv run python -m cli.images build --force --env bench-golden__auth__py311 --env bench-golden__cart__py311 --env bench-golden__pager__py311 --env bench-golden__textkit__py311
# 候选：全走文件缓存，不联网不花钱
cd backend && uv run python -m cli.mine run --repo pallets/click --restart
cd backend && uv run python -m cli.prescreen clean
# 模型名必须显式给（.env 里没有 JUDGE_MODEL 就会直接报"没配模型"）。
# 它只用来拼缓存 key，回答全在 var/cache/ 里，实测 80 次调用 80 次命中、0 次计费
cd backend && uv run python -m cli.prescreen score --model deepseek/deepseek-chat
# benchmark-dev 的题。探测结果在 var/promote/ 下，没被清库带走，所以这一步走缓存、
# 只有上次没探成的那几条会真起容器（想全部重探加 --redo）
make promote-probe && make promote-assemble
cd backend && uv run python -m cli.images build --env pallets__click__py311
# tortoise 的题也在这一批里（2026-09-14 起），它的镜像也要在验证前建好，见下面"E8-T3 之后多出来的那一段"
cd backend && uv run python -m cli.images build --env tortoise__tortoise-orm__py311
make validate-tasks                # 八步验证，click 约 8 分钟；tortoise 套件 14 秒一趟，14 道另加几分钟
# 人工终审的结论（E8-T2 定档 22 收 9 否）不在库里，要从提交进仓库的 CSV 导回来。
# 不导的话 22 道题会停在 REVIEW_REQUIRED，而数据集只收 VALID —— 快照会是空的
cd backend && uv run python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-2026-09-10-final.csv
cd backend && uv run python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-3642-2026-09-10-final.csv
# click 剩下那 20 道的终审（2026-09-14，收 11 否 9）和 tortoise 14 道的终审（2026-09-15，收 8 否 6）
cd backend && uv run python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-2026-09-14-parked20.csv
cd backend && uv run python -m cli.promote import-review \
  ../datasets/benchmark-dev/review-2026-09-15-tortoise14.csv
# ⚠ 还要把**没人审过**的题退回 REVIEW_REQUIRED（2026-09-12 E9-T2 补的一步）。
# 八步验证会把跑得通的题一律置 VALID，而人工终审只覆盖上面四份 CSV 里的题；
# 不退的话 dataset stage 会把没审过的题一起冻进快照，快照摘要和已发布的
# benchmark-dev@v1 对不上（正确的是 sha256:300746559b84…），而且不会报错。
# 到 2026-09-15 为止四份 CSV 已经盖住全部 65 道，这一步退回 0 道，但别省——下次多推一批就不是 0 了
cd backend && uv run python -m cli.promote park-unreviewed \
  ../datasets/benchmark-dev/review-2026-09-10-final.csv \
  ../datasets/benchmark-dev/review-3642-2026-09-10-final.csv \
  ../datasets/benchmark-dev/review-2026-09-14-parked20.csv \
  ../datasets/benchmark-dev/review-2026-09-15-tortoise14.csv
# 数据集版本（E1-T6）。**`make enqueue` 和 `cli.experiment start` 从这张快照里取题**，
# 不冻的话它们一道题都选不出来
make dataset-stage                 # benchmark-dev → 一版 DRAFT
make dataset-gate && make worker   # Oracle / Noop 门禁，22 × 2 次评测
make dataset-publish               # 门禁过了才发布
# ⚠ 上面三步重灌出来的是 benchmark-dev@v1（22 道，摘要 300746559b84…）。**最终实验用的是另一版**：
# benchmark-cn-v1@v1（2026-09-17 发布，benchmark-dev 下全部 41 道 VALID，摘要 1701c943ff5b…）。
# 它的题 dataset_id 仍是 benchmark-dev，只是 slug 不同，所以 DATASET 不变、SLUG 要指过去：
make dataset-stage DATASET=benchmark-dev SLUG=benchmark-cn-v1
make dataset-gate SLUG=benchmark-cn-v1 && make worker     # 41 × 2 次评测，约 3 分钟
make dataset-publish SLUG=benchmark-cn-v1
# 摘要对不上 1701c943ff5b… 的话，先查 tortoise 那 8 道的 P2P 里还有没有
# test_init_creates_migrations_package —— 它依赖执行顺序（03 §8.12 二），
# `assembly.ORDER_DEPENDENT_TEST_FUNCTIONS` 在组装时剔掉，重灌走 promote-assemble 会自动生效
# ⚠ 上面重灌出来的是 v1（英文题面）。**最终实验用的是 benchmark-cn-v1@v2**（2026-09-18，中文题面，§8.5 Plan B）。
# `promote assemble` 组装出来的永远是候选里的**英文原文**；改写、复核过的中文题面只存在提交进仓库的
# 对照表里，要用 `cli.localize import` 导回 —— 它只换 issue_title / issue_body / issue_language / tags，
# 测试和补丁不动，validation_state 不动，content_hash 重算。这一步不跑的话题面退回英文、快照摘要和 v2 对不上，
# 而且不报错。**顺序**：要连 v1 一起复现的话先把上面 v1 冻出来再导（导完 v1 的题就变了，再 stage 只能得到 v2）；
# 只要 v2 的话 park-unreviewed 之后直接导
cd backend && uv run python -m cli.localize import ../datasets/benchmark-dev/localize-2026-09-18.csv
make dataset-stage DATASET=benchmark-dev SLUG=benchmark-cn-v1
make dataset-gate SLUG=benchmark-cn-v1 && make worker
make dataset-publish SLUG=benchmark-cn-v1                  # 摘要以 datasets/manifests/benchmark-cn-v1@v2.json 为准
```

**工作区不干净时，建实验的那几步会被拒**（协议 C-27，E5-T4 落的）。
`make dataset-gate` / `make dataset-publish` / `make enqueue` 和
`python -m cli.experiment start` 都会先查一次 `git status --porcelain`。
重灌时如果手上带着未提交的改动：

```bash
make dataset-gate ALLOW_DIRTY=1     # Makefile 的口子
python -m cli.experiment start --agent oracle --allow-dirty   # CLI 的口子
```

放行的实验会被标 `dirty = true`，按 C-28 **不得进排行榜**。这不是一句提醒，
是记在 `evaluation_runs.dirty` 那一列上的事实（manifest 里也记着一份），
排行榜按它过滤 —— 前提是排行榜那一侧真去查这一列，那还没写（E7、E10-T3）。

逃生口：`BENCH_TEST_FORCE_DB_RESET=1`。

**`promote-assemble` 现在不用给 `--limit` 了**（2026-09-15 起）。它会把全部探测通过的候选
都推成题目 —— click 51 条 + tortoise 14 条 —— 而这 65 道的终审结论上面四份 CSV 已经全盖住了。
以前要给 `--limit 30` 是因为只审了 30 条、多推的会混进快照，2026-09-14 把剩下 20 道也审完之后
这个前提没了。等距抽样是确定性的（同一批候选每次抽出同一批题），要是哪天又只想推一部分，
`--limit N` 复现出来的题号和当时的 CSV 逐个相同，不需要记题号。

**不要只删 `benchmark_tasks` 想重来一遍。** 候选的状态会留在 `PROMOTED`，
而 `cli.promote assemble` 只挑 `PRESCREENED` 的（`cli/promote.py:125`），
`cli.mine run` 的 upsert 也只更新 `DISCOVERED` 的行（`cli/mine.py:417`）——
表现是"一条候选都选不出来"，看起来像缓存坏了。这条流水线是有意单向的。
要么整库重置（跑一个集成测试就行），要么两张表一起回退：

```bash
docker exec bench-postgres psql -U bench -d bench -c "delete from benchmark_tasks where raw_definition->>'dataset_id'='benchmark-dev';"
docker exec bench-postgres psql -U bench -d bench -c "update task_candidates set state = 'PRESCREENED' where state = 'PROMOTED';"
```

### E8-T3 之后多出来的那一段（2026-09-13 起）

上面那一串只重灌 `benchmark-dev`（click 一个仓库）。E8-T3 第一段把另外 7 个仓库的候选
也挖进库了，重灌时要多跑这两步——**同样全走 `var/cache/`，不联网不花钱**
（实测 1071 次预筛调用全部命中缓存）：

```bash
cd backend && uv run python -m cli.mine run --restart \
  --repo sgl-project/sglang --repo sqlfluff/sqlfluff --repo xorbitsai/inference \
  --repo tortoise/tortoise-orm --repo hiyouga/LlamaFactory \
  --repo InternLM/lmdeploy --repo Delgan/loguru
cd backend && uv run python -m cli.prescreen clean
cd backend && uv run python -m cli.prescreen score --model deepseek/deepseek-chat
```

挖掘结果另有一份存档在 `datasets/mining/*-2026-09-13.json`，可以拿来对数。

两个新环境的镜像（**建之前先 `docker images` 看一眼在不在**，11.3 GB 那个建一次十几分钟）：

```bash
cd backend && uv run python -m cli.images build --env sqlfluff__sqlfluff__py311
cd backend && uv run python -m cli.images build --env xorbitsai__inference__py311
```

第二段真正推成题的只有 tortoise（2026-09-14 起；xorbitsai 判不可用、sqlfluff 暂缓，见 E8-T3 卡）。
上面 `make promote-probe && make promote-assemble` 那两步不带 `--repo`，会把 tortoise 一起带上：
探测结果同样缓存在 `var/promote/tortoise__tortoise-orm/`，镜像 43 秒就建好：

```bash
cd backend && uv run python -m cli.images build --env tortoise__tortoise-orm__py311
```

顺序仍然是 assemble → images build → validate-tasks，理由和 click 一样（digest 只写回已有的行）。

配方在 `images/envs/*.json`，里面已经带了三个踩出来的坑，改配方前先读注释：

- **sqlfluff 要单独装 `appdirs`。** 镜像按 2026 年的快照建，而候选的 `base_commit`
  停在 2024–2025，那时候的代码还 `import appdirs`。不装的话套件一条测试都收集不到，
  探测直接报 `ENV_NOT_RUNNABLE`
- **sqlfluff 全量套件 663 秒**，超过探测原来写死的 480 秒。探测那一侧已经改成
  可配的 `--suite-timeout`（默认 1800）；题目运行期的 `test_timeout_s` 没动，
  它跑的是 §7.7 的子集，不是全量
- **xorbitsai 要跳过 `test_got_ocr2.py`**。它在模块层 `import diffusers`，
  装了也没用——那批测试还要联网下模型权重。配方里用 `--ignore=` 跳过

**`xorbitsai/inference` 直接 clone 会断**（实测三次：HTTP/2 CANCEL、
early EOF、GnuTLS decode error；仓库大，又过代理）。浅克隆再按需补：

```bash
git clone --bare --shallow-since="2.5 years ago" https://github.com/xorbitsai/inference var/mirrors/xorbitsai__inference.git
```

拿到 2094 个提交、86 MB；剩下 5 个不在浅克隆里的 `base_commit`
用 `git fetch --depth 1 origin <sha>` 一条条补，补完 51 条候选全部能物化。

### E1-T7 之后多出来的那一段：SWE-bench 官方题（2026-09-15 起）

官方题的 slug 单独是 `swebench-verified-subset`，**不进 `benchmark-cn-v1` 的统计**。
重灌时它和上面那串互不干扰，顺序是：

```bash
# 官方数据集 500 行（走 HF 的 JSON 分页接口，不装 pyarrow；有缓存就不重拉）
make swebench-fetch
# 固定种子分层抽样。2026-09-18 起默认抽 100（Makefile 的 SWEBENCH_N），名单在
# datasets/swebench/sample-seed20260915-n100.json，重算必须逐字相同；n50 / n75 是旧版本的证据，保留。
# 原 n75 的 75 道全部保留、相对顺序不变；新题按仓库插入，不是数组前 75 个位置相同。
make swebench-sample && cd backend && uv run python -m cli.swebench sample --check
# 官方配方导出（要联网装 swebench 包，uv 有缓存）。改了抽样数要重导，build-specs.json 会跟着变：
uv run --with "swebench==3.0.15" --isolated python scripts/export_swebench_specs.py
# git 镜像：按 base_commit 浅拉，不 clone 全史（astropy / matplotlib 全史几百 MB 还常被代理掐断）
make swebench-mirror
# 环境镜像。官方镜像（`make swebench-pull`）在这台机器上拉不动：50 个去重 38.9 GB，过代理
# 0.2–6 MB/s 还整条卡死，一夜拉到 2 个。**主路是按官方配方本机建**（2026-09-16 起，72 道这么来的）：
# 配方原文在 datasets/swebench/build-specs.json（`scripts/export_swebench_specs.py` 从 swebench 包导出），
# 下载全走清华源，改动只有 03-benchmark-spec.md §8.6 七点六那五处加九那两处（FreeType 预先放进
# matplotlib 的下载缓存、没有 setup.py 的仓库把 pip 按回 24）。
# 并行数：不用 conda 的题 2 个没问题；matplotlib 的 3 个 conda 环境各吃 5 GB 内存，11 GB 的机器只能 1 个。
# 建之前 `docker images | grep -c bench-env:swebench` 看一眼：2026-09-16 晚是 76 个（名单里 72 道本机建的
# + 重抽前多建的 4 道 sphinx；另 2 道 flask-5014 / pylint-6386 用的是官方镜像 swebench/sweb.eval.…），
# 全在的话这一步几秒就过。
make swebench-build                # 先建能并行的
make swebench-build SWEBENCH_JOBS=1   # 再补 conda 那几个（已建好的会跳过）
# 无人值守：等 build 退出后自动串 build(补跑) → import → validate → 终审表 → 导回 → 漏斗：
#   nohup scripts/swebench_build_then_validate.sh > var/swebench-logs/chain.log 2>&1 &
# 入库：environment_specs 一题一行（镜像就是按题发的），digest 和 READY 一起写，不用 cli.images build。
# 它会顺手套上 datasets/swebench/p2p-overrides.json（逐题剔掉执行器选不中的 P2P，3 道题 4 条 id），
# 清单写错一个字就报错不入库
make swebench-import
# 八步验证只跑声明的用例（官方 P2P 已经给定，不需要全量候选池；全量套件一道题要一小时）。
# ⚠ 这条按 --dataset 选题，**重灌（全部 DISCOVERED）时才这么跑**。平时只补几道新题要用
#   cd backend && uv run python -m cli.validate run --task <id> --task <id> --scope declared
# 否则已终审否掉的题会被重验回 REVIEW_REQUIRED，人工结论被盖掉（2026-09-16 晚差点踩到）
make swebench-validate
# 终审从 CSV 导回，不导它们会一直停在 REVIEW_REQUIRED：flask-5014 按"题面短"政策收（第一份），
# 6 道人工 0 收 6 否（第二份），n75 的 7 道人工 0 收 7 否（第三份），n100 的 7 道人工 1 收 6 否
# （第四份，收 sphinx-9711）；官方题没有候选行，终审理由只在这四份 CSV 里，库里不存
cd backend && uv run python -m cli.promote import-review ../datasets/swebench/review-2026-09-16-official.csv
cd backend && uv run python -m cli.promote import-review ../datasets/swebench/review-2026-09-16-final-official.csv
cd backend && uv run python -m cli.promote import-review ../datasets/swebench/review-2026-09-16-n75-official.csv
cd backend && uv run python -m cli.promote import-review ../datasets/swebench/review-2026-09-18-n100-official.csv
# 之后走 E1-T6 那套，DATASET / SLUG 都要指过来。已发布 v1（42 道）、v2（59 道）、v3（75 道）；
# 重灌后 stage 出来的清单摘要应该是 v3 的 6003a0519a52…（98 入库，75 VALID / 23 INVALID）。
make dataset-stage DATASET=swebench-verified-subset
make dataset-gate SLUG=swebench-verified-subset && make worker
make dataset-publish SLUG=swebench-verified-subset
make swebench-report               # 漏斗，--save 落 datasets/swebench/（会按日期覆盖同名文件，留旧的要先改名）
```

**终审政策（2026-09-16 定）**：官方题只因"题面 < 200 字"停在 REVIEW_REQUIRED 的一律收
（`cli.swebench review-csv` 自动填 ACCEPT，理由写明是官方原文）；其他原因的留给人填。
**题目定义变了要重验**：`cli.swebench import` 重跑不会把已验的题打回 DISCOVERED，
所以改了 `pre_test_command` / 用例清洗规则 / `p2p-overrides.json` 之后，要自己 `cli.validate run --task`
重跑受影响的题。**改官方题的定义只有一条口子**：`datasets/swebench/p2p-overrides.json`（2026-09-16 晚加的，
规矩在 `app/benchmark/swebench_overrides.py`）—— 只许剔 P2P、不许碰 F2P，每条带理由和日期，
剔过的题打 `swebench-p2p-override` 标签。

**官方镜像只能用在测试阶段。** 镜像里 `/testbed/.git` 是完整 clone，`git log --all` 翻得到修复；
Agent 阶段用的是 `bench-agent` 镜像、只挂我们物化的工作区，碰不到它。谁要是把 Agent 放进官方镜像跑，
§5.3 的防泄题就破了。

**前端类型不要手写。** 改完后端接口跑一次 `make gen-api`，
用错字段的地方会直接编译不过。手写的类型漂移了不会报错，只会在运行时拿到 undefined。
